# 跨服務診斷

這些是需要在工作區明確執行的量測及成果檢查，不是常駐服務，也不隨服務 wheel 安裝。
從 repo 根目錄使用對應 Python 環境及模組命令，工具不再修改 `sys.path`：

```bash
python -m examples.deployment.diagnostics.ocr_corpus --help
python -m examples.deployment.diagnostics.ocr_score --help
python -m examples.deployment.diagnostics.ocr_local_matrix --help
python -m examples.deployment.diagnostics.ocr_local_score --help
python -m examples.deployment.diagnostics.company_data_artifact_audit --help
python -m examples.deployment.diagnostics.gemini4_smoke --help
python -m examples.deployment.diagnostics.model_capacity --help
```

OCR 推論需要對應 GPU／SDK 環境；Office 成果驗算需要公司 Office 容器。
Gemini smoke、容量量測與實際推論會呼叫真實服務；本次整理只驗證離線入口，不執行它們。
私有文件、憑證與原始診斷輸出依原規則保留在私人目錄，公開報告只記錄去識別數值。

容量量測使用 `model_capacity`，模型由 workload JSON 的必要欄位 `model` 決定，
另需 `profile` 與 `requests`；每筆 request 保留 `label`、`messages`、`max_tokens`、
`input_tokens_tokenizer`。模型名稱必須是目標 gateway／WebUI 已提供的名稱，工具不部署模型。

```bash
python -m examples.deployment.diagnostics.model_capacity workload.json --output /tmp/model-capacity.json
```

`--root` 預設為目前工作目錄，用於定位中央設定；輸出會記錄所用模型以供追溯。
每次量測另存 workload 並補上 `model`，結果輸出到 `artifacts/` 或 repo 外的私人目錄。
`docs/` 維護操作與設計文件，不存放逐輪實驗報告、來源包或預覽產物。
