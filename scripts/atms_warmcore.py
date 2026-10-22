#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v19：ATMS 衛星微波暖心分析（NOAA-20／NOAA-21／Suomi NPP，NOAA NODD AWS 公開桶，免帳號）。

白話：颱風眼裡的空氣下沉被壓縮而變暖（暖心）。ATMS 的 50–60 GHz 氧氣通道可以「穿透」雲層量到
不同高度的大氣溫度，所以把颱風中心的亮度溫度減掉周圍環境，就得到暖心有多強、在哪個高度。

方法（照已發表的做法，簡化版）：
  • Kidder et al. 2000（BAMS 81, 1241–1260）：AMSU 上對流層溫度異常與颱風強度的關係。
  • Brueske & Velden 2003（MWR 131, 687–697）：54.94 GHz（AMSU ch7＝ATMS ch8，約 250 hPa）亮溫異常 ΔTb
    → 中心氣壓。這裡用論文式 (6) 的「未校正 (raw)」版本：MSLP = (68.9 − ΔTb) / 0.07。
  • Herndon et al. 2004（CIMSS AMSU 演算法升級）／Demuth et al. 2006（JAMC 45, 1573–1581）：
    眼小於足跡（取樣不足）、中心落在兩個足跡之間（bracketing）、降水散射會讓暖心偏弱 → 這裡只做「品質標示」，不做校正。
  • 臨邊效應：環境值用「同一掃描位置（FOV）」在 600–1100 km 環帶的中位數（同角度相減，抵消臨邊變暗）。

流程：
  1. 颱風位置：site/latest.json（himawari_pipeline 的 NRL/JTWC 定位＋移動向量外推）。
  2. 軌道：CelesTrak TLE（一天最多抓一次，存在 atms/latest.json 裡）＋ sgp4 → 算出過去 6 小時／未來 12 小時的過境時間。
  3. 只下載涵蓋颱風的那幾個 32 秒 granule（SDR＋GEO，每個約 150＋120 KB，一次過境約 20 個 ≈ 5 MB）。
  4. 畫圖（暖心垂直剖面、約 250 hPa 平面圖、位置小地圖、資訊框）→ site/atms/；history.py 收進 48 小時回放。
  4b. v20：同一批下載的資料再畫「被動微波影像」89 GHz（ch16）、165 GHz（ch17）→ site/atms/mw/（atms_mw.py，不另外下載）。
  5. 寫 atms/latest.json（含「下一次過境」plan，quick_check.py 只用標準函式庫就能判斷有沒有新資料）。

模式：
  --carry：不分析，只把上一版 atms/（Actions 快取 hist_cache/atms，沒有就從線上網站）原樣放回 site/。只用標準函式庫。
