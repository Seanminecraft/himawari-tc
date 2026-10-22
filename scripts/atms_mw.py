#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v20：ATMS 被動微波颱風影像（89 GHz、165 GHz），用 atms_warmcore.py 已經下載的同一批 SDR granule（不另外下載）。

白話：微波可以「穿過」高空的卷雲，看到下面的雨帶、冰晶和眼牆。大雨滴、冰霰會把地面往上發的微波「散射」掉，
所以強對流在 89／165 GHz 會顯得「很冷」（亮度溫度很低）。這就是 NRL 颱風網頁上 89 GHz 圖的原理。

產品
  • 89 GHz：ATMS ch16（88.2 GHz，準垂直極化 QV，波束 2.2° → 正下方足跡約 32 km）。
      色階：NRL 89H 色階（GeoIPS pmw_tb/cmap_89H，NRL Open License），冷（強散射、深對流）＝紅／棕／黑，
      中等＝黃／綠，暖（海面、沒有對流）＝青／藍。注意 ATMS 是 QV 不是 H 極化，海面背景會比 SSMIS／GMI 的 89H 暖（偏深藍）。
  • 165 GHz：ATMS ch17（165.5 GHz，準水平極化 QH，波束 1.1° → 正下方約 16 km）。
      色階：NRL 150H 色階（GeoIPS pmw_tb/cmap_150H），冷（冰晶散射強）＝藍／青，暖＝紅／紫紅。
  • 不做 37 GHz／PCT 彩色合成：NRL 的 37／89 GHz 彩色合成與 PCT 需要同頻率的 V、H 兩個極化；ATMS 每個頻率只有一個
    （且隨掃描角混合的）極化，31.4 GHz（ch2）足跡約 75 km、又沒有 37 GHz，硬做會是假的合成 → 不做。

重新取樣（不增加細節）
  • 以定位中心（外推到過境時間）為中心、1400 km 見方、0.05° 經緯度格點。
  • 每個格點用附近足跡中心的高斯權重平均，σ＝該掃描位置「取樣間距」的一半（正下方約 8 km、掃描帶邊緣約 17 km），
    3σ 以外不用；附近 2σ 內沒有任何足跡的格點＝沒有資料（灰色）。這只把離散的足跡畫成連續圖，不會比儀器本身更清楚。
  • 圖上標示颱風位置的實際足跡大小（沿軌×跨軌 km）。

References
  Hawkins, J. D., T. F. Lee, J. Turk, C. Sampson, J. Kent, and K. Richardson, 2001: Real-time internet distribution of
    satellite products for tropical cyclone reconnaissance. Bull. Amer. Meteor. Soc., 82, 567–578.（NRL 颱風網頁）
  Lee, T. F., F. J. Turk, J. Hawkins, and K. Richardson, 2002: Interpretation of TRMM TMI images of tropical cyclones.
    Earth Interactions, 6(3), 1–17.（85／89 GHz 與 37 GHz 颱風影像判讀）
  色階：NRL GeoIPS pmw_tb/cmap_89H、cmap_150H（NRL Open License）。
