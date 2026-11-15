# Gemma 4 遠端推論部署背景

本頁保留 Gemma 部署與相容命名的背景，通用啟動方式統一見
[LiteLLM 本機推論](../examples/litellm/README.md#本機推論)。
目前的來源入口為根目錄 `docker-compose.yml` 的 `local-model` profile，
使用 `ai-gateway compose local-model up -d`；後端參數使用私人
`vllm.local.yaml`；不再以 `gemma4/` 目錄及模型專屬環境變數承接部署。

既有 Open WebUI／LiteLLM 公開模型 alias `gemma-4-12b` 與上游 ID
`gemma-4-12B-it-exl3-4.00bpw_mul1` 保持相容。上游名稱含有 EXL3 並不表示
實際權重或推論軟體；後端實際使用的模型與 parser 由該次原生 YAML 決定。

2026-10-14 的 vLLM 0.31.0／Gemma QAT W4A16 遷移、工具呼叫、256K 上下文及並發限制見
[vLLM 遷移報告](gemma4-vllm-migration-2026-10-14.md)與
[聚合結果](gemma4-vllm-migration-results.json)。重新量測容量使用
[目前診斷工具](../examples/deployment/diagnostics/README.md)，另存 workload 與執行結果。
這些證據記錄測試當時的參數與主機，不代表通用範本對其他模型的容量保證。

既有主機的 Nginx、systemd、來源 ACL 與網路入口需另外核對；本次來源整理不會
替換它們、重啟容器或變更 gateway 金鑰權限。更新與回復使用該主機保存的部署收條與設定。

通用 Nginx／systemd 範本及預設 loopback 限制見上述本機推論文件。
切換時須更新既有 unit 的設定路徑，不再引用舊 `infra/` 來源。
