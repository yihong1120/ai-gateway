# AI Gateway (LiteLLM Proxy)

This repository provides a unified LLM gateway service that:
1. Centrally manages models (Google, OpenAI, Ollama)
2. Exposes an OpenAI-compatible API (`http://localhost:4000/v1`)

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

## Integrating with the main application

Back in the main project (`Conversational-Llama-Integration`), set the following in `.env`:

```ini
LITELLM_BASE_URL=http://localhost:4000
LITELLM_API_KEY=         # The local gateway does not validate; can be left blank
LITELLM_MODEL=gemini-2.5-pro
```