"""
import json
import math
import os
from datetime import timedelta

HALF_KM = 700.0            # 圖面半寬（km）→ 1400 km 見方
GRID_DEG = 0.05            # 格點間距
SAMPLE_DEG = 1.11          # ATMS 掃描取樣角（每個 FOV 相隔 1.11°）
ALONG_KM = 16.5            # 沿軌取樣間距（每 8/3 秒一條掃描線）
PRODUCTS = {
    # 代號: (通道, 頻率標籤, 極化, 波束寬°, 色階範圍, 色階分段, 顏色, 刻度, 中文標題)
    '89': dict(ch=16, freq='88.2 GHz', pol='QV', beam=2.2, rng=(105, 305),
               vals=[(105, 180), (180, 212), (212, 228), (228.1, 254), (254.1, 280), (280, 305)],
               cols=[('white', 'black'), ('#A4641A', '#FC0603'), ('#F4CD03', '#F2F403'), ('#8CF303', '#0FB503'),
                     ('#06DCFD', '#0708B5'), ('navy', 'white')],
               ticks=[105, 150, 180, 212, 228, 254, 280, 305], title='微波 89 GHz', cmap='NRL 89H（GeoIPS）'),
    '165': dict(ch=17, freq='165.5 GHz', pol='QH', beam=1.1, rng=(110, 310),
                vals=[(110, 130), (130, 160), (160, 180), (180, 210), (210, 230), (230, 250), (250, 270), (270, 290), (290, 310)],
                cols=[('black', 'blue'), ('blue', 'cyan'), ('cyan', 'green'), ('green', 'yellow'), ('yellow', 'orange'),
                      ('orange', 'red'), ('red', 'maroon'), ('maroon', 'darkmagenta'), ('darkmagenta', 'white')],
                ticks=[110, 130, 150, 170, 190, 210, 230, 250, 270, 290, 310], title='微波 165 GHz', cmap='NRL 150H（GeoIPS）'),
}
READ = {
    '89': '紅／棕／黑＝強散射（深對流、眼牆、強雨帶）；黃／綠＝中等對流；青／藍＝海面或沒有對流（ATMS 為 QV 極化，海面偏深藍）',
    '165': '藍／青＝冰晶散射強（深對流、眼牆）；綠／黃＝中等；紅／紫紅＝暖（沒有冰晶的區域）',
}


def make_cmap(p):
    """GeoIPS create_linear_segmented_colormap 同樣的分段線性色階（各段內兩色線性、段與段之間可不連續）"""
    from matplotlib.colors import LinearSegmentedColormap, to_rgb
    lo, hi = p['rng']
    cd = {'red': [], 'green': [], 'blue': []}
    prev = None
    for (a, b), (c0, c1) in zip(p['vals'], p['cols']):
        x0, x1 = (a - lo) / (hi - lo), (b - lo) / (hi - lo)
        r0, r1 = to_rgb(c0), to_rgb(c1)
        for i, k in enumerate(('red', 'green', 'blue')):
            if prev is None:
                cd[k].append((x0, r0[i], r0[i]))
            else:
                cd[k][-1] = (cd[k][-1][0], cd[k][-1][1], r0[i])
            cd[k].append((x1, r1[i], r1[i]))
        prev = x1
    for k in cd:   # 端點強制 0 與 1
        cd[k][0] = (0.0,) + cd[k][0][1:]
        cd[k][-1] = (1.0,) + cd[k][-1][1:]
        # 修正浮點數造成的非遞增
        fixed, last = [], -1
        for x, y0, y1 in cd[k]:
            x = max(x, last)
            fixed.append((x, y0, y1))
            last = x
        cd[k] = fixed
    return LinearSegmentedColormap('pmw_' + p['ch'].__str__(), cd, N=512)


def footprint(zen_deg, beam):
    """足跡（沿軌, 跨軌）km：斜距 × 波束寬；跨軌再除以 cos(天頂角)"""
    R, H = 6371.0, 824.0
    z = math.radians(abs(zen_deg))
    eta = math.asin(R / (R + H) * math.sin(z))
    slant = (R + H) * math.cos(eta) - math.sqrt(max(0.0, R ** 2 - (R + H) ** 2 * math.sin(eta) ** 2))
    a = slant * math.radians(beam)
    return a, a / max(0.2, math.cos(z))


def remap(lat, lon, zen, tb, clat, clon):
    """足跡 → 以中心為準的 0.05° 經緯度格點（高斯權重，σ＝半個取樣間距）。回傳 (glat, glon, grid, 足跡中心數)"""
    import numpy as np
    k = 111.195
    cl = math.cos(math.radians(clat))
    dlat = HALF_KM / k
    dlon = HALF_KM / (k * cl)
    glat = np.arange(clat - dlat, clat + dlat + 1e-6, GRID_DEG)
    glon = np.arange(clon - dlon, clon + dlon + 1e-6, GRID_DEG)
    la, lo, ze, v = lat.ravel(), lon.ravel(), zen.ravel(), tb.ravel()
    dl = ((lo - clon + 180) % 360) - 180
    x = dl * k * np.cos(np.radians(la))
    y = (la - clat) * k
    ok = np.isfinite(v) & np.isfinite(x) & np.isfinite(y) & (np.abs(x) < HALF_KM + 120) & (np.abs(y) < HALF_KM + 120) & (v > 50) & (v < 340)
    x, y, v, ze = x[ok], y[ok], v[ok], ze[ok]
    if len(v) < 50:
        return glat, glon, None, int(len(v))
    # 每個足跡的取樣間距（跨軌隨掃描角變大）→ σ
    zr = np.radians(np.abs(ze))
    R, H = 6371.0, 824.0
    eta = np.arcsin(R / (R + H) * np.sin(zr))
    slant = (R + H) * np.cos(eta) - np.sqrt(np.maximum(0, R ** 2 - (R + H) ** 2 * np.sin(eta) ** 2))
    sx = slant * math.radians(SAMPLE_DEG) / np.maximum(0.2, np.cos(zr))
    sy = np.maximum(ALONG_KM, slant * math.radians(SAMPLE_DEG))
    sig = 0.5 * np.sqrt(sx * sy)
    GX, GY = np.meshgrid((((glon - clon + 180) % 360) - 180) * k, (glat - clat) * k)
    GX = GX * np.cos(np.radians(np.meshgrid(glon, glat)[1]))
    gx, gy = GX.ravel(), GY.ravel()
    out = np.full(gx.size, np.nan, 'f4')
    # 粗網格分桶，只比對附近足跡
    cell = 60.0
    bx, by = np.floor(x / cell).astype(int), np.floor(y / cell).astype(int)
    buckets = {}
    for i, key in enumerate(zip(bx.tolist(), by.tolist())):
        buckets.setdefault(key, []).append(i)
    gbx, gby = np.floor(gx / cell).astype(int), np.floor(gy / cell).astype(int)
    order = np.lexsort((gby, gbx))
    keys = np.stack([gbx[order], gby[order]], 1)
    cut = np.flatnonzero(np.any(np.diff(keys, axis=0) != 0, axis=1)) + 1
    for grp in np.split(order, cut):
        cx, cy = gbx[grp[0]], gby[grp[0]]
        cand = [j for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j in buckets.get((cx + dx, cy + dy), ())]
        if not cand:
            continue
        cand = np.array(cand)
        d2 = (gx[grp, None] - x[None, cand]) ** 2 + (gy[grp, None] - y[None, cand]) ** 2
        s2 = sig[None, cand] ** 2
        w = np.exp(-0.5 * d2 / s2) * (d2 < 9 * s2)
        ws = w.sum(1)
        near = (d2 < 4 * s2).any(1)
        val = (w * v[None, cand]).sum(1) / np.where(ws > 0, ws, 1)
        out[grp] = np.where(near & (ws > 1e-6), val, np.nan)
    return glat, glon, out.reshape(GY.shape), int(len(v))


def render(prod, info, glat, glon, grid, out_path, root):
    import numpy as np
    import himawari_pipeline as hp
    hp.setup_font(os.path.join(root, 'fonts'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Ellipse
    p = PRODUCTS[prod]
    W, H = 1000, 1060
    fig = plt.figure(figsize=(W / 100, H / 100), dpi=100, facecolor='#101418')
    t = info['tc']
    tw = t + timedelta(hours=8)
    fig.text(0.015, 0.975, '%s　%s %s' % (p['title'], info['label'], info['name']), color='white', fontsize=17, fontweight='bold', va='center')
    fig.text(0.985, 0.975, '%s ATMS ch%d %s（%s）' % (info['satName'], p['ch'], p['freq'], p['pol']), color='#dddddd', fontsize=12, ha='right', va='center')
    fig.text(0.015, 0.947, '過境 %s UTC（台灣 %s）' % (t.strftime('%Y-%m-%d %H:%M'), tw.strftime('%m/%d %H:%M')), color='#ffd54f', fontsize=12.5, va='center')
    fig.text(0.985, 0.947, '颱風位置足跡約 %d×%d km（沿軌×跨軌；正下方 %d km）' % (info['fp'][0], info['fp'][1], round(footprint(0, p['beam'])[0])),
             color='#dddddd', fontsize=11, ha='right', va='center')
    ax = fig.add_axes([0.075, 0.175, 0.90, 0.75], facecolor='#7a7a7a')
    cmap = make_cmap(p)
    clat, clon = info['clat'], info['clon']
    ext = [glon[0], glon[-1], glat[0], glat[-1]]
    im = ax.imshow(np.ma.masked_invalid(grid), origin='lower', extent=ext, cmap=cmap, vmin=p['rng'][0], vmax=p['rng'][1],
                   interpolation='nearest', aspect=1 / max(0.3, math.cos(math.radians(clat))))
    try:
        with open(os.path.join(root, 'assets', 'coast_wpac.json'), encoding='utf-8') as f:
            for ln in json.load(f)['lines']:
                a = np.array(ln)
                if a[:, 0].max() < ext[0] - 1 or a[:, 0].min() > ext[1] + 1 or a[:, 1].max() < ext[2] - 1 or a[:, 1].min() > ext[3] + 1:
                    continue
                ax.plot(a[:, 0], a[:, 1], color='black', lw=2.2, solid_capstyle='round')
                ax.plot(a[:, 0], a[:, 1], color='white', lw=1.0, solid_capstyle='round')
    except Exception as e:
        print('（略過海岸線：%s）' % str(e)[:60])
    ax.set_xlim(ext[0], ext[1])
    ax.set_ylim(ext[2], ext[3])
    # 經緯線（每 2°）
    for v in np.arange(math.ceil(ext[0] / 2) * 2, ext[1], 2):
        ax.axvline(v, color='white', lw=0.5, alpha=0.55, ls=(0, (4, 4)))
    for v in np.arange(math.ceil(ext[2] / 2) * 2, ext[3], 2):
        ax.axhline(v, color='white', lw=0.5, alpha=0.55, ls=(0, (4, 4)))
    ax.set_xticks(np.arange(math.ceil(ext[0] / 2) * 2, ext[1], 2))
    ax.set_yticks(np.arange(math.ceil(ext[2] / 2) * 2, ext[3], 2))
    ax.set_xticklabels(['%g°E' % v for v in ax.get_xticks()], color='#dddddd', fontsize=9)
    ax.set_yticklabels(['%g°N' % v for v in ax.get_yticks()], color='#dddddd', fontsize=9)
    ax.tick_params(colors='#aaaaaa', length=3)
    # 定位中心
    ax.plot(clon, clat, marker='+', ms=22, mew=4.2, color='black')
    ax.plot(clon, clat, marker='+', ms=20, mew=2.0, color='#00ff66')
    # 足跡大小示意（左下角）
    kx = 1 / (111.195 * max(0.3, math.cos(math.radians(clat))))
    ex, ey = ext[0] + 0.9, ext[2] + 0.8
    ax.add_patch(Ellipse((ex, ey), info['fp'][1] * kx, info['fp'][0] / 111.195, fc='none', ec='white', lw=1.6))
    ax.text(ex + 0.6, ey, '← 足跡大小（%d×%d km）' % (info['fp'][0], info['fp'][1]), color='white', fontsize=9.5, va='center',
            bbox=dict(fc='black', alpha=0.45, ec='none', pad=2))
    ax.text(0.99, 0.012, '＋定位中心（外推到過境時間）　灰色＝掃描帶外沒有資料', transform=ax.transAxes, ha='right', va='bottom',
            color='white', fontsize=9.5, bbox=dict(fc='black', alpha=0.45, ec='none', pad=2))
    for s in ax.spines.values():
        s.set_color('#888888')
    cax = fig.add_axes([0.075, 0.108, 0.90, 0.020])
    cb = fig.colorbar(im, cax=cax, orientation='horizontal', ticks=p['ticks'])
    cb.ax.tick_params(labelsize=9, colors='#dddddd')
    cb.outline.set_edgecolor('#888888')
    cb.set_label('亮度溫度 TB（K）　%s' % READ[prod], color='#dddddd', fontsize=8.6)
    fig.text(0.015, 0.045, '資料：%s ATMS SDR（NOAA NODD AWS 公開資料）｜色階：%s｜0.05° 格點、高斯權重（σ≈半個取樣間距），不增加細節' % (info['satName'], p['cmap']),
             color='#aaaaaa', fontsize=8.3)
    fig.text(0.015, 0.022, 'ATMS 足跡 %d–%d km，比 GMI／AMSR2 粗、圖較模糊｜參考：Hawkins et al. 2001 BAMS；Lee et al. 2002 Earth Interact.｜自行繪製，僅供參考'
             % (round(footprint(0, p['beam'])[0]), max(info['fp'])), color='#aaaaaa', fontsize=8.3)
    try:
        logo = hp.load_logo()
        if logo is not None:
            hh = 40
            ww = hh * logo.shape[1] / logo.shape[0]
            lax = fig.add_axes([(W - 8 - ww) / W, 4 / H, ww / W, hh / H])
            lax.imshow(logo, alpha=0.95)
            lax.set_axis_off()
    except Exception:
        pass
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fig.savefig(out_path, dpi=100, facecolor=fig.get_facecolor())
    plt.close(fig)
    return os.path.getsize(out_path)


def make(storm, sat_name, tc, clat, clon, lat, lon, zen, bt, out, pid, root):
    """一次過境 → atms/mw/<pid>_89.png、_165.png。回傳 latest.json 用的摘要（沒有涵蓋中心就回傳 None, 原因）"""
    import numpy as np
    # 定位中心最近的足跡 → 掃描位置、天頂角
    dl = ((lon - clon + 180) % 360) - 180
    d = np.hypot(dl * 111.195 * math.cos(math.radians(clat)), (lat - clat) * 111.195)
    d = np.where(np.isfinite(d), d, 1e9)
    i = np.unravel_index(np.argmin(d), d.shape)
    if d[i] > 60:
        return None, '定位中心不在掃描帶內（最近足跡 %d km）' % d[i]
    fov = int(i[1])
    z = float(zen[i])
    # 中心附近有效範圍（判斷是不是在掃描帶邊緣被切掉）
    res = dict(id=pid, atcf=storm['atcf'], label=storm['label'], name=storm.get('name') or '', sat=pid.split('_')[1], satName=sat_name,
               t=tc.strftime('%Y-%m-%dT%H:%M:%SZ'), center=[round(clat, 2), round(clon, 2)], fov=fov + 1, zenith=round(z, 1), products={})
    for prod, p in PRODUCTS.items():
        tb = bt[:, :, p['ch'] - 1]
        glat, glon, grid, n = remap(lat, lon, zen, tb, clat, clon)
        if grid is None:
            continue
        cov = float(np.isfinite(grid).mean())
        fp = [int(round(x)) for x in footprint(z, p['beam'])]
        # 中心 100 km 內最低亮溫（看眼牆／對流強度用）
        GLON, GLAT = np.meshgrid(glon, glat)
        near = np.hypot((GLON - clon) * 111.195 * math.cos(math.radians(clat)), (GLAT - clat) * 111.195) < 150
        mn = np.nanmin(np.where(near, grid, np.nan)) if np.isfinite(np.where(near, grid, np.nan)).any() else None
        info = dict(tc=tc, label=storm['label'], name=storm.get('name') or '', satName=sat_name, clat=clat, clon=clon, fp=fp)
        rel = 'atms/mw/%s_%s.png' % (pid, prod)
        render(prod, info, glat, glon, grid, os.path.join(out, rel), root)
        res['products'][prod] = dict(src=rel, ch=p['ch'], freq=p['freq'], pol=p['pol'], footprintKm=fp,
                                     nadirKm=int(round(footprint(0, p['beam'])[0])), coverage=round(cov, 2),
                                     tbMin150km=(round(float(mn), 1) if mn is not None else None), nFov=n)
    if not res['products']:
        return None, '中心附近有效足跡太少'
    return res, ''
