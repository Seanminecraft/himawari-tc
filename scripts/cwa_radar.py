#!/usr/bin/env python3
"""氣象署 雷達整合回波（O-A0059-001）＋ 未來 1 小時定量降雨預報（F-B0046-001）→ 網頁地圖用的透明 PNG 圖層。

白話：把氣象署每 10 分鐘更新的「雷達回波格點」與「1 小時雨量預報格點」畫成透明圖片，
放到 GitHub Pages 的 radar/ 資料夾，網站地圖（Leaflet）直接疊上去，不用再畫「熱點」圓點。

資料來源（不用金鑰優先）：
  1. 氣象署開放資料 AWS S3 公開鏡像 cwaopendata（ap-northeast-1），與 opendata.cwa.gov.tw 同一份 JSON：
       Observation/O-A0059-001.json、Forecast/F-B0046-001.json
     雷達歷史檔（動畫用）：history/Observation/{台灣時間 YYYYMMDDHHMM}compref_mosaic.xml
  2. 備援：opendata.cwa.gov.tw fileapi（需要環境變數 CWA_API_KEY；GitHub Actions 由 secrets 注入，絕不印出）
格點規格（取自資料本身的 contentDescription，程式會再讀檔確認）：
  雷達 O-A0059-001：921×881，0.0125°，第一點（左下）115.0E 18.0N，先西→東再南→北；
                    -99＝無效（涵蓋範圍內無回波）、-999＝雷達觀測範圍外／品管移除；單位 dBZ；每 10 分鐘。
  QPF  F-B0046-001：441×561，0.0125°，第一點 117.975E 19.975N（StartPoint 欄位寫 118/20，以說明文字為準）；
                    -99＝無效（無降雨）；單位 mm（未來 1 小時）；每 10 分鐘。
  座標系統：TWD67 經緯度。轉 WGS84 約 +0.0081° 經度、−0.0018° 緯度（約東 820 m、南 200 m，
           pyproj aust_SA + towgs84=-752,-358,-179 計算），程式套用此平移。
投影：Leaflet 是 Web Mercator，影像在「投影後」才是均勻格子，因此每一列依 Mercator y 重新取樣（最近鄰），
     L.imageOverlay 用四角經緯度就會對準。
色階：assets/colorscales/radar_dbz.json（線性內插，同使用者提供／Windy 格式）、rain_mm.json（分級階梯，
     同氣象署雨量圖分級）。QPF 的 0 mm 與無效值不上色（透明）。
只用 numpy＋pillow＋標準函式庫。
"""
import argparse
import io
import json
import math
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SCALE_DIR = os.path.join(HERE, '..', 'assets', 'colorscales')
S3 = 'https://cwaopendata.s3.ap-northeast-1.amazonaws.com'
FILEAPI = 'https://opendata.cwa.gov.tw/fileapi/v1/opendataapi/'
UA = {'User-Agent': 'doujin-weather-radar/1.0 (+GitHub Actions)'}
TPE = timezone(timedelta(hours=8))
DATUM_DLON, DATUM_DLAT = 0.0081, -0.0018     # TWD67 → WGS84
RADAR_FRAMES = 12                              # 雷達動畫保留張數（10 分鐘一張 → 2 小時）
BACKFILL_MAX = 4                               # 線上影格不夠時，每次最多從 S3 歷史檔補幾張（第一次上線約 3 次執行就補滿）
QPF_FRAMES = 1
PRODUCTS = {
    'radar': dict(ds='O-A0059-001', s3='Observation/O-A0059-001.json', title='雷達整合回波（dBZ）',
                  scale='radar_dbz.json', mode='linear', unit='dBZ'),
    'qpf': dict(ds='F-B0046-001', s3='Forecast/F-B0046-001.json', title='未來 1 小時定量降雨預報（mm）',
                scale='rain_mm.json', mode='step', unit='mm'),
}


def log(*a):
    print(*a, flush=True)


def _ctx():
    c = ssl.create_default_context()
    try:
        c.verify_flags &= ~ssl.VERIFY_X509_STRICT   # 氣象署憑證缺 SKI，Python 3.13 嚴格模式會拒絕
    except Exception:
        pass
    return c


