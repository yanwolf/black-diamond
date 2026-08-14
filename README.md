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

前端「選股清單」頁提供三種取得候選股的方式：

1. **從後端跑一次篩選（少量自選股）**：輸入代碼清單，呼叫
   `POST /api/screen/run`（`source: "symbols"`）觸發背景執行緒跑濾網邏輯，
   前端每 1.5 秒輪詢 `GET /api/screen/status` 顯示進度。適合你已經在關注的股票。

2. **全市場掃描（網頁按鈕）**：點「開始全市場掃描」會呼叫
   `POST /api/scan/full-market/run`，伺服器背景執行緒抓取 NASDAQ + NYSE
   完整上市清單（數千檔）並套用預設濾網參數逐檔檢查，過程中可離開頁面，
   回來會自動看到最新結果與已通過檔數。**這是長時間工作（可能數十分鐘），
   請留意 Zeabur 服務是否有請求逾時或休眠設定**，若容器會因為沒有前台流量
   自動休眠，長時間背景任務可能被中斷。

3. **自動排程**：同一個面板可以設定「每隔幾小時自動掃描一次」並啟用，
   設定會存進 `schedule.json`，用 APScheduler 在伺服器內用
   `IntervalTrigger` 定期觸發全市場掃描，掃描結果直接覆蓋 `candidates.json`。
   面板會顯示「上次掃描時間／通過檔數」與「下次排程時間」。
   注意：
   - 排程只在伺服器程序持續運行時才會觸發，若服務重啟，會依照儲存設定
     重新註冊定期任務。
   - 若 Zeabur 該服務有「無流量自動休眠」的設定，排程可能因為沒有流量
     而不會準時觸發；建議先在 Zeabur 服務設定確認是否有休眠機制，
     若有，需要關閉或改用外部 cron 定期打 `/api/scan/full-market/run`
     喚醒服務。
   - 全市場掃描與自選股篩選共用同一個背景工作位，同時間只能跑一個，
     若排程觸發時剛好有其他篩選在跑，會自動略過本次排程等下一輪。

4. **貼上 candidates.json 匯入**：如果想在本機先跑好再匯入（例如想調整
   濾網參數做離線測試），可以：
   ```bash
   python backend/engine.py --fetch-nasdaq --out candidates.json
   ```
   跑完把 JSON 內容貼到前端「貼上 candidates.json 匯入」欄位即可。

## 已知限制

- yfinance 抓取的是免費公開資料，準確度與更新頻率不如 ZACKS / Portfolio123，
  僅供初篩參考，最終仍需人工複核（5年線圖型態、AMEX排除等，SOP速查頁都有列）。
- 目前資料儲存是單檔 JSON，非多人協作/多帳號設計，適合個人使用。
