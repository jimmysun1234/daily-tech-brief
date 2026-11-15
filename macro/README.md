# 美國總經與 Fed 自動更新管線（`macro/latest.json`）

本目錄包含可直接放入 [`jimmysun1234/daily-tech-brief`](https://github.com/jimmysun1234/daily-tech-brief) 根目錄的 GitHub Actions 自動化管線，負責每日定時產出 `macro/latest.json`，供 Asset Manager App「籌碼總經」頁籤同步讀取。

## 設計原則
- **官方來源優先**：優先使用 FRED 官方 API（`DFEDTARL`、`DFEDTARU`、`CPIAUCSL`、`PCEPILFE`、`PAYEMS`、`UNRATE`、`PPIFID`、`A191RL1Q225SBEA`），未設定 Key 時亦可退回 FRED 官方公開 CSV；FOMC 會議日期、聲明與紀要原文直接抓取 Federal Reserve 官網。
- **不編造預期值與機率**：`consensus`（市場預期）與 `CME FedWatch` 機率若無授權／明確來源之資料，一律維持 `null` 並在 App 顯示「暫無資料」及 CME 官網連結，或透過 `macro/consensus_override.json` 手動／外部補登（須附 `source` 與 `updated_at`）。
- **分析結果明確標示**：FOMC 紀要之「偏鷹／中性／偏鴿」標籤明確標記 `minutes_tone_is_analysis: true`，僅供分析參考，不代表聯準會官方立場。
- **狀態保留與防損壞**：任一來源逾時或錯誤（404／429／500）時自動重試並保留既有歷史數值（標為 `stale`），且在驗證通過後才允許寫入／提交 `macro/latest.json`。

## 檔案清單（部署至 `daily-tech-brief` 根目錄）
- `macro/latest.json`：總經指標、Fed 利率與 FOMC 摘要主檔
- `macro/consensus_override.json`：選用之市場預期／FedWatch 覆寫檔
- `scripts/fetch_macro.py`：核心擷取、年增率計算、狀態保留與 JSON 驗證腳本
- `scripts/test_fetch_macro.py`：計算與驗證邏輯單元測試
- `requirements.txt`：Python 依賴套件
- `.github/workflows/update-macro.yml`：GitHub Actions 排程（週一至週五 UTC 14:00、22:00）與手動觸發工作流程

## 部署與設定步驟
1. **申請免費 API Key**：
   - **FRED API Key**（必填以啟用官方 API）：前往 [St. Louis Fed FRED API Keys](https://fredaccount.stlouisfed.org/apikeys) 登入後免費申請。
   - **Gemini API Key**（選填，用於自動翻譯與摘要最新 FOMC 會議紀要）：前往 [Google AI Studio](https://aistudio.google.com/apikey) 建立免費金鑰；若未設定將自動退回原文關鍵指標摘要。
2. **將本資料夾內容放入 `daily-tech-brief` 根目錄**並推送至 `main` 分支。
3. **設定 GitHub Actions Secrets**：
   - 開啟 `daily-tech-brief` → `Settings` → `Secrets and variables` → `Actions` → `New repository secret`：
     - `FRED_API_KEY`：貼上 FRED 金鑰
     - `GEMINI_API_KEY`（選填）：貼上 Gemini 金鑰
4. **確認權限**：
   - 在 `Settings` → `Actions` → `General` → `Workflow permissions` 確認允許 `Read and write permissions`（本 workflow 亦已宣告 `contents: write`）。
5. **手動測試與驗證**：
   - 前往 `Actions` → `Update US Macro & FOMC Brief` → `Run workflow`。
   - 執行完成後確認 `https://raw.githubusercontent.com/jimmysun1234/daily-tech-brief/main/macro/latest.json` 可回傳 HTTP 200 與合法 JSON。

## 本機測試指令
```bash
pip install -r requirements.txt
python -m unittest discover -s scripts -p "test_*.py" -v
python scripts/fetch_macro.py --dry-run
```