def http(url, timeout=60, method='GET'):
    req = urllib.request.Request(url, headers=UA, method=method)
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
        return r.read(), dict(r.headers)


def _mask_key(s):
    k = os.environ.get('CWA_API_KEY') or ''
    s = str(s)
    return s.replace(k, '***') if k else s


def fetch_json(prod):
    """回傳 (dict, 來源說明, S3 Last-Modified)。先 S3 公開鏡像，失敗才用金鑰 fileapi。"""
    p = PRODUCTS[prod]
    errs = []
    try:
        b, h = http(S3 + '/' + p['s3'])
        return json.loads(b.decode('utf-8')), 'cwaopendata S3', h.get('Last-Modified', '')
    except Exception as e:
        errs.append('S3：' + str(e)[:120])
    key = os.environ.get('CWA_API_KEY') or ''
    if key:
        try:
            b, _ = http(FILEAPI + p['ds'] + '?' + urllib.parse.urlencode({'Authorization': key, 'format': 'JSON'}))
            return json.loads(b.decode('utf-8')), 'opendata.cwa.gov.tw', ''
        except Exception as e:
            errs.append('fileapi：' + _mask_key(e)[:120])
    raise RuntimeError('；'.join(errs))


def parse_grid(doc):
    """cwaopendata JSON → dict(values[ny,nx] 南→北, lon0, lat0, res, time)。"""
    root = doc.get('cwaopendata', doc)
    ds = root['dataset']
    ps = ds['datasetInfo']['parameterSet']
    desc = ds['contents'].get('contentDescription', '')
    nx, ny = int(ps['GridDimensionX']), int(ps['GridDimensionY'])
    res = float(ps['GridResolution'])
    lon0, lat0 = float(ps['StartPointLongitude']), float(ps['StartPointLatitude'])
    m = re.search(r'東經\s*([0-9.]+)\s*[、,，]\s*北緯\s*([0-9.]+)', desc)
    if m:
        lon0, lat0 = float(m.group(1)), float(m.group(2))
    a = np.array(ds['contents']['content'].split(','), dtype=np.float32)
    if a.size != nx * ny:
        raise ValueError('格點數不符 %d != %d×%d' % (a.size, nx, ny))
    t = datetime.fromisoformat(ps['DateTime'])
    return dict(v=a.reshape(ny, nx), lon0=lon0, lat0=lat0, res=res, nx=nx, ny=ny, time=t)


def parse_xml_grid(text):
    """歷史檔 compref_mosaic.xml（與 JSON 同內容）。"""
    def tag(t):
        m = re.search(r'<%s>([^<]*)</%s>' % (t, t), text)
        return m.group(1).strip() if m else None
    doc = {'cwaopendata': {'dataset': {
        'datasetInfo': {'parameterSet': {k: tag(k) for k in ('GridDimensionX', 'GridDimensionY', 'GridResolution',
                                                             'StartPointLongitude', 'StartPointLatitude', 'DateTime')}},
        'contents': {'contentDescription': tag('contentDescription') or '', 'content': tag('content') or ''}}}}
    return parse_grid(doc)


def fetch_radar_at(t_utc, tries=(0, -10, 10, -20)):
    """動畫／疊圖用：抓指定時間（UTC）最接近的雷達歷史檔；回傳 grid 或 None。"""
    for dm in tries:
        t = (t_utc + timedelta(minutes=dm)).astimezone(TPE)
        key = 'history/Observation/%scompref_mosaic.xml' % t.strftime('%Y%m%d%H%M')
        try:
            b, _ = http(S3 + '/' + key, timeout=60)
            return parse_xml_grid(b.decode('utf-8'))
        except Exception:
            continue
    return None


# ----------------------------------------------------------------------------- 色階
def load_scale(name):
    with open(os.path.join(SCALE_DIR, name), encoding='utf-8') as f:
        st = json.load(f)
    return [(float(v), [min(255, int(c)) for c in rgba]) for v, rgba in st]


