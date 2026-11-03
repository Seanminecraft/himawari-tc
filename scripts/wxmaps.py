#!/usr/bin/env python3
"""天氣圖：抓 ECMWF Open Data 與 NOAA GFS（免金鑰），畫 7 日預報天氣圖，輸出到 --out（含 manifest.json）。"""
import os, sys, json, time, glob, argparse, datetime as dt
import numpy as np, requests, eccodes
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt, cartopy.crs as ccrs, cartopy.feature as cf
from matplotlib import font_manager as fm
from matplotlib.colors import ListedColormap, BoundaryNorm
from multiprocessing import Pool
from PIL import Image

STEPS = list(range(0, 169, 24))
UTC = dt.timezone.utc; TW = dt.timezone(dt.timedelta(hours=8))
DOM = {"tw": ("台灣周邊", [118, 126, 20, 27]), "ea": ("東亞", [100, 150, 15, 50]), "wnp": ("西北太平洋", [100, 180, 0, 50])}
LAY = {"sfc": "地面：海平面氣壓＋2m 氣溫＋24h 累積雨量", "t850": "850 hPa 溫度＋風", "rh700": "700 hPa 相對濕度",
       "z500": "500 hPa 高度＋相對渦度", "jet200": "200 hPa 風速（噴流）＋流線"}
MN = {"ecmwf": "ECMWF IFS HRES", "gfs": "NOAA GFS"}
SRC = {"ecmwf": "資料來源：ECMWF Open Data (IFS HRES 0.25°)，CC-BY-4.0 © ECMWF",
       "gfs": "資料來源：NOAA NCEP GFS 0.25°"}
DISC = "數值模式原始輸出，僅供參考，非官方預報；請以中央氣象署發布為準"
# 降雨：不透明離散色階（不與氣溫色混色）
PR_LV = [1, 5, 10, 25, 50, 100, 200, 400]
PR_CM = ListedColormap(["#a6f28f", "#3dba3d", "#1f8f1f", "#3f9cff", "#0a3fd6", "#b53ad6", "#ff2fae"])
PR_CM.set_over("#ffb3e6")
PR_NORM = BoundaryNorm(PR_LV, PR_CM.N)

def font():
    for p in glob.glob("/usr/share/fonts/**/NotoSansCJK-Regular.ttc", recursive=True):
        fm.fontManager.addfont(p)
    plt.rcParams["font.family"] = ["Noto Sans CJK TC", "Noto Sans CJK JP", "sans-serif"]
    plt.rcParams["axes.unicode_minus"] = False

def ok(url):
    try: return requests.head(url, timeout=30, allow_redirects=True).status_code == 200
    except Exception: return False

