# 黑鑽選股系統 — Zeabur 部署包

架構與您現有的加密貨幣篩選器一致：**FastAPI 後端 + React 前端**，
用單一 Dockerfile 打包成一個服務（前端 build 後由後端直接 serve），
方便在 Zeabur 上以單一服務部署。

```
bd-app/
├── Dockerfile              # 多階段build：先build前端，再放進Python image
├── .dockerignore
├── .env.template
├── backend/
│   ├── main.py              # FastAPI：API + 靜態檔serve
│   ├── engine.py            # 黑鑽SOP 6道濾網篩選引擎
│   ├── storage.py           # JSON檔案儲存（settings/candidates/holdings）
│   └── requirements.txt
└── frontend/
    ├── src/App.jsx           # 主要UI（4個分頁：選股/計算/追蹤/SOP）
    ├── src/main.jsx
    ├── index.html
    ├── package.json
    └── vite.config.js
```

## 本機測試（部署前建議先跑一次）

```bash
# 1. 打包前端
cd frontend
npm install
npm run build

# 2. 把build結果放進後端的static目錄
cp -r dist ../backend/static

# 3. 啟動後端（會同時serve API與前端頁面）
cd ../backend
pip install -r requirements.txt --break-system-packages
uvicorn main:app --port 8000

# 4. 瀏覽器開 http://localhost:8000
```

這個流程我已經在沙盒環境完整測試過一輪（API的CRUD、靜態檔serve、SPA路由fallback都正常）。

## 部署到 Zeabur

1. 把整個 `bd-app/` 目錄推到一個 GitHub repo（或您現有專案的子目錄）。
2. 在 Zeabur 建立新服務，選擇這個 repo，Zeabur 會自動偵測到根目錄的 `Dockerfile` 並用它建置。
3. **重要：掛載 Volume 才能保留資料**
   - 這個服務用本機 JSON 檔存 `settings.json` / `candidates.json` / `holdings.json`，
     預設寫在容器內的 `./data`，**容器重啟會被清空**。
   - 到 Zeabur 服務設定 → Volume，掛載一個路徑，例如掛到 `/data`。
   - 然後在環境變數加上 `DATA_DIR=/data`。
4. 其餘環境變數可參考 `.env.template`：
   - `PORT`：Zeabur 會自動注入，不用自己設定。
   - `CORS_ORIGINS`：只有前後端分開部署在不同網域時才需要設定成該網域；
     這個包預設前後端同源部署，可以不用管。
5. 部署完成後，直接打開 Zeabur 給的網址即可看到「選股清單／停損停利計算／持股追蹤／SOP速查」四個分頁。

## 關於「執行篩選」功能

前端「選股清單」頁有兩種取得候選股的方式：

1. **從後端跑一次篩選**：輸入代碼清單（例如自選觀察股），呼叫
   `POST /api/screen/run` 觸發背景執行緒跑 `engine.py` 的濾網邏輯，
   前端每 1.5 秒輪詢 `GET /api/screen/status` 顯示進度，跑完自動載入結果。
   適合少量、你自己已經在關注的股票。

2. **貼上 candidates.json 匯入**：如果要對整個美股市場（`--fetch-nasdaq`，
   數千檔股票）跑全市場篩選，建議另外在本機或用排程器（cron）執行：
   ```bash
   python backend/engine.py --fetch-nasdaq --out candidates.json
   ```
   跑完把 JSON 內容貼到前端的匯入欄位即可。全市場篩選會對 Yahoo Finance
   發出數千次請求，直接掛在網頁請求裡容易逾時，所以拆開成離線批次跑會更穩定。
   之後如果想全自動化，也可以做一支排程腳本定期呼叫
   `POST /api/candidates/import`，把結果餵進同一個資料庫。

## 已知限制

- yfinance 抓取的是免費公開資料，準確度與更新頻率不如 ZACKS / Portfolio123，
  僅供初篩參考，最終仍需人工複核（5年線圖型態、AMEX排除等，SOP速查頁都有列）。
- 目前資料儲存是單檔 JSON，非多人協作/多帳號設計，適合個人使用。
