# PaddleOCR-VL-1.6 正式服務

2026-10-14 已將 A 的 `127.0.0.1:8113/layout-parsing` 切換為 PaddleOCR-VL-1.6。Open WebUI 文件處理仍使用原端點和原回應格式。新服務由兩部分組成：

| 程序 | 管理方式 | 監聽位置 | 用途 |
| --- | --- | --- | --- |
| OCR vLLM 後端 | 根目錄 Compose 的 `ocr` profile、`restart: unless-stopped` | `127.0.0.1:18114` | vLLM 0.31.0 承載 1.6 的視覺語言模型 |
| `paddleocr-vl16-api.service` | 使用者 systemd | `127.0.0.1:8113` | PaddleX 3.7.2 版面解析與 `/layout-parsing` |

Docker 設定統一在根目錄 [docker-compose.yml](../docker-compose.yml)，生成參數使用
[vLLM 原生範本](../examples/official_document_workflow/deployment/ocr/vllm.example.yaml)。
PaddleX 的獨立設定在 [PaddleOCR-VL-1.6-vllm.yaml](../examples/official_document_workflow/deployment/ocr/PaddleOCR-VL-1.6-vllm.yaml)。
API 使用 `/home/changdar/.conda/envs/paddleocr_vl_env`，已安裝 `paddlex[genai-client]==3.7.2`；
啟動程式為 [run_vl16_api.sh](../examples/official_document_workflow/deployment/ocr/run_vl16_api.sh)。
user service 範本集中在 [paddleocr-vl16-api.service.example](../examples/deployment/systemd/paddleocr-vl16-api.service.example)，
安裝時移除 `.example`，保留後端 readiness 檢查及啟動期限。
vLLM 與同機文字模型及 PaddleX 共用 GPU，原生範本保留單一推論序列、512 MiB KV cache
及既有解析限制。模型後端及 API 都只綁定 loopback。

## 設定與啟動

從 repo 根目錄建立私人 vLLM 設定：

```bash
cp -n examples/official_document_workflow/deployment/ocr/vllm.example.yaml ocr-vllm.local.yaml
chmod 600 ocr-vllm.local.yaml
```

中央 `config.local.toml` 的 `[ocr]` 保存以下容器設定：

| 變數 | 用途 |
| --- | --- |
| `OCR_INFERENCE_IMAGE` | vLLM 映像版本 |
| `OCR_MODEL_PATH` | 本機 PaddleOCR-VL-1.6 模型目錄的絕對路徑 |
| `OCR_INFERENCE_CONFIG_PATH` | 私人 `ocr-vllm.local.yaml` 的絕對路徑 |
| `OCR_GPU_ID` | 使用的 GPU |
| `OCR_HOST_PORT` | loopback 後端埠，既有 PaddleX 設定使用 `18114` |

模型目錄唯讀掛在 `/models/model`。模型、served name、context、batch 與 KV cache
直接交給 vLLM 原生 YAML，不另建立一套參數映射；範本的 `host: 0.0.0.0`、`port: 8000`
是容器內監聽位置。PaddleX 的 `server_url` 及 systemd 啟動檢查必須與主機後端埠一致。

```bash
ai-gateway compose ocr up -d
```

此命令只啟動 vLLM 後端；`8113/layout-parsing` 仍由獨立的 PaddleX API 提供。
切換既有部署前須核對原 project、容器及 unit，避免同時啟動佔用相同 GPU／埠的兩個後端。
來源整理未變更或重啟主機已安裝的服務。

正式切換前，測試埠的合成公文 12/12 個欄位、表格 3/3 個欄位及既有 PDF 7/7 個欄位命中；切換後 `8113` 的三張圖片和一份原 PDF 均實際成功處理。重跑精度與顯存量測使用[診斷工具](../examples/deployment/diagnostics/README.md)，以目前模型、來源與硬體分批保存結果。

日常檢查：

```bash
ai-gateway compose ocr ps
systemctl --user status paddleocr-vl16-api.service
curl --fail --silent http://127.0.0.1:8113/health | jq .
curl --fail --silent http://127.0.0.1:18114/v1/models | jq '.data[].id'
```

採用根目錄 Compose 的部署，後端日誌用 `ai-gateway compose ocr logs --tail 100`，
API 日誌用 `journalctl --user -u paddleocr-vl16-api.service`。
既有主機的 `changdar` 使用者已開啟 systemd linger；API 使用者服務會隨使用者管理員啟動，
vLLM 容器由 Docker 自動重啟。本 repo 僅維護 OCR 1.6；舊 1.5 Compose 與 PP-OCR 備援
server／systemd 範本已移除。

## B 的 OCR 轉送與開機順序

B 的 `127.0.0.1:8113` 經 Nginx 轉送到 A 的
`100.96.17.117:14888/ocr/`，再由 A 轉送到本機 `8113`。
若 A 的 OCR `/health` 正常、B 的 `/health` 卻回傳 502，應先檢查 A 的
`nginx.service`，不能只檢查 OCR 模型服務。

2026-10-26 追查掃描公文失敗時，發現 A 的 Nginx 在 10/22 開機時先於
Tailscale 位址就緒而失敗，錯誤為 `Cannot assign requested address`。
[Nginx 啟動設定](../examples/deployment/systemd/nginx-tailscale.conf) 增加 Tailscale 啟動順序與
失敗後每 10 秒重試，涵蓋 daemon 已啟動但位址尚未配置完成的情況。
設定保留既有 Nginx 監聽位址、來源 ACL 與轉送路徑。

在 A 執行下列修復會先驗證現有 Nginx 設定、備份 systemd unit／既有 drop-in，
再安裝啟動設定並啟動服務：

```bash
sudo bash /home/changdar/Documents/ai-gateway/examples/deployment/systemd/repair-nginx-tailscale.sh
```

完成後，從 B 核對 `http://127.0.0.1:8113/health` 以及實際 PDF 的
`/layout-parsing` 回應；只檢查 `/health` 仍不足以驗收文件辨識。

完整公文流程還會執行 `useLayoutDetection:false`、`promptLabel:spotting`，
為可編輯 Word 取得整頁文字位置。2026-10-26 的兩頁掃描測試發現，
原本四個 vLLM 序列及多 GiB 自動 KV cache 會在此步驟發生 GPU OOM。
目前 vLLM `max_num_seqs=1`、`kv_cache_memory_bytes=536870912`，
PaddleX VLRecognition `batch_size=1`、`max_concurrency=1`；保留原掃描解析度、
8192 token context 及 4096 token 輸出上限，沒有省略頁面或跳過 Word 辨識。
驗收需同時涵蓋文字版面解析、整頁 spotting 與正式公文流程輸出。

版本回復應使用目標主機已保存的部署備份與相應版本來源；目前工作樹不再提供
OCR 1.5 或 PP-OCR 備援啟動入口。清理 repo 不改變主機上的容器、資料與服務狀態。
