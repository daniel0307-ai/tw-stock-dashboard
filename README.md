# 台股每日儀表板

每個交易日 17:47（台北時間）自動抓取證交所與櫃買中心盤後資料，跑三種篩選（價值、動能、成長），更新 GitHub Pages 上的儀表板。

## 一次性設定（約 10 分鐘）

1. **建立 repo**：在 GitHub 新增一個 repo（例如 `tw-stock-dashboard`），把這個資料夾的所有檔案上傳（包含隱藏的 `.github` 資料夾）。
   - 免費帳號只有 **public repo** 能開 GitHub Pages，自選股清單會是公開的。不想公開的話需要 GitHub Pro。
2. **開啟 Actions 寫入權限**：Settings → Actions → General → Workflow permissions → 選「Read and write permissions」→ Save。
3. **開啟 GitHub Pages**：Settings → Pages → Source 選「Deploy from a branch」→ Branch 選 `main`、資料夾 `/docs` → Save。
4. **第一次手動執行**：Actions → 「每日台股篩選」→ Run workflow。第一次會往回補 70 個交易日的歷史，大約 8–10 分鐘。
5. 跑完後打開 `https://<你的帳號>.github.io/<repo 名稱>/` 就能看到儀表板。

之後每個交易日 17:47 和 20:26（補跑）會自動執行，休市日不會更新。

## 日常調整

| 想做的事 | 怎麼做 |
|---|---|
| 增刪自選股 | 儀表板「編輯自選股」（或直接改 `config/watchlist.csv`） |
| 調整篩選門檻 | 儀表板「調整篩選條件」（或直接改 `config/strategies.json`） |
| 補跑某一天 | Actions → Run workflow → 填日期 |

### 在儀表板上編輯（一次性設定）

儀表板右上角「連線設定」→ 依畫面步驟到 GitHub 建立 fine-grained token：
只勾選這個 repo，權限 `Contents` 與 `Actions` 設為 Read and write，貼回儀表板即可。
權杖只存在該裝置的瀏覽器；換電腦或手機要再貼一次。儲存後會自動觸發排程重算（週末也可以），約 3–5 分鐘後重新整理頁面。

## 啟用成長型（之後）

1. 註冊 FinMind，取得 API token。
2. Settings → Secrets and variables → Actions → New repository secret，名稱 `FINMIND_TOKEN`。
3. 實作 `src/providers/finmind.py` 的 `get_monthly_revenue()`（檔案內有回傳格式說明），其他程式不用改。

## 資料夾結構

```
config/                 設定（自選股、篩選門檻）
src/twse.py             證交所資料抓取與解析
src/analysis.py         指標、篩選、成效追蹤
src/main.py             排程進入點
src/providers/          其他資料來源（FinMind 預留）
docs/index.html         儀表板頁面
docs/data/latest.json   儀表板讀取的資料（排程產生）
docs/data/history/      歷史行情、法人、大盤、選股紀錄
tests/mock_run.py       用模擬資料離線測試：python tests/mock_run.py 2026-11-17
```

## 已知限制

- 只涵蓋上市股票；上櫃股票放進自選股會顯示「無資料」。
- 尚未排除全額交割股（流動性門檻通常已濾掉大部分）。
- 證交所有頻率限制，程式每次請求間隔 3.5 秒；若 GitHub 的機器被證交所擋住，Actions 紀錄會顯示「抓取失敗」。

本專案的篩選結果僅供參考，不構成投資建議。
