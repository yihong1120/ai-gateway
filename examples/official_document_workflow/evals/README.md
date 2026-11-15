# 公文驗收工具

工具與公文套件一起安裝，不依賴 `tests`，也不在匯入時連線。
執行時使用根目錄中央設定，服務 Python 環境需已安裝 Open WebUI 宿主。
`examples/README.md` 保留服務索引，原本散落在 examples 根目錄的公文腳本已移除。

## 本機文件與附件組

先盤點檔案，不執行 OCR 或模型：

```bash
python -m examples.official_document_workflow.evals.sample_corpus --input-root example_files
```

預設將根目錄每份 PDF／Office 檔案視為一個案例。多附件或子目錄案例使用明確的 JSON manifest：

```json
[{"name": "函文與附件", "files": ["函文.doc", "附件.pdf"]}]
```

```bash
python -m examples.official_document_workflow.evals.sample_corpus \
  --input-root example_files --manifest /path/to/cases.json \
  --execute --output test_outputs/document-acceptance \
  --config config.local.toml
```

`--cases 1,5` 選擇盤點中的編號；省略時選全部，明確提供空字串或不合法編號會在執行前拒絕。
只盤點、雜湊及執行選中的案例；manifest 每筆必須符合 `name`、`files` 契約。
編號取決於該次目錄或 manifest 順序，舊腳本的編號不沿用。
執行需要包含 Open WebUI 與公文依賴的服務 Python 環境。設定只讀取一次：預設從
`DATABASE_URL` 指向的 PostgreSQL 讀取公文 Function valves，或用 `--settings valves.json` 明確提供。
`--config` 指定中央 TOML，工具使用 `[webui]`；既有程序環境變數優先。

輸出目錄必須不存在；內含來源雜湊、程式雜湊、模型請求／回應、抽取文字、報告及 PDF／Word／CSV。
檔案可能包含私人資料，請放在已忽略的 `test_outputs/`。執行採私人權限，來源保持不變，
不寫入 WebUI 檔案庫、不匯入 EIP 行事曆、不使用持久分析快取或正式續接狀態。
每案最多 15 次主要分析請求；其他 OCR／審核請求仍受正式服務設定控制，這不是總費用上限。
遇到供應商暫時故障停止後續案例；報告保留已完成成果與待確認資訊。
格式檢查與執行完成只代表執行證據，不代表內容語意已驗收通過。

## 從 EIP 下載驗收附件

舊腳本的固定帳號、日期與文件編號已移除。此入口不依賴 WebUI 帳號綁定，
從環境取得 `EIP_SAMPLE_ACCOUNT`、`EIP_SAMPLE_PASSWORD`、`EIP_SAMPLE_IDENTITY`；
請依根目錄公開範本的註解，在私人 TOML 啟用獨立的 `[eip_samples]` 區塊，
或使用既有 secret 注入方式；程序環境優先，
不要把憑證寫入命令列或 manifest。

```bash
python -m examples.official_document_workflow.evals.eip_samples --execute \
  --folder sign --document-id DOCUMENT_ID --start 2026-10-01 --end 2026-10-02 \
  --output test_outputs/eip-sample --config /private/config.toml
```

工具確認登入身分及指定文件在所選本人清單的第一頁後，才下載附件；不會略過清單驗證。
日期區間應足夠精確；不在第一頁時請縮小區間。輸出中的 `manifest.json` 只在所有附件下載成功後建立。
下載失敗的目錄保留供診斷，重新下載應另選新目錄。

接著使用同一套本機驗收流程：

```bash
python -m examples.official_document_workflow.evals.sample_corpus \
  --input-root test_outputs/eip-sample/inputs --manifest test_outputs/eip-sample/manifest.json
```

加上 `--execute --output 新目錄` 及服務設定才會啟動 OCR／模型。下載與模型驗收分開執行，
讓本機重跑不需要再次登入 EIP。

## CSV 直接匯入轉換

```bash
python -m examples.official_document_workflow.cli.prepare_calendar_csv downloaded.csv --output direct-import.csv
```

保留原本 UTF-8 與百分號／加號處理，既有輸出不會被覆寫。這個工具本身不連線或寫入 EIP。
