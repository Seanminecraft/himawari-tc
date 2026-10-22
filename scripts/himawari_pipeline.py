#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
向日葵 9 號 (Himawari-9) AHI 原始檔 (HSD) → 颱風中心浮動視窗 + 台灣/西北太平洋區域總覽 圖片

流程
  1. 找活躍颱風：NRL TC Web active-storms（和 tc_diag.gs 同一來源）→ 失敗時改用 UCAR/NCAR b-deck；
     以 b-deck 最近兩筆計算移動向量，把定位外推到衛星掃描時間（最多外推 9 小時）。
  2. 到 NOAA AWS 公開桶 noaa-himawari9（免帳號）找最新且「需要的分段都已上傳」的全盤掃描，
     只下載涵蓋各繪圖範圍的分段 (segment)。
  3. satpy (ahi_hsd) 讀檔、校正 → 依繪圖範圍裁切 → 依輸出解析度先平均降採樣 → 最近鄰取樣到麥卡托格網。
  4. cartopy 畫海岸線、經緯線、色條、時間、資料來源 → WebP。
  5. 寫 site/latest.json；動畫影格從「目前線上的 GitHub Pages」抓回最近 2 小時，不在 git 累積歷史。

用法
  python scripts/himawari_pipeline.py --out site --pages-base https://<USERNAME>.github.io/<REPO>/
  python scripts/himawari_pipeline.py --out site --scan 202610210400   # 指定掃描時間（測試日間真彩用）
