# 🛰️ 用 GitHub 自動畫向日葵 9 號颱風衛星圖：零基礎手把手教學

> 全程只用瀏覽器操作 github.com，**不用安裝 git，也不用打指令**。
> 預計花 20～30 分鐘。做完後，GitHub 每 10 分鐘會自動畫一次圖，網站的「🛰️ 颱風診斷分析」分頁也會自動顯示這些圖。

---

## 0. 先搞懂三個名詞（30 秒）

| 名詞 | 白話解釋 |
|---|---|
| **Repository（儲存庫，簡稱 repo）** | GitHub 上的一個「專案資料夾」，放程式碼。 |
| **GitHub Actions** | GitHub 免費借你的雲端電腦，照 `.github/workflows/*.yml` 寫的排程自動跑程式。 |
| **GitHub Pages** | GitHub 免費幫你架的網站，畫好的圖就放在這裡，網址長得像 `https://你的帳號.github.io/himawari-tc/`。 |

**要錢嗎？** 只要 repo 是 **Public（公開）**，Actions 和 Pages 都**免費**，每 10 分鐘跑一次也不會收費。
（如果設成 Private，每月只有 2,000 分鐘免費額度，每 10 分鐘跑一次會超過，所以**一定要選 Public**。）
公開的意思是：程式碼和圖片任何人都看得到。這個專案沒有任何密碼或金鑰，公開沒有風險。

---

## 1. 準備檔案

1. 把 `himawari-pipeline.zip` 下載到電腦，**按右鍵 →「解壓縮全部」**。
2. 解壓後會看到一個 `himawari-pipeline` 資料夾，裡面有：
   ```
   himawari-pipeline/
   ├─ .github/workflows/himawari.yml     ← 排程設定（等一下用「新增檔案」貼上）
   ├─ .github/workflows/keepalive.yml    ← 防止 60 天被停用（等一下用「新增檔案」貼上）
   ├─ scripts/himawari_pipeline.py       ← 主程式
   ├─ fonts/                             ← 中文字型（子集）
   ├─ assets/logo.png                    ← 圖片右下角的標誌（Japari Weather Bureau）
   ├─ site_static/index.html             ← 網站首頁
   ├─ tools/make_font_subset.py
   ├─ requirements.txt
   ├─ README.md
   ├─ README_GitHub_教學_zh-TW.md         ← 就是這份
   └─ samples/                           ← 範例圖（不用上傳）
   ```
   > 💡 Windows 有時候看不到 `.github` 這種「點開頭」的資料夾，沒關係，步驟 3 會教你直接在網頁上建立。

---

## 2. 建立公開的 repo

1. 登入 <https://github.com>。
2. 右上角按 **＋** → **New repository**。
   （懶人捷徑：直接打開 <https://github.com/new?name=himawari-tc&visibility=public>，名稱和公開都幫你填好了。）
3. 填寫：
   - **Repository name**：`himawari-tc`（建議用這個；只能用英文、數字、`-`、`_`、`.`）
   - **Description**（可不填）：`向日葵 9 號颱風衛星圖自動繪製`
   - 可見度選 **Public** ← ⚠️ 一定要選 Public
   - 有「Add a README / Add README」選項的話**不要勾**（我們自己有 README）
4. 按綠色 **Create repository**。

建好後網址是 `https://github.com/你的帳號/himawari-tc`。

---

## 3. 上傳檔案

### 3-1 先上傳一般檔案和資料夾

1. 在剛建好的 repo 頁面，找到 **uploading an existing file** 這個連結並點它
   （如果 repo 已經有檔案，則是在檔案清單上方按 **Add file** → **Upload files**）。
