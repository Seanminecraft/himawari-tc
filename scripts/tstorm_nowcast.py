#!/usr/bin/env python3
"""大雷雨監測與提前警示（實驗性，tstorm-nowcast 1.0）→ site/radar/nowcast.json

白話：每 10 分鐘拿氣象署最近 3 張雷達整合回波（O-A0059-001），
  ① 找出 ≥40 dBZ 的對流胞（cell，連通區域；SCIT／TITAN 的做法）並和上一張配對、算移動與增強／減弱；
  ② 用相鄰兩張回波的「區塊相關比對」（TREC，Rinehart & Garvey 1978）算出移動場，
     把現在的回波沿移動場往前推 10–60 分鐘（半拉格朗日外延、強度維持不變；pySTEPS／rainymotion 的基本做法）；
  ③ 加上氣象署閃電（O-A0039-001，近 1 小時逐筆）與雨量站（O-A0002-001，10 分鐘／1 小時雨量），
     對每個場館與每個雨量站位置判定 無／注意／警戒／危險，並估計強回波抵達時間（0–60 分）。
  縣市燈號＝該縣市所有雨量站位置的最高等級（雨量站當取樣點，全臺約 1,300 個）。

這是本站自動推估的實驗產品，不是氣象署的大雷雨即時訊息或任何官方警特報。
只用 numpy＋標準函式庫（與 cwa_radar.py 同一組輕量套件），不需要金鑰（cwaopendata S3 公開檔）。
任何一步失敗都只寫 status=error 的 nowcast.json，不影響雷達圖層與網站其他部分。
"""
import argparse
import io
import json
import math
import os
import re
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

VERSION = 'tstorm-nowcast-1.0'
TPE = timezone(timedelta(hours=8))
S3 = 'https://cwaopendata.s3.ap-northeast-1.amazonaws.com'
UA = {'User-Agent': 'doujin-weather-nowcast/1.0 (+GitHub Actions)'}
DATUM_DLON, DATUM_DLAT = 0.0081, -0.0018          # TWD67 → WGS84（與 cwa_radar.py 相同）
# 計算範圍（含金門、馬祖與東部外海，讓移入的對流胞來得及看到）
CROP_LON, CROP_LAT = (117.0, 124.5), (20.5, 27.5)
DS = 2                                              # 0.0125° → 0.025°（約 2.5 km）
KM_Y = 0.025 * 111.2
STEP_MIN = 10
LEADS = [10, 20, 30, 40, 50, 60]
TH = {'cell': 40.0, 'watch': 45.0, 'warn': 50.0, 'danger': 55.0}
LEVELS = ['none', 'watch', 'warn', 'danger']
LEVEL_T = {'none': '無', 'watch': '注意', 'warn': '警戒', 'danger': '危險'}
MAX_CELLS = 60
DISCLAIMER = ('本資料為本站依氣象署雷達、閃電、雨量站公開資料自動推估之實驗性結果，並非中央氣象署發布之大雷雨即時訊息或任何警特報；'
              '資料有延遲（約 10–25 分鐘）且外延預報準確度有限，對流可能突然生成或消散。請以中央氣象署官方發布為準。')


def log(*a):
    print(*a, flush=True)


# ============================================================================ 下載
def http(url, timeout=60):
    import ssl
    import urllib.request
    c = ssl.create_default_context()
    try:
        c.verify_flags &= ~ssl.VERIFY_X509_STRICT
    except Exception:
        pass
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=c) as r:
        return r.read(), dict(r.headers)


def tid(t):
    return t.astimezone(TPE).strftime('%Y%m%d%H%M')


def load_radar_frames(t0=None, frames_dir=None, n=3):
    """回傳 [grid(t0-20), grid(t0-10), grid(t0)]（有缺就少）。grid 同 cwa_radar.parse_grid。"""
    import cwa_radar as cr
    out = []
    if frames_dir:
        files = sorted(f for f in os.listdir(frames_dir) if re.match(r'^\d{12}compref_mosaic\.xml$', f))
        if t0:
            files = [f for f in files if f[:12] <= tid(t0)]
        for f in files[-n:]:
            with open(os.path.join(frames_dir, f), encoding='utf-8') as fh:
                out.append(cr.parse_xml_grid(fh.read()))
        return out
    doc, _, _ = cr.fetch_json('radar')
    g0 = cr.parse_grid(doc)
    prev = []
    for k in range(1, n):
        g = cr.fetch_radar_at((g0['time'] - timedelta(minutes=STEP_MIN * k)).astimezone(timezone.utc), tries=(0,))
        if g is None or abs((g0['time'] - g['time']).total_seconds() - 600 * k) > 60:
            break
        prev.insert(0, g)
    return prev + [g0]


def fetch_lightning(path=None):
    """O-A0039-001（KMZ，近 1 小時逐筆）。回傳 dict(ok, strikes=[(t_ms, lat, lon, kind)], window, error)"""
    try:
        if path:
            blob = open(path, 'rb').read()
        else:
            blob, _ = http(S3 + '/Observation/O-A0039-001.kmz', timeout=30)
        return parse_lightning_kmz(blob)
    except Exception as e:
        return {'ok': False, 'strikes': [], 'error': str(e)[:160]}