def runs_back(n=12):
    now = dt.datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    t = now.replace(hour=now.hour // 6 * 6)
    out = [t - dt.timedelta(hours=6 * i) for i in range(n)]
    zero = [r for r in out if r.hour == 0]
    return zero[:2] + [r for r in out if r not in zero[:2]]  # 先試最新兩個 00Z，再退回其他場次

def pick_ecmwf():
    for r in runs_back():
        if r.hour in (6, 18): continue   # 06/18Z 只到 +90h
        u = f"https://data.ecmwf.int/forecasts/{r:%Y%m%d}/{r:%H}z/ifs/0p25/oper/{r:%Y%m%d%H}0000-168h-oper-fc.index"
        if ok(u): return r
    raise SystemExit("ECMWF: no complete run found")

def pick_gfs():
    for r in runs_back():
        u = f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/gfs.{r:%Y%m%d}/{r:%H}/atmos/gfs.t{r:%H}z.pgrb2.0p25.f168.idx"
        if ok(u): return r
    raise SystemExit("GFS: no complete run found")

def fetch_ecmwf(r, D):
    from ecmwf.opendata import Client
    c = Client(source="ecmwf", model="ifs", resol="0p25")
    kw = dict(date=r.strftime("%Y%m%d"), time=r.hour, type="fc", step=STEPS)
    reqs = [("sfc", dict(param=["msl", "2t", "tp"])),
            ("p850", dict(param=["t", "u", "v"], levelist=[850])),
            ("p700", dict(param=["r"], levelist=[700])),
            ("p500", dict(param=["gh", "vo"], levelist=[500])),
            ("p200", dict(param=["u", "v"], levelist=[200]))]
    files = []
    for name, q in reqs:
        fn = f"{D}/ec_{name}.grib2"; c.retrieve(target=fn, **kw, **q); files.append(fn)
    return files

def fetch_gfs(r, D):
    files = []
    for s in STEPS:
        url = ("https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl?"
               f"dir=%2Fgfs.{r:%Y%m%d}%2F{r:%H}%2Fatmos&file=gfs.t{r:%H}z.pgrb2.0p25.f{s:03d}"
               "&var_PRMSL=on&var_TMP=on&var_APCP=on&var_UGRD=on&var_VGRD=on&var_RH=on&var_HGT=on&var_ABSV=on"
               "&lev_mean_sea_level=on&lev_2_m_above_ground=on&lev_surface=on&lev_850_mb=on&lev_700_mb=on&lev_500_mb=on&lev_200_mb=on"
               "&subregion=&toplat=60&leftlon=90&rightlon=180&bottomlat=0")
        for k in range(5):
            try:
                x = requests.get(url, timeout=180)
                if x.ok and x.content[:4] == b"GRIB": break
            except Exception: pass
            time.sleep(10 * (k + 1))
        else:
            print("GFS step failed", s); continue
        fn = f"{D}/gfs_f{s:03d}.grib2"; open(fn, "wb").write(x.content); files.append(fn); time.sleep(1)
    return files

def read(files, want):
    out = {}; meta = {}
    for fn in files:
        with open(fn, "rb") as f:
            while (h := eccodes.codes_grib_new_from_file(f)) is not None:
                sn = eccodes.codes_get(h, "shortName"); st = eccodes.codes_get(h, "endStep")
                if sn == "tp" and eccodes.codes_get(h, "startStep") != 0: eccodes.codes_release(h); continue
                if sn in want:
                    ni, nj = eccodes.codes_get(h, "Ni"), eccodes.codes_get(h, "Nj")
                    if "lat" not in meta:
                        la = eccodes.codes_get_array(h, "latitudes").reshape(nj, ni); lo = eccodes.codes_get_array(h, "longitudes").reshape(nj, ni)
                        meta.update(lat=la[:, 0], lon=np.where(lo[0] > 180, lo[0] - 360, lo[0]))
                    lv = 0 if sn in ("msl", "prmsl", "2t", "tp") else eccodes.codes_get(h, "level")
                    out[(sn, lv, st)] = eccodes.codes_get_values(h).reshape(nj, ni)
                eccodes.codes_release(h)
    return out, meta

def load(model, files):
    if model == "ecmwf":
        d, m = read(files, {"msl", "2t", "tp", "t", "u", "v", "r", "gh", "vo"})
        for k in [k for k in d if k[0] == "tp"]: d[k] = d[k] * 1000.0
    else:
        d, m = read(files, {"prmsl", "2t", "tp", "t", "u", "v", "r", "gh", "absv"})
        for k in list(d):
            if k[0] == "prmsl": d[("msl", 0, k[2])] = d.pop(k)
            elif k[0] == "absv":
                f = 2 * 7.292e-5 * np.sin(np.deg2rad(m["lat"]))[:, None]; d[("vo", k[1], k[2])] = d.pop(k) - f
    la, lo = m["lat"], m["lon"]; jy = (la >= -1) & (la <= 61); ix = (lo >= 89) & (lo <= 180.1)
    d = {k: v[np.ix_(jy, ix)] for k, v in d.items()}; m["lat"], m["lon"] = la[jy], lo[ix]
    return d, m

G = {}
def plot(job):
    model, dk, lk, s, out = job
    d, m, init = G[model]
    lat, lon = m["lat"], m["lon"]; LO, LA = np.meshgrid(lon, lat); name, ext = DOM[dk]
    valid = init + dt.timedelta(hours=s); pc = ccrs.PlateCarree()
    fig = plt.figure(figsize=(8, 6.6) if dk != "wnp" else (10, 6.4))
    ax = fig.add_subplot(1, 1, 1, projection=pc); ax.set_extent(ext, pc)
    sk = 1 if dk == "tw" else (6 if dk == "ea" else 10)
    g = lambda sn, lv, st=s: d.get((sn, lv, st))
    try:
        if lk == "sfc":
            T, P, tp = g("2t", 0), g("msl", 0), g("tp", 0)
            cs = ax.contourf(LO, LA, T - 273.15, levels=np.arange(-30, 37, 3), cmap="RdYlBu_r", transform=pc, extend="both")
            fig.colorbar(cs, ax=ax, shrink=.75, pad=.02, label="2m 氣溫 (°C)")
            if s >= 24 and tp is not None:
                tp0 = g("tp", 0, s - 24) if s > 24 else 0
                pr = np.clip(tp - tp0, 0, None)
                ps = ax.contourf(LO, LA, pr, levels=PR_LV, cmap=PR_CM, norm=PR_NORM, extend="max", transform=pc)
                cb = fig.colorbar(ps, ax=ax, orientation="horizontal", shrink=.7, pad=.06, aspect=40)
                cb.set_label("過去 24 小時累積雨量 (mm)")
            c = ax.contour(LO, LA, P / 100, levels=np.arange(940, 1061, 2 if dk == "tw" else 4), colors="k", linewidths=.8, transform=pc)
            ax.clabel(c, fmt="%d", fontsize=7)
        elif lk == "t850":
            T, U, V = g("t", 850), g("u", 850), g("v", 850)
            cs = ax.contourf(LO, LA, T - 273.15, levels=np.arange(-36, 31, 2), cmap="turbo", transform=pc, extend="both")
            fig.colorbar(cs, ax=ax, shrink=.75, pad=.02, label="850 hPa 溫度 (°C)")
            ax.barbs(LO[::sk, ::sk], LA[::sk, ::sk], U[::sk, ::sk] * 1.944, V[::sk, ::sk] * 1.944, length=4.5, linewidth=.4, transform=pc)
        elif lk == "rh700":
            cs = ax.contourf(LO, LA, g("r", 700), levels=[0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100], cmap="BrBG", transform=pc)
            fig.colorbar(cs, ax=ax, shrink=.75, pad=.02, label="700 hPa 相對濕度 (%)")
        elif lk == "z500":
            Z, VO = g("gh", 500), g("vo", 500)
            cs = ax.contourf(LO, LA, VO * 1e5, levels=np.arange(-20, 21, 2), cmap="PuOr_r", transform=pc, extend="both")
            fig.colorbar(cs, ax=ax, shrink=.75, pad=.02, label="500 hPa 相對渦度 (10⁻⁵ s⁻¹)")
            c = ax.contour(LO, LA, Z, levels=np.arange(4800, 6000, 30 if dk == "tw" else 60), colors="k", linewidths=.9, transform=pc)
            ax.clabel(c, fmt="%d", fontsize=7)
        elif lk == "jet200":
            U, V = g("u", 200), g("v", 200); W = np.hypot(U, V) * 1.944
            cs = ax.contourf(LO, LA, W, levels=np.arange(50, 201, 10), cmap="YlOrRd", transform=pc, extend="max")
            fig.colorbar(cs, ax=ax, shrink=.75, pad=.02, label="200 hPa 風速 (kt)")
            ax.streamplot(lon, lat, U, V, density=1.4 if dk != "tw" else 1, color="0.3", linewidth=.5, transform=pc)
    except TypeError:
        ax.text(.5, .5, "此時間資料暫缺", transform=ax.transAxes, ha="center", fontsize=18, color="r")
    ax.coastlines("50m" if dk != "wnp" else "110m", linewidth=.7); ax.add_feature(cf.BORDERS, linewidth=.3)
    gl = ax.gridlines(draw_labels=True, linewidth=.3, color="gray", alpha=.5); gl.top_labels = gl.right_labels = False
    ax.plot(121.0, 23.7, "r*", ms=8 if dk != "wnp" else 6, transform=pc)
    ax.text(121.6, 23.2 if dk == "tw" else 22.2, "台灣", color="r", fontsize=10, fontweight="bold", transform=pc)
    ax.set_title(f"{MN[model]}｜{name}｜{LAY[lk]}\n初始 {init:%Y-%m-%d %HZ} UTC（台灣時間 {init.astimezone(TW):%m/%d %H:%M}）"
                 f"｜+{s}h｜有效 台灣時間 {valid.astimezone(TW):%Y-%m-%d %H:%M}", fontsize=9, loc="left")
    fig.text(.01, .005, SRC[model] + "｜" + DISC, fontsize=6.5, color="0.3")
    fn = f"{out}/{model}_{dk}_{lk}_{s:03d}.png"
    fig.savefig(fn, dpi=85, bbox_inches="tight"); plt.close(fig)
    im = Image.open(fn).convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT); im.save(fn, optimize=True)
    return fn

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--out", default="wxmaps"); ap.add_argument("--workers", type=int, default=os.cpu_count())
    ap.add_argument("--cache", default="wx_cache"); a = ap.parse_args()
    font(); os.makedirs(a.out, exist_ok=True); os.makedirs(a.cache, exist_ok=True)
    for f in glob.glob(f"{a.out}/*.png"): os.remove(f)
    man = {"generated": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ"), "steps": STEPS,
           "domains": list(DOM), "layers": list(LAY), "models": {}}
    for model, pick, fetch in (("ecmwf", pick_ecmwf, fetch_ecmwf), ("gfs", pick_gfs, fetch_gfs)):
        t = time.time()
        try:
            r = pick(); files = fetch(r, a.cache)
            nbytes = sum(os.path.getsize(f) for f in files)
            d, m = load(model, files); G[model] = (d, m, r)
            have = sorted({k[2] for k in d})
            man["models"][model] = {"init": r.strftime("%Y-%m-%dT%HZ"), "steps": [s for s in STEPS if s in have]}
            print(f"{model}: run {r:%Y-%m-%d %HZ}, {nbytes/1e6:.1f} MB, {time.time()-t:.0f}s")
        except SystemExit as e: print(e)
        except Exception as e: print(model, "failed:", e)
    jobs = [(mo, dk, lk, s, a.out) for mo in man["models"] for dk in DOM for lk in LAY for s in man["models"][mo]["steps"]]
    t = time.time()
    with Pool(a.workers) as p: p.map(plot, jobs, chunksize=4)
    print(f"plotted {len(jobs)} maps in {time.time()-t:.0f}s")
    json.dump(man, open(f"{a.out}/manifest.json", "w"), ensure_ascii=False, indent=1)
    if not man["models"]: sys.exit(1)

if __name__ == "__main__": main()