"""
import argparse
import bz2
import io
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
import requests

warnings.filterwarnings('ignore')
os.environ.setdefault('PYTHONWARNINGS', 'ignore')

# ----------------------------------------------------------------------------- 設定
BUCKET = 'https://noaa-himawari9.s3.amazonaws.com'
S3_PREFIX = 'AHI-L1b-FLDK'
NRL_ACTIVE = 'https://science.nrlmry.navy.mil/geoips/prod_api/tcweb4/active-storms'
UCAR_BDECK_DIR = 'https://hurricanes.ral.ucar.edu/repository/data/bdecks_open/{y}/'
UA = {'User-Agent': 'doujin-weather-himawari/1.0 (+GitHub Actions; public Himawari-9 renderer)'}

SUB_LON = 140.7
GEOS = dict(proj='geos', h=35785863.0, lon_0=SUB_LON, a=6378137.0, rf=298.257024882273, units='m')
BAND_RES = {'B01': 'R10', 'B02': 'R10', 'B03': 'R05', 'B04': 'R10', 'B08': 'R20', 'B13': 'R20'}
IR_BANDS = ['B13', 'B08']
VIS_BANDS = ['B01', 'B02', 'B03', 'B04']

MAX_STORMS = 3            # 同時最多幾個颱風（依強度排序）
STORM_HALF_KM = 700       # 颱風視窗半寬（公里）→ 約 1400 km 見方
STORM_PX = 900            # 颱風視窗輸出像素
OVERVIEW_BBOX = (105.0, 5.0, 155.0, 38.0)   # 區域總覽：經度 105–155E、緯度 5–38N
OVERVIEW_PX = 1400
ANIM_PRODUCTS = ['ir_bd', 'ir_color', 'wv']
ANIM_FRAMES = 12          # 動畫保留張數（每 10 分鐘一張 → 約 2 小時）
WEBP_QUALITY = 80
SOURCE_LABEL = '資料來源：JMA Himawari-9 (NOAA AWS)'
LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'assets', 'logo.png')
LOGO_FRAC = 0.11          # 標誌高度＝地圖高度的 11%
LOGO_ALPHA = 0.9
LOGO_MARGIN_PX = 10       # 右側邊距
LOGO_BOTTOM_PX = 30       # 底部邊距（留給地圖內的經度標籤）

PRODUCTS = {
    'ir_bd':     {'title': '紅外線 B13 (10.4µm) Dvorak BD 強化', 'short': 'Dvorak BD'},
    'ir_color':  {'title': '紅外線 B13 (10.4µm) 彩色強化', 'short': '彩色紅外線'},
    'wv':        {'title': '水氣 B08 (6.2µm) 上層水氣', 'short': '水氣'},
    'truecolor': {'title': '真彩 B01/B02/B03/B04 (僅日間)', 'short': '真彩 (日間)'},
}
# v17：台灣 衛星＋雷達回波疊圖（cwa_radar.py 提供雷達格點；不用金鑰，S3 公開鏡像）
COMPOSITE_BBOX = (116.6, 20.4, 124.4, 27.6)   # 經度 116.6–124.4E、緯度 20.4–27.6N（含澎湖、金門、馬祖）
COMPOSITE_PX = 1000
COMPOSITE_FRAMES = 12
MAPLAYER_BBOX = (110.0, 12.0, 135.0, 32.0)    # v17：網站首頁地圖用的真實色／水氣圖層（Web Mercator，直接 L.imageOverlay）
MAPLAYER_PX = 2000                             # 寬 2000 px ≈ 1.3 km／px
COUNTY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'assets', 'tw_county_lines.json')
CAT_ZH = [(130, '超級颱風'), (64, '颱風'), (34, '熱帶風暴'), (0, '熱帶性低氣壓')]


def log(*a):
    print('[%6.1fs]' % (time.time() - T0), *a, flush=True)


T0 = time.time()


# ----------------------------------------------------------------------------- HTTP
def http_get(url, timeout=20, binary=False, tries=2):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, timeout=timeout, headers=UA)
            if r.status_code == 200:
                return r.content if binary else r.text
            last = 'HTTP %s' % r.status_code
            if r.status_code in (403, 404):
                break
        except Exception as e:  # noqa
            last = str(e)
        time.sleep(1 + i)
    raise RuntimeError('%s → %s' % (url, last))


# ----------------------------------------------------------------------------- 颱風清單
def _f(v):
    try:
        x = float(v)
        return None if math.isnan(x) else x
    except Exception:
        return None


def parse_bdeck(text):
    """回傳依時間排序的 [(datetime, lat, lon, vmax, mslp, dir, spd, name)]（每個時間只取第一列）"""
    recs, seen = [], set()
    for line in text.splitlines():
        p = [x.strip() for x in line.split(',')]
        if len(p) < 9 or p[4] != 'BEST':
            continue
        try:
            t = datetime.strptime(p[2], '%Y%m%d%H').replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if t in seen:
            continue
        seen.add(t)
        la, lo = p[6], p[7]
        lat = int(la[:-1]) / 10.0 * (1 if la[-1] == 'N' else -1)
        lon = int(lo[:-1]) / 10.0 * (1 if lo[-1] == 'E' else -1)
        if lon < 0:
            lon += 360
        d = _f(p[25]) if len(p) > 26 else None
        s = _f(p[26]) if len(p) > 26 else None
        name = p[27] if len(p) > 27 else ''
        recs.append((t, lat, lon, _f(p[8]), _f(p[9]), d, s, name))
    recs.sort(key=lambda r: r[0])
    return recs


def motion_from_bdeck(recs):
    """(方向°, 速度 kt)；優先用最近兩筆位置計算，否則用 b-deck 欄位"""
    if len(recs) >= 2:
        (t1, la1, lo1, *_), (t2, la2, lo2, *_) = recs[-2], recs[-1]
        hrs = (t2 - t1).total_seconds() / 3600
        if 0 < hrs <= 12:
            dy = (la2 - la1) * 60
            dx = (lo2 - lo1) * 60 * math.cos(math.radians((la1 + la2) / 2))
            spd = math.hypot(dx, dy) / hrs
            return (math.degrees(math.atan2(dx, dy)) % 360, spd)
    if recs and recs[-1][5] is not None and recs[-1][6] is not None:
        return (recs[-1][5], recs[-1][6])
    return None


def fetch_bdeck(num, year):
    url = UCAR_BDECK_DIR.format(y=year) + 'bwp%s%s.dat' % (num, year)
    return parse_bdeck(http_get(url, timeout=20))


def find_storms(now, errors):
    storms = {}
    src_used = []
    # 1) NRL（主要）
    try:
        gj = json.loads(http_get(NRL_ACTIVE, timeout=25))
        for f in gj.get('features', []):
            p = f.get('properties') or {}
            sid = str(p.get('storm_id') or '').lower()
            if not sid.startswith('wp'):
                continue
            atcf = sid[:8]
            lat, lon = _f(p.get('center_latitude')), _f(p.get('center_longitude'))
            if lat is None or lon is None:
                continue
            if lon < 0:
                lon += 360
            fix = None
            try:
                fix = datetime.strptime(str(p.get('start_date')) + str(p.get('start_time') or '0000').zfill(4),
                                        '%Y%m%d%H%M').replace(tzinfo=timezone.utc)
            except Exception:
                pass
            storms[atcf] = dict(atcf=atcf, num=atcf[2:4], year=atcf[4:8], label=atcf[2:4].upper() + 'W',
                                name=str(p.get('storm_name') or '').upper(), lat=lat, lon=lon,
                                vmax=_f(p.get('vmax')), mslp=_f(p.get('pressure')),
                                fixTime=fix, src='NRL')
        src_used.append('NRL TC Web active-storms')
    except Exception as e:
        errors.append('NRL active-storms: %s' % e)
    # 2) UCAR b-deck（NRL 失敗或沒有任何 WP 系統時的後援；同時用來算移動向量）
    year = now.year
    if not storms:
        try:
            idx = http_get(UCAR_BDECK_DIR.format(y=year), timeout=25)
            nums = sorted(set(re.findall(r'href="bwp(\d{2})%d\.dat"' % year, idx)))
            def one(n):
                try:
                    return n, fetch_bdeck(n, year)
                except Exception:
                    return n, []
            with ThreadPoolExecutor(8) as ex:
                for n, recs in ex.map(one, nums):
                    if not recs:
                        continue
                    t, lat, lon, vmax, mslp, d, s, name = recs[-1]
                    if (now - t) > timedelta(hours=30):
                        continue
                    atcf = 'wp%s%d' % (n, year)
                    storms[atcf] = dict(atcf=atcf, num=n, year=str(year), label=n + 'W', name=name.upper(),
                                        lat=lat, lon=lon, vmax=vmax, mslp=mslp, fixTime=t, src='UCAR b-deck',
                                        _bdeck=recs)
            src_used.append('UCAR/NCAR b-deck (bdecks_open)')
        except Exception as e:
            errors.append('UCAR b-deck: %s' % e)
    lst = sorted(storms.values(), key=lambda s: (-(s['vmax'] or 0), s['mslp'] or 9999))
    # 熱帶擾動 (90–99) 排在正式編號系統之後
    lst.sort(key=lambda s: int(s['num']) >= 90)
    lst = [s for s in lst if 90 <= s['lon'] <= 200 and -50 <= s['lat'] <= 50][:MAX_STORMS]
    for s in lst:
        if '_bdeck' not in s:
            try:
                s['_bdeck'] = fetch_bdeck(s['num'], s['year'])
            except Exception as e:
                s['_bdeck'] = []
                errors.append('b-deck %s: %s' % (s['atcf'], e))
        s['motion'] = motion_from_bdeck(s['_bdeck'])
        if not s['name'] or s['name'] in ('INVEST', 'NONAME'):
            s['name'] = 'INVEST' if int(s['num']) >= 90 else (s['name'] or s['label'])
    return lst, src_used


def extrapolate(storm, scan_time):
    s = storm
    s['latC'], s['lonC'], s['centerNote'] = s['lat'], s['lon'], '定位 (未外推)'
    if not s.get('fixTime') or not s.get('motion'):
        return
    hrs = (scan_time - s['fixTime']).total_seconds() / 3600
    if abs(hrs) <= 0.1:
        s['centerNote'] = '定位'
        return
    hrs = max(-9.0, min(hrs, 9.0))
    d, v = s['motion']
    dist = v * hrs  # 海浬
    dlat = dist * math.cos(math.radians(d)) / 60
    dlon = dist * math.sin(math.radians(d)) / 60 / max(0.2, math.cos(math.radians(s['lat'])))
    s['latC'], s['lonC'] = round(s['lat'] + dlat, 2), round(s['lon'] + dlon, 2)
    s['centerNote'] = '定位外推 %.1f h' % hrs


def category(s):
    if int(s['num']) >= 90:
        return '熱帶擾動'
    v = s.get('vmax') or 0
    for k, n in CAT_ZH:
        if v >= k:
            return n
    return ''


# ----------------------------------------------------------------------------- 區域/格網
def build_regions(storms):
    import cartopy.crs as ccrs
    regions = []
    for s in storms:
        lat, lon = s['latC'], s['lonC']
        crs = ccrs.Mercator(central_longitude=lon)
        cx, cy = crs.transform_point(lon, lat, ccrs.PlateCarree())
        h = STORM_HALF_KM * 1000.0 / math.cos(math.radians(lat))
        regions.append(dict(id=s['atcf'], type='storm', storm=s, crs=crs,
                            extent=(cx - h, cx + h, cy - h, cy + h), nx=STORM_PX, ny=STORM_PX))
    w, so, e, n = OVERVIEW_BBOX
    clon = (w + e) / 2
    crs = ccrs.Mercator(central_longitude=clon)
    x0, y0 = crs.transform_point(w, so, ccrs.PlateCarree())
    x1, y1 = crs.transform_point(e, n, ccrs.PlateCarree())
    nx = OVERVIEW_PX
    ny = int(round(nx * (y1 - y0) / (x1 - x0)))
    regions.append(dict(id='overview', type='overview', storm=None, crs=crs, extent=(x0, x1, y0, y1), nx=nx, ny=ny,
                        marks=[s for s in storms if w <= s['lonC'] <= e and so <= s['latC'] <= n]))
    for r in regions:
        x0, x1, y0, y1 = r['extent']
        dx, dy = (x1 - x0) / r['nx'], (y1 - y0) / r['ny']
        xs = x0 + dx * (np.arange(r['nx']) + 0.5)
        ys = y1 - dy * (np.arange(r['ny']) + 0.5)      # 由北往南（影像列順序）
        X, Y = np.meshgrid(xs, ys)
        pts = ccrs.PlateCarree().transform_points(r['crs'], X, Y)
        r['lons'], r['lats'] = pts[..., 0], pts[..., 1]
        # 輸出像素的地面大小（公里，取中心緯度）
        latc = float(np.nanmean(r['lats']))
        r['res_km'] = dx / 1000.0 * math.cos(math.radians(latc))
        lo = np.where(r['lons'] < 0, r['lons'] + 360, r['lons'])
        r['bbox'] = (float(lo.min()) - 0.6, float(r['lats'].min()) - 0.6, float(lo.max()) + 0.6, float(r['lats'].max()) + 0.6)
    return regions


def bbox_region(rid, bbox, px):
    """任意經緯度範圍 → 與 build_regions 相同結構的 Mercator 繪圖範圍（給疊圖用）"""
    import cartopy.crs as ccrs
    w, so, e, n = bbox
    crs = ccrs.Mercator(central_longitude=(w + e) / 2)
    x0, y0 = crs.transform_point(w, so, ccrs.PlateCarree())
    x1, y1 = crs.transform_point(e, n, ccrs.PlateCarree())
    nx = px
    ny = int(round(nx * (y1 - y0) / (x1 - x0)))
    r = dict(id=rid, type='composite', storm=None, crs=crs, extent=(x0, x1, y0, y1), nx=nx, ny=ny, marks=[])
    dx, dy = (x1 - x0) / nx, (y1 - y0) / ny
    X, Y = np.meshgrid(x0 + dx * (np.arange(nx) + 0.5), y1 - dy * (np.arange(ny) + 0.5))
    pts = ccrs.PlateCarree().transform_points(crs, X, Y)
    r['lons'], r['lats'] = pts[..., 0], pts[..., 1]
    r['res_km'] = dx / 1000.0 * math.cos(math.radians(float(np.nanmean(r['lats']))))
    r['bbox'] = (w - 0.6, so - 0.6, e + 0.6, n + 0.6)
    return r


def merc_region(rid, bbox, px):
    """經緯度範圍 → 球面 Web Mercator 均勻格網（與 Leaflet 相同），影像可直接用四角 L.imageOverlay。"""
    w, so, e, n = bbox
    my = lambda la: math.log(math.tan(math.pi / 4 + math.radians(la) / 2))
    y0, y1 = my(so), my(n)
    nx = px
    ny = int(round(nx * (y1 - y0) / math.radians(e - w)))
    lon = w + (e - w) * (np.arange(nx) + 0.5) / nx
    y = y1 - (y1 - y0) * (np.arange(ny) + 0.5) / ny
    lat = np.degrees(2 * np.arctan(np.exp(y)) - math.pi / 2)
    lons, lats = np.meshgrid(lon, lat)
    res = (e - w) / nx * 111.32 * math.cos(math.radians((so + n) / 2))
    return dict(id=rid, type='maplayer', storm=None, nx=nx, ny=ny, marks=[], lons=lons, lats=lats, res_km=res,
                bbox=(w - 0.6, so - 0.6, e + 0.6, n + 0.6))


def build_map_layers(scn_ir, scn_vis, scan_id, scan_time, out, errors):
    """v17：網站首頁地圖的「真實色」「水氣」圖層（取代連不上的 RAMMB/CIRA SLIDER）。
    輸出 img/maplayer/{truecolor,wv}.webp；回傳 latest.json 的 maplayers 欄位。"""
    from PIL import Image
    r = merc_region('maplayer', MAPLAYER_BBOX, MAPLAYER_PX)
    w, so, e, n = MAPLAYER_BBOX
    tstr = scan_time.isoformat().replace('+00:00', 'Z')
    res = {'bounds': [[so, w], [n, e]], 'scanId': scan_id, 'time': tstr, 'projection': 'Web Mercator（球面，與 Leaflet 相同）'}
    os.makedirs(os.path.join(out, 'img', 'maplayer'), exist_ok=True)
    c_ir = scn_ir.crop(ll_bbox=r['bbox'])
    cm, (v0, v1) = wv_cmap()
    t = sample_band(c_ir, 'B08', r) - 273.15
    rgba = (cm(np.clip((t - v0) / (v1 - v0), 0, 1)) * 255).astype(np.uint8)
    rgba[..., 3] = np.where(np.isfinite(t), 255, 0)
    rel = 'img/maplayer/wv_%s.webp' % scan_id
    Image.fromarray(rgba, 'RGBA').save(os.path.join(out, rel), 'WEBP', quality=WEBP_QUALITY, method=4)
    res['wv'] = {'src': rel, 'title': '水氣 B08（6.2µm）上層水氣', 'bytes': os.path.getsize(os.path.join(out, rel))}
    sza = sun_zenith(scan_time, r['lons'], r['lats'])
    day_frac = float(np.mean(sza < 85))
    res['dayFrac'] = round(day_frac, 2)
    if scn_vis is not None and day_frac >= 0.15:
        c_vis = scn_vis.crop(ll_bbox=r['bbox'])
        bands = [sample_band(c_vis, b, r) for b in VIS_BANDS]
        rgb = true_color(*bands, sza)
        alpha = np.where(np.isfinite(bands[2]), np.clip((92.0 - sza) / 6.0, 0, 1) * 255, 0).astype(np.uint8)   # 夜間側透明
        rel = 'img/maplayer/truecolor_%s.webp' % scan_id
        Image.fromarray(np.dstack([rgb, alpha]), 'RGBA').save(os.path.join(out, rel), 'WEBP', quality=WEBP_QUALITY, method=4)
        res['truecolor'] = {'src': rel, 'title': '真實色 B01/B02/B03（僅日間）', 'bytes': os.path.getsize(os.path.join(out, rel))}
    else:
        res['night'] = True
    log('完成 首頁地圖圖層（%s，%d×%d）' % ('、'.join(k for k in ('truecolor', 'wv') if k in res), r['nx'], r['ny']))
    return res


def decode_polyline(sx):
    i, lat, lng, out = 0, 0, 0, []
    while i < len(sx):
        vals = []
        for _ in range(2):
            r = sh = 0
            while True:
                b = ord(sx[i]) - 63
                i += 1
                r |= (b & 31) << sh
                sh += 5
                if b < 32:
                    break
            vals.append(~(r >> 1) if r & 1 else r >> 1)
        lat += vals[0]
        lng += vals[1]
        out.append((lat / 1e4, lng / 1e4))
    return out


def county_lines():
    """縣市界線（內政部國土測繪中心 直轄市、縣市界線，經 taiwan-atlas 簡化；與網站同一份）"""
    try:
        with open(COUNTY_PATH, encoding='utf-8') as f:
            d = json.load(f)
        return [decode_polyline(x) for x in d.get('inner', []) + d.get('coast', [])]
    except Exception as e:
        log('（略過縣市界線：%s）' % e)
        return []


def segments_for(regions):
    """各繪圖範圍需要的全盤分段（1–10，由北往南）。"""
    from pyproj import Proj
    p = Proj(**GEOS)
    segs = set()
    for r in regions:
        lo = r['lons'][::20, ::20].ravel()
        la = r['lats'][::20, ::20].ravel()
        # 加上外框 0.6° 邊界
        lo = np.concatenate([lo, lo + 0.6, lo - 0.6])
        la = np.concatenate([la + 0.6, la - 0.6, la])
        x, y = p(lo, la)
        ok = np.isfinite(y) & (np.abs(y) < 1e20)
        line = (5500000.0 - y[ok]) / 2000.0
        for sgm in np.unique(np.clip((line // 550).astype(int) + 1, 1, 10)):
            segs.add(int(sgm))
    return sorted(segs)


# ----------------------------------------------------------------------------- 太陽天頂角
def sun_zenith(t, lons, lats):
    from pyorbital.astronomy import sun_zenith_angle
    return sun_zenith_angle(t.replace(tzinfo=None), lons, lats)


# ----------------------------------------------------------------------------- AWS S3
def s3_list(prefix, delimiter=None):
    out_keys, out_pref, token = {}, [], None
    while True:
        url = BUCKET + '/?list-type=2&prefix=' + prefix + ('&delimiter=' + delimiter if delimiter else '')
        if token:
            url += '&continuation-token=' + requests.utils.quote(token, safe='')
        x = http_get(url, timeout=20)
        for m in re.finditer(r'<Contents><Key>([^<]+)</Key>.*?<Size>(\d+)</Size>', x, re.S):
            out_keys[m.group(1)] = int(m.group(2))
        out_pref += re.findall(r'<CommonPrefixes><Prefix>([^<]+)</Prefix>', x)
        mt = re.search(r'<NextContinuationToken>([^<]+)</NextContinuationToken>', x)
        if not mt:
            break
        token = mt.group(1)
    return out_keys, out_pref


def seg_key(scan_id, band, seg):
    d = scan_id
    return '%s/%s/%s/%s/%s/HS_H09_%s_%s_%s_FLDK_%s_S%02d10.DAT.bz2' % (
        S3_PREFIX, d[:4], d[4:6], d[6:8], d[8:12], d[:8], d[8:12], band, BAND_RES[band], seg)


def find_scan(now, segs, want_scan=None, errors=None):
    """回傳 (scan_id, keys{key:size})：最新且 B13/B08 所需分段都齊全的掃描。"""
    if want_scan:
        dd = want_scan
        keys, _ = s3_list('%s/%s/%s/%s/%s/' % (S3_PREFIX, dd[:4], dd[4:6], dd[6:8], dd[8:12]))
        return want_scan, keys
    cands = []
    for back in (0, 1):
        d = (now - timedelta(days=back))
        _, prefs = s3_list('%s/%s/' % (S3_PREFIX, d.strftime('%Y/%m/%d')), delimiter='/')
        for pfx in prefs:
            m = re.search(r'/(\d{4})/(\d{2})/(\d{2})/(\d{4})/$', pfx)
            if m:
                cands.append(''.join(m.groups()))
        if len(cands) >= 6:
            break
    for sid in sorted(set(cands), reverse=True)[:6]:
        keys, _ = s3_list('%s/%s/%s/%s/%s/' % (S3_PREFIX, sid[:4], sid[4:6], sid[6:8], sid[8:12]))
        need = [seg_key(sid, b, s) for b in IR_BANDS for s in segs]
        if all(k in keys for k in need):
            return sid, keys
        if errors is not None:
            errors.append('掃描 %s 分段尚未上傳完成，改用前一個' % sid)
    raise RuntimeError('找不到完整的 Himawari-9 掃描')


def download(keys, tmpdir):
    """下載 + 解壓（bz2 解壓會釋放 GIL，可用多執行緒並行）。回傳 (檔案列表, 下載位元組)"""
    def one(k):
        data = http_get(BUCKET + '/' + k, timeout=120, binary=True, tries=3)
        fn = os.path.join(tmpdir, os.path.basename(k)[:-4])
        with open(fn, 'wb') as f:
            f.write(bz2.decompress(data))
        return fn, len(data)
    with ThreadPoolExecutor(12) as ex:
        res = list(ex.map(one, keys))
    return [r[0] for r in res], sum(r[1] for r in res)


# ----------------------------------------------------------------------------- 取樣
def sample_band(creg, band, region):
    """裁切後的 satpy 資料 → 依輸出解析度平均降採樣 → 最近鄰取樣到輸出格網（NaN = 無資料）"""
    da = creg[band]
    area = da.attrs['area']
    native_km = abs(area.pixel_size_x) / 1000.0
    f = max(1, int(region['res_km'] / native_km))
    if f > 1:
        da = da.coarsen(y=f, x=f, boundary='trim').mean()
        xmin, ymin, xmax, ymax = area.area_extent
        px, py = (xmax - xmin) / area.width, (ymax - ymin) / area.height
        wn, hn = area.width // f, area.height // f
        area = area.copy(area_extent=(xmin, ymax - hn * f * py, xmin + wn * f * px, ymax), width=wn, height=hn)
    arr = np.asarray(da.values, dtype=np.float32)
    cols, rows = area.get_array_indices_from_lonlat(region['lons'].ravel(), region['lats'].ravel())
    mask = np.ma.getmaskarray(cols) | np.ma.getmaskarray(rows)
    c = np.ma.getdata(cols)
    r_ = np.ma.getdata(rows)
    ok = (~mask) & (c >= 0) & (c < arr.shape[1]) & (r_ >= 0) & (r_ < arr.shape[0])
    out = np.full(c.shape, np.nan, dtype=np.float32)
    out[ok] = arr[r_[ok], c[ok]]
    return out.reshape(region['lons'].shape)


# ----------------------------------------------------------------------------- 色階
def _lsc(name, stops, vmin, vmax):
    from matplotlib.colors import LinearSegmentedColormap, to_rgb
    stops = sorted(stops)
    return LinearSegmentedColormap.from_list(name, [((t - vmin) / (vmax - vmin), to_rgb(c)) for t, c in stops], N=1024)


BD_SEGMENTS = [  # (上界 °C, 下界 °C, 灰階 0–255 或 (暖端, 冷端) 漸層, 代碼)
    (40, 9, (35, 105), 'WMG'),
    (9, -30, (125, 215), 'OW'),
    (-30, -42, 70, 'DG'),
    (-42, -54, 115, 'MG'),
    (-54, -64, 165, 'LG'),
    (-64, -70, 0, 'B'),
    (-70, -76, 255, 'W'),
    (-76, -81, 135, 'CMG'),
    (-81, -110, 85, 'CDG'),
]


def bd_cmap():
    from matplotlib.colors import ListedColormap
    t = np.linspace(-110, 40, 1501)
    g = np.zeros_like(t)
    for hi, lo, val, _ in BD_SEGMENTS:
        m = (t <= hi) & (t > lo) if lo > -110 else (t <= hi)
        if isinstance(val, tuple):
            g[m] = val[1] + (val[0] - val[1]) * (t[m] - lo) / (hi - lo)
        else:
            g[m] = val
    g[t > 40] = 35
    rgb = np.stack([g, g, g], -1) / 255.0
    return ListedColormap(rgb, 'dvorak_bd'), (-110, 40)


def ir_color_cmap():
    stops = [(-100, '#ffffff'), (-90, '#c8a0ff'), (-82, '#ff00ff'), (-76, '#800000'), (-70, '#ff0000'),
             (-64, '#ff8c00'), (-58, '#ffff00'), (-50, '#00b000'), (-42, '#0040ff'), (-31.01, '#00e0ff'),
             (-31, '#c8c8c8'), (40, '#000000')]
    return _lsc('ir_rainbow', stops, -100, 40), (-100, 40)


def wv_cmap():
    stops = [(-90, '#ffffff'), (-75, '#9be3ff'), (-62, '#2e7fd0'), (-52, '#e6e6e6'), (-42, '#8c8c8c'),
             (-32, '#3c3c3c'), (-22, '#a8580f'), (-8, '#4a1a00'), (10, '#200800')]
    return _lsc('wv', stops, -90, 10), (-90, 10)


def true_color(b1, b2, b3, b4, sza):
    r, g, b, nir = [np.nan_to_num(x, nan=0.0) / 100.0 for x in (b3, b2, b1, b4)]
    g = 0.85 * g + 0.15 * nir                     # hybrid green（AHI B02 為 0.51 µm）
    cz = np.cos(np.radians(np.minimum(sza, 85.0)))
    rgb = np.stack([r, g, b], -1) / np.maximum(cz, 0.087)[..., None]
    x = np.clip(rgb, 0, 1.2) * 255
    y = np.interp(x, [0, 25, 55, 100, 255, 306], [0, 90, 140, 175, 255, 255])   # CIRA 式非線性拉伸
    fade = np.clip((90.0 - sza) / 8.0, 0, 1)[..., None]                          # 晨昏線漸暗
    out = (y * fade).astype(np.uint8)
    nod = ~np.isfinite(b3)
    out[nod] = 0
    return out


# ----------------------------------------------------------------------------- 繪圖
_FONT_READY = False
_LOGO = None


def load_logo():
    """讀取 assets/logo.png（RGBA）；檔案不存在或壞掉就回傳 None（略過標誌）。"""
    global _LOGO
    if _LOGO is None:
        try:
            from PIL import Image
            _LOGO = np.asarray(Image.open(LOGO_PATH).convert('RGBA'), dtype=np.float32) / 255.0
        except Exception as e:
            log('（略過標誌：%s）' % e)
            _LOGO = False
    return _LOGO if _LOGO is not False else None


def add_logo(fig, W, H, map_bottom_px, map_h_px):
    """把標誌放在地圖區右下角（在經度標籤上方，不壓到色條與頁尾）。"""
    logo = load_logo()
    if logo is None:
        return
    h = map_h_px * LOGO_FRAC
    w = h * logo.shape[1] / logo.shape[0]
    lax = fig.add_axes([(W - LOGO_MARGIN_PX - w) / W, (map_bottom_px + LOGO_BOTTOM_PX) / H, w / W, h / H])
    lax.imshow(logo, alpha=LOGO_ALPHA, interpolation='antialiased')
    lax.set_axis_off()


def setup_font(font_dir):
    global _FONT_READY
    if _FONT_READY:
        return
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import font_manager, rcParams
    fams = []
    for fn in sorted(os.listdir(font_dir)) if os.path.isdir(font_dir) else []:
        if fn.lower().endswith(('.otf', '.ttf')):
            font_manager.fontManager.addfont(os.path.join(font_dir, fn))
            fams.append(font_manager.FontProperties(fname=os.path.join(font_dir, fn)).get_name())
    extra = [] if os.environ.get('HW_FONT_STRICT') else ['Noto Sans CJK TC', 'Noto Sans CJK JP']  # 測試字型子集用
    rcParams['font.family'] = ['DejaVu Sans'] + fams + extra
    rcParams['axes.unicode_minus'] = False
    import logging
    logging.getLogger('matplotlib.font_manager').setLevel(logging.ERROR)
    _FONT_READY = True


def render(region, product, data, scan_time, out_path, cmap=None, vr=None, rgb=None):
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    from matplotlib.colorbar import ColorbarBase
    from matplotlib.colors import Normalize
    from PIL import Image

    nx, ny = region['nx'], region['ny']
    top, bot = 74, 92 if product != 'truecolor' else 40
    W, H = nx, ny + top + bot
    fig = plt.figure(figsize=(W / 100, H / 100), dpi=100, facecolor='#101418')
    ax = fig.add_axes([0, bot / H, 1, ny / H], projection=region['crs'])
    ax.set_extent(region['extent'], crs=region['crs'])
    ext = region['extent']
    if rgb is not None:
        ax.imshow(rgb, origin='upper', extent=ext, transform=region['crs'], interpolation='nearest')
    else:
        img = np.ma.masked_invalid(data - 273.15)
        cm = cmap.copy()
        cm.set_bad('#000000')
        ax.imshow(img, origin='upper', extent=ext, transform=region['crs'], cmap=cm,
                  norm=Normalize(*vr), interpolation='nearest')
    lw = 0.7 if region['type'] == 'storm' else 0.55
    ax.add_feature(cfeature.COASTLINE.with_scale('50m'), edgecolor='#ffe94d', linewidth=lw)
    ax.add_feature(cfeature.BORDERS.with_scale('50m'), edgecolor='#ffe94d', linewidth=lw * 0.6, linestyle=':')
    step = 2 if region['type'] == 'storm' else 5
    gl = ax.gridlines(crs=ccrs.PlateCarree(), draw_labels=True, linewidth=0.4, color='#ffffff', alpha=0.45,
                      linestyle='--', xlocs=np.arange(80, 220, step), ylocs=np.arange(-60, 70, step),
                      x_inline=False, y_inline=False)
    gl.top_labels = gl.right_labels = False
    import matplotlib.patheffects as pe
    halo = [pe.withStroke(linewidth=2.2, foreground='#000000')]
    gl.xlabel_style = gl.ylabel_style = {'size': 8, 'color': '#ffffff', 'path_effects': halo}
    gl.xpadding = gl.ypadding = -12
    st = region['storm']
    for s2 in region.get('marks', []):   # 區域總覽：標出各颱風
        mx, my = region['crs'].transform_point(s2['lonC'], s2['latC'], ccrs.PlateCarree())
        ax.plot([mx], [my], marker='+', ms=10, mew=1.3, color='#00ff66', transform=region['crs'])
        ax.text(mx, my, '  %s %s' % (s2['label'], s2['name']), color='#00ff66', fontsize=9, fontweight='bold',
                va='bottom', transform=region['crs'], path_effects=[pe.withStroke(linewidth=2, foreground='#000000')])
    if st:
        cx, cy = region['crs'].transform_point(st['lonC'], st['latC'], ccrs.PlateCarree())
        ax.plot([cx], [cy], marker='+', ms=11, mew=1.3, color='#00ff66', transform=region['crs'])

    add_logo(fig, W, H, bot, ny)

    # 標題列
    tw = scan_time + timedelta(hours=8)
    if st:
        vm = ('%d kt' % st['vmax']) if st.get('vmax') else ''
        pr = (' / %d hPa' % st['mslp']) if st.get('mslp') else ''
        head = '%s %s %s  %s%s' % (st['label'], st['name'], category(st), vm, pr)
    else:
        head = '台灣及西北太平洋 區域總覽'
    fig.text(0.012, 1 - 28 / H, head, color='#ffffff', fontsize=15, fontweight='bold', va='center')
    fig.text(0.012, 1 - 56 / H, PRODUCTS[product]['title'], color='#9fd3ff', fontsize=12, va='center')
    fig.text(0.988, 1 - 28 / H, scan_time.strftime('%Y-%m-%d %H:%M UTC'), color='#ffffff', fontsize=13,
             ha='right', va='center')
    fig.text(0.988, 1 - 56 / H, '台灣時間 ' + tw.strftime('%m/%d %H:%M'), color='#ffd27f', fontsize=12,
             ha='right', va='center')
    # 色條
    if rgb is None:
        cax = fig.add_axes([0.05, 52 / H, 0.9, 16 / H])
        cb = ColorbarBase(cax, cmap=cmap, norm=Normalize(*vr), orientation='horizontal')
        cb.outline.set_edgecolor('#cccccc')
        cax.tick_params(colors='#ffffff', labelsize=8, length=2, pad=1)
        if product == 'ir_bd':
            ticks = [9, -30, -42, -54, -64, -70, -76, -81]
            cb.set_ticks(ticks)
            cb.set_ticklabels([str(t) for t in ticks])
            for hi, lo, _, code in BD_SEGMENTS:
                mid = (max(hi if hi <= 40 else 40, vr[0]) + max(lo, vr[0])) / 2
                cax.text((mid - vr[0]) / (vr[1] - vr[0]), 1.25, code, transform=cax.transAxes, ha='center',
                         va='bottom', color='#ffd27f', fontsize=7)
        else:
            cb.set_ticks(list(range(int(vr[0]), int(vr[1]) + 1, 10)))
        cax.text(1.0, -1.9, '亮度溫度 (°C)', transform=cax.transAxes, ha='right', va='center',
                 color='#cccccc', fontsize=8)
    foot = SOURCE_LABEL + '　自行繪製（GitHub Actions）'
    if st:
        foot += '　＋ 中心：NRL/JTWC %s' % st['centerNote']
    elif product == 'truecolor':
        foot += '　未做瑞利散射校正'
    fig.text(0.012, 14 / H, foot, color='#e0e0e0', fontsize=9, va='center')
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=100, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    im = Image.open(buf).convert('RGB')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    im.save(out_path, 'WEBP', quality=WEBP_QUALITY, method=4)
    return os.path.getsize(out_path)


# ----------------------------------------------------------------------------- 動畫影格
def restore_frames(prev, pages_base, out_dir, region_ids, scan_time):
    """從線上的 Pages 抓回上一版已列出的影格（2 小時內），不需要 git 也不需要 cache。"""
    kept = {}
    if not prev or not pages_base:
        return kept
    cutoff = scan_time - timedelta(minutes=10 * ANIM_FRAMES + 5)
    jobs = []
    for reg in prev.get('regions', []):
        if reg.get('id') not in region_ids:
            continue
        for pk, pv in (reg.get('products') or {}).items():
            for fr in pv.get('frames') or []:
                try:
                    t = datetime.strptime(fr['scanId'], '%Y%m%d%H%M').replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if cutoff <= t < scan_time and re.match(r'^frames/[a-z0-9_]+/[a-z_]+_\d{12}\.webp$', fr['src']):
                    jobs.append((reg['id'], pk, fr))

    def one(j):
        rid, pk, fr = j
        try:
            data = http_get(pages_base.rstrip('/') + '/' + fr['src'], timeout=20, binary=True)
            fn = os.path.join(out_dir, fr['src'])
            os.makedirs(os.path.dirname(fn), exist_ok=True)
            with open(fn, 'wb') as f:
                f.write(data)
            return j
        except Exception:
            return None
    with ThreadPoolExecutor(12) as ex:
        for j in ex.map(one, jobs):
            if j:
                kept.setdefault((j[0], j[1]), []).append(j[2])
    return kept


# ----------------------------------------------------------------------------- v17 衛星＋雷達疊圖
def radar_on_region(g, region):
    """雷達格點（TWD67 經緯度、南→北）→ 繪圖範圍每個像素的 RGBA（最近鄰；範圍外透明）"""
    import cwa_radar as cr
    rgba = cr.colorize(g['v'], 'radar')
    lon = region['lons'] - cr.DATUM_DLON
    lat = region['lats'] - cr.DATUM_DLAT
    ix = np.round((lon - g['lon0']) / g['res']).astype(int)
    iy = np.round((lat - g['lat0']) / g['res']).astype(int)
    ok = (ix >= 0) & (ix < g['nx']) & (iy >= 0) & (iy < g['ny'])
    out = np.zeros(region['lons'].shape + (4,), dtype=np.uint8)
    out[ok] = rgba[iy[ok], ix[ok]]
    return out


def render_composite(region, base_rgb, radar_rgba, scan_time, radar_time, is_day, out_path):
    import matplotlib.pyplot as plt
    import matplotlib.patheffects as pe
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    from matplotlib.colors import ListedColormap, BoundaryNorm
    from matplotlib.colorbar import ColorbarBase
    from PIL import Image
    import cwa_radar as cr

    a = radar_rgba[..., 3:4].astype(np.float32) / 255.0
    rgb = (base_rgb.astype(np.float32) * (1 - a) + radar_rgba[..., :3].astype(np.float32) * a).astype(np.uint8)
    nx, ny = region['nx'], region['ny']
    top, bot = 74, 96
    W, H = nx, ny + top + bot
    fig = plt.figure(figsize=(W / 100, H / 100), dpi=100, facecolor='#101418')
    ax = fig.add_axes([0, bot / H, 1, ny / H], projection=region['crs'])
    ax.set_extent(region['extent'], crs=region['crs'])
    ax.imshow(rgb, origin='upper', extent=region['extent'], transform=region['crs'], interpolation='nearest')
    halo = [pe.withStroke(linewidth=2.0, foreground='#000000')]
    for ln in county_lines():
        la = [p[0] for p in ln]
        lo = [p[1] for p in ln]
        ax.plot(lo, la, color='#0b1020', lw=1.9, alpha=0.55, transform=ccrs.PlateCarree())
        ax.plot(lo, la, color='#f8fafc', lw=0.75, alpha=0.95, transform=ccrs.PlateCarree())
    ax.add_feature(cfeature.COASTLINE.with_scale('50m'), edgecolor='#ffe94d', linewidth=0.5)   # 大陸／菲律賓海岸
    gl = ax.gridlines(crs=ccrs.PlateCarree(), draw_labels=True, linewidth=0.4, color='#ffffff', alpha=0.4,
                      linestyle='--', xlocs=np.arange(110, 130, 1), ylocs=np.arange(15, 35, 1), x_inline=False, y_inline=False)
    gl.top_labels = gl.right_labels = False
    gl.xlabel_style = gl.ylabel_style = {'size': 8, 'color': '#ffffff', 'path_effects': halo}
    gl.xpadding = gl.ypadding = -12
    add_logo(fig, W, H, bot, ny)
    tw = scan_time + timedelta(hours=8)
    base_t = '衛星：Himawari-9 真實色（B01/B02/B03）' if is_day else '衛星：Himawari-9 紅外線 B13（夜間，無真實色）'
    fig.text(0.012, 1 - 28 / H, '台灣 衛星＋雷達回波疊圖', color='#ffffff', fontsize=15, fontweight='bold', va='center')
    fig.text(0.012, 1 - 56 / H, base_t, color='#9fd3ff' if is_day else '#ffb347', fontsize=12, va='center')
    fig.text(0.988, 1 - 28 / H, '衛星 ' + tw.strftime('%m/%d %H:%M') + '（台灣）', color='#ffd27f', fontsize=12, ha='right', va='center')
    fig.text(0.988, 1 - 56 / H, '雷達 ' + radar_time.astimezone(cr.TPE).strftime('%m/%d %H:%M') + '（台灣）', color='#ffd27f',
             fontsize=12, ha='right', va='center')
    # dBZ 色條（與網頁同一份色階；線性內插，畫成 0.5 dBZ 細格）
    stops = cr.load_scale('radar_dbz.json')
    xs = np.array([x for x, _ in stops if x <= 70], dtype=float)
    vals = np.arange(0, 70.01, 0.5)
    cs = np.stack([np.interp(vals, [x for x, _ in stops], [c[k] for _, c in stops]) for k in range(4)], -1) / 255.0
    cs[:, 3] = np.maximum(cs[:, 3], 0.25)
    cax = fig.add_axes([0.05, 56 / H, 0.9, 14 / H])
    cax.set_facecolor('#3a4048')
    ColorbarBase(cax, cmap=ListedColormap(cs[:-1]), norm=BoundaryNorm(vals, len(vals) - 1), orientation='horizontal',
                 ticks=list(range(0, 71, 5)))
    cax.tick_params(colors='#ffffff', labelsize=8, length=2, pad=1)
    cax.text(1.0, -2.1, '雷達回波 (dBZ)', transform=cax.transAxes, ha='right', va='center', color='#cccccc', fontsize=8)
    fig.text(0.012, 14 / H, SOURCE_LABEL + '　雷達：中央氣象署 O-A0059-001　縣市界：內政部國土測繪中心　自行繪製（GitHub Actions）',
             color='#e0e0e0', fontsize=8.5, va='center')
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=100, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    im = Image.open(buf).convert('RGB')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    im.save(out_path, 'WEBP', quality=WEBP_QUALITY, method=4)
    return os.path.getsize(out_path)


def build_composite(scn_ir, scn_vis, scan_id, scan_time, prev, pages_base, out, errors):
    """回傳 latest.json 的 composite 欄位（失敗回傳 None，不影響其他產品）"""
    import cwa_radar as cr
    r = bbox_region('taiwan', COMPOSITE_BBOX, COMPOSITE_PX)
    g = cr.fetch_radar_at(scan_time)          # 與衛星同時間的雷達歷史檔（±10–20 分鐘）
    if g is None:
        try:
            g = cr.parse_grid(cr.fetch_json('radar')[0])
        except Exception as e:
            errors.append('疊圖：抓不到雷達（%s）' % cr._mask_key(e)[:100])
            return None
    sza = sun_zenith(scan_time, r['lons'], r['lats'])
    is_day = bool(scn_vis is not None and float(np.mean(sza < 80)) >= 0.5)
    if is_day:
        c_vis = scn_vis.crop(ll_bbox=r['bbox'])
        base = true_color(*[sample_band(c_vis, b, r) for b in VIS_BANDS], sza)
    else:
        c_ir = scn_ir.crop(ll_bbox=r['bbox'])
        t = sample_band(c_ir, 'B13', r) - 273.15
        gray = np.clip((30.0 - np.nan_to_num(t, nan=30.0)) / 110.0, 0, 1) * 235 + 10   # 冷＝白、暖＝暗
        base = np.repeat(gray[..., None], 3, -1).astype(np.uint8)
    rel = 'img/taiwan/radar_sat.webp'
    size = render_composite(r, base, radar_on_region(g, r), scan_time, g['time'], is_day, os.path.join(out, rel))
    # 動畫影格：抓回線上舊影格＋本次
    os.makedirs(os.path.join(out, 'frames', 'taiwan'), exist_ok=True)
    fr_rel = 'frames/taiwan/radar_sat_%s.webp' % scan_id
    shutil.copyfile(os.path.join(out, rel), os.path.join(out, fr_rel))
    cur = {'scanId': scan_id, 'time': scan_time.isoformat().replace('+00:00', 'Z'), 'src': fr_rel,
           'radarTime': g['time'].isoformat(), 'base': 'truecolor' if is_day else 'ir'}
    frames = []
    cutoff = scan_time - timedelta(minutes=10 * COMPOSITE_FRAMES + 5)
    for fr in ((prev or {}).get('composite') or {}).get('frames') or []:
        try:
            ft = datetime.strptime(fr['scanId'], '%Y%m%d%H%M').replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if not (cutoff <= ft < scan_time) or not re.match(r'^frames/taiwan/radar_sat_\d{12}\.webp$', fr.get('src', '')):
            continue
        try:
            data = http_get(pages_base.rstrip('/') + '/' + fr['src'], timeout=20, binary=True)
            with open(os.path.join(out, fr['src']), 'wb') as f:
                f.write(data)
            frames.append(fr)
        except Exception:
            pass
    frames = sorted(frames, key=lambda x: x['scanId'])[-(COMPOSITE_FRAMES - 1):] + [cur]
    log('完成 台灣衛星＋雷達疊圖（%s，雷達 %s，%d KB，影格 %d）' % ('真實色' if is_day else '紅外線', cr.tid(g['time']), size // 1024, len(frames)))
    return {'id': 'taiwan', 'title': '台灣 衛星（真實色／夜間紅外線）＋雷達回波疊圖', 'src': rel, 'base': cur['base'],
            'scanId': scan_id, 'radarTime': cur['radarTime'], 'bboxLonLat': list(COMPOSITE_BBOX), 'frames': frames}


# ----------------------------------------------------------------------------- 主程式
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='site')
    ap.add_argument('--pages-base', default=os.environ.get('PAGES_BASE', ''))
    ap.add_argument('--scan', default='', help='指定掃描 YYYYMMDDHHMM（預設：最新）')
    ap.add_argument('--force', action='store_true', help='就算掃描時間和線上相同也重畫')
    ap.add_argument('--no-storms', action='store_true', help='測試：不抓颱風，只畫區域總覽')
    ap.add_argument('--storm', default='', help='測試：手動指定 "名稱,緯度,經度"')
    ap.add_argument('--font-dir', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'fonts'))
    ap.add_argument('--static-dir', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'site_static'))
    a = ap.parse_args()

    now = datetime.now(timezone.utc)
    errors, stats = [], {}
    gh_out = os.environ.get('GITHUB_OUTPUT')

    def set_output(k, v):
        if gh_out:
            with open(gh_out, 'a') as f:
                f.write('%s=%s\n' % (k, v))

    # 1) 颱風
    storms, storm_src = [], []
    if a.storm:
        nm, la, lo = a.storm.split(',')
        storms = [dict(atcf='wp99' + str(now.year), num='99', year=str(now.year), label='TEST', name=nm, lat=float(la),
                       lon=float(lo), vmax=None, mslp=None, fixTime=None, motion=None, src='manual')]
        storm_src = ['manual']
    elif not a.no_storms:
        storms, storm_src = find_storms(now, errors)
    log('颱風：', ', '.join('%s %s (%.1fN %.1fE %s kt)' % (s['label'], s['name'], s['lat'], s['lon'], s['vmax'])
                            for s in storms) or '無（只畫區域總覽）')
    stats['t_storms'] = round(time.time() - T0, 1)

    # 2) 先用定位估算分段 → 找掃描 → 外推到掃描時間後重建格網
    for s in storms:
        s['latC'], s['lonC'], s['centerNote'] = s['lat'], s['lon'], '定位'
    regions = build_regions(storms)
    segs = segments_for(regions)
    scan_id, keys = find_scan(now, segs, a.scan or None, errors)
    scan_time = datetime.strptime(scan_id, '%Y%m%d%H%M').replace(tzinfo=timezone.utc)
    for s in storms:
        extrapolate(s, scan_time)
    regions = build_regions(storms)
    segs = segments_for(regions)
    log('掃描 %s，需要分段 %s' % (scan_id, segs))

    prev = None
    if a.pages_base:
        try:
            prev = json.loads(http_get(a.pages_base.rstrip('/') + '/latest.json?t=%d' % time.time(), timeout=15))
        except Exception as e:
            errors.append('讀取線上 latest.json 失敗（第一次執行屬正常）：%s' % str(e)[:120])
    cur_ids = [r['id'] for r in regions]
    if prev and not a.force and prev.get('scanId') == scan_id and [r.get('id') for r in prev.get('regions', [])] == cur_ids:
        log('線上已是 %s 的圖，略過（不重新部署）' % scan_id)
        set_output('skip', 'true')
        return 0

    # 日夜判斷
    for r in regions:
        sz = sun_zenith(scan_time, r['lons'][::10, ::10], r['lats'][::10, ::10])
        r['dayFrac'] = float(np.mean(sz < 80))
        r['day'] = r['dayFrac'] >= 0.15
    any_day = any(r['day'] for r in regions)

    # 3) 下載
    need = [seg_key(scan_id, b, s) for b in IR_BANDS for s in segs]
    if any_day:
        vsegs = segments_for([r for r in regions if r['day']])   # 只下載「白天」範圍需要的可見光分段
        vk = [seg_key(scan_id, b, s) for b in VIS_BANDS for s in vsegs]
        if all(k in keys for k in vk):
            need += vk
        else:
            errors.append('可見光分段尚未齊全，本次略過真彩')
            any_day = False
    need = [k for k in need if k in keys]
    tmp = tempfile.mkdtemp(prefix='hw_')
    t1 = time.time()
    files, nbytes = download(need, tmp)
    stats['download_mb'] = round(nbytes / 1e6, 1)
    stats['download_files'] = len(files)
    stats['t_download'] = round(time.time() - t1, 1)
    log('下載 %d 個檔案 %.1f MB，%.1f 秒' % (len(files), nbytes / 1e6, time.time() - t1))

    # 4) 讀檔、取樣、繪圖
    from satpy import Scene
    setup_font(a.font_dir)
    out = a.out
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out)
    ir_files = [f for f in files if any('_%s_' % b in f for b in IR_BANDS)]
    vis_files = [f for f in files if any('_%s_' % b in f for b in VIS_BANDS)]
    scn_ir = Scene(reader='ahi_hsd', filenames=ir_files)
    scn_ir.load(IR_BANDS)
    scn_vis = None
    if any_day and vis_files:
        scn_vis = Scene(reader='ahi_hsd', filenames=vis_files)
        scn_vis.load(VIS_BANDS)
    cm_bd, vr_bd = bd_cmap()
    cm_ir, vr_ir = ir_color_cmap()
    cm_wv, vr_wv = wv_cmap()
    frames_prev = restore_frames(prev, a.pages_base, out, cur_ids, scan_time)
    t2 = time.time()
    out_regions, sizes = [], 0
    for r in regions:
        rid = r['id']
        c_ir = scn_ir.crop(ll_bbox=r['bbox'])
        b13 = sample_band(c_ir, 'B13', r)
        b08 = sample_band(c_ir, 'B08', r)
        prods = {}
        for pk, dat, cm, vr in (('ir_bd', b13, cm_bd, vr_bd), ('ir_color', b13, cm_ir, vr_ir), ('wv', b08, cm_wv, vr_wv)):
            rel = 'img/%s/%s.webp' % (rid, pk)
            sizes += render(r, pk, dat, scan_time, os.path.join(out, rel), cmap=cm, vr=vr)
            prods[pk] = {'src': rel, 'title': PRODUCTS[pk]['title']}
        if r['day'] and scn_vis is not None:
            c_vis = scn_vis.crop(ll_bbox=r['bbox'])
            bands = [sample_band(c_vis, b, r) for b in VIS_BANDS]
            sza = sun_zenith(scan_time, r['lons'], r['lats'])
            rgb = true_color(*bands, sza)
            rel = 'img/%s/truecolor.webp' % rid
            sizes += render(r, 'truecolor', None, scan_time, os.path.join(out, rel), rgb=rgb)
            prods['truecolor'] = {'src': rel, 'title': PRODUCTS['truecolor']['title']}
        # 動畫影格
        for pk in ANIM_PRODUCTS:
            rel = 'frames/%s/%s_%s.webp' % (rid, pk, scan_id)
            os.makedirs(os.path.join(out, 'frames', rid), exist_ok=True)
            shutil.copyfile(os.path.join(out, prods[pk]['src']), os.path.join(out, rel))
            fr = sorted(frames_prev.get((rid, pk), []), key=lambda x: x['scanId'])
            fr = [x for x in fr if x['scanId'] != scan_id] + [{'scanId': scan_id, 'time': scan_time.isoformat().replace('+00:00', 'Z'), 'src': rel}]
            fr = fr[-ANIM_FRAMES:]
            keep = {x['src'] for x in fr}
            for fn in os.listdir(os.path.join(out, 'frames', rid)):
                if fn.startswith(pk + '_') and 'frames/%s/%s' % (rid, fn) not in keep:
                    os.remove(os.path.join(out, 'frames', rid, fn))
            prods[pk]['frames'] = fr
        st = r['storm']
        ent = {'id': rid, 'type': r['type'], 'day': r['day'], 'dayFrac': round(r['dayFrac'], 2), 'products': prods,
               'bboxLonLat': [round(float(np.nanmin(np.where(r['lons'] < 0, r['lons'] + 360, r['lons']))), 2),
                              round(float(np.nanmin(r['lats'])), 2),
                              round(float(np.nanmax(np.where(r['lons'] < 0, r['lons'] + 360, r['lons']))), 2),
                              round(float(np.nanmax(r['lats'])), 2)]}
        if st:
            ent.update({'atcf': st['atcf'], 'label': st['label'], 'name': st['name'], 'category': category(st),
                        'vmax': st.get('vmax'), 'mslp': st.get('mslp'), 'fixLat': st['lat'], 'fixLon': st['lon'],
                        'fixTime': st['fixTime'].isoformat().replace('+00:00', 'Z') if st.get('fixTime') else None,
                        'centerLat': st['latC'], 'centerLon': st['lonC'], 'centerNote': st['centerNote'],
                        'motion': {'dir': round(st['motion'][0]), 'kt': round(st['motion'][1], 1)} if st.get('motion') else None,
                        'stormSource': st['src']})
        else:
            ent.update({'name': '台灣及西北太平洋 區域總覽'})
        out_regions.append(ent)
        log('完成 %s（%s）' % (rid, ', '.join(prods)))
    composite = None
    try:
        t3 = time.time()
        composite = build_composite(scn_ir, scn_vis, scan_id, scan_time, prev, a.pages_base, out, errors)
        stats['t_composite'] = round(time.time() - t3, 1)
    except Exception as e:   # 疊圖失敗不影響其他衛星圖
        errors.append('衛星＋雷達疊圖失敗：%s' % str(e)[:160])
    maplayers = None
    try:
        maplayers = build_map_layers(scn_ir, scn_vis, scan_id, scan_time, out, errors)
    except Exception as e:   # 失敗不影響其他衛星圖（網站會退回紅外線）
        errors.append('首頁地圖圖層失敗：%s' % str(e)[:160])
    stats['t_render'] = round(time.time() - t2, 1)
    shutil.rmtree(tmp, ignore_errors=True)

    # 5) 靜態檔 + latest.json
    if os.path.isdir(a.static_dir):
        for fn in os.listdir(a.static_dir):
            shutil.copyfile(os.path.join(a.static_dir, fn), os.path.join(out, fn))
    stats['t_total'] = round(time.time() - T0, 1)
    stats['image_kb'] = round(sizes / 1024)
    latest = {
        'version': 1,
        'satellite': 'Himawari-9 AHI',
        'source': SOURCE_LABEL,
        'sourceUrl': 'https://registry.opendata.aws/noaa-himawari/',
        'stormSources': storm_src,
        'scanId': scan_id,
        'scanTime': scan_time.isoformat().replace('+00:00', 'Z'),
        'generated': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
        'products': {k: v['title'] for k, v in PRODUCTS.items()},
        'regions': out_regions,
        'composite': composite,
        'maplayers': maplayers,
        'errors': errors,
        'stats': stats,
    }
    with open(os.path.join(out, 'latest.json'), 'w', encoding='utf-8') as f:
        json.dump(latest, f, ensure_ascii=False, indent=1)
    set_output('skip', 'false')
    log('完成：%s' % json.dumps(stats, ensure_ascii=False))
    if errors:
        log('備註：' + ' | '.join(errors))
    return 0


if __name__ == '__main__':
    sys.exit(main())