任何錯誤都不會擋住部署（工作流程 continue-on-error，失敗時再跑一次 --carry）。不會讀取或印出任何金鑰。
"""
import argparse
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

UA = {'User-Agent': 'doujin-weather-himawari/1.0 (+GitHub Actions; ATMS warm core)'}
SATS = {  # 代號: (S3 桶, 檔名代號, 顯示名, NORAD 編號)
    'n20': ('noaa-nesdis-n20-pds', 'j01', 'NOAA-20', 43013),
    'n21': ('noaa-nesdis-n21-pds', 'j02', 'NOAA-21', 54234),
    'npp': ('noaa-nesdis-snpp-pds', 'npp', 'Suomi NPP', 37849),
}
TLE_URL = 'https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=tle'
TLE_MAX_AGE_H = 24
LOOKBACK_H, LOOKAHEAD_H = 6, 12
SWATH_KM = 1150            # 颱風離星下點軌跡多遠以內算「在掃描帶內」（ATMS 掃描帶約 ±1300 km，留邊）
HALF_WIN_MIN = 5.5         # 最接近時間 ±5.5 分鐘的 granule（約 ±1100 km，夠算 600–1100 km 環境環帶）
GIVEUP_MIN = 150           # 過境後 2.5 小時還沒上傳 → 放棄這次
ENV_R = (600.0, 1100.0)    # 環境環帶（km）
CORE_SEARCH_KM = 90        # 從定位中心找最暖足跡的搜尋半徑
PLAN_HALF_KM = 700         # 平面圖半寬
HIST_HOURS = 48
# ATMS 溫度通道（1 起算）→ 頻率、約略權重函數峰值（hPa；近天底，靠掃描邊緣會再高一點）
CHANNELS = [(5, '52.8', 700), (6, '53.596', 550), (7, '54.40', 350), (8, '54.94', 250), (9, '55.50', 150), (10, '57.29', 90)]
CH_P = 8                   # 估氣壓用的通道（54.94 GHz＝AMSU-A ch7，Brueske & Velden 2003）
CH_SCAT = 16               # 88.2 GHz（冰晶散射指標）
BEAM_DEG = 2.2             # ATMS 50–60 GHz 波束寬（天底足跡約 32 km）
SAT_ALT_KM = 824.0
R_E = 6371.0
GRAN_RE = re.compile(r'_(j01|j02|npp)_d(\d{8})_t(\d{7})_e(\d{7})_b(\d+)_')


def log(*a):
    print(*a, flush=True)


def get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def iso(t):
    return t.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def ptime(s):
    return datetime.fromisoformat(str(s).replace('Z', '+00:00')).astimezone(timezone.utc)


def sid(t):
    return t.astimezone(timezone.utc).strftime('%Y%m%d%H%M')


# ----------------------------------------------------------------------------- 狀態保存（carry）
SAFE_SRC = re.compile(r'^atms/img/[a-z0-9_]+_\d{12}\.png$')
SAFE_MW = re.compile(r'^atms/mw/[a-z0-9_]+_\d{12}_(89|165)\.png$')   # v20 被動微波影像


def carry(out, cache, pages_base):
    """上一版 atms/ → site/atms/（快取優先，否則從線上網站）。回傳上一版 latest.json（dict）或 None。"""
    dst = os.path.join(out, 'atms')
    src = os.path.join(cache, 'atms') if cache else ''
    man = None
    if src and os.path.isfile(os.path.join(src, 'latest.json')):
        try:
            with open(os.path.join(src, 'latest.json'), encoding='utf-8') as f:
                man = json.load(f)
            os.makedirs(dst, exist_ok=True)
            shutil.copytree(src, dst, dirs_exist_ok=True)
            log('ATMS：沿用快取裡的上一版（%s）' % man.get('generated'))
            return man
        except Exception as e:
            log('⚠️ ATMS 快取讀取失敗：', str(e)[:100])
            man = None
    base = (pages_base or '').rstrip('/')
    if not base:
        return None
    try:
        man = json.loads(get(base + '/atms/latest.json?t=%d' % time.time(), 20).decode('utf-8'))
    except Exception as e:
        log('（線上還沒有 atms/latest.json，第一次屬正常）', str(e)[:80])
        return None
    os.makedirs(os.path.join(dst, 'img'), exist_ok=True)
    os.makedirs(os.path.join(dst, 'mw'), exist_ok=True)
    rels = []
    for s in man.get('storms') or []:
        rels.append(((s.get('latest') or {}).get('src') or ''))
        rels += [(v or {}).get('src') or '' for v in ((s.get('mwLatest') or {}).get('products') or {}).values()]
    for rel in rels:
        if SAFE_SRC.match(rel) or SAFE_MW.match(rel):
            try:
                b = get(base + '/' + rel, 30)
                with open(os.path.join(out, rel), 'wb') as f:
                    f.write(b)
            except Exception as e:
                log('⚠️ 抓不到線上 %s：%s' % (rel, str(e)[:80]))
    with open(os.path.join(dst, 'latest.json'), 'w', encoding='utf-8') as f:
        json.dump(man, f, ensure_ascii=False, indent=1)
    log('ATMS：沿用線上網站的上一版（%s）' % man.get('generated'))
    return man


def save_cache(out, cache):
    if not cache:
        return
    src, dst = os.path.join(out, 'atms'), os.path.join(cache, 'atms')
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


# ----------------------------------------------------------------------------- 颱風
def load_storms(out, now):
    storms = []
    try:
        with open(os.path.join(out, 'latest.json'), encoding='utf-8') as f:
            latest = json.load(f)
    except Exception as e:
        log('⚠️ 讀不到 site/latest.json（颱風位置）：', str(e)[:80])
        return storms
    for r in latest.get('regions') or []:
        if r.get('type') != 'storm' or not r.get('atcf'):
            continue
        lat, lon = r.get('fixLat', r.get('centerLat')), r.get('fixLon', r.get('centerLon'))
        if lat is None or lon is None:
            continue
        try:
            ft = ptime(r['fixTime']) if r.get('fixTime') else ptime(latest.get('scanTime'))
        except Exception:
            ft = now
        storms.append(dict(atcf=r['atcf'], label=r.get('label') or '', name=r.get('name') or '', category=r.get('category') or '',
                           vmax=r.get('vmax'), mslp=r.get('mslp'), lat=float(lat), lon=float(lon), fixTime=ft,
                           motion=r.get('motion') or None, src=r.get('stormSource') or ''))
    return storms


def storm_pos(s, t):
    """定位＋移動向量外推到時間 t（最多 ±12 小時）"""
    m = s.get('motion') or {}
    hrs = max(-12.0, min(12.0, (t - s['fixTime']).total_seconds() / 3600))
    if not m or m.get('dir') is None or m.get('kt') is None:
        return s['lat'], s['lon']
    dist = float(m['kt']) * hrs / 60.0
    d = math.radians(float(m['dir']))
    lat = s['lat'] + dist * math.cos(d)
    lon = s['lon'] + dist * math.sin(d) / max(0.2, math.cos(math.radians(s['lat'])))
    return lat, lon


def gc_km(la1, lo1, la2, lo2):
    p1, p2 = math.radians(la1), math.radians(la2)
    dl = math.radians(lo2 - lo1)
    c = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)
    return R_E * math.acos(max(-1.0, min(1.0, c)))


# ----------------------------------------------------------------------------- 軌道（TLE＋sgp4）
def refresh_tle(prev, now, errors):
    tle = (prev or {}).get('tle') or {}
    fetched = (prev or {}).get('tleFetched')
    try:
        age = (now - ptime(fetched)).total_seconds() / 3600 if fetched else 1e9
    except Exception:
        age = 1e9
    if tle and age < TLE_MAX_AGE_H and all(k in tle for k in SATS):
        return tle, fetched
    try:
        txt = None
        if os.environ.get('ATMS_TLE_FILE'):          # 測試用：本機 TLE 檔
            with open(os.environ['ATMS_TLE_FILE'], encoding='utf-8') as f:
                txt = f.read().splitlines()
        urls = [TLE_URL] + ['https://celestrak.org/NORAD/elements/gp.php?CATNR=%d&FORMAT=tle' % v[3] for v in SATS.values()]
        last = None
        if txt is None:
            txt = []
            for i, u in enumerate(urls):
                if i and len([1 for l in txt if l.startswith('1 ')]) >= len(SATS):
                    break
                for k in range(2):
                    try:
                        txt += get(u, 25).decode('utf-8', 'replace').splitlines()
                        break
                    except Exception as e:
                        last = e
                        time.sleep(2 + 3 * k)
                if i == 0 and txt:
                    break
        if not txt and last:
            raise last
        new = {}
        for i in range(len(txt) - 2):
            l1, l2 = txt[i + 1].strip(), txt[i + 2].strip()
            if l1.startswith('1 ') and l2.startswith('2 '):
                for k, (_, _, _, cat) in SATS.items():
                    if int(l1[2:7]) == cat:
                        new[k] = [l1, l2]
        if new:
            log('ATMS：已更新 TLE（%s）' % ', '.join(sorted(new)))
            tle.update(new)
            return tle, iso(now)
        errors.append('CelesTrak TLE 沒有 NOAA-20/21/SNPP')
    except Exception as e:
        errors.append('CelesTrak TLE：%s' % str(e)[:80])
    return tle, fetched     # 抓不到就用舊的（幾天內誤差只有幾公里，之後還會用 GEO 檔實際確認）


def subsat_track(tle, t0, t1, step_s=20):
    from sgp4.api import Satrec, jday
    sat = Satrec.twoline2rv(tle[0], tle[1])
    out = []
    t = t0
    while t <= t1:
        jd, fr = jday(t.year, t.month, t.day, t.hour, t.minute, t.second + t.microsecond / 1e6)
        e, r, _ = sat.sgp4(jd, fr)
        if e == 0:
            tu = (jd - 2451545.0) + fr
            gmst = math.radians((280.46061837 + 360.98564736629 * tu) % 360)
            x, y, z = r
            lon = math.degrees(math.atan2(y, x) - gmst)
            lon = (lon + 540) % 360 - 180
            latc = math.atan2(z, math.hypot(x, y))
            lat = math.degrees(math.atan(math.tan(latc) / (1 - 0.00669438)))
            out.append((t, lat, lon % 360))
        t += timedelta(seconds=step_s)
    return out


def is_done(done, atcf, sat, tc):
    """同一颱風、同一顆衛星、前後 10 分鐘內已處理過 → 同一次過境（軌道取樣時間每次略有不同）"""
    pre = '%s_%s_' % (atcf, sat)
    for d in done:
        if d.startswith(pre):
            try:
                if abs((datetime.strptime(d[-12:], '%Y%m%d%H%M').replace(tzinfo=timezone.utc) - tc).total_seconds()) <= 600:
                    return True
            except Exception:
                pass
    return False


def find_passes(storm, tle, t0, t1):
    """[(最接近時間, 距離 km)]：颱風在掃描帶內的每一次過境"""
    tr = subsat_track(tle, t0, t1)
    d = [gc_km(la, lo, *storm_pos(storm, t)) for t, la, lo in tr]
    res = []
    for i in range(1, len(d) - 1):
        if d[i] <= d[i - 1] and d[i] < d[i + 1] and d[i] < SWATH_KM:
            res.append((tr[i][0], d[i]))
    return res


# ----------------------------------------------------------------------------- S3
def s3_keys(bucket, prefix):
    keys, token = [], None
    while True:
        url = 'https://%s.s3.amazonaws.com/?list-type=2&prefix=%s' % (bucket, prefix)
        if token:
            url += '&continuation-token=' + urllib.request.quote(token, safe='')
        x = get(url, 20).decode('utf-8')
        keys += re.findall(r'<Key>([^<]+)</Key>', x)
        m = re.search(r'<NextContinuationToken>([^<]+)</NextContinuationToken>', x)
        if not m:
            return keys
        token = m.group(1)


def gran_time(key):
    m = GRAN_RE.search(key)
    if not m:
        return None
    d, t, e = m.group(2), m.group(3), m.group(4)
    st = datetime.strptime(d + t[:6], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc) + timedelta(seconds=int(t[6]) / 10)
    return st


def list_granules(sat, tc):
    bucket, code = SATS[sat][0], SATS[sat][1]
    lo, hi = tc - timedelta(minutes=HALF_WIN_MIN), tc + timedelta(minutes=HALF_WIN_MIN)
    hours = sorted({(lo + timedelta(minutes=m)).strftime('%Y%m%d%H') for m in range(0, int((hi - lo).total_seconds() / 60) + 2)})
    got = {'SDR': {}, 'GEO': {}}
    for h in hours:
        day = '%s/%s/%s' % (h[:4], h[4:6], h[6:8])
        for kind, folder, pre in (('SDR', 'ATMS-SDR', 'SATMS'), ('GEO', 'ATMS-SDR-GEO', 'GATMO')):
            for k in s3_keys(bucket, '%s/%s/%s_%s_d%s_t%s' % (folder, day, pre, code, h[:8], h[8:10])):
                st = gran_time(k)
                if st and lo - timedelta(seconds=32) <= st <= hi:
                    got[kind].setdefault(st.strftime('%Y%m%d%H%M%S%f')[:15], k)   # 同時間有重送 → 用第一個
    pairs = [(st, got['SDR'][st], got['GEO'][st]) for st in sorted(set(got['SDR']) & set(got['GEO']))]
    return pairs


def probe_prefix(sat, t):
    """quick_check.py 用：這個時間（分鐘）開始的 SDR granule 前綴；有任何檔案＝資料到了"""
    bucket, code = SATS[sat][0], SATS[sat][1]
    return bucket, 'ATMS-SDR/%s/SATMS_%s_d%s_t%s' % (t.strftime('%Y/%m/%d'), code, t.strftime('%Y%m%d'), t.strftime('%H%M'))


def download_pass(sat, pairs, tmp):
    bucket = SATS[sat][0]

    def one(k):
        fn = os.path.join(tmp, os.path.basename(k))
        if not os.path.isfile(fn):
            b = get('https://%s.s3.amazonaws.com/%s' % (bucket, k), 40)
            with open(fn, 'wb') as f:
                f.write(b)
        return fn
    keys = [k for _, s, g in pairs for k in (s, g)]
    with ThreadPoolExecutor(8) as ex:
        files = list(ex.map(one, keys))
    nbytes = sum(os.path.getsize(f) for f in files)
    return [(files[2 * i], files[2 * i + 1]) for i in range(len(pairs))], nbytes


def read_pass(file_pairs):
    import h5py
    import numpy as np
    L, O, Z, B = [], [], [], []
    for sdr, geo in file_pairs:
        with h5py.File(sdr, 'r') as hs, h5py.File(geo, 'r') as hg:
            raw = hs['All_Data/ATMS-SDR_All/BrightnessTemperature'][...]
            sc, of = hs['All_Data/ATMS-SDR_All/BrightnessTemperatureFactors'][...][:2]
            bt = raw.astype('f4') * sc + of
            bt[raw >= 65528] = np.nan
            g = hg['All_Data/ATMS-SDR-GEO_All']
            la, lo, za = g['Latitude'][...], g['Longitude'][...], g['SatelliteZenithAngle'][...]
        bad = (la < -90) | (la > 90) | (lo < -180) | (lo > 360)
        la, lo = la.astype('f8'), lo.astype('f8')
        la[bad] = np.nan
        lo[bad] = np.nan
        bt[bad] = np.nan
        L.append(la); O.append(lo % 360); Z.append(za.astype('f8')); B.append(bt)
    return np.concatenate(L), np.concatenate(O), np.concatenate(Z), np.concatenate(B)


# ----------------------------------------------------------------------------- 分析
def footprint_km(zen_deg):
    """V 波段足跡（沿軌, 跨軌）km：斜距 × 波束寬；跨軌再除以 cos(地面天頂角)"""
    z = math.radians(abs(zen_deg))
    eta = math.asin(R_E / (R_E + SAT_ALT_KM) * math.sin(z))
    slant = (R_E + SAT_ALT_KM) * math.cos(eta) - math.sqrt(max(0.0, R_E ** 2 - (R_E + SAT_ALT_KM) ** 2 * math.sin(eta) ** 2))
    a = slant * math.radians(BEAM_DEG)
    return a, a / max(0.2, math.cos(z))


P_ENV = 1008.0      # 西北太平洋颱風外圍環境氣壓（hPa，近似 POCI）
P_TOP = 70.0        # 假設 70 hPa 以上沒有氣壓擾動（Kidder et al. 2000 的靜力積分也取上層參考面）
T_MEAN = 255.0      # 1000–70 hPa 氣柱平均溫度（K，熱帶近似）


def hydro_pressure(prof):
    """流體靜力積分（Kidder et al. 2000；Demuth et al. 2004 的做法，簡化為 ΔT≈ΔTb）：
    在上方參考面（70 hPa）沒有擾動的假設下，氣柱平均暖異常 ΔT̄（以 ln p 加權）讓地面氣壓下降
    Δp ≈ p_env × ln(p_env / p_top) × ΔT̄ / T̄。負值（多半是降水散射造成）當 0。
    回傳 (中心氣壓, Δp)；暖心太弱回傳 (None, None)。"""
    pts = [(P_ENV, 0.0)] + sorted(((p, max(0.0, v)) for p, v in prof if v == v), reverse=True) + [(P_TOP, 0.0)]
    tot = 0.0
    for (p1, v1), (p2, v2) in zip(pts[:-1], pts[1:]):
        tot += 0.5 * (v1 + v2) * math.log(p1 / p2)
    mean = tot / math.log(P_ENV / P_TOP)
    if mean < 0.3:
        return None, None
    dp = P_ENV * (1 - math.exp(-math.log(P_ENV / P_TOP) * mean / T_MEAN))
    return P_ENV - dp, dp


def analyse(storm, sat, tc, lat, lon, zen, bt):
    import numpy as np
    clat, clon = storm_pos(storm, tc)
    kx = 111.195 * math.cos(math.radians(clat))
    dx = ((lon - clon + 540) % 360 - 180) * kx
    dy = (lat - clat) * 111.195
    dist = np.hypot(dx, dy)
    nscan, nfov, nch = bt.shape
    fov = np.broadcast_to(np.arange(nfov), (nscan, nfov))
    # 同掃描位置的環境值（600–1100 km 環帶中位數）
    ring = (dist >= ENV_R[0]) & (dist <= ENV_R[1])
    env = np.full((nfov, nch), np.nan)
    nenv = np.zeros(nfov, int)
    for j in range(nfov):
        sel = ring & (np.abs(fov - j) <= 1)
        n = int(sel.sum())
        if n < 6:
            jj = nfov - 1 - j           # 掃描對稱：借用另一側同角度
            sel = ring & (np.abs(fov - jj) <= 1)
            n = int(sel.sum())
        if n >= 6:
            env[j] = np.nanmedian(bt[sel], axis=0)
            nenv[j] = n
    anom = bt - env[fov]
    ci = CH_P - 1
    near = (dist <= CORE_SEARCH_KM) & np.isfinite(anom[..., ci])
    if not near.any():
        return None, '颱風中心附近沒有有效的 ATMS 足跡（可能在掃描帶邊緣外）'
    a = np.where(near, anom[..., ci], -np.inf)
    i, j = np.unravel_index(int(np.argmax(a)), a.shape)
    fa, fc = footprint_km(zen[i, j])
    prof = [float(anom[i, j, c - 1]) for c, _, _ in CHANNELS]
    core = (np.hypot(dx - dx[i, j], dy - dy[i, j]) <= 50)
    prof50 = [float(np.nanmean(anom[..., c - 1][core])) for c, _, _ in CHANNELS]
    annu = (dist >= 150) & (dist <= 250)
    prof_ring = [float(np.nanmean(anom[..., c - 1][annu])) if annu.any() else float('nan') for c, _, _ in CHANNELS]
    k = int(np.nanargmax(prof))
    scat = float(np.nanmin(np.where(dist <= 120, bt[..., CH_SCAT - 1], np.nan)))
    d8 = prof[[c for c, _, _ in CHANNELS].index(CH_P)]
    d9 = prof[[c for c, _, _ in CHANNELS].index(9)]
    p_est, dp = hydro_pressure([(pp, v) for (_, _, pp), v in zip(CHANNELS, prof)])
    if p_est is not None:
        p_est = max(870.0, min(P_ENV, p_est))
    reasons = []
    strong = (storm.get('vmax') or 0) >= 90 or (p_est is not None and p_est < 960) or (storm.get('mslp') or 9999) < 960
    if strong:
        reasons.append('強颱風：眼通常小於 %d km 足跡 → 暖心被平均掉，估計氣壓會偏高（偏弱）' % round(fa))
    if fc > 50:
        reasons.append('颱風靠近掃描帶邊緣：足跡約 %d×%d km' % (round(fa), round(fc)))
    off = float(dist[i, j])
    if off > 0.6 * fa:
        reasons.append('最暖足跡離定位中心 %d km（中心可能落在兩個足跡之間）' % round(off))
    if np.isfinite(scat) and scat < 200:
        reasons.append('88 GHz 最低 %.0f K：眼牆強對流冰晶散射，低層通道（ch5–6）會偏冷' % scat)
    if d9 > d8 + 0.3:
        reasons.append('ch9（~150 hPa）暖異常大於 ch8：暖心偏高，單用 ch8 的估計可能偏弱（Kabat et al.）')
    if nenv[j] < 12:
        reasons.append('環境樣本偏少（%d 個）' % nenv[j])
    if p_est is None:
        quality = '無法估計'
    elif strong or len(reasons) >= 2:
        quality = '低'
    elif reasons:
        quality = '中'
    else:
        quality = '尚可'
    # 平面圖資料（ch8）
    box = (np.abs(dx) <= PLAN_HALF_KM + 60) & (np.abs(dy) <= PLAN_HALF_KM + 60) & np.isfinite(anom[..., ci])
    res = dict(
        t=iso(tc), sat=sat, satName=SATS[sat][2], atcf=storm['atcf'], label=storm['label'], name=storm['name'],
        category=storm.get('category'), btVmax=storm.get('vmax'), btMslp=storm.get('mslp'),
        center=[round(clat, 2), round(clon, 2)], core=[round(float(lat[i, j]), 2), round(float(lon[i, j]), 2)],
        coreOffsetKm=round(off), fov=int(j) + 1, scanAngle=round(float((j - (nfov - 1) / 2) * 1.11), 1),
        zenith=round(float(zen[i, j]), 1), footprintKm=[round(fa), round(fc)], envN=int(nenv[j]),
        channels=[dict(ch=c, ghz=g, hPa=p, anom=round(v, 2), core50=round(m, 2), ring=(None if not np.isfinite(r) else round(r, 2)))
                  for (c, g, p), v, m, r in zip(CHANNELS, prof, prof50, prof_ring)],
        maxAnom=round(prof[k], 2), maxCh=CHANNELS[k][0], maxHPa=CHANNELS[k][2], dTb8=round(d8, 2), dP=(None if dp is None else round(dp, 1)), pEnv=P_ENV,
        tb88min=(None if not np.isfinite(scat) else round(scat, 1)),
        pEst=(None if p_est is None else round(p_est)), pErr=10, quality=quality, reasons=reasons,
        method='流體靜力積分（Kidder et al. 2000；Demuth et al. 2004），ΔT≈ΔTb，p_env=%d hPa、上方參考面 %d hPa' % (P_ENV, P_TOP))
    plan = dict(x=dx[box], y=dy[box], v=anom[..., ci][box], cx=float(dx[i, j]), cy=float(dy[i, j]))
    edges = []
    for col in (0, nfov - 1):
        ok = np.isfinite(lat[:, col])
        edges.append((lon[ok, col], lat[ok, col]))
    return dict(res=res, plan=plan, edges=edges, clat=clat, clon=clon), None


# ----------------------------------------------------------------------------- 繪圖
def render(an, out_path):
    import numpy as np
    import himawari_pipeline as hp
    hp.setup_font(os.path.join(ROOT, 'fonts'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.tri as mtri
    from matplotlib.patches import Ellipse
    res, plan = an['res'], an['plan']
    W, H = 1200, 760
    fig = plt.figure(figsize=(W / 100, H / 100), dpi=100, facecolor='white')
    bg = '#eaeaf2'
    t = ptime(res['t'])
    tw = t + timedelta(hours=8)
    fig.text(0.010, 0.970, 'ATMS 衛星微波暖心分析　%s %s %s' % (res['label'], res['name'], res.get('category') or ''),
             fontsize=15, fontweight='bold', va='center')
    fig.text(0.990, 0.970, '%s　%s UTC（台灣 %s）' % (res['satName'], t.strftime('%Y-%m-%d %H:%M'), tw.strftime('%m/%d %H:%M')),
             fontsize=11.5, ha='right', va='center', color='#333')
    # 左：資訊框
    q = res['quality']
    qc = {'尚可': '#2e7d32', '中': '#e08a00', '低': '#c62828'}.get(q, '#555')
    axi = fig.add_axes([0.010, 0.07, 0.245, 0.83])
    axi.set_axis_off()
    axi.add_patch(plt.Rectangle((0, 0), 1, 1, transform=axi.transAxes, fc='#f4f4f4', ec='#dddddd'))
    bt = '%s kt／%s hPa' % (('%d' % res['btVmax']) if res.get('btVmax') else '—', ('%d' % res['btMslp']) if res.get('btMslp') else '—')
    rows = [('衛星', '%s ATMS' % res['satName']),
            ('時間', '%s UTC' % t.strftime('%m-%d %H:%M')),
            ('颱風', '%s %s' % (res['label'], res['name'])),
            ('掃描位置', 'FOV %d/96（天頂角 %.0f°）' % (res['fov'], res['zenith'])),
            ('足跡大小', '約 %d×%d km' % tuple(res['footprintKm'])),
            ('最暖足跡', '離定位中心 %d km' % res['coreOffsetKm']),
            ('最大暖異常', '%+.1f K' % res['maxAnom']),
            ('　所在高度', 'ch%d（約 %d hPa）' % (res['maxCh'], res['maxHPa'])),
            ('~250 hPa', 'ΔTb %+.1f K（ch8）' % res['dTb8']),
            ('88 GHz 最低', ('%.0f K' % res['tb88min']) if res.get('tb88min') else '—'),
            ('最佳路徑', bt)]
    y = 0.965
    for k, v in rows:
        axi.text(0.05, y, k, fontsize=10, color='#555', va='top', transform=axi.transAxes)
        axi.text(0.40, y, v, fontsize=10.2, color='#111', va='top', transform=axi.transAxes)
        y -= 0.052
    y -= 0.015
    axi.plot([0.05, 0.95], [y, y], color='#cccccc', lw=1, transform=axi.transAxes)
    y -= 0.03
    pe = ('P ≈ %d hPa' % res['pEst']) if res.get('pEst') else 'P：暖心太弱，不估計'
    axi.text(0.05, y, pe, fontsize=15, fontweight='bold', va='top', transform=axi.transAxes)
    if res.get('dP'):
        axi.text(0.05, y - 0.055, 'ΔP ≈ %.0f hPa（環境 %d hPa）' % (res['dP'], res['pEnv']), fontsize=9.5, va='top', color='#333', transform=axi.transAxes)
    axi.text(0.05, y - 0.105, '可信度：', fontsize=11, va='top', transform=axi.transAxes)
    axi.text(0.33, y - 0.103, q, fontsize=12.5, va='top', color=qc, fontweight='bold', transform=axi.transAxes)
    note = '粗估：流體靜力積分（ΔT≈ΔTb）\n微波足跡比颱風眼大，強颱風\n通常會被估得偏弱（氣壓偏高）'
    axi.text(0.05, y - 0.16, note, fontsize=8.6, va='top', color='#555', linespacing=1.4, transform=axi.transAxes)
    # 中：垂直剖面
    ax = fig.add_axes([0.335, 0.145, 0.285, 0.755], facecolor=bg)
    ps = [c['hPa'] for c in res['channels']]
    v1 = [c['anom'] for c in res['channels']]
    v2 = [c['core50'] for c in res['channels']]
    v3 = [c['ring'] if c['ring'] is not None else np.nan for c in res['channels']]
    ax.axvline(0, color='white', lw=2, zorder=1)
    ax.grid(True, color='white', lw=1, zorder=0)
    ax.plot(v3, ps, '-', color='#9a9a9a', lw=1.6, marker='o', ms=4, mfc='white', zorder=2, label='半徑 150–250 km 平均')
    ax.plot(v2, ps, '-', color='#1f77b4', lw=2.2, marker='o', ms=5, mfc='white', zorder=3, label='暖心周圍 50 km 平均')
    ax.plot(v1, ps, '-', color='#d62728', lw=2.6, marker='o', ms=6, mfc='white', zorder=4, label='暖心中心（最暖足跡）')
    for x, p in zip(v1, ps):
        ax.annotate('%+.1f' % x, (x, p), xytext=(9, 6), textcoords='offset points', color='#d62728', fontsize=10.5, va='center')
    ax.set_yscale('log')
    ax.set_ylim(1250, 75)
    ax.set_yticks(ps)
    ax.set_yticklabels(['ch%d %s GHz\n~%d hPa' % (c['ch'], c['ghz'], c['hPa']) for c in res['channels']], fontsize=8.8)
    ax.minorticks_off()
    allv = [v for v in v1 + v2 + v3 if v == v]
    ax.set_xlim(min(-1.0, min(allv) - 0.5), max(3.0, max(allv) + 1.6))
    ax.set_xlabel('亮度溫度異常 ΔTb（K）\n＝中心 − 同掃描角度環境（600–1100 km）', fontsize=9.5)
    ax.set_title('暖心垂直剖面 Warm-Core Profile', fontsize=12.5)
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.legend(loc='lower right', fontsize=8.5, frameon=True, facecolor='white', framealpha=0.9)
    # 右上：平面圖
    ax2 = fig.add_axes([0.675, 0.45, 0.27, 0.45], facecolor=bg)
    x, y2, v = plan['x'], plan['y'], plan['v']
    vm = min(8.0, max(2.0, float(np.nanmax(np.abs(v))) if v.size else 2.0))
    im = None
    try:
        tri = mtri.Triangulation(x, y2)
        xa, ya = x[tri.triangles], y2[tri.triangles]
        ed = np.max(np.hypot(xa - np.roll(xa, 1, axis=1), ya - np.roll(ya, 1, axis=1)), axis=1)
        tri.set_mask(ed > 160)
        im = ax2.tripcolor(tri, v, cmap='RdBu_r', vmin=-vm, vmax=vm, shading='gouraud')
        lv = [l for l in np.arange(1, 10) if l < vm + 0.01]
        if lv:
            ax2.tricontour(tri, v, levels=lv, colors='k', linewidths=0.6)
    except Exception:
        im = ax2.scatter(x, y2, c=v, cmap='RdBu_r', vmin=-vm, vmax=vm, s=30, marker='s', linewidths=0)
    ax2.plot(0, 0, marker='+', color='k', ms=14, mew=2)
    ax2.plot(plan['cx'], plan['cy'], marker='o', mfc='none', mec='#ffcc00', ms=10, mew=2)
    fa, fc = res['footprintKm']
    ax2.add_patch(Ellipse((plan['cx'], plan['cy']), fc, fa, fill=False, ec='#333', lw=1, ls='--'))
    ax2.set_xlim(-PLAN_HALF_KM, PLAN_HALF_KM)
    ax2.set_ylim(-PLAN_HALF_KM, PLAN_HALF_KM)
    ax2.set_aspect('equal')
    ax2.set_title('ch8 54.94 GHz（~250 hPa）ΔTb 平面圖', fontsize=11)
    ax2.tick_params(labelsize=8)
    ax2.set_xlabel('離定位中心（km，東＋）', fontsize=8.5)
    for sp in ax2.spines.values():
        sp.set_visible(False)
    cax = fig.add_axes([0.952, 0.48, 0.012, 0.39])
    cb = fig.colorbar(im, cax=cax)
    cb.ax.tick_params(labelsize=8)
    cb.set_label('K', fontsize=8)
    ax2.text(0.02, 0.02, '＋定位中心　○最暖足跡（虛線＝足跡）', transform=ax2.transAxes, fontsize=7.8, color='#222')
    # 右下：位置小地圖
    ax3 = fig.add_axes([0.675, 0.075, 0.27, 0.30], facecolor='#dfe8f2')
    clat, clon = an['clat'], an['clon']
    try:
        with open(os.path.join(ROOT, 'assets', 'coast_wpac.json'), encoding='utf-8') as f:
            for ln in json.load(f)['lines']:
                arr = np.array(ln)
                ax3.plot(arr[:, 0], arr[:, 1], color='#4a4a4a', lw=0.6)
    except Exception as e:
        log('（略過海岸線：%s）' % str(e)[:60])
    for ex, ey in an['edges']:
        ax3.plot(ex, ey, color='#d62728', lw=1, ls='--')
    k = PLAN_HALF_KM / 111.195
    kx = k / max(0.2, math.cos(math.radians(clat)))
    ax3.plot([clon - kx, clon + kx, clon + kx, clon - kx, clon - kx], [clat - k, clat - k, clat + k, clat + k, clat - k], color='#1f77b4', lw=1)
    ax3.plot(clon, clat, marker='o', color='#d62728', ms=6)
    ax3.set_xlim(clon - 16, clon + 16)
    ax3.set_ylim(clat - 10, clat + 10)
    ax3.set_aspect(1 / max(0.3, math.cos(math.radians(clat))), adjustable='box')
    ax3.tick_params(labelsize=7)
    ax3.set_title('位置（紅虛線＝掃描帶邊緣、藍框＝平面圖）', fontsize=8.5)
    fig.text(0.010, 0.018, '資料：%s ATMS SDR（NOAA NODD AWS 公開資料）｜方法：Kidder et al. 2000；Brueske & Velden 2003；Herndon et al. 2004；Demuth et al. 2004/2006｜自行計算，僅供參考'
             % res['satName'], fontsize=7.6, color='#555')
    try:
        logo = hp.load_logo()
        if logo is not None:
            hh = 34
            ww = hh * logo.shape[1] / logo.shape[0]
            lax = fig.add_axes([(W - 8 - ww) / W, 4 / H, ww / W, hh / H])
            lax.imshow(logo, alpha=0.9)
            lax.set_axis_off()
    except Exception:
        pass
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fig.savefig(out_path, dpi=100, facecolor='white')
    plt.close(fig)
    return os.path.getsize(out_path)


# ----------------------------------------------------------------------------- 主流程
def run(out, cache, pages_base, now=None, only=None):
    now = now or datetime.now(timezone.utc)
    errors = []
    prev = carry(out, cache, pages_base) or {}
    storms = load_storms(out, now)
    tle, tle_t = refresh_tle(prev, now, errors)
    done = {d for d in (prev.get('done') or []) if d[-12:] >= sid(now - timedelta(hours=HIST_HOURS))}
    passes = [p for p in (prev.get('passes') or []) if ptime(p['t']) >= now - timedelta(hours=HIST_HOURS)]
    prev_storm = {s['atcf']: s for s in prev.get('storms') or []}
    plan, new_n, new_src = [], 0, []
    mw = [m for m in (prev.get('mw') or []) if ptime(m['t']) >= now - timedelta(hours=HIST_HOURS)]   # v20 被動微波影像
    new_mw = 0
    stat_dl = 0
    tmp = tempfile.mkdtemp(prefix='atms_')
    try:
        for s in storms:
            for sat in SATS:
                if only and sat != only:
                    continue
                if sat not in tle:
                    continue
                try:
                    found = find_passes(s, tle[sat], now - timedelta(hours=LOOKBACK_H), now + timedelta(hours=LOOKAHEAD_H))
                except Exception as e:
                    errors.append('軌道計算 %s：%s' % (sat, str(e)[:80]))
                    continue
                for tc, dkm in found:
                    tc = tc.replace(second=0, microsecond=0) + timedelta(minutes=1 if tc.second >= 30 else 0)
                    pid = '%s_%s_%s' % (s['atcf'], sat, sid(tc))
                    if is_done(done, s['atcf'], sat, tc):
                        continue
                    ready_t = tc + timedelta(minutes=HALF_WIN_MIN) - timedelta(seconds=40)   # 最後一個需要的 granule 開始時間
                    bucket, probe = probe_prefix(sat, ready_t)
                    if ready_t > now:
                        plan.append(dict(id=pid, sat=sat, t=iso(tc), distKm=round(dkm), bucket=bucket, probe=probe))
                        continue
                    try:
                        pairs = list_granules(sat, tc)
                    except Exception as e:
                        errors.append('S3 清單 %s：%s' % (sat, str(e)[:80]))
                        plan.append(dict(id=pid, sat=sat, t=iso(tc), distKm=round(dkm), bucket=bucket, probe=probe))
                        continue
                    last = max((p[0] for p in pairs), default='')
                    complete = bool(pairs) and last >= ready_t.strftime('%Y%m%d%H%M%S%f')[:15]
                    if not complete and now < tc + timedelta(minutes=GIVEUP_MIN):
                        plan.append(dict(id=pid, sat=sat, t=iso(tc), distKm=round(dkm), bucket=bucket, probe=probe))
                        log('ATMS：%s 過境 %s 資料還沒上傳完（%d 個 granule），下次再試' % (SATS[sat][2], sid(tc), len(pairs)))
                        continue
                    done.add(pid)
                    if len(pairs) < 4:
                        errors.append('%s %s 過境：S3 上只有 %d 個 granule，略過' % (SATS[sat][2], sid(tc), len(pairs)))
                        continue
                    try:
                        fps, nb = download_pass(sat, pairs, tmp)
                        stat_dl += nb
                        lat, lon, zen, bt = read_pass(fps)
                        # v20：同一批資料畫 89／165 GHz 被動微波影像（失敗不影響暖心分析）
                        try:
                            import atms_mw
                            mla, mlo = storm_pos(s, tc)
                            mres, mwhy = atms_mw.make(s, SATS[sat][2], tc, mla, mlo, lat, lon, zen, bt, out, pid, ROOT)
                            if mres:
                                mw.append(mres)
                                new_mw += 1
                                new_src += [v['src'] for v in mres['products'].values()]
                                log('✅ 微波影像 %s %s %s：%s' % (s['label'], SATS[sat][2], iso(tc), '、'.join(
                                    '%s GHz（足跡 %d×%d km、中心 150 km 內最低 %s K）' % (k, v['footprintKm'][0], v['footprintKm'][1], v['tbMin150km'])
                                    for k, v in mres['products'].items())))
                            else:
                                errors.append('%s %s %s 微波影像：%s' % (s['label'], SATS[sat][2], sid(tc), mwhy))
                        except Exception as e:
                            errors.append('%s %s %s 微波影像失敗：%s' % (s['label'], SATS[sat][2], sid(tc), str(e)[:120]))
                        an, why = analyse(s, sat, tc, lat, lon, zen, bt)
                        if not an:
                            errors.append('%s %s %s：%s' % (s['label'], SATS[sat][2], sid(tc), why))
                            continue
                        rel = 'atms/img/%s.png' % pid
                        render(an, os.path.join(out, rel))
                        an['res']['src'] = rel
                        passes.append(an['res'])
                        new_n += 1
                        new_src.append(rel)
                        r = an['res']
                        log('✅ ATMS %s %s %s：最大暖異常 %+.1f K（ch%d ~%d hPa）、ch8 %+.1f K → P≈%s hPa（可信度 %s）、下載 %.1f MB'
                            % (r['label'], r['satName'], r['t'], r['maxAnom'], r['maxCh'], r['maxHPa'], r['dTb8'], r['pEst'], r['quality'], nb / 1048576))
                    except Exception as e:
                        errors.append('%s %s %s 分析失敗：%s' % (s['label'], SATS[sat][2], sid(tc), str(e)[:120]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    passes.sort(key=lambda p: p['t'])
    mw.sort(key=lambda p: p['t'])
    # 每個颱風的最新一張
    st_out = []
    keep = set()
    for s in storms:
        mine = [p for p in passes if p['atcf'] == s['atcf']]
        lat = mine[-1] if mine else None
        nxt = sorted((p for p in plan if p['id'].startswith(s['atcf'] + '_') and ptime(p['t']) > now), key=lambda p: p['t'])
        mine_mw = [m for m in mw if m['atcf'] == s['atcf']]
        mwl = mine_mw[-1] if mine_mw else None
        st_out.append(dict(atcf=s['atcf'], label=s['label'], name=s['name'], latest=lat, mwLatest=mwl,
                           next=(dict(sat=nxt[0]['sat'], satName=SATS[nxt[0]['sat']][2], t=nxt[0]['t']) if nxt else None)))
        if lat:
            keep.add(lat['src'])
        if mwl:
            keep.update('atms/mw/' + os.path.basename(v['src']) for v in mwl['products'].values())
    # 清掉不是「各颱風最新」的大圖（回放用的縮圖在 history/atms/）
    imgd = os.path.join(out, 'atms', 'img')
    os.makedirs(imgd, exist_ok=True)
    keep |= set(new_src)      # 這次新分析的圖也留著，history.py 才收得到回放
    for fn in os.listdir(imgd):
        if 'atms/img/' + fn not in keep:
            os.remove(os.path.join(imgd, fn))
    mwd = os.path.join(out, 'atms', 'mw')      # v20：微波影像同樣只留「各颱風最新」＋這次新的
    os.makedirs(mwd, exist_ok=True)
    for fn in os.listdir(mwd):
        if 'atms/mw/' + fn not in keep:
            os.remove(os.path.join(mwd, fn))
    try:
        import himawari_pipeline as hp
        if hp.MISSING_GLYPHS:
            errors.append('字型缺字（圖上會變方框）：' + ''.join(sorted(hp.MISSING_GLYPHS)))
    except Exception:
        pass
    man = dict(version=1, generated=iso(now), source='NOAA NODD on AWS（noaa-nesdis-n20/n21/snpp-pds）ATMS-SDR＋ATMS-SDR-GEO',
               method='Kidder et al. 2000; Brueske & Velden 2003; Herndon et al. 2004; Demuth et al. 2006',
               status=('nostorm' if not storms else 'ok'), storms=st_out, passes=passes[-60:],
               plan=sorted(plan, key=lambda p: p['t']), done=sorted(done), tle=tle, tleFetched=tle_t,
               newPasses=new_n, downloadedBytes=stat_dl, errors=errors[:20],
               mw=mw[-60:], newMw=new_mw,
               mwMethod='ATMS ch16 88.2 GHz（QV）／ch17 165.5 GHz（QH）；0.05° 格點高斯權重（σ≈半個取樣間距）；色階 NRL GeoIPS 89H／150H；參考 Hawkins et al. 2001、Lee et al. 2002')
    with open(os.path.join(out, 'atms', 'latest.json'), 'w', encoding='utf-8') as f:
        json.dump(man, f, ensure_ascii=False, indent=1)
    save_cache(out, cache)
    nx = man['plan'][0] if man['plan'] else None
    log('ATMS：颱風 %d 個、新過境 %d 次（微波影像 %d 次）、下載 %.1f MB；下一次過境：%s' % (
        len(storms), new_n, new_mw, stat_dl / 1048576, ('%s %s' % (SATS[nx['sat']][2], nx['t'])) if nx else '—'))
    for e in errors:
        log('  ⚠️', e)
    return man


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='site')
    ap.add_argument('--cache', default='hist_cache')
    ap.add_argument('--pages-base', default=os.environ.get('PAGES_BASE', ''))
    ap.add_argument('--carry', action='store_true', help='只沿用上一版，不分析')
    ap.add_argument('--now', default='', help='測試用：假裝現在時間（ISO）')
    ap.add_argument('--only', default='', help='測試用：只看某顆衛星 n20/n21/npp')
    a = ap.parse_args()
    if a.carry:
        try:
            carry(a.out, a.cache, a.pages_base)
        except Exception as e:
            log('⚠️ ATMS 沿用上一版失敗：', str(e)[:200])
        return 0
    now = ptime(a.now) if a.now else None
    man = run(a.out, a.cache, a.pages_base, now=now, only=a.only or None)
    gh = os.environ.get('GITHUB_OUTPUT')
    if gh:
        with open(gh, 'a') as f:
            f.write('atms_new=%s\n' % ('true' if (man.get('newPasses') or man.get('newMw')) else 'false'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
