#!/usr/bin/env python3
"""部署後核對：確認 GitHub Pages 原站真的換成「這次」的內容。

為什麼不能直接讀 deploy-check.json：GitHub Pages 的 CDN（Fastly）會忽略 ?查詢參數，
而且快取 10 分鐘，讀到的可能是舊快取。所以打包時另外放一組「這次專用、從沒被請求過」的
探針檔 deploy-probe/<runId>-<attempt>-<k>.json；每輪換一個 k，保證每次都直接問到原站。
探針存在且 runId 相符＝原站已換成這次的部署。只用標準函式庫。
"""
import argparse, json, sys, time, urllib.request, urllib.error


def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'himawari-deploy-verify', 'Cache-Control': 'no-cache'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read(), r.headers.get('last-modified')
    except urllib.error.HTTPError as e:
        return e.code, b'', None
    except Exception as e:  # 網路暫時錯誤：當作這輪沒過
        return None, str(e).encode(), None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', required=True, help='網站網址（結尾有沒有 / 都可以）')
    ap.add_argument('--run-id', required=True)
    ap.add_argument('--attempt', required=True)
    ap.add_argument('--start', type=int, default=0, help='這一輪從第幾個探針開始用')
    ap.add_argument('--count', type=int, default=24, help='最多用幾個探針（每個只問一次）')
    ap.add_argument('--interval', type=float, default=10.0)
    ap.add_argument('--first-wait', type=float, default=5.0)
    a = ap.parse_args()
    base = a.base.rstrip('/') + '/'
    tag = f'{a.run_id}-{a.attempt}'
    time.sleep(a.first_wait)
    t0 = time.time()
    for k in range(a.start, a.start + a.count):
        url = f'{base}deploy-probe/{tag}-{k}.json'
        code, body, lm = fetch(url)
        if code == 200:
            try:
                d = json.loads(body.decode('utf-8'))
            except Exception:
                d = {}
            if str(d.get('runId')) == str(a.run_id) and str(d.get('runAttempt')) == str(a.attempt):
                print(f'✅ 線上已是這次的部署（探針 {k}，等了 {time.time() - t0:.0f} 秒；scanId {d.get("scanId")}；last-modified {lm}）')
                return 0
            print(f'⚠️ 探針 {k} 內容不符：{body[:200]!r}')
        else:
            print(f'… 探針 {k}：HTTP {code}（原站還沒換成這次的部署）')
        if k < a.start + a.count - 1:
            time.sleep(a.interval)
    print(f'::warning::GitHub Pages 回報成功，但 {time.time() - t0:.0f} 秒內原站仍未提供這次（run {tag}）的內容')
    return 1


if __name__ == '__main__':
    sys.exit(main())