2. 打開電腦裡的 `himawari-pipeline` 資料夾，**選取下面這些**，一起拖進瀏覽器的上傳區：
   - `scripts` 資料夾
   - `fonts` 資料夾
   - `assets` 資料夾（裡面是 `logo.png`，圖片右下角的標誌；沒上傳的話圖照樣會畫，只是沒有標誌）
   - `site_static` 資料夾
   - `tools` 資料夾
   - `requirements.txt`
   - `README.md`
   - `README_GitHub_教學_zh-TW.md`
   > `samples` 資料夾可以不用傳（只是範例圖）。
   > 拖資料夾會保留資料夾結構；GitHub 網頁一次最多 100 個檔案、每個 25 MB，這裡遠遠不到。
3. 等清單都跑完（每個檔案前面變成檔案圖示），往下拉到 **Commit changes** 區：
   - 訊息欄可以打：`上傳主程式`
   - 選 **Commit directly to the `main` branch**
   - 按綠色 **Commit changes**。
4. 回到 repo 首頁，確認看到 `scripts/`、`fonts/`、`assets/`、`site_static/`、`tools/`、`requirements.txt`、`README.md`。

### 3-2 用「新增檔案」建立 `.github/workflows/himawari.yml`

> 這一步是讓 GitHub 知道「每 10 分鐘要跑什麼」。`.github` 是點開頭的資料夾，用網頁新增最保險。

1. 在 repo 首頁按 **Add file** → **Create new file**。
2. 在最上面的檔名欄位，**逐字輸入**：
   ```
   .github/workflows/himawari.yml
   ```
   打到每個 `/` 時，前面那段會自動變成資料夾（灰色小標籤），這是正常的。
3. 用記事本打開電腦裡的 `himawari-pipeline/.github/workflows/himawari.yml`
   （看不到的話，可以在 zip 檔裡直接點開），**全選 → 複製**，貼到網頁的大編輯框。
   > ⚠️ YAML 檔對「空白縮排」很敏感，請整份原樣貼上，不要自己改縮排、不要用 Tab。
4. 按右上角綠色 **Commit changes...** → 在跳出的視窗選 **Commit directly to the `main` branch** → 再按 **Commit changes**。

### 3-3 同樣方法建立 `.github/workflows/keepalive.yml`

1. 回到 repo 首頁 → **Add file** → **Create new file**。
2. 檔名輸入：
   ```
   .github/workflows/keepalive.yml
   ```
3. 貼上電腦裡 `keepalive.yml` 的全部內容 → **Commit changes...** → **Commit changes**。

完成後 repo 首頁應該會有 `.github` 資料夾，點進去是 `workflows`，裡面有 `himawari.yml`、`keepalive.yml` 兩個檔案。

---

## 4. 開啟 GitHub Pages（只要做一次）

1. 在 repo 上方的分頁列按 **Settings**（齒輪圖示；畫面太窄時在 **⋯** 選單裡）。
2. 左側選單，在「Code and automation／Code, planning, and automation」區塊點 **Pages**。
3. 在 **Build and deployment** 底下的 **Source** 下拉選單，選 **GitHub Actions**。
   （選完就自動儲存了；GitHub 可能會推薦一些範本 workflow，**不用理它**，我們已經有自己的。）

### （確認即可，不用改）Actions 權限

- **Settings** → 左側 **Actions** → **General**：
  - 「Actions permissions」保持預設的 **Allow all actions and reusable workflows**。
  - 「Workflow permissions」保持預設（唯讀）就好。我們的 workflow 檔已經自己寫好需要的權限（Pages 部署、keepalive），**不需要**改成 Read and write。

---

## 5. 第一次手動執行

1. repo 上方分頁列按 **Actions**。
   - 如果看到一個 **I understand my workflows, go ahead and enable them** 的綠色按鈕，按下去。
2. 左側清單點 **himawari**。
3. 右邊列表上方有 **Run workflow** 按鈕 → 按下去 → Branch 保持 `main` → 兩個選項都不用填 → 按綠色 **Run workflow**。
4. 等 5～10 秒重新整理，列表會出現一筆新的執行：
   - 🟡 黃色圓圈轉動＝執行中
   - ✅ 綠色勾勾＝成功
   - ❌ 紅色叉叉＝失敗
