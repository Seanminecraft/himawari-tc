#!/usr/bin/env python3
"""v17：向日葵沒有新掃描、只有雷達要更新時，先把「目前線上網站」的衛星圖原樣抓回 site/，
再由 cwa_radar.py 加上新的雷達圖層後整包部署（GitHub Pages 每次部署都是整包取代，不抓回就會不見）。
只用標準函式庫。任何一個檔案抓不到就失敗（exit 1）→ 這次不部署，避免把線上衛星圖弄壞。"""
import json
import os
import shutil
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = {'User-Agent': 'doujin-weather-himawari/1.0 (+GitHub Actions; mirror)'}
HERE = os.path.dirname(os.path.abspath(__file__))


def get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def main():
    out = sys.argv[sys.argv.index('--out') + 1] if '--out' in sys.argv else 'site'
    base = (os.environ.get('PAGES_BASE') or '').rstrip('/')
    if not base:
        print('沒有 Pages 網址'); return 1
    latest = json.loads(get(base + '/latest.json?t=%d' % time.time()).decode('utf-8'))
    srcs = set()
    for reg in latest.get('regions') or []:
        for pv in (reg.get('products') or {}).values():
            if pv.get('src'):
                srcs.add(pv['src'])
            for fr in pv.get('frames') or []:
                srcs.add(fr['src'])
    comp = latest.get('composite') or {}
    if comp.get('src'):
        srcs.add(comp['src'])
    for fr in comp.get('frames') or []:
        srcs.add(fr['src'])
    for k in ('truecolor', 'wv'):   # v17：首頁地圖真實色／水氣圖層
        v = (latest.get('maplayers') or {}).get(k) or {}
        if v.get('src'):
            srcs.add(v['src'])
        v = ((latest.get('maplayers') or {}).get('fulldisk') or {}).get(k) or {}   # v21.1：全圓盤圖層
        if v.get('src'):
            srcs.add(v['src'])
    srcs = sorted(s for s in srcs if s and '..' not in s and not s.startswith('/'))
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out)

    def one(rel):
        fn = os.path.join(out, rel)
        os.makedirs(os.path.dirname(fn), exist_ok=True)
        with open(fn, 'wb') as f:
            f.write(get(base + '/' + rel))
        return rel
    with ThreadPoolExecutor(12) as ex:
        done = list(ex.map(one, srcs))        # 任何一個失敗會丟例外 → exit 1
    sd = os.path.join(HERE, '..', 'site_static')
    for fn in os.listdir(sd) if os.path.isdir(sd) else []:
        shutil.copyfile(os.path.join(sd, fn), os.path.join(out, fn))
    with open(os.path.join(out, 'latest.json'), 'w', encoding='utf-8') as f:
        json.dump(latest, f, ensure_ascii=False, indent=1)
    print('已抓回線上衛星圖 %d 個檔案（掃描 %s）' % (len(done), latest.get('scanId')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