def colorize(v, prod):
    """數值 → RGBA uint8。radar：線性內插；qpf：分級（每級＝下界顏色）。無效／0 → 透明。"""
    p = PRODUCTS[prod]
    stops = load_scale(p['scale'])
    xs = np.array([s[0] for s in stops], dtype=np.float32)
    cs = np.array([s[1] for s in stops], dtype=np.float32)
    out = np.zeros(v.shape + (4,), dtype=np.uint8)
    if p['mode'] == 'linear':
        ok = v >= 0
        vv = np.clip(v[ok], xs[0], xs[-1])
        for ch in range(4):
            out[..., ch][ok] = np.round(np.interp(vv, xs, cs[:, ch])).astype(np.uint8)
    else:
        ok = v > 0
        idx = np.clip(np.searchsorted(xs, v[ok], side='right') - 1, 0, len(xs) - 1)
        out[ok] = cs[idx].astype(np.uint8)
    return out


# ----------------------------------------------------------------------------- 投影
def merc_y(lat):
    return np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))


def grid_bounds(g):
    """格點外框（WGS84，已套 TWD67 平移）：(south, west, north, east)"""
    h = g['res'] / 2
    w = g['lon0'] - h + DATUM_DLON
    e = g['lon0'] + (g['nx'] - 1) * g['res'] + h + DATUM_DLON
    s = g['lat0'] - h + DATUM_DLAT
    n = g['lat0'] + (g['ny'] - 1) * g['res'] + h + DATUM_DLAT
    return s, w, n, e


def to_mercator_rows(rgba, g):
    """南→北排列的經緯度格點 RGBA → 北→南、Mercator 均勻列的影像。"""
    s, w, n, e = grid_bounds(g)
    ys, yn = merc_y(s), merc_y(n)
    H = int(round((yn - ys) / math.radians(g['res'])))
    yc = yn - (np.arange(H) + 0.5) * (yn - ys) / H
    lat = np.degrees(2 * np.arctan(np.exp(yc)) - np.pi / 2)
    j = np.clip(np.floor((lat - s) / g['res']).astype(int), 0, g['ny'] - 1)
    return rgba[j]


def png_bytes(img):
    from PIL import Image
    im = Image.fromarray(img, 'RGBA')
    # 減色成調色盤 PNG（檔案小很多；雷達色階本來就只有幾百色）
    try:
        q = im.quantize(colors=255, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)
        buf = io.BytesIO(); q.save(buf, 'PNG', optimize=True)
        b1 = buf.getvalue()
    except Exception:
        b1 = None
    buf = io.BytesIO(); im.save(buf, 'PNG', optimize=True)
    b2 = buf.getvalue()
    return b1 if b1 and len(b1) < len(b2) else b2


def tid(t):
    return t.astimezone(TPE).strftime('%Y%m%d%H%M')


def layer_entry(prod, g, src):
    s, w, n, e = grid_bounds(g)
    return {'id': tid(g['time']), 'time': g['time'].astimezone(TPE).isoformat(), 'src': src,
            'bounds': [[round(s, 5), round(w, 5)], [round(n, 5), round(e, 5)]]}


