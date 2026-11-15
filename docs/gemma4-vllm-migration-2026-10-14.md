# Gemma 4 12B 遷移至 vLLM 與多人並發實測（2026-10-14）

正式服務已由 TabbyAPI／ExLlamaV3 的 Gemma 4 12B EXL3 換成 **vLLM 0.31.0 + Google 官方 QAT W4A16 權重**，與正式 PaddleOCR-VL-1.6 共用 A 的 RTX 4090 24 GiB。B 的 LiteLLM 及 Open WebUI 沿用 `gemma-4-12b`，不需更換使用者看到的模型名稱。部署設定與回復步驟見[接入文件](gemma4-remote-inference.md)；OCR 配置見[OCR 服務文件](paddleocr-vl-service.md)。

Gemma 4 12B 原始 BF16 的[官方 vLLM 部署配方](https://github.com/vllm-project/recipes/blob/main/Google/Gemma4.md)列出至少 40 GB 的 NVIDIA GPU，官方 QAT W4A16 權重則約 8.3 GB，適合這張 24 GB 卡同時保留 OCR 服務。本機實際載入權重約 8.28 GiB。模型固定為 `google/gemma-4-12B-it-qat-w4a16-ct` revision `1d2c2d7f2466070e69d6fb3fd5ce9a7d75f2f6ee`；`model.safetensors` SHA256 為 `60b6e3989502969d8ae04185d72ecbbc7db63978d5af747a493d53895aa6bfa3`。

vLLM 設定 `max_model_len=262144`、`max_num_seqs=8`、`max_num_batched_tokens=4096`、FP8 KV cache、`gpu_memory_utilization=0.55`、`long_prefill_token_threshold=2048`。OCR 後端另預留 GPU 比例 0.25。vLLM 啟動日誌顯示可用 KV cache 約 **304,501 tokens**，約為一筆滿 262,144 tokens 請求的 1.16 倍。因此「支援 256K」是**單筆容量**，八人各開 256K 並不能同時裝入快取。這個文字服務停用了影像、影片、音訊輸入；文件圖片走獨立 OCR 管線。

後續收到 Open WebUI 的工具選擇錯誤，原因是首輪容量測試只有純聊天，vLLM 當時未啟用 `tool_choice=auto` 所需參數。已依 [vLLM Gemma 4 官方配方](https://github.com/vllm-project/recipes/blob/main/Google/Gemma4.md)補上 Gemma 4 工具與 reasoning 解析器及工具聊天模板；A 直連、B LiteLLM、B Open WebUI API 的串流 `tool_choice=auto` 測試均回傳 HTTP 200 和結構化工具呼叫。這項設定更動不影響上方既有的上下文上限。

## 實測結果

以下是突發並發測試，均使用合成內容。短聊天每筆約 1,970–2,004 輸入 tokens、256 輸出 tokens；32K 文件每筆約 32,700 輸入 tokens、256 輸出 tokens。經 B 的正式服務鏈路送入，不是只有 A 的本地 API。表內「首字最慢」是自送出請求起計算的 TTFT；「整輪完成」是全部請求完成的牆鐘時間。

| 場景 | 成功 | 首字最慢 | 整輪完成 |
| --- | ---: | ---: | ---: |
| 舊 EXL3 batch 1，B LiteLLM，8 筆短聊天，OCR 1.6 已載入 | 8/8 | 25.95 秒 | 28.86 秒 |
| 新 vLLM，B LiteLLM，8 筆短聊天 | 8/8 | 2.48 秒 | 8.25 秒 |
| 新 vLLM，B Open WebUI API，8 筆短聊天 | 8/8 | 3.03 秒 | 8.78 秒 |
| 新 vLLM，B LiteLLM 8 筆短聊天 + A 同時 4 筆 OCR | 8/8；OCR 4/4 | 3.67 秒 | 聊天 9.63 秒；OCR 6.47 秒 |
| 新 vLLM，B LiteLLM 4 筆約 32K 文件 + A 同時 4 筆 OCR | 4/4；OCR 4/4 | 27.26 秒 | 聊天 33.00 秒；OCR 7.15 秒 |

同負載的短聊天基準中，新服務全部完成約快 **3.5 倍**（28.86 ÷ 8.25）；這是一次突發工作負載的觀測值，不是持續吞吐保證。Open WebUI API 測試使用同一個現有管理員登入驗證轉送，未測獨立帳號與瀏覽器互動。混合測試使用不同的合成前綴，避免重複工作負載誤觸 prefix cache。A 直連 vLLM 的 8 筆短聊天 + 4 筆 OCR 測試也全部成功，GPU 全卡峰值 22,547 MiB、最少剩餘 1,531 MiB；這是**全 GPU 用量**，不能視為 Gemma 容器單獨用量。

近 256K 場景另外從 A 直連測試：先送一筆約 **257,912 輸入 tokens**、64 輸出 tokens 的文件，兩秒後送三筆約 1,960 輸入 tokens、128 輸出 tokens 的短聊天。四筆都成功，但單卡的長文件仍顯著影響互動延遲：

| vLLM 設定 | 長文件首字／完成 | 短聊天首字範圍 | 短聊天完成範圍 |
| --- | ---: | ---: | ---: |
| 未設定長 prefill 門檻 | 114.02／116.33 秒 | 112.61–112.92 秒 | 115.80 秒 |
| `long_prefill_token_threshold=2048`（目前正式設定） | 118.56／120.83 秒 | 1.57–2.83 秒 | 116.58–117.54 秒 |

門檻讓後送的短聊天較快取得**第一個 token**，但仍要約兩分鐘才能產生完整回覆。故目前已改善一般多人短聊天並發，**沒有達到「近 256K 文件進行時，短聊天也完全不等」**。若此情境必須維持穩定互動速度，需要獨立 GPU／服務或縮短文件上下文；單靠把 `max_num_seqs` 設為 8 無法保證。

## 驗證範圍與紀錄

正式切換後已驗證 A 的 vLLM `/v1/models`、A Tailscale 入口、B LiteLLM 單筆請求與 8 筆並發，以及 B Open WebUI API 8 筆並發。OCR 正式 API 換版後通過三張測試影像、一份原始 PDF，另有四筆同時 OCR 測試。未測多人各自接近 256K、16 人以上突發、長時間連續負載、4,096-token 輸出、不同 Open WebUI 帳號或完整瀏覽器流程；也尚未比較 QAT W4A16 與舊 EXL3 權重的回答品質。這些速度數字不應外推至上述情境。

去識別的[聚合結果 JSON](gemma4-vllm-migration-results.json)保留每輪成功數、延遲、token 數和近 256K 請求逐筆時間；不含 prompt、回覆或憑證。原始 B 結果位於 `~/.local/share/gemma4-capacity-20261014/`，A 的直連及 GPU 取樣結果位於 `/home/changdar/opt/gemma4-vllm/`。當時的 B 測試腳本 `gemma4_capacity.py` 已整合為 [model_capacity.py](../examples/deployment/diagnostics/model_capacity.py)。重跑時使用[現行診斷工具](../examples/deployment/diagnostics/README.md)，結果另存於私人目錄。
