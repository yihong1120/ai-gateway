# AI Gateway (LiteLLM Proxy)

This repository provides a unified LLM gateway service that:
1. Centrally manages models (Google, OpenAI, Ollama)
2. Exposes an OpenAI-compatible API (`http://localhost:4000/v1`)
3. **Integrates MCP (Model Context Protocol) Tools** for specialized capabilities

## Features

- 🚀 **Unified Model API**: Single endpoint for multiple LLM providers
- 🔧 **MCP Tools Support**: Integrate external tools via Model Context Protocol
- 🌐 **OpenAI Compatible**: Works with OpenAI SDK and clients
- 🐳 **Docker Ready**: Easy deployment with docker-compose
- 🔒 **Flexible Auth**: Optional authentication for local development

Local development mode: no JWT and no enforced authorisation. You only need to specify `model` in your request.

## Quick start

This directory includes a local environment file: `.env` (replace any `REPLACE_ME` values with your own).

1. Copy the environment template:
   ```bash
   cp .env.example .env
   ```

2. Edit `.env` and fill in your API keys:
   ```ini
   GOOGLE_API_KEY=yourGoogleKey
   ```

3. Start the service (choose one of the following):

    - Option A: Docker
       ```bash
       docker-compose up -d
       ```

    - Option B: Run locally (FastAPI/Uvicorn, no Docker required)
       ```bash
       # Install dependencies (uv recommended)
       uv sync

       # If you do not have uv installed, you can use pip instead:
       # pip install -r requirements.txt

       # Load local environment variables (.env) and start the LiteLLM Proxy
       set -a
       source ./.env
       set +a

       uv run litellm --config ./config.yaml --port 4000 --detailed_debug
       ```

4. Test connectivity:
   ```bash
    # Works without a token
    curl http://localhost:4000/v1/models

    # Call with a specified model
    curl http://localhost:4000/v1/chat/completions \
       -H 'Content-Type: application/json' \
       -d '{"model":"gemini-2.5-flash-lite","messages":[{"role":"user","content":"ping"}],"max_tokens":8}'

    # Ollama / gpt-oss-20b notes:
    # The gpt-oss family may output "thinking" first, then the final answer.
    # If max_tokens is too low, a common result is an empty message.content.
    # Fix: increase max_tokens, or set reasoning_effort=low when you need short outputs.
    curl http://localhost:4000/v1/chat/completions \
       -H 'Content-Type: application/json' \
       -d '{"model":"gpt-oss-20b","messages":[{"role":"user","content":"Reply with exactly: OK"}],"reasoning_effort":"low","max_tokens":16,"temperature":0}'

    # Embeddings (e.g. Ollama's bge-m3)
    curl http://localhost:4000/v1/embeddings \
       -H 'Content-Type: application/json' \
       -d '{"model":"bge-m3","input":["hello","world"]}'
   ```

## Local Gemma 4 31B via TabbyAPI / ExLlamaV2

`config.yaml` includes a `gemma-4-31b` route that forwards LiteLLM requests to a local TabbyAPI server:

```yaml
model: openai/gemma-4-31b
api_base: http://127.0.0.1:5000/v1
```

Before using it, start TabbyAPI with a Gemma 4 31B EXL2 model loaded. The upstream model id in `config.yaml` (`gemma-4-31b`) should match the id returned by TabbyAPI's OpenAI-compatible models endpoint:

```bash
curl http://127.0.0.1:5000/v1/models
```

Then restart this gateway and test through LiteLLM:

```bash
curl http://localhost:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemma-4-31b","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":16,"temperature":0}'
```

If your TabbyAPI instance uses a different port, model id, or API key, update the `gemma-4-31b` entry in `config.yaml`. Keep the `api_base` value ending in `/v1`.

## MCP Tools Integration

This gateway includes **MCP (Model Context Protocol)** support for accessing external tools:

### Currently Configured MCP Servers

- **Word MCP**: Document manipulation tools at `http://192.168.50.239:9000/mcp`
  - Create, read, edit Word documents
  - Automated document generation

### Testing MCP Tools

```bash
# Test MCP integration
python test_mcp_tools.py

# Test Open-WebUI integration patterns
python openwebui_integration_example.py
```

### Using MCP Tools in Code

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-1234",
    base_url="http://localhost:4000"
)

# Define MCP tools
mcp_tools = [{
    "type": "mcp",
    "server_label": "word-mcp",
    "server_url": "litellm_proxy",
    "require_approval": "never"
}]