# ----------------------------------------------------------------------------- 主程式（網頁圖層）
def build_layers(out, pages_base='', errors=None, now=None):
    errors = errors if errors is not None else []
    os.makedirs(os.path.join(out, 'radar'), exist_ok=True)
    prev = None
    if pages_base:
        try:
            b, _ = http(pages_base.rstrip('/') + '/radar/latest.json?t=%d' % time.time(), timeout=20)
            prev = json.loads(b.decode('utf-8'))
        except Exception as e:
            errors.append('讀不到線上 radar/latest.json（第一次屬正常）：%s' % str(e)[:100])
    man = {'version': 1, 'generated': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
           'datum': 'TWD67→WGS84 平移 %+.4f° 經 %+.4f° 緯' % (DATUM_DLON, DATUM_DLAT),
           'projection': 'Web Mercator 列重取樣（L.imageOverlay 直接用 bounds）',
           'license': '資料來源：中央氣象署（政府資料開放授權條款第1版）', 'errors': errors}
    ok_any = False
    grids = {}
    for prod, p in PRODUCTS.items():
        keep = RADAR_FRAMES if prod == 'radar' else QPF_FRAMES
        old = (prev or {}).get(prod) or {}
        try:
            doc, via, lm = fetch_json(prod)
            g = parse_grid(doc)
            grids[prod] = g
            rel = 'radar/%s_%s.png' % (prod, tid(g['time']))
            img = to_mercator_rows(colorize(g['v'], prod), g)   # 北→南、Mercator 均勻列
            b = png_bytes(img)
            with open(os.path.join(out, rel), 'wb') as f:
                f.write(b)
            cur = layer_entry(prod, g, rel)
            cur['bytes'] = len(b)
            # 接續舊影格（從線上抓回）
            frames = []
            cutoff = g['time'] - timedelta(minutes=10 * keep + 5)
            for fr in (old.get('frames') or []):
                try:
                    ft = datetime.fromisoformat(fr['time'])
                except Exception:
                    continue
                if fr.get('id') == cur['id'] or ft < cutoff or not re.match(r'^radar/[a-z]+_\d{12}\.png$', fr.get('src', '')):
                    continue
                try:
                    fb, _ = http(pages_base.rstrip('/') + '/' + fr['src'], timeout=30)
                    with open(os.path.join(out, fr['src']), 'wb') as f:
                        f.write(fb)
                    frames.append(fr)
                except Exception:
                    pass
            # 影格不夠（第一次上線或中間漏跑）→ 從 S3 歷史檔補，最多 BACKFILL_MAX 張（每張約 9 MB 下載、1 秒）
            if prod == 'radar':
                have = {fr.get('id') for fr in frames} | {cur['id']}
                filled = 0
                for k in range(1, keep):
                    if filled >= BACKFILL_MAX:
                        break
                    tk = g['time'] - timedelta(minutes=10 * k)
                    if tid(tk) in have:
                        continue
                    gk = fetch_radar_at(tk.astimezone(timezone.utc), tries=(0,))
                    filled += 1
                    if not gk or tid(gk['time']) in have:
                        continue
                    relk = 'radar/radar_%s.png' % tid(gk['time'])
                    bk = png_bytes(to_mercator_rows(colorize(gk['v'], prod), gk))
                    with open(os.path.join(out, relk), 'wb') as f:
                        f.write(bk)
                    ek = layer_entry(prod, gk, relk)
                    ek['bytes'] = len(bk)
                    frames.append(ek)
                    have.add(ek['id'])
            frames = sorted(frames + [cur], key=lambda x: x['id'])[-keep:]
            man[prod] = dict(cur, title=p['title'], unit=p['unit'], dataset=p['ds'], via=via, srcModified=lm,
                             frames=frames, scale=load_scale(p['scale']), scaleMode=p['mode'])
            ok_any = True
            log('✅ %s %s（%s，%d KB，影格 %d）' % (prod, cur['id'], via, len(b) // 1024, len(frames)))
        except Exception as e:
            errors.append('%s 失敗：%s' % (prod, _mask_key(e)[:160]))
            log('⚠️ %s 失敗：%s' % (prod, _mask_key(e)[:160]))
            # 失敗就沿用線上舊版（避免整個圖層消失）
            if old.get('src') and pages_base:
                kept = []
                for fr in (old.get('frames') or [old]):
                    try:
                        fb, _ = http(pages_base.rstrip('/') + '/' + fr['src'], timeout=30)
                        with open(os.path.join(out, fr['src']), 'wb') as f:
                            f.write(fb)
                        kept.append(fr)
                    except Exception:
                        pass
                if kept:
                    man[prod] = dict(old, frames=kept, stale=True)
    with open(os.path.join(out, 'radar', 'latest.json'), 'w', encoding='utf-8') as f:
        json.dump(man, f, ensure_ascii=False, indent=1)
    return ok_any, man, grids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='site')
    ap.add_argument('--pages-base', default=os.environ.get('PAGES_BASE', ''))
    a = ap.parse_args()
    ok, man, _ = build_layers(a.out, a.pages_base)
    gh = os.environ.get('GITHUB_OUTPUT')
    if gh:
        with open(gh, 'a') as f:
            f.write('radar_ok=%s\n' % ('true' if ok else 'false'))
    if man.get('errors'):
        log('備註：' + ' | '.join(man['errors']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
