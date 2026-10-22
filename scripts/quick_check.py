#!/usr/bin/env python3
"""快速檢查（只用 Python 標準函式庫，不必先 pip install）：
線上 GitHub Pages 的 latest.json 已經是 AWS 上最新的向日葵 9 號掃描 → 輸出 skip=true，後面的安裝／繪圖／部署全部略過。
只要有任何疑問（讀不到線上網站、讀不到 S3、手動強制重畫…）一律輸出 skip=false，交給完整流程判斷。
Apps Script 每 10 分鐘 workflow_dispatch＋GitHub 排程備援同時存在時，多出來的執行大多在這裡 10 秒內結束。

v17：另外輸出 radar_skip——氣象署雷達／1 小時雨量預報（cwaopendata S3 公開檔）是否和線上 radar/latest.json 相同。
向日葵沒有新掃描、但雷達有新資料時，skip=true、radar_skip=false → 工作流程只更新雷達圖層（不會被擋住）。

v19：另外輸出 atms_skip——ATMS 微波暖心（atms_warmcore.py）需不需要重新分析：
線上 atms/latest.json 的 plan 裡有「已經過境、而且 NOAA S3 上已經有那個時間的 granule」→ atms_skip=false（10–20 分鐘內就會更新）；
颱風清單變了、或上次分析超過 3 小時（重算下一次過境）也會 false。其餘 true（工作流程只把上一版原樣放回網站）。"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

BUCKET = 'https://noaa-himawari9.s3.amazonaws.com'
S3_PREFIX = 'AHI-L1b-FLDK'
UA = {'User-Agent': 'doujin-weather-himawari/1.0 (+GitHub Actions; quick check)'}


def get(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'replace')


def newest_slot(now):
    """AWS 上最新的掃描時段（YYYYMMDDHHMM，有任何 B13 檔案就算；未上傳完也算 → 交給完整流程處理）"""
    for back in (0, 1):
        d = now - timedelta(days=back)
        x = get('%s/?list-type=2&prefix=%s/%s/&delimiter=/' % (BUCKET, S3_PREFIX, d.strftime('%Y/%m/%d')))
        slots = sorted({''.join(m) for m in re.findall(r'<Prefix>%s/(\d{4})/(\d{2})/(\d{2})/(\d{4})/</Prefix>' % S3_PREFIX, x)}, reverse=True)
        for sid in slots[:3]:
            y = get('%s/?list-type=2&max-keys=200&prefix=%s/%s/%s/%s/%s/' % (BUCKET, S3_PREFIX, sid[:4], sid[4:6], sid[6:8], sid[8:12]))
            if '_B13_' in y:
                return sid
    return None


CWA_S3 = 'https://cwaopendata.s3.ap-northeast-1.amazonaws.com'
CWA_KEYS = {'radar': 'Observation/O-A0059-001.json', 'qpf': 'Forecast/F-B0046-001.json'}


def head_modified(url, timeout=15):
    req = urllib.request.Request(url, headers=UA, method='HEAD')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.headers.get('Last-Modified', '')


def radar_up_to_date(base):
    """(True/False, 說明)。有任何疑問 → False（交給 cwa_radar.py 畫）"""
    if not base:
        return False, '沒有 Pages 網址'
    try:
        man = json.loads(get(base + '/radar/latest.json?t=%d' % time.time()))
    except Exception as e:  # noqa
        return False, '讀不到線上 radar/latest.json（%s）' % str(e)[:60]
    for k, key in CWA_KEYS.items():
        try:
            lm = head_modified(CWA_S3 + '/' + key)
        except Exception as e:  # noqa
            return False, '讀不到氣象署 S3（%s）' % str(e)[:60]
        if not lm or ((man.get(k) or {}).get('srcModified') or '') != lm:
            return False, '%s 有新資料（%s）' % (k, lm)
    return True, '雷達／雨量預報都已是最新'


ATMS_REFRESH_H = 3


def s3_has(bucket, prefix):
    x = get('https://%s.s3.amazonaws.com/?list-type=2&max-keys=1&prefix=%s' % (bucket, prefix))
    return '<Key>' in x


def atms_up_to_date(base, now=None):
    """(True/False, 說明)。有任何疑問 → False（交給 atms_warmcore.py 判斷；它自己也會只下載需要的檔案）"""
    if not base:
        return False, '沒有 Pages 網址'
    now = now or datetime.now(timezone.utc)
    try:
        man = json.loads(get(base + '/atms/latest.json?t=%d' % time.time()))
    except Exception as e:  # noqa
        return False, '讀不到線上 atms/latest.json（%s）' % str(e)[:60]
    try:
        gen = datetime.fromisoformat(str(man.get('generated')).replace('Z', '+00:00'))
    except Exception:
        return False, 'atms/latest.json 沒有時間'
    if now - gen > timedelta(hours=ATMS_REFRESH_H):
        return False, '上次分析已超過 %d 小時（重算過境預報）' % ATMS_REFRESH_H
    try:
        live = json.loads(get(base + '/latest.json?t=%d' % time.time()))
        cur = sorted(r.get('atcf') for r in live.get('regions') or [] if r.get('type') == 'storm' and r.get('atcf'))
        had = sorted(s.get('atcf') for s in man.get('storms') or [])
        if cur != had:
            return False, '颱風清單改變（%s → %s）' % (','.join(had) or '無', ','.join(cur) or '無')
    except Exception as e:  # noqa
        return False, '讀不到線上 latest.json（%s）' % str(e)[:60]
    for p in man.get('plan') or []:
        try:
            t = datetime.fromisoformat(str(p['t']).replace('Z', '+00:00'))
        except Exception:
            continue
        if t + timedelta(minutes=5) > now:
            continue
        try:
            if s3_has(p['bucket'], p['probe']):
                return False, '有新過境資料（%s %s）' % (p.get('sat'), p['t'])
        except Exception as e:  # noqa
            return False, '讀不到 NOAA S3（%s）' % str(e)[:60]
    nxt = [p['t'] for p in man.get('plan') or [] if p.get('t')]
    return True, '沒有新過境（下一次：%s）' % (min(nxt) if nxt else '—')


def main():
    out = os.environ.get('GITHUB_OUTPUT')
    try:
        a_ok, a_why = atms_up_to_date((os.environ.get('PAGES_BASE') or '').rstrip('/'))
    except Exception as e:  # noqa
        a_ok, a_why = False, 'ATMS 檢查失敗（%s）' % str(e)[:60]
    print(('⏭️ ATMS 略過：' if a_ok else '▶️ ATMS 分析：') + a_why)
    if out:
        with open(out, 'a') as f:
            f.write('atms_skip=%s\n' % ('true' if a_ok else 'false'))
    base0 = (os.environ.get('PAGES_BASE') or '').rstrip('/')
    try:
        r_ok, r_why = radar_up_to_date(base0)
    except Exception as e:  # noqa
        r_ok, r_why = False, '雷達檢查失敗（%s）' % str(e)[:60]
    print(('⏭️ 雷達略過：' if r_ok else '▶️ 雷達更新：') + r_why)
    if out:
        with open(out, 'a') as f:
            f.write('radar_skip=%s\n' % ('true' if r_ok else 'false'))

    def say(skip, why):
        print(('⏭️ 略過：' if skip else '▶️ 繼續：') + why)
        if out:
            with open(out, 'a') as f:
                f.write('skip=%s\n' % ('true' if skip else 'false'))
        return 0

    if os.environ.get('EVENT') == 'workflow_dispatch' and (os.environ.get('IN_FORCE') == 'true' or re.fullmatch(r'\d{12}', os.environ.get('IN_SCAN') or '')):
        return say(False, '手動指定 force／scan')
    base = (os.environ.get('PAGES_BASE') or '').rstrip('/')
    if not base:
        return say(False, '沒有 Pages 網址')
    try:
        live = json.loads(get(base + '/latest.json?t=%d' % time.time()))
        live_id = str(live.get('scanId') or '')
    except Exception as e:  # noqa
        return say(False, '讀不到線上 latest.json（%s）' % str(e)[:80])
    try:
        sid = newest_slot(datetime.now(timezone.utc))
    except Exception as e:  # noqa
        return say(False, '讀不到 AWS 清單（%s）' % str(e)[:80])
    if not sid:
        return say(False, 'AWS 上找不到掃描清單')
    if re.fullmatch(r'\d{12}', live_id) and live_id >= sid:
        return say(True, '線上已是最新掃描 %s（AWS 最新 %s）' % (live_id, sid))
    return say(False, '有新掃描 %s（線上 %s）' % (sid, live_id or '無'))


if __name__ == '__main__':
    sys.exit(main())