5. 點進那一筆，會看到兩個方塊：**build**（畫圖）→ **deploy**（部署）。
   - 點 **build** 可以看每個步驟的紀錄；點「下載衛星原始檔並繪圖」展開，會看到類似：
     ```
     [   0.7s] 颱風： 29W CHAMPI (18.7N 128.9E 150.0 kt)
     [   2.4s] 掃描 202610211000，需要分段 [2, 3, 4, 5]
     [   2.8s] 下載 8 個檔案 15.4 MB，0.4 秒
     [   9.3s] 完成 wp292026（ir_bd, ir_color, wv）
     [  13.7s] 完成 overview（ir_bd, ir_color, wv）
     ```
   - 第一次執行有一行「讀取線上 latest.json 失敗（第一次執行屬正常）」是**正常的**（那時網站還不存在）。
6. 整個流程大約 2～4 分鐘（第一次安裝套件比較久；之後有快取會快一些）。
7. 兩個都綠色勾勾後，在 **deploy** 方塊下方會出現網址，或到 **Settings → Pages** 最上面看到
   **Your site is live at `https://你的帳號.github.io/himawari-tc/`**。
   點開這個網址，就能看到所有颱風衛星圖 🎉

> 之後**不用再手動按**，GitHub 每 10 分鐘（每小時的 03、13、23、33、43、53 分）會自動執行。
> 如果線上已經是最新一次掃描，它會略過繪圖和部署（紀錄裡會寫「線上已是 … 的圖，略過」），這是正常的省資源設計。

### 失敗了怎麼辦？

點紅色叉叉那一筆 → 點紅色的方塊 → 找到紅色的步驟展開，看最後幾行：

| 錯誤訊息裡有 | 原因 / 解法 |
|---|---|
| `Get Pages site failed` 或 `Not Found` 出現在「讀取 GitHub Pages 網址」 | 第 4 步 Pages 的 Source 還沒選 **GitHub Actions** |
| `Branch "xxx" is not allowed to deploy to github-pages` | 請從 `main` 分支執行（Run workflow 的 Branch 選 `main`） |
| `yaml` / `Invalid workflow file` | `himawari.yml` 貼上時縮排跑掉了，回到檔案按鉛筆 ✏️ 全選刪掉、重新整份貼上 |
| `找不到完整的 Himawari-9 掃描` | AWS 資料延遲，下一次排程通常就好了 |
| 其他看不懂的 | 把整段紅字複製給我，我幫你看 |

失敗的那一次不會影響網站：網站會維持上一次成功的圖；網站前端也會在超過 60 分鐘沒更新時，自動改用原本的 NRL／RAMMB／CyclonicWx 圖片。

---

## 6. 接到你的網站（Apps Script）

1. 打開 Apps Script 專案（儀表板那個），點左側檔案清單的 **Page.html**。
2. 按 **Ctrl + F** 搜尋 `HW_PAGES_BASE`，會找到這一行：
   ```js
   var HW_PAGES_BASE = 'https://<USERNAME>.github.io/<REPO>/';
   ```
3. 改成你的 Pages 網址（https 開頭、**結尾一定要有 `/`**），例如帳號是 `sean1115`：
   ```js
   var HW_PAGES_BASE = 'https://sean1115.github.io/himawari-tc/';
   ```
   > 帳號有大寫字母的話，網址裡請用**小寫**。
4. 按 💾 儲存（Ctrl + S）。
5. 重新部署成新版本（網址不會變）：
   右上角 **部署** → **管理部署作業** → 選目前的網頁應用程式 → 按 ✏️（編輯）→ **版本** 選 **新版本** → 按 **部署**。