def parse_lightning_kmz(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [n for n in z.namelist() if n.lower().endswith('.kml')] or z.namelist()
        kml = z.read(names[0]).decode('utf-8', 'ignore')
    win = re.search(r'<name>[^<]*?(\d{4}-\d\d-\d\d \d\d:\d\d)\s*~\s*(\d{4}-\d\d-\d\d \d\d:\d\d)\s*</name>', kml)
    w = None
    if win:
        w = [datetime.strptime(x, '%Y-%m-%d %H:%M').replace(tzinfo=TPE) for x in win.groups()]
    folder = re.search(r'閃電資料\s*\((\d+)筆\)', kml)
    strikes = []
    for pm in re.findall(r'<Placemark\b.*?</Placemark\s*>', kml, re.S):
        m_c = re.search(r'<coordinates>\s*([-\d.]+)\s*,\s*([-\d.]+)', pm)
        if not m_c:
            continue
        lon, lat = float(m_c.group(1)), float(m_c.group(2))
        t = None
        m_w = re.search(r'<when>\s*([\dT:\-]+?)(Z|[+-]\d\d:?\d\d)?\s*</when\s*>', pm)
        if m_w:
            s = m_w.group(1)
            fmt = '%Y-%m-%dT%H:%M:%S' if s.count(':') == 2 else '%Y-%m-%dT%H:%M'
            try:
                tz = timezone.utc if (m_w.group(2) or 'Z') == 'Z' else datetime.strptime(m_w.group(2).replace(':', ''), '%z').tzinfo
                t = datetime.strptime(s, fmt).replace(tzinfo=tz)
            except ValueError:
                t = None
        if t is None:
            m_d = re.search(r'(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d(?::\d\d)?)', pm)
            if m_d:
                s = m_d.group(1) + ' ' + m_d.group(2)
                t = datetime.strptime(s, '%Y-%m-%d %H:%M:%S' if s.count(':') == 2 else '%Y-%m-%d %H:%M').replace(tzinfo=TPE)
        m_k = re.search(r'閃電種類\s*[:：]\s*(\S+?)(?:<|\s|$)', pm)
        kind = 'cg' if (m_k and '地' in m_k.group(1)) else ('ic' if m_k else None)
        if t is not None:
            strikes.append((int(t.timestamp() * 1000), lat, lon, kind))
    res = {'ok': True, 'strikes': strikes, 'window': [x.isoformat() for x in w] if w else None,
           'declared': int(folder.group(1)) if folder else None}
    if folder and int(folder.group(1)) > 0 and not strikes:
        res['ok'] = False
        res['error'] = '閃電檔列出 %s 筆但無法解析（格式可能變了）' % folder.group(1)
    return res


def fetch_gauges(path=None):
    """O-A0002-001 雨量站（JSON，或歷史檔 XML）。回傳 dict(ok, time, st=[(lat, lon, county, town, name, r10, r1h)])"""
    try:
        if path:
            raw = open(path, 'rb').read()
        else:
            raw, _ = http(S3 + '/Observation/O-A0002-001.json', timeout=60)
        txt = raw.decode('utf-8', 'ignore')
        rows = parse_gauges_json(txt) if txt.lstrip().startswith('{') else parse_gauges_xml(txt)
        times = sorted(r[7] for r in rows if r[7])
        return {'ok': bool(rows), 'time': times[-1] if times else None, 'st': rows}
    except Exception as e:
        return {'ok': False, 'st': [], 'error': str(e)[:160]}


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x >= 0 else None          # -998（儀器故障）、-999 等一律當缺值，不當 0


def parse_gauges_json(txt):
    d = json.loads(txt)
    st = d.get('cwaopendata', d).get('dataset', {}).get('Station') or d.get('records', {}).get('Station') or []
    out = []
    for s in st:
        g = s.get('GeoInfo', {})
        co = {c.get('CoordinateName'): c for c in g.get('Coordinates', [])}
        c = co.get('WGS84') or {}
        try:
            lat, lon = float(c['StationLatitude']), float(c['StationLongitude'])
        except Exception:
            continue
        r = s.get('RainfallElement', {})
        out.append((lat, lon, g.get('CountyName', ''), g.get('TownName', ''), s.get('StationName', ''),
                    _num((r.get('Past10Min') or {}).get('Precipitation')), _num((r.get('Past1hr') or {}).get('Precipitation')),
                    (s.get('ObsTime') or {}).get('DateTime')))
    return out


def parse_gauges_xml(txt):
    out = []
    for s in re.findall(r'<Station>(.*?)</Station>', txt, re.S):
        m = re.search(r'<CoordinateName>WGS84</CoordinateName>.*?<StationLatitude>([^<]+)</StationLatitude>\s*<StationLongitude>([^<]+)</StationLongitude>', s, re.S)
        if not m:
            continue
        f = lambda tag: (re.search(r'<%s>([^<]*)</%s>' % (tag, tag), s) or [None, ''])[1]
        p = lambda tag: (re.search(r'<%s>\s*<Precipitation>([^<]*)</Precipitation>' % tag, s) or [None, None])[1]
        out.append((float(m.group(1)), float(m.group(2)), f('CountyName'), f('TownName'), f('StationName'),
                    _num(p('Past10Min')), _num(p('Past1hr')), f('DateTime')))
    return out


# ============================================================================ 格點
class Grid:
    """裁切＋2×2 取最大值後的計算格點（南→北）。"""
    def __init__(self, g):
        res = g['res']
        x0 = int(round((CROP_LON[0] - g['lon0']) / res)); x1 = int(round((CROP_LON[1] - g['lon0']) / res))
        y0 = int(round((CROP_LAT[0] - g['lat0']) / res)); y1 = int(round((CROP_LAT[1] - g['lat0']) / res))
        x1 = x0 + (x1 - x0) // DS * DS; y1 = y0 + (y1 - y0) // DS * DS
        v = g['v'][y0:y1, x0:x1].astype(np.float32)
        miss = v <= -990                                  # 雷達範圍外／品管移除
        v = np.where(v < 0, 0.0, v)                       # -99＝無回波 → 0 dBZ（只用來計算，不輸出成觀測值）
        ny, nx = v.shape[0] // DS, v.shape[1] // DS
        self.v = v.reshape(ny, DS, nx, DS).max(axis=(1, 3))
        self.miss = miss.reshape(ny, DS, nx, DS).all(axis=(1, 3))
        self.ny, self.nx, self.res = ny, nx, res * DS
        self.lat_c0 = g['lat0'] + (y0 + (DS - 1) / 2) * res + DATUM_DLAT
        self.lon_c0 = g['lon0'] + (x0 + (DS - 1) / 2) * res + DATUM_DLON
        self.time = g['time']
        self.km_x = self.res * 111.2 * math.cos(math.radians(24.0))
        self.km_y = self.res * 111.2

    def ll(self, iy, ix):
        return self.lat_c0 + iy * self.res, self.lon_c0 + ix * self.res

    def ij(self, lat, lon):
        return (lat - self.lat_c0) / self.res, (lon - self.lon_c0) / self.res


def shift(a, dy, dx, fill=0.0):
    """a 往 (+dy 北, +dx 東) 平移（out[y,x] = a[y-dy, x-dx]）。"""
    out = np.full_like(a, fill)
    ny, nx = a.shape
    ys, yd = (slice(0, ny - dy), slice(dy, ny)) if dy >= 0 else (slice(-dy, ny), slice(0, ny + dy))
    xs, xd = (slice(0, nx - dx), slice(dx, nx)) if dx >= 0 else (slice(-dx, nx), slice(0, nx + dx))
    out[yd, xd] = a[ys, xs]
    return out


def disk_offsets(r_km, kx, ky):
    ry, rx = int(math.ceil(r_km / ky)), int(math.ceil(r_km / kx))
    return [(dy, dx) for dy in range(-ry, ry + 1) for dx in range(-rx, rx + 1) if (dy * ky) ** 2 + (dx * kx) ** 2 <= r_km ** 2]


def max_filter_disk(a, r_km, kx, ky):
    out = a.copy()
    for dy, dx in disk_offsets(r_km, kx, ky):
        if dy or dx:
            np.maximum(out, shift(a, dy, dx), out=out)
    return out


# ============================================================================ 對流胞（連通區域）
def label(mask):
    """8 連通標記（純 numpy：最小標號擴散，直到收斂）。回傳 (labels, n)；0＝背景。"""
    big = np.iinfo(np.int32).max
    lab = np.where(mask, np.arange(mask.size, dtype=np.int32).reshape(mask.shape) + 1, big).astype(np.int32)
    for _ in range(2000):
        m = lab.copy()
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy or dx:
                    np.minimum(m, shift(lab, dy, dx, fill=big), out=m)
        m = np.where(mask, m, big)
        if np.array_equal(m, lab):
            break
        lab = m
    lab = np.where(mask, lab, 0)
    u, inv = np.unique(lab, return_inverse=True)
    lab = inv.reshape(lab.shape).astype(np.int32)       # 0 仍是背景（u[0]==0）
    return lab, len(u) - 1


def hull(pts):
    pts = sorted(set(pts))
    if len(pts) <= 2:
        return pts
    cross = lambda o, a, b: (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, up = [], []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(up) >= 2 and cross(up[-2], up[-1], p) <= 0:
            up.pop()
        up.append(p)
    return lo[:-1] + up[:-1]


def find_cells(G, th=TH['cell'], min_px=3):
    lab, n = label(G.v >= th)
    cells = []
    if not n:
        return cells, lab
    idx = np.arange(1, n + 1)
    cnt = np.bincount(lab.ravel(), minlength=n + 1)
    for k in idx:
        if cnt[k] < min_px:
            continue
        ys, xs = np.nonzero(lab == k)
        w = G.v[ys, xs]
        ww = np.power(10.0, w / 10.0)                    # 以 Z（線性）加權，強回波主導重心
        cy, cx = float((ys * ww).sum() / ww.sum()), float((xs * ww).sum() / ww.sum())
        ring = []
        for y, x in zip(ys, xs):
            ring += [(y - .5, x - .5), (y - .5, x + .5), (y + .5, x - .5), (y + .5, x + .5)]
        h = hull([(float(a), float(b)) for a, b in ring])
        if len(h) > 24:
            h = h[::int(math.ceil(len(h) / 24))]
        cells.append({'k': int(k), 'cy': cy, 'cx': cx, 'px': int(cnt[k]), 'max': float(w.max()),
                      'n45': int((w >= 45).sum()), 'n50': int((w >= 50).sum()), 'hull': h,
                      'area': float(cnt[k] * G.km_x * G.km_y)})
    cells.sort(key=lambda c: (-c['max'], -c['px']))
    return cells, lab


# ============================================================================ 移動場（TREC 區塊相關）
def trec(F0, F1, B=16, step=8, S=8, min_echo=20, min_corr=0.5):
    """F0→F1（相隔 10 分鐘）。回傳 [(cy, cx, dy, dx, corr)]，dy/dx 單位：格點／10 分鐘。"""
    a0 = np.clip(F0, 0, 60).astype(np.float32); a1 = np.clip(F1, 0, 60).astype(np.float32)
    a0[a0 < 15] = 0; a1[a1 < 15] = 0
    ny, nx = a0.shape
    p1 = np.pad(a1, S)
    from numpy.lib.stride_tricks import sliding_window_view as swv
    vec = []
    for i in range(0, ny - B + 1, step):
        for j in range(0, nx - B + 1, step):
            a = a0[i:i + B, j:j + B]
            if (a >= 20).sum() < min_echo:
                continue
            reg = p1[i:i + B + 2 * S, j:j + B + 2 * S]
            W = swv(reg, (B, B))                         # (2S+1, 2S+1, B, B)；W[S+dy, S+dx] = a1 平移 (dy,dx) 處
            am = a - a.mean(); an = math.sqrt(float((am * am).sum()))
            if an == 0:
                continue
            Wm = W - W.mean(axis=(2, 3), keepdims=True)
            num = (Wm * am).sum(axis=(2, 3)); den = np.sqrt((Wm * Wm).sum(axis=(2, 3))) * an
            with np.errstate(invalid='ignore', divide='ignore'):
                C = np.where(den > 0, num / den, -1)
            k = int(np.argmax(C)); py, px = divmod(k, C.shape[1]); c = float(C[py, px])
            if c < min_corr:
                continue
            sy, sx = 0.0, 0.0                            # 拋物線次格點修正
            if 0 < py < C.shape[0] - 1:
                d = C[py - 1, px] - 2 * c + C[py + 1, px]
                sy = 0.5 * (C[py - 1, px] - C[py + 1, px]) / d if d < 0 else 0
            if 0 < px < C.shape[1] - 1:
                d = C[py, px - 1] - 2 * c + C[py, px + 1]
                sx = 0.5 * (C[py, px - 1] - C[py, px + 1]) / d if d < 0 else 0
            vec.append((i + B / 2, j + B / 2, py - S + sy, px - S + sx, c))
    return qc_vectors(vec, step)


def qc_vectors(vec, step):
    """與鄰近向量中位數差太多（>3 格／10 分鐘）的剔除（雜訊、新生或消散造成的假配對）。"""
    if len(vec) < 3:
        return vec
    A = np.array(vec)
    keep = []
    for r in A:
        d = np.hypot(A[:, 0] - r[0], A[:, 1] - r[1])
        nb = A[(d > 0) & (d <= 2.5 * step)]
        if len(nb) < 2:
            keep.append(tuple(r)); continue
        if math.hypot(r[2] - np.median(nb[:, 2]), r[3] - np.median(nb[:, 3])) <= 3:
            keep.append(tuple(r))
    return keep


def dense_motion(vec, ny, nx, sigma=16.0, coarse=4):
    """向量 → 每格移動場（高斯加權平均；距離向量太遠的地方漸漸用全區中位數）。"""
    if not vec:
        return np.zeros((ny, nx), np.float32), np.zeros((ny, nx), np.float32), None
    A = np.array(vec)
    gy = np.arange(0, ny, coarse) + coarse / 2; gx = np.arange(0, nx, coarse) + coarse / 2
    YY, XX = np.meshgrid(gy, gx, indexing='ij')
    med = (float(np.median(A[:, 2])), float(np.median(A[:, 3])))
    w = np.exp(-(((YY[..., None] - A[:, 0]) ** 2 + (XX[..., None] - A[:, 1]) ** 2) / (2 * sigma ** 2))) * A[:, 4]
    w0 = 0.05                                             # 背景權重：沒有向量的地方回到全區中位數
    U = ((w * A[:, 2]).sum(-1) + w0 * med[0]) / (w.sum(-1) + w0)
    V = ((w * A[:, 3]).sum(-1) + w0 * med[1]) / (w.sum(-1) + w0)
    U = np.repeat(np.repeat(U, coarse, 0), coarse, 1)[:ny, :nx]
    V = np.repeat(np.repeat(V, coarse, 0), coarse, 1)[:ny, :nx]
    return U.astype(np.float32), V.astype(np.float32), med


def advect(F, U, V, steps):
    """半拉格朗日倒推：每 10 分鐘一步，回傳 [F(+10), F(+20), …]（強度不變、無生成消散）。"""
    ny, nx = F.shape
    Y, X = np.meshgrid(np.arange(ny, dtype=np.float32), np.arange(nx, dtype=np.float32), indexing='ij')
    out = []
    for _ in range(steps):
        iy = np.clip(np.rint(Y).astype(int), 0, ny - 1); ix = np.clip(np.rint(X).astype(int), 0, nx - 1)
        Y = Y - U[iy, ix]; X = X - V[iy, ix]
        iy = np.rint(Y).astype(int); ix = np.rint(X).astype(int)
        ok = (iy >= 0) & (iy < ny) & (ix >= 0) & (ix < nx)
        f = np.zeros_like(F)
        f[ok] = F[iy[ok], ix[ok]]
        out.append(f)
    return out


# ============================================================================ 追蹤
def track(cells_now, cells_prev, U, V, G):
    """把上一張的對流胞沿移動場推 10 分鐘，和現在的配對（最近、距離 < max(5 格, 0.75×等效半徑)）。"""
    used = set()
    for c in cells_now:
        iy, ix = int(round(c['cy'])), int(round(c['cx']))
        iy = min(max(iy, 0), G.ny - 1); ix = min(max(ix, 0), G.nx - 1)
        tu, tv = float(U[iy, ix]), float(V[iy, ix])
        best, bd = None, 1e9
        r_eq = math.sqrt(c['px'] / math.pi)
        for p in cells_prev:
            if p['k'] in used:
                continue
            py = min(max(int(round(p['cy'])), 0), G.ny - 1); px = min(max(int(round(p['cx'])), 0), G.nx - 1)
            fy, fx = p['cy'] + U[py, px], p['cx'] + V[py, px]
            d = math.hypot(c['cy'] - fy, c['cx'] - fx)
            if d < bd:
                best, bd = p, d
        lim = max(5.0, 0.75 * r_eq)
        if best is not None and bd <= lim:
            used.add(best['k'])
            vy, vx = c['cy'] - best['cy'], c['cx'] - best['cx']
            # 分裂／合併會讓重心跳動：和移動場差太多（>4 格／10 分）就用移動場，否則兩者平均
            if math.hypot(vy - tu, vx - tv) > 4:
                vy, vx, how = tu, tv, 'trec'
            else:
                vy, vx, how = (vy + tu) / 2, (vx + tv) / 2, 'track'
            c.update(prev=best, vy=vy, vx=vx, how=how, dmax=c['max'] - best['max'], darea=(c['px'] - best['px']) / max(best['px'], 1))
        else:
            c.update(prev=None, vy=tu, vx=tv, how='trec', dmax=None, darea=None)
        if c['dmax'] is None:
            c['trend'] = 'new'
        elif c['dmax'] >= 3 or c['darea'] >= 0.3:
            c['trend'] = 'up'
        elif c['dmax'] <= -3 or c['darea'] <= -0.3:
            c['trend'] = 'down'
        else:
            c['trend'] = 'steady'
    return cells_now


# ============================================================================ 評估
def hav_km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(min(1, a)))


class Lightning:
    def __init__(self, L, ref_ms):
        self.ok = bool(L.get('ok'))
        self.ref = ref_ms
        s = L.get('strikes') or []
        self.a = np.array([[x[0], x[1], x[2], 1 if x[3] == 'cg' else 0] for x in s], dtype=np.float64) if s else np.zeros((0, 4))

    def count(self, lat, lon, r_km, minutes, start_min=0):
        if not self.ok:
            return None
        if not len(self.a):
            return 0
        t_hi = self.ref - start_min * 60000; t_lo = t_hi - minutes * 60000
        m = (self.a[:, 0] > t_lo) & (self.a[:, 0] <= t_hi)
        if not m.any():
            return 0
        b = self.a[m]
        dy = (b[:, 1] - lat) * 111.2; dx = (b[:, 2] - lon) * 111.2 * math.cos(math.radians(lat))
        return int((dy * dy + dx * dx <= r_km * r_km).sum())


def nearest_gauge(gauges, lat, lon, r_km=5.0):
    best = None
    for g in gauges:
        if abs(g[0] - lat) > 0.06 or abs(g[1] - lon) > 0.07:
            continue
        d = hav_km(lat, lon, g[0], g[1])
        if d <= r_km and (best is None or d < best[0]):
            best = (d, g)
    if not best:
        return None
    d, g = best
    return {'st': g[4], 'km': round(d, 1), 'r10': g[5], 'r1h': g[6]}


def judge(now, eta, ltg, rain):
    """回傳 (level, reasons)。門檻見 README：參考氣象署大雷雨即時訊息（回波 55 dBZ、每分鐘 5 次閃電、時雨量 40 mm）、
    大雨特報（時雨量 40 mm）與 QPEplus 對流胞（≥40 dBZ）。整合回波是「垂直最大值」，層狀降雨的亮帶也可能到 40–45 dBZ，
    所以「注意」從 45 dBZ 起算，避免一般鋒面雨也全臺亮燈。"""
    R = []
    lv = 0
    def up(n, why):
        nonlocal lv
        lv = max(lv, n); R.append((n, why))
    def eta_t(th, lim, lo=-1):
        e = eta.get(str(th))
        return e is not None and lo < e <= lim
    def ew(th):
        e = eta.get(str(th))
        return ('≥%d dBZ 回波影響中' % th) if e == 0 else ('≥%d dBZ 回波約 %d 分鐘內移入' % (th, e))
    d5, d10, d20 = now
    n10 = ltg.get('n10'); n30 = ltg.get('n30')
    r1h = rain.get('r1h') if rain else None; r10 = rain.get('r10') if rain else None
    st = rain.get('st') if rain else ''
    # 危險
    if d5 >= 55: up(3, '5 公里內回波 %d dBZ' % d5)
    if eta_t(55, 30): up(3, ew(55))
    if d5 >= 50 and ((n10 or 0) >= 1 or (r10 or 0) >= 5): up(3, '5 公里內回波 %d dBZ 且有閃電或強降雨' % d5)
    if n10 is not None and n10 >= 10: up(3, '10 公里內近 10 分鐘閃電 %d 次' % n10)
    if r1h is not None and r1h >= 40: up(3, '%s 站時雨量 %.1f mm' % (st, r1h))
    if r10 is not None and r10 >= 10: up(3, '%s 站 10 分鐘雨量 %.1f mm' % (st, r10))
    # 警戒
    if d10 >= 50: up(2, '10 公里內回波 %d dBZ' % d10)
    if eta_t(50, 60): up(2, ew(50))
    if eta_t(55, 60, 30): up(2, ew(55))
    if n10 is not None and n10 >= 1: up(2, '10 公里內近 10 分鐘有閃電（%d 次）' % n10)
    if r1h is not None and r1h >= 20: up(2, '%s 站時雨量 %.1f mm' % (st, r1h))
    if r10 is not None and r10 >= 5: up(2, '%s 站 10 分鐘雨量 %.1f mm' % (st, r10))
    # 注意
    if d20 >= 45: up(1, '20 公里內回波 %d dBZ' % d20)
    if eta_t(45, 60): up(1, ew(45))
    if n30 is not None and n30 >= 1: up(1, '20 公里內近 30 分鐘有閃電（%d 次）' % n30)
    R.sort(key=lambda x: -x[0])
    seen, why = set(), []
    for _, w in R:
        if w not in seen:
            seen.add(w); why.append(w)
    return LEVELS[lv], why[:4]


def build(frames, lightning, gauges, venues, now_ms=None):
    t_start = time.time()
    if not frames:
        raise RuntimeError('沒有雷達資料')
    Gs = [Grid(g) for g in frames]
    G = Gs[-1]
    # ---- 移動場 ----
    vec_all, pairs = [], 0
    for a, b in zip(Gs[:-1], Gs[1:]):
        if abs((b.time - a.time).total_seconds() - 600) > 60:
            continue
        v = trec(a.v, b.v)
        pairs += 1
        w = 1.0 if b is G else 0.6                         # 最新一組權重較高
        vec_all += [(cy, cx, dy, dx, c * w) for cy, cx, dy, dx, c in v]
    U, V, med = dense_motion(vec_all, G.ny, G.nx)
    motion_ok = bool(vec_all)
    # ---- 對流胞＋追蹤 ----
    cells, _ = find_cells(G)
    if len(Gs) >= 2 and abs((G.time - Gs[-2].time).total_seconds() - 600) <= 60:
        prev, _ = find_cells(Gs[-2])
        if len(Gs) >= 3 and abs((Gs[-2].time - Gs[-3].time).total_seconds() - 600) <= 60:
            pp, _ = find_cells(Gs[-3])
            track(prev, pp, U, V, Gs[-2])
        track(cells, prev, U, V, G)
    else:
        for c in cells:
            iy, ix = min(max(int(round(c['cy'])), 0), G.ny - 1), min(max(int(round(c['cx'])), 0), G.nx - 1)
            c.update(prev=None, vy=float(U[iy, ix]), vx=float(V[iy, ix]), how='trec', dmax=None, darea=None, trend='new')
    cells = cells[:MAX_CELLS]
    # ---- 外延 ----
    fc = advect(G.v, U, V, len(LEADS))
    kx, ky = G.km_x, G.km_y
    now5, now10, now20 = (max_filter_disk(G.v, r, kx, ky) for r in (5, 10, 20))
    fcmax = [max_filter_disk(f, 5 + 1.0 * (i + 1), kx, ky) for i, f in enumerate(fc)]   # 不確定性：每 10 分鐘半徑 +1 km
    t0_ms = int(G.time.timestamp() * 1000)
    Lr = Lightning(lightning, now_ms or t0_ms)
    ltg_ref = Lr.ref
    if lightning.get('ok') and lightning.get('window'):
        ltg_ref = int(datetime.fromisoformat(lightning['window'][1]).timestamp() * 1000)
        Lr.ref = ltg_ref

    def sample(arr, lat, lon):
        fy, fx = G.ij(lat, lon)
        iy, ix = int(round(fy)), int(round(fx))
        if not (0 <= iy < G.ny and 0 <= ix < G.nx) or G.miss[iy, ix]:
            return None
        return float(arr[iy, ix])

    def point(lat, lon, rain):
        n5, n10, n20 = sample(now5, lat, lon), sample(now10, lat, lon), sample(now20, lat, lon)
        if n5 is None:
            return None
        eta = {}
        for th in (45, 50, 55):
            if n5 >= th:
                eta[str(th)] = 0; continue
            for i, L_ in enumerate(LEADS):
                v = sample(fcmax[i], lat, lon)
                if v is not None and v >= th:
                    eta[str(th)] = L_; break
            else:
                eta[str(th)] = None
        ltg = {'n10': Lr.count(lat, lon, 10, 10), 'n30': Lr.count(lat, lon, 20, 30), 'n60': Lr.count(lat, lon, 20, 60),
               'prev10': Lr.count(lat, lon, 10, 10, start_min=10)}
        lv, why = judge((round(n5), round(n10), round(n20)), eta, ltg, rain)
        return {'level': lv, 'why': why, 'now': [round(n5), round(n10), round(n20)], 'eta': eta, 'ltg': ltg}

    # ---- 對流胞輸出 ----
    def cell_out(i, c):
        lat, lon = G.ll(c['cy'], c['cx'])
        vy_km, vx_km = c['vy'] * ky, c['vx'] * kx
        spd = math.hypot(vy_km, vx_km) * 6                  # km/h
        toward = (math.degrees(math.atan2(vx_km, vy_km)) + 360) % 360
        fcp = [[round(v, 4) for v in G.ll(c['cy'] + c['vy'] * (L_ / 10), c['cx'] + c['vx'] * (L_ / 10))] for L_ in LEADS]
        poly = [[round(v, 4) for v in G.ll(a, b)] for a, b in c['hull']]
        trk = []
        p = c.get('prev')
        while p is not None and len(trk) < 3:
            trk.insert(0, [round(v, 4) for v in Gs[-2 - len(trk)].ll(p['cy'], p['cx'])]) if len(Gs) >= 2 + len(trk) else None
            p = p.get('prev')
        clat, clon = lat, lon
        ln10 = Lr.count(clat, clon, max(8.0, math.sqrt(c['area'] / math.pi) + 5), 10)
        lp10 = Lr.count(clat, clon, max(8.0, math.sqrt(c['area'] / math.pi) + 5), 10, start_min=10)
        jump = None
        if ln10 is not None and lp10 is not None:
            jump = bool(ln10 >= 5 and ln10 >= 2 * max(lp10, 1))     # 簡化版 lightning jump（閃電頻率 10 分鐘內倍增）
        return {'id': 'C%d' % (i + 1), 'lat': round(lat, 4), 'lon': round(lon, 4), 'max': round(c['max']), 'area': round(c['area']),
                'a45': round(c['n45'] * kx * ky), 'a50': round(c['n50'] * kx * ky), 'poly': poly,
                'spd': round(spd), 'dir': round(toward), 'how': c['how'], 'trend': c['trend'],
                'dmax': None if c['dmax'] is None else round(c['dmax'], 1), 'darea': None if c['darea'] is None else round(c['darea'] * 100),
                'fc': fcp, 'track': trk, 'ltg10': ln10, 'ltgPrev10': lp10, 'jump': jump}
    cells_out = [cell_out(i, c) for i, c in enumerate(cells)]

    def nearest_cell(lat, lon):
        best, bd = None, 1e9
        for c in cells_out:
            pts = [[c['lat'], c['lon']]] + c['fc']
            d = min(hav_km(lat, lon, a, b) for a, b in pts)
            if d < bd:
                best, bd = c, d
        return (best['id'], round(bd)) if best and bd <= 40 else (None, None)

    g_ok = gauges.get('ok')
    vout = []
    for v in venues:
        rain = nearest_gauge(gauges['st'], v['lat'], v['lon']) if g_ok else None
        r = point(v['lat'], v['lon'], rain)
        if r is None:
            vout.append({'k': v['k'], 'n': v['n'], 'c': v['c'], 'lat': v['lat'], 'lon': v['lon'], 'level': None, 'why': ['不在雷達有效範圍']})
            continue
        cid, ckm = nearest_cell(v['lat'], v['lon'])
        vout.append(dict({'k': v['k'], 'n': v['n'], 'c': v['c'], 'lat': v['lat'], 'lon': v['lon'], 'rain': rain, 'cell': cid, 'cellKm': ckm}, **r))
    # ---- 縣市：雨量站位置當取樣點 ----
    counties = {}
    if g_ok:
        for g in gauges['st']:
            r = point(g[0], g[1], {'st': g[4], 'km': 0, 'r10': g[5], 'r1h': g[6]})
            if r is None or not g[2]:
                continue
            c = counties.setdefault(g[2], {'c': g[2], 'level': 'none', 'n': {'watch': 0, 'warn': 0, 'danger': 0}, 'pts': 0, 'eta': None, 'towns': {}})
            c['pts'] += 1
            if r['level'] != 'none':
                c['n'][r['level']] += 1
                c['towns'][g[3]] = max(c['towns'].get(g[3], 0), LEVELS.index(r['level']))
            if LEVELS.index(r['level']) > LEVELS.index(c['level']):
                c['level'] = r['level']
            e = r['eta'].get('50')
            if e is not None and e > 0 and (c['eta'] is None or e < c['eta']):
                c['eta'] = e
        for c in counties.values():
            c['towns'] = [t for t, _ in sorted(c['towns'].items(), key=lambda x: -x[1])][:6]
    ages = {'radar': t0_ms}
    out = {
        'schema': VERSION, 'status': 'ok', 'generated': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
        'radarTime': G.time.astimezone(TPE).isoformat(), 'frames': [g.time.astimezone(TPE).isoformat() for g in Gs],
        'leads': LEADS, 'thresholds': TH,
        'motion': {'ok': motion_ok, 'pairs': pairs, 'vectors': len(vec_all),
                   'median': None if not med else {'spd': round(math.hypot(med[0] * ky, med[1] * kx) * 6), 'dir': round((math.degrees(math.atan2(med[1] * kx, med[0] * ky)) + 360) % 360)}},
        'sources': {
            'radar': {'ok': True, 'dataset': 'O-A0059-001', 'time': G.time.astimezone(TPE).isoformat(), 'frames': len(Gs)},
            'lightning': {'ok': bool(lightning.get('ok')), 'dataset': 'O-A0039-001', 'window': lightning.get('window'),
                          'count': len(lightning.get('strikes') or []) if lightning.get('ok') else None, 'error': lightning.get('error')},
            'gauges': {'ok': bool(g_ok), 'dataset': 'O-A0002-001', 'time': gauges.get('time'), 'n': len(gauges.get('st') or []) if g_ok else None, 'error': gauges.get('error')},
        },
        'cells': cells_out,
        'venues': sorted(vout, key=lambda x: (-(LEVELS.index(x['level']) if x['level'] else -1), x['k'])),
        'counties': sorted(counties.values(), key=lambda c: (-LEVELS.index(c['level']), c['c'])),
        'disclaimer': DISCLAIMER,
        'method': 'TREC 區塊相關移動場＋半拉格朗日外延（強度持續）＋≥40 dBZ 連通對流胞配對追蹤；閃電 O-A0039-001、雨量 O-A0002-001',
        'runtimeSec': None,
    }
    # 閃電點（近 30 分鐘，地圖用；最多 400 點）
    if lightning.get('ok') and lightning.get('strikes'):
        s = [x for x in lightning['strikes'] if x[0] > ltg_ref - 30 * 60000]
        s.sort(key=lambda x: -x[0])
        out['lightning'] = [[round(x[1], 3), round(x[2], 3), int(round((ltg_ref - x[0]) / 60000)), 1 if x[3] == 'cg' else 0] for x in s[:400]]
    out['runtimeSec'] = round(time.time() - t_start, 2)
    return out, {'G': G, 'Gs': Gs, 'fc': fc, 'U': U, 'V': V}


def error_doc(msg):
    return {'schema': VERSION, 'status': 'error', 'generated': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
            'error': str(msg)[:200], 'disclaimer': DISCLAIMER}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='site')
    ap.add_argument('--frames-dir', help='測試用：讀本機 compref_mosaic.xml')
    ap.add_argument('--t0', help='測試用：ISO 時間，取不晚於此的 3 張')
    ap.add_argument('--lightning', help='測試用：本機 KMZ')
    ap.add_argument('--gauges', help='測試用：本機 O-A0002-001 JSON／歷史 XML')
    ap.add_argument('--venues', default=os.path.join(HERE, '..', 'assets', 'tstorm_venues.json'))
    a = ap.parse_args()
    os.makedirs(os.path.join(a.out, 'radar'), exist_ok=True)
    dst = os.path.join(a.out, 'radar', 'nowcast.json')
    try:
        venues = json.load(open(a.venues, encoding='utf-8'))['venues']
        t0 = datetime.fromisoformat(a.t0) if a.t0 else None
        frames = load_radar_frames(t0, a.frames_dir)
        L = fetch_lightning(a.lightning)
        Gg = fetch_gauges(a.gauges)
        doc, _ = build(frames, L, Gg, venues)
        log('✅ nowcast %s：對流胞 %d、場館最高 %s、閃電 %s、雨量站 %s（%.1f 秒）' % (doc['radarTime'], len(doc['cells']),
            (doc['venues'][0]['level'] if doc['venues'] else '-'), doc['sources']['lightning']['count'], doc['sources']['gauges']['n'], doc['runtimeSec']))
    except Exception as e:
        import traceback
        traceback.print_exc()
        doc = error_doc(e)
        log('⚠️ nowcast 失敗：%s' % str(e)[:200])
    with open(dst, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, separators=(',', ':'))
    log('寫入 %s（%d KB）' % (dst, os.path.getsize(dst) // 1024))
    return 0


if __name__ == '__main__':
    sys.exit(main())
