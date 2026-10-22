#!/usr/bin/env python3
"""v18：48 小時回放（雷達、衛星、颱風圖）——維護 GitHub Pages 上的 history/ 資料夾與 history/history.json。

白話：GitHub Pages 每次部署都是「整包取代」，所以過去的圖不會自己留著。這支程式每次部署前：
  1. 先拿回「上一次」的回放圖：優先用 GitHub Actions 快取（hist_cache/，不吃 Pages 流量），
     快取沒有的才從線上網站補抓（第一次上線或快取被清掉時）。
  2. 把這次新畫好的圖縮小一份加進去（雷達每 10 分鐘、衛星／疊圖每 20 分鐘、颱風／區域圖每 30 分鐘）。
  3. 雷達缺的時段，直接從氣象署 S3 歷史檔補畫（不用金鑰；每次最多補 BACKFILL_MAX 張，約 1～2 小時就補滿 48 小時）。
  4. 刪掉超過 48 小時的圖，總量超過預算（HIST_BUDGET_MB）就從最舊、最大的產品開始刪。
  5. 寫出 history/history.json（網頁回放用的索引：每種產品的時間與圖片網址）。
任何一步失敗都不會擋住部署：最壞情況是沿用快取裡上一版的回放圖。
需要 numpy＋pillow（兩種安裝路徑都有）。不會讀取或印出任何金鑰。
"""
import argparse
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

HOURS = 48
HIST_BUDGET_MB = 250            # history/ 總量上限（GitHub Pages 網站上限 1 GB；整站目標 < 300 MB）
BACKFILL_MAX = int(os.environ.get('HIST_BACKFILL_MAX', '36'))   # 每次最多從氣象署 S3 補幾張雷達（每張下載約 9 MB）
CADENCE_MIN = {'radar': 10, 'sat': 20, 'comp': 20, 'region': 30}
SAT_PX, COMP_PX = 1000, 800                    # 首頁地圖衛星圖層、衛星＋雷達疊圖的回放寬度
REGION_PX = {'overview': 900, 'storm': 640}    # 區域總覽／颱風視窗回放寬度
WEBP_Q = 70
REGION_PRODUCTS = ('ir_bd', 'ir_color', 'wv')
UA = {'User-Agent': 'doujin-weather-himawari/1.0 (+GitHub Actions; history)'}
SAFE = re.compile(r'^history/[a-z0-9_]+/[a-z0-9_]+_\d{12}\.(png|webp)$')


def log(*a):
    print(*a, flush=True)


def get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def iso(t):
    return t.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')


def ptime(s):
    s = str(s)
    if re.fullmatch(r'\d{12}', s):
        return datetime.strptime(s, '%Y%m%d%H%M').replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone(timezone.utc)


def sid(t):
    return t.astimezone(timezone.utc).strftime('%Y%m%d%H%M')


def empty_index():
    return {'version': 1, 'hours': HOURS, 'cadenceMin': CADENCE_MIN, 'radar': {'bounds': None, 'frames': []},
            'sat': {'bounds': None, 'truecolor': [], 'wv': []}, 'comp': {'frames': []}, 'regions': {}, 'radarMissing': []}


# ----------------------------------------------------------------------------- 索引走訪
def groups(idx):
    """回傳 [(名稱, 影格 list)]（list 是索引裡的同一個物件，可就地修改）"""
    out = [('radar', idx['radar']['frames']), ('sat.truecolor', idx['sat']['truecolor']), ('sat.wv', idx['sat']['wv']),
           ('comp', idx['comp']['frames'])]
    for rid, r in idx['regions'].items():
        for pk in REGION_PRODUCTS:
            out.append(('%s.%s' % (rid, pk), r.setdefault(pk, [])))
    return out


def merge(a, b):
    """兩份索引合併（以 src 去重）"""
    if not b:
        return a
    if not a:
        return b
    for k in ('bounds',):
        a['radar'][k] = a['radar'].get(k) or b['radar'].get(k)
        a['sat'][k] = a['sat'].get(k) or b['sat'].get(k)
    for name, lst in groups(b):
        dst = dict(groups(a)).get(name)
        if dst is None:
            rid, pk = name.split('.')
            meta = {k: v for k, v in b['regions'][rid].items() if k not in REGION_PRODUCTS}
            a['regions'].setdefault(rid, meta)
            dst = a['regions'][rid].setdefault(pk, [])
        have = {f['src'] for f in dst}
        dst.extend(f for f in lst if f['src'] not in have)
    a['radarMissing'] = sorted(set(a.get('radarMissing') or []) | set(b.get('radarMissing') or []))
    return a


