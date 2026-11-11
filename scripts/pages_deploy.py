#!/usr/bin/env python3
"""用「這次專用」的版本字串部署 GitHub Pages（取代 actions/deploy-pages 寫死 commit SHA 的做法）。

actions/deploy-pages 一律把 GITHUB_SHA 當 pages_build_version（也就是 Pages 部署 ID），而且
runner 不讓工作流程覆寫 GITHUB_SHA。這個 repo 很少 commit，幾百次部署共用同一個 ID，
Pages 會不定期把新部署當重複、繼續提供舊內容（actions/deploy-pages#383）。
這裡直接呼叫同一支官方 API（POST /repos/{repo}/pages/deployments），只是版本字串每次不同。
需要：job 權限 pages: write、id-token: write；環境變數 GITHUB_TOKEN。只用標準函式庫，不印出任何權杖。
"""
import argparse, json, os, sys, time, urllib.request, urllib.error

API = os.environ.get('GITHUB_API_URL', 'https://api.github.com')
DONE_BAD = {'deployment_failed', 'deployment_content_failed', 'deployment_cancelled', 'deployment_lost', 'deployment_attempt_error'}


def call(method, url, token, body=None, auth='Bearer'):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Authorization': f'{auth} {token}', 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'himawari-pages-deploy',
        **({'Content-Type': 'application/json'} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b'{}').get('message')
        except Exception:
            msg = None
        return e.code, {'message': msg}


def oidc_token():
    url, tok = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL'), os.environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN')
    if not url or not tok:
        raise SystemExit('::error::拿不到 OIDC 權杖（job 需要 permissions: id-token: write）')
    code, d = call('GET', url, tok)
    if code != 200 or not d.get('value'):
        raise SystemExit(f'::error::OIDC 權杖請求失敗 HTTP {code}')
    return d['value']


def out(k, v):
    p = os.environ.get('GITHUB_OUTPUT')
    if p:
        with open(p, 'a', encoding='utf-8') as f:
            f.write(f'{k}={v}\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--artifact-id', required=True)
    ap.add_argument('--version', required=True, help='唯一版本字串，例如 <sha>-r<run_id>-a<attempt>')
    ap.add_argument('--timeout', type=int, default=480)
    a = ap.parse_args()
    if not a.artifact_id.strip().isdigit():
        raise SystemExit(f'::error::artifact id 不正確：{a.artifact_id!r}')
    repo, gh = os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_TOKEN']
    payload = {'artifact_id': int(a.artifact_id), 'pages_build_version': a.version, 'oidc_token': oidc_token()}
    print(f'建立 Pages 部署：artifact_id={payload["artifact_id"]}，pages_build_version={a.version}')
    code, d = call('POST', f'{API}/repos/{repo}/pages/deployments', gh, payload)
    if code not in (200, 201):
        raise SystemExit(f'::error::建立 Pages 部署失敗 HTTP {code}：{d.get("message")}')
    dep_id = str(d.get('id') or a.version)
    out('page_url', d.get('page_url', ''))
    print(f'已建立部署 ID {dep_id}；page_url {d.get("page_url")}')
    t0, last, errs = time.time(), None, 0
    while time.time() - t0 < a.timeout:
        time.sleep(5)
        code, s = call('GET', f'{API}/repos/{repo}/pages/deployments/{dep_id}', gh)
        if code != 200:
            errs += 1
            print(f'… 讀取狀態 HTTP {code}（第 {errs} 次）')
            if errs >= 10:
                raise SystemExit('::error::連續讀不到部署狀態')
            continue
        st = s.get('status')
        if st != last:
            print(f'… {time.time() - t0:.0f}s 狀態：{st}')
            last = st
        if st == 'succeed':
            print('✅ Pages 回報部署完成')
            return 0
        if st in DONE_BAD:
            raise SystemExit(f'::error::Pages 部署失敗：{st}')
    call('POST', f'{API}/repos/{repo}/pages/deployments/{dep_id}/cancel', gh)
    raise SystemExit(f'::error::Pages 部署逾時（最後狀態 {last}），已取消')


if __name__ == '__main__':
    sys.exit(main())
