# himawari-tc：向日葵 9 號原始檔 → 颱風衛星圖（GitHub Actions + GitHub Pages）

每 10 分鐘自動：

1. 從 NRL TC Web 找出西北太平洋活躍颱風（失敗時改用 UCAR/NCAR b-deck），並把定位外推到衛星掃描時間；
2. 從 NOAA AWS 公開資料桶 `noaa-himawari9`（免帳號）下載最新一次全盤掃描中**只涵蓋需要範圍的分段**；
3. 用 satpy 讀 HSD 原始檔並校正，再畫出颱風中心約 1400 km 見方的浮動視窗，以及台灣/西北太平洋區域總覽：
   - 紅外線 B13 Dvorak BD 強化（WMG／OW／DG／MG／LG／B／W／CMG／CDG）
   - 紅外線 B13 彩色強化
   - 水氣 B08（上層水氣）
   - 真彩 B01/B02/B03/B04（只有白天才會產生）
4. 部署到 GitHub Pages：`latest.json` + `img/<區域>/<產品>.webp` + `frames/`（最近 2 小時動畫影格）。

圖片不會存進 git 歷史，每次都直接部署到 Pages。網站（Google Sites／Apps Script）的 🛰️ 颱風診斷分析分頁直接讀
`https://<USERNAME>.github.io/<REPO>/latest.json`。

| 檔案 | 用途 |
|---|---|
| `scripts/himawari_pipeline.py` | 主程式（下載、校正、繪圖、寫 latest.json） |
| `.github/workflows/himawari.yml` | 每 10 分鐘排程＋手動執行，部署到 Pages |
| `.github/workflows/keepalive.yml` | 每週一次用官方 API 重新啟用排程，避免 60 天沒有活動被自動停用（不產生 commit） |
| `requirements.txt` | Python 套件 |
| `assets/logo.png` | 每張圖地圖區右下角的標誌（256 px；檔案不存在就自動略過） |
| `fonts/` | 圖上中文字用的 Noto Sans TC 子集字型（SIL OFL 1.1） |
| `site_static/index.html` | Pages 首頁（直接打開網址就能看所有圖） |
| `tools/make_font_subset.py` | 改了圖上的中文字才需要重新產生字型子集 |
| `samples/` | 本機實測產生的範例圖（可不上傳） |

本機測試：

```bash
pip install -r requirements.txt
python scripts/himawari_pipeline.py --out site                         # 最新掃描
python scripts/himawari_pipeline.py --out site --scan 202610210400     # 指定時間（測白天真彩）
python scripts/himawari_pipeline.py --out site --no-storms             # 只畫區域總覽
```

資料來源：JMA Himawari-9（NOAA Open Data Dissemination，AWS `noaa-himawari9`）；颱風定位：NRL Monterey TC Web、UCAR/NCAR b-deck（JTWC best track）。
