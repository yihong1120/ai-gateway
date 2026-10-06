# 官方 PaddleOCR-VL-1.6 vLLM 服務

現役 OCR 服務是官方 PaddleOCR-VL 完整 Docker Compose 架構：

- paddleocr-vl-api：版面分析與相容的 POST /layout-parsing API。
- paddleocr-vlm-server：PaddleOCR-VL-1.6 VLM 的 vLLM GPU 後端。

兩個容器使用 GPU 0；只有 API 綁定 127.0.0.1:8113，VLM server 沒有對外 port。
Open WebUI 不需要變更，仍呼叫 http://127.0.0.1:8113/layout-parsing。

Compose 設定位於
[infra/paddleocr/paddleocr-vl-vllm.compose.yaml](../infra/paddleocr/paddleocr-vl-vllm.compose.yaml)，
使用官方固定映像：

~~~text
ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/paddleocr-vl:paddleocr3.4-nvidia-gpu
ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/paddleocr-genai-vllm-server:paddleocr3.4-nvidia-gpu
~~~

## 日常操作

~~~bash
cd ~/Documents/ai-gateway
docker compose -p paddleocr-vl-vllm \
  -f infra/paddleocr/paddleocr-vl-vllm.compose.yaml ps

curl --fail --silent http://127.0.0.1:8113/health | jq .

docker compose -p paddleocr-vl-vllm \
  -f infra/paddleocr/paddleocr-vl-vllm.compose.yaml logs --tail=100
~~~

容器採 restart: unless-stopped，因此 Docker 服務啟動時會自動恢復。不要同時啟動
paddleocr-vl.service；它是 PP-OCR fallback，會與 8113 port 及 GPU 衝突。

## Smoke test

~~~bash
cd ~/Documents/ai-gateway
base64 -w 0 example_files/fb431d7d-9228-4240-b939-20a59b171d69.pdf \
  | jq -Rs '{file: ., fileType: 0, useLayoutDetection: true, restructurePages: false, visualize: false, maxNewTokens: 4096}' \
  | curl --fail --silent --max-time 120 \
      -H 'Content-Type: application/json' --data-binary @- \
      http://127.0.0.1:8113/layout-parsing \
  | jq '{errorCode, errorMsg, pages: (.result.layoutParsingResults | length)}'
~~~

範例單頁繁中公文實測約 5 秒，且完整讀取日期、字號、主旨與 115 年 9 月 10 日期限。

## 回復 PP-OCR fallback

若官方服務失效，先停掉 Compose，再恢復舊的 systemd service：

~~~bash
cd ~/Documents/ai-gateway
docker compose -p paddleocr-vl-vllm \
  -f infra/paddleocr/paddleocr-vl-vllm.compose.yaml down
systemctl --user enable --now paddleocr-vl.service
curl --fail --silent http://127.0.0.1:8113/health
~~~

這會回到 GPU 常駐 PP-OCRv6-medium，不會更動 Open WebUI endpoint。