def normalize(idx):
    base = empty_index()
    if not isinstance(idx, dict):
        return base
    for k in ('radar', 'sat', 'comp'):
        if isinstance(idx.get(k), dict):
            base[k].update(idx[k])
    base['regions'] = {k: v for k, v in (idx.get('regions') or {}).items() if isinstance(v, dict)}
    base['radarMissing'] = list(idx.get('radarMissing') or [])
    return base


# ----------------------------------------------------------------------------- 圖片
def shrink(src_path, dst_path, width, rgba=False):
    from PIL import Image
    im = Image.open(src_path)
    im = im.convert('RGBA' if (rgba or im.mode in ('RGBA', 'LA', 'P')) else 'RGB')
    if im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    im.save(dst_path, 'WEBP', quality=WEBP_Q, method=4)
    return os.path.getsize(dst_path)


def due(frames, t, cad):
    """這個時間點要不要收進回放（跟已收的最近一張至少差 cadence−2 分鐘）"""
    for f in frames:
        if abs((ptime(f['t']) - t).total_seconds()) < (cad - 2) * 60:
            return False
    return True


# ----------------------------------------------------------------------------- 主流程
def run(out, cache, pages_base, now=None, backfill_max=BACKFILL_MAX):
    base = (pages_base or '').rstrip('/')
    hroot = os.path.join(cache, 'history')
    os.makedirs(hroot, exist_ok=True)
    stats = {'downloaded': 0, 'added': {}, 'backfilled': 0, 'pruned': 0}

    # 1) 上一版索引：快取＋線上（合併；線上是實際部署的那份，快取可能更新一點）
    idx = None
    cj = os.path.join(cache, 'history.json')
    if os.path.isfile(cj):
        try:
            with open(cj, encoding='utf-8') as f:
                idx = normalize(json.load(f))
        except Exception as e:
            log('⚠️ 快取索引讀取失敗：', str(e)[:100])
    if base:
        try:
            live = normalize(json.loads(get(base + '/history/history.json?t=%d' % time.time(), 20).decode('utf-8')))
            idx = merge(idx, live)
        except Exception as e:
            log('（線上還沒有 history/history.json，第一次屬正常）', str(e)[:80])
    idx = normalize(idx) if idx else empty_index()

    # 現在時間＝這次資料的最新時間（避免機器時間與資料時間差太多）
    times = []
    try:
        with open(os.path.join(out, 'latest.json'), encoding='utf-8') as f:
            latest = json.load(f)
        times.append(ptime(latest['scanTime']))
    except Exception:
        latest = {}
    try:
        with open(os.path.join(out, 'radar', 'latest.json'), encoding='utf-8') as f:
            rman = json.load(f)
        if (rman.get('radar') or {}).get('time'):
            times.append(ptime(rman['radar']['time']))
    except Exception:
        rman = {}
    now = now or (max(times) if times else datetime.now(timezone.utc))
    cutoff = now - timedelta(hours=HOURS, minutes=5)

    # 2) 刪掉過期／不安全的項目；快取沒有的檔案從線上補抓
    need = []
    for name, lst in groups(idx):
        keep = []
        for f in lst:
            try:
                ok = SAFE.match(f.get('src', '')) and ptime(f['t']) >= cutoff
            except Exception:
                ok = False
            if ok:
                keep.append(f)
                if not os.path.isfile(os.path.join(cache, f['src'])):
                    need.append(f['src'])
            else:
                stats['pruned'] += 1
        lst[:] = keep

    def dl(rel):
        try:
            b = get(base + '/' + rel, 30)
            fn = os.path.join(cache, rel)
            os.makedirs(os.path.dirname(fn), exist_ok=True)
            with open(fn, 'wb') as f:
                f.write(b)
            return rel, len(b)
        except Exception:
            return rel, None
    missing = set()
    if need and base:
        with ThreadPoolExecutor(16) as ex:
            for rel, n in ex.map(dl, need):
                if n is None:
                    missing.add(rel)
                else:
                    stats['downloaded'] += 1
    elif need:
        missing = set(need)
    if missing:
        for _, lst in groups(idx):
            lst[:] = [f for f in lst if f['src'] not in missing]
    log('回放：沿用 %d 張（快取沒有、從線上補抓 %d 張、抓不到 %d 張）' % (sum(len(l) for _, l in groups(idx)), stats['downloaded'], len(missing)))

    def add(name, lst, entry, maker):
        try:
            entry['bytes'] = maker(os.path.join(cache, entry['src']))
            lst.append(entry)
            stats['added'][name] = stats['added'].get(name, 0) + 1
        except Exception as e:
            log('⚠️ 回放加入失敗 %s：%s' % (entry['src'], str(e)[:120]))

    # 3a) 雷達：這次（含線上接續來的）12 張全收（原檔就很小，照搬）
    rad = (rman or {}).get('radar') or {}
    if rad.get('bounds'):
        idx['radar']['bounds'] = rad['bounds']
    rl = idx['radar']['frames']
    for fr in rad.get('frames') or []:
        srcf = os.path.join(out, fr.get('src', ''))
        if not re.match(r'^radar/radar_\d{12}\.png$', fr.get('src', '')) or not os.path.isfile(srcf):
            continue
        t = ptime(fr['time'])
        if t < cutoff or not due(rl, t, CADENCE_MIN['radar']):
            continue
        rel = 'history/radar/radar_%s.png' % sid(t)
        add('radar', rl, {'t': iso(t), 'src': rel}, lambda dst, s=srcf: (os.makedirs(os.path.dirname(dst), exist_ok=True), shutil.copyfile(s, dst), os.path.getsize(dst))[2])

    # 3b) 雷達缺的時段 → 氣象署 S3 歷史檔補畫（新的缺口先補）
    if backfill_max > 0:
        try:
            stats['backfilled'] = backfill_radar(idx, cache, now, cutoff, backfill_max)
        except Exception as e:
            log('⚠️ 雷達補檔失敗：', str(e)[:160])

    # 3c) 衛星（首頁地圖真實色／水氣圖層）
    ml = (latest or {}).get('maplayers') or {}
    if ml.get('scanId'):
        t = ptime(ml['scanId'])
        idx['sat']['bounds'] = ml.get('bounds') or idx['sat']['bounds']
        for k in ('truecolor', 'wv'):
            e = ml.get(k) or {}
            p = os.path.join(out, e.get('src', '') or '-')
            if e.get('src') and os.path.isfile(p) and t >= cutoff and due(idx['sat'][k], t, CADENCE_MIN['sat']):
                add('sat.' + k, idx['sat'][k], {'t': iso(t), 'src': 'history/sat/%s_%s.webp' % (k, sid(t))},
                    lambda dst, p=p: shrink(p, dst, SAT_PX, rgba=True))

    # 3d) 衛星＋雷達疊圖（線上接續來的 12 張也一起收 → 第一次上線就有約 2 小時）
    comp = (latest or {}).get('composite') or {}
    cl = idx['comp']['frames']
    for fr in sorted(comp.get('frames') or [], key=lambda x: x.get('scanId', ''), reverse=True):
        p = os.path.join(out, fr.get('src', '') or '-')
        if not fr.get('scanId') or not os.path.isfile(p):
            continue
        t = ptime(fr['scanId'])
        if t >= cutoff and due(cl, t, CADENCE_MIN['comp']):
            add('comp', cl, {'t': iso(t), 'src': 'history/comp/radar_sat_%s.webp' % fr['scanId'], 'rt': fr.get('radarTime'), 'b': fr.get('base')},
                lambda dst, p=p: shrink(p, dst, COMP_PX))

    # 3e) 颱風視窗／區域總覽（三種動畫產品；同樣先收線上接續來的影格）
    for reg in (latest or {}).get('regions') or []:
        rid = reg.get('id')
        if not rid or not re.fullmatch(r'[a-z0-9_]+', rid):
            continue
        meta = idx['regions'].setdefault(rid, {})
        meta.update({k: reg.get(k) for k in ('type', 'name', 'label', 'atcf') if reg.get(k) is not None})
        px = REGION_PX['storm' if reg.get('type') == 'storm' else 'overview']
        for pk in REGION_PRODUCTS:
            pv = (reg.get('products') or {}).get(pk) or {}
            lst = meta.setdefault(pk, [])
            for fr in sorted(pv.get('frames') or [], key=lambda x: x.get('scanId', ''), reverse=True):
                p = os.path.join(out, fr.get('src', '') or '-')
                if not fr.get('scanId') or not os.path.isfile(p):
                    continue
                t = ptime(fr['scanId'])
                if t >= cutoff and due(lst, t, CADENCE_MIN['region']):
                    add(rid + '.' + pk, lst, {'t': iso(t), 'src': 'history/%s/%s_%s.webp' % (rid, pk, fr['scanId'])},
                        lambda dst, p=p, px=px: shrink(p, dst, px))
    for rid in [r for r, m in idx['regions'].items() if not any(m.get(pk) for pk in REGION_PRODUCTS)]:
        del idx['regions'][rid]          # 颱風消散超過 48 小時 → 整個視窗移除

    # 4) 預算：超過就從「最大的那組」刪最舊的
    budget = HIST_BUDGET_MB * 1024 * 1024

    def fsize(f):
        if not f.get('bytes'):
            fn = os.path.join(cache, f['src'])
            f['bytes'] = os.path.getsize(fn) if os.path.isfile(fn) else 0
        return f['bytes']
    total = sum(fsize(f) for _, l in groups(idx) for f in l)
    while total > budget:
        name, lst = max(((n, l) for n, l in groups(idx) if len(l) > 1), key=lambda x: sum(fsize(f) for f in x[1]), default=(None, None))
        if not lst:
            break
        lst.sort(key=lambda f: f['t'])
        total -= fsize(lst.pop(0))
        stats['pruned'] += 1

    # 5) 排序、清掉沒用到的檔案、輸出
    used = set()
    for _, lst in groups(idx):
        lst.sort(key=lambda f: f['t'])
        used.update(f['src'] for f in lst)
    for dp, _, fns in os.walk(hroot):
        for fn in fns:
            rel = os.path.relpath(os.path.join(dp, fn), cache).replace(os.sep, '/')
            if rel not in used:
                os.remove(os.path.join(dp, fn))
    idx['radarMissing'] = sorted(m for m in idx.get('radarMissing') or [] if m >= sid(cutoff))
    idx['generated'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    idx['latest'] = iso(now)
    idx['bytes'] = total
    idx['count'] = {n: len(l) for n, l in groups(idx) if l}
    with open(cj, 'w', encoding='utf-8') as f:
        json.dump(idx, f, ensure_ascii=False, separators=(',', ':'))
    publish(out, cache)
    log('✅ 回放 history：%d 張、%.1f MB（新增 %s；雷達補檔 %d；刪除 %d）' % (
        sum(idx['count'].values()), total / 1048576, json.dumps(stats['added'], ensure_ascii=False), stats['backfilled'], stats['pruned']))
    for n, c in idx['count'].items():
        log('   %-22s %3d 張  %6.1f MB' % (n, c, sum(fsize(f) for f in dict(groups(idx))[n]) / 1048576))
    return idx


def publish(out, cache):
    """快取 → 網站（site/history/）"""
    src = os.path.join(cache, 'history')
    if not os.path.isdir(src) or not os.path.isdir(out):
        return False
    dst = os.path.join(out, 'history')
    shutil.copytree(src, dst, dirs_exist_ok=True)
    cj = os.path.join(cache, 'history.json')
    if os.path.isfile(cj):
        shutil.copyfile(cj, os.path.join(dst, 'history.json'))
    return True


def backfill_radar(idx, cache, now, cutoff, limit):
    import cwa_radar as cr
    rl = idx['radar']['frames']
    have = {f['t'] for f in rl}
    tried = set(idx.get('radarMissing') or [])
    t0 = now.replace(minute=now.minute - now.minute % 10, second=0, microsecond=0)
    slots, t = [], t0
    while t >= cutoff + timedelta(minutes=5):
        if iso(t) not in have and sid(t) not in tried:
            slots.append(t)
        t -= timedelta(minutes=10)
    slots = slots[:limit]
    if not slots:
        return 0

    def one(t):
        try:
            return t, cr.fetch_radar_at(t, tries=(0,))
        except Exception:
            return t, None
    n = 0
    with ThreadPoolExecutor(4) as ex:
        for t, g in ex.map(one, slots):
            if not g:
                idx.setdefault('radarMissing', []).append(sid(t))
                continue
            gt = g['time'].astimezone(timezone.utc)
            if iso(gt) in have:
                continue
            rel = 'history/radar/radar_%s.png' % sid(gt)
            b = cr.png_bytes(cr.to_mercator_rows(cr.colorize(g['v'], 'radar'), g))
            fn = os.path.join(cache, rel)
            os.makedirs(os.path.dirname(fn), exist_ok=True)
            with open(fn, 'wb') as f:
                f.write(b)
            ent = cr.layer_entry('radar', g, rel)
            idx['radar']['bounds'] = idx['radar']['bounds'] or ent['bounds']
            rl.append({'t': iso(gt), 'src': rel, 'bytes': len(b)})
            have.add(iso(gt))
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='site')
    ap.add_argument('--cache', default='hist_cache')
    ap.add_argument('--pages-base', default=os.environ.get('PAGES_BASE', ''))
    ap.add_argument('--backfill', type=int, default=BACKFILL_MAX)
    a = ap.parse_args()
    try:
        run(a.out, a.cache, a.pages_base, backfill_max=a.backfill)
    except Exception as e:
        # 出錯也不要擋部署：把快取裡上一版的回放原樣放上去
        log('⚠️ 回放整理失敗，沿用上一版：', str(e)[:200])
        try:
            publish(a.out, a.cache)
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