# Use with chat completions
response = client.chat.completions.create(
    model="gemini-2.5-flash",
    messages=[{"role": "user", "content": "Create a document"}],
    tools=mcp_tools
)
```

See [MCP_INTEGRATION.md](MCP_INTEGRATION.md) for detailed documentation.

## Architecture with Open-WebUI

```
Open-WebUI (Frontend + File/Web Search)
    ↓
LiteLLM Gateway (Model Router + MCP Tools)
    ↓
Multiple LLM Providers (Google, OpenAI, Ollama)
```

**Responsibility Division**:
- **Open-WebUI**: User interface, file search, web search, RAG
- **LiteLLM Gateway**: Model management, MCP tools execution
- Clear separation of concerns, no feature duplication

## Integrating with Open-WebUI

In Open-WebUI settings, configure:

```ini
OPENAI_API_BASE_URL=http://localhost:4000/v1
OPENAI_API_KEY=sk-1234  # Any value for local dev
```

Enable in Open-WebUI:
- ✅ Web Search (Open-WebUI feature)
- ✅ Document Search (Open-WebUI feature)
- ✅ MCP Tools (via LiteLLM Gateway)


### 公司助理與公文的多人並行

Open WebUI 維持單一應用程序；公司助理由持有 PostgreSQL lease 的單一排程器管理多件並行工作。請勿單純增加 Uvicorn workers：EIP Session 及處理名額仍屬程序內狀態。

公司助理與公文上傳均採異步提交：聊天立即取得工作編號，執行與瀏覽器連線分離；進度、下載及取消集中於 `/agents`。同一帳號跨公司助理與公文工作依序執行，各類工作有獨立並行名額，避免公文佔滿公司助理名額。公文使用已啟用的處理程式與提交時設定，執行前重驗本人附件與程式版本；版本變更會停止舊工作。已輸出的公文檔案逐筆保留，中斷工作不自動重播。

`COMPANY_AGENT_CONFIG` 的設定：`max_concurrent_jobs` 預設 4（可設 1–32），`max_pending_jobs` 預設 500，`max_pending_per_user` 預設 20。後兩者計算等待及執行中的工作；重送同一請求會連回既有工作。同一使用者跨聊天最多執行一件；有大量待處理工作的帳號不會擋住其他帳號。工作失敗或服務重啟不自動重播外部操作。

2026-10-04 本機正式設定：公司助理、公文上傳與 EIP 各 8 件並行，共用模型 16、OCR 2。公司助理與公文上傳共用的持久化佇列最多 1000 件待處理（含執行中）、每人 20 件。公司設定在 `company_agent/configs/config.production.json`，共用資源設定在使用者 systemd 的 `openwebui.service.d/company-concurrency.conf`。50 人與 100 人的本機測試數據見 `company_agent/evals/capacity_results.json`；模型 16 的雲端供應商限流仍需實際觀察。

公文共用資源可用服務環境變數調整，修改後需重啟服務：

| 變數 | 預設 | 範圍 | 用途 |
| --- | ---: | --- | --- |
| `DOCUMENT_MAX_CONCURRENT_JOBS` | 4 | 1–32 | PDF／Word／ODT／圖片上傳的完整處理流程 |
| `EIP_MAX_CONCURRENT_JOBS` | 4 | 1–32 | 手動、自動與公司助理的 EIP 公文處理 |
| `MODEL_MAX_CONCURRENT_REQUESTS` | 8 | 1–64 | 公司助理與公文共用的模型請求名額 |
| `OCR_MAX_CONCURRENT_REQUESTS` | 2 | 1–8 | 共用 GPU OCR 請求名額 |

公文與 EIP 各自對同一使用者依序處理，不同使用者可並行；程序內的資源等待名額各最多 500 件、同一帳號最多等待 20 件，與公司助理／公文的持久化佇列上限分開計算，滿額時明確拒絕新請求。等待資源採非同步方式，取得名額後才開始模型／OCR 的網路逾時計時。取消中的同步 OCR 會保留名額直到原請求結束。模型供應商的速率／token 配額仍可能限制吞吐量；調大並行值前應確認 GPU 與供應商容量。

管理員可登入後查詢 `/agents/capacity` 查看各流程上限及使用量；端點不回傳帳號、指令或文件內容。服務更新應在執行、排隊及自動帳號處理數量皆為零時進行。