6. 打開 Google Sites 的儀表板 → 🌀 颱風 → **🛰️ 颱風診斷分析**，最上面會多一個
   「🛰️ 向日葵 9 號原始資料自繪（每 10 分鐘）」方塊：
   - 上排按鈕：選颱風或「🗺️ 台灣及西北太平洋」區域總覽
   - 下排按鈕：Dvorak BD／彩色紅外線／水氣／真彩（日間；晚上會變灰色不能按）
   - **▶ 播放動畫**：最近約 2 小時的動畫；拉桿可以一張一張看
   - 點圖可以放大
   - 下方標示資料來源、掃描時間（台灣時間與 UTC）、多久以前
   原本的 NRL／RAMMB／CyclonicWx／ADT／JTWC 內容**全部保留**在下面。

> 只改了 Page.html 這一個檔案；**沒有新增任何 Apps Script 觸發器**（仍是 4 個），其他 .gs 檔都不用動。

---

## 7. 常見問題 FAQ

**Q1. 圖會延遲多久？**
衛星每 10 分鐘掃描一次，資料傳到 AWS 大約要 15～20 分鐘，GitHub 的排程本身也可能晚 5～15 分鐘（尖峰時段更久），
所以網站上的圖通常是 **20～40 分鐘前**的掃描。圖上與網站都會標示掃描時間。

**Q2. 真的不用錢嗎？**
公開 repo 的 GitHub Actions（標準 runner）和 GitHub Pages 都免費。Pages 有「網站 1 GB、每月流量約 100 GB」的軟性上限，
這個網站約 5～15 MB，正常瀏覽量不會碰到。衛星資料來自 NOAA 的 AWS 公開資料集，也不用錢、不用帳號。
（請不要把 repo 改成 Private，否則每月 2,000 分鐘免費額度會不夠。）

**Q3. 聽說 60 天沒動會被停用？**
對：公開 repo 如果 60 天都沒有任何活動，GitHub 會自動停用排程。
我們放了 `keepalive.yml`，每週一用 GitHub 官方 API「重新啟用」排程一次，**不會產生任何 commit、不需要金鑰**。
萬一還是被停用了（GitHub 會寄 email 通知），到 **Actions** → 左邊點 **himawari** → 按 **Enable workflow** 就恢復了。

**Q4. 怎麼暫停／停止？**
- 暫停：**Actions** → 左邊點 **himawari** → 右上角 **⋯** → **Disable workflow**。要恢復就按 **Enable workflow**。
- 網站暫時不顯示自繪圖：把 Page.html 的 `HW_PAGES_BASE` 改回 `'https://<USERNAME>.github.io/<REPO>/'` 再部署新版本，就完全回到原本的畫面。
- 永久刪除：**Settings** → 最下面 **Danger Zone** → **Delete this repository**（請三思）。

**Q5. 晚上為什麼沒有真彩？**
真彩用的是可見光，晚上沒有太陽光所以沒有資料；晚上會只提供紅外線與水氣，天亮後自動恢復。

**Q6. 颱風中心的綠色「＋」為什麼跟眼不完全重疊？**
「＋」是 NRL／JTWC 最近一次定位，用 best track 的移動方向速度外推到衛星掃描時間的**估計位置**，只是方便對照；
真正的中心請以雲圖的眼為準。視窗是以這個估計位置為中心，大約 1400 公里見方。

**Q7. 同時有好幾個颱風？**
最多同時畫 3 個（依強度排序，熱帶擾動 Invest 排後面），再加一張台灣/西北太平洋區域總覽。沒有颱風時只畫區域總覽。

**Q8. 想換標誌？**
用同樣檔名 `logo.png`（建議 256×256 左右、透明背景 PNG）在 `assets` 資料夾裡 **Add file → Upload files** 上傳覆蓋即可，下一次排程就會換上新標誌。

**Q9. 想改程式怎麼辦？**
在 repo 點開檔案 → 右上角 ✏️ 鉛筆 → 修改 → **Commit changes**。改完下一次排程就會用新版。不確定的話先問我。
