from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import BaseModel
from pydantic import Field


# --- Path keys that may need normalization (module-level constant) ---
PATH_KEYS: set[str] = {
    'filename',
    'file_path',
    'source_file',
    'target_file',
    'source_path',
    'target_path',
    'source_paths',
    'output_path',
    'pdf_path',
    'path',
    'document_path',
    'doc_path',
    'image_path',
    'image_paths',
    'file_path1',
    'file_path2',
}


# --- Constants ---

TOOL_ROUTER_PROMPT = """[WORD_MCP_TOOL_ROUTER_V2]
You have access to Word MCP tools for document operations. Use them ONLY when the user explicitly requests document creation/modification.

When to Use Tools:
- Use tools when user asks to:
  - Create a new document/report/letter
  - Modify/edit an existing document
  - Convert documents (Word to PDF, etc.)
  - Add content to documents (paragraphs, tables, images)

- Do NOT use tools when user:
  - Asks general questions or for information
  - Requests web searches or research
  - Wants explanations or advice
  - Has casual conversation

Path Rules (when using tools):
- Use Windows shared drive paths: X:\\filename.docx
- Download links: {file_server_url}/<filename>

IMPORTANT: Use the provided tools directly. Do not output XML or JSON in the content.
"""


# --- Helper functions (module-level for better memory usage) ---

def parse_arguments(args_raw: Any) -> dict[str, Any]:
    """Parse tool call arguments from various formats."""
    if args_raw is None:
        return {}
    if isinstance(args_raw, dict):
        return args_raw
    if isinstance(args_raw, str):
        s = args_raw.strip()
        if not s:
            return {}
        return json.loads(s)
    raise TypeError('Unsupported arguments type: ' + str(type(args_raw)))


def guess_output_filename(arguments: dict[str, Any]) -> str | None:
    """Extract output filename from arguments."""
    keys = ('filename', 'file_path', 'output_path', 'target_path', 'path', 'doc_path', 'document_path', 'pdf_path')
    for k in keys:
        v = arguments.get(k)
        if isinstance(v, str) and v.strip():
            # Extract filename from path
            name = v.replace('/', '\\').split('\\')[-1]
            return name
    return None


def normalize_path(path: str, windows_prefix: str, linux_prefix: str) -> str:
    """Normalize a file path to Windows format."""
    if not path or path.startswith('\\\\'):
        return path

    # Already has drive letter
    if len(path) >= 3 and path[1] == ':' and path[2] in ('\\', '/'):
        return path

    # Linux shared path => Windows
    linux_dir = linux_prefix.rstrip('/') + '/'
    if path.startswith(linux_dir):
        rel = path[len(linux_dir):].lstrip('/\\')
        return windows_prefix + rel

    # Relative path => put under share drive
    clean = path.lstrip('./\\')
    return windows_prefix + clean


def walk_and_fix_paths(obj: Any, windows_prefix: str, linux_prefix: str) -> None:
    """Recursively walk and fix file paths in dict/list structures."""
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if k in PATH_KEYS:
                if isinstance(v, str):
                    obj[k] = normalize_path(v, windows_prefix, linux_prefix)
                elif isinstance(v, list):
                    obj[k] = [
                        normalize_path(x, windows_prefix, linux_prefix) if isinstance(x, str) else x
                        for x in v
                    ]
            else:
                walk_and_fix_paths(v, windows_prefix, linux_prefix)
    elif isinstance(obj, list):
        for it in obj:
            walk_and_fix_paths(it, windows_prefix, linux_prefix)


def convert_mcp_tool_to_openai(mcp_tool: dict) -> dict:
    """Convert MCP tool format to OpenAI function calling format."""
    return {
        'type': 'function',
        'function': {
            'name': mcp_tool.get('name', ''),
            'description': mcp_tool.get('description', ''),
            'parameters': mcp_tool.get('inputSchema', {'type': 'object', 'properties': {}}),
        },
    }


def get_assistant_message(body: dict) -> dict | None:
    """Extract assistant message from response body (robust search)."""
    msgs = body.get('messages')
    if isinstance(msgs, list) and msgs:
        for msg in reversed(msgs):
            if not isinstance(msg, dict):
                continue
            if msg.get('role') == 'assistant' or 'tool_calls' in msg or 'function_call' in msg:
                return msg

    choices = body.get('choices')
    if isinstance(choices, list) and choices:
        msg = (choices[0] or {}).get('message')
        if isinstance(msg, dict):
            return msg
        delta = (choices[0] or {}).get('delta')
        if isinstance(delta, dict):
            return delta

    msg = body.get('message')
    if isinstance(msg, dict):
        return msg
    return None


def has_router_prompt(messages: list[dict]) -> bool:
    """Check if router prompt was already injected."""
    marker = '[WORD_MCP_TOOL_ROUTER_V2]'
    for msg in messages:
        if isinstance(msg, dict) and msg.get('role') == 'system':
            content = msg.get('content') or ''
            if marker in content:
                return True
    return False


def extract_tool_calls_from_body(body: dict, assistant_message: dict) -> list[dict]:
    """Extract tool_calls from multiple Open WebUI response shapes."""
    tool_calls = assistant_message.get('tool_calls')
    if not tool_calls:
        tool_calls = body.get('tool_calls')

    if not tool_calls:
        fn_call = assistant_message.get('function_call')
        if isinstance(fn_call, dict):
            tool_calls = [{'function': fn_call}]

    if not tool_calls:
        choices = body.get('choices')
        if isinstance(choices, list) and choices:
            choice = choices[0] or {}
            delta = choice.get('delta') or {}
            if isinstance(delta, dict) and delta.get('tool_calls'):
                tool_calls = delta.get('tool_calls')
            message = choice.get('message') or {}
            if not tool_calls and isinstance(message, dict) and message.get('tool_calls'):
                tool_calls = message.get('tool_calls')

    if not tool_calls:
        return []
    if isinstance(tool_calls, list):
        return tool_calls
    return [tool_calls]


async def post_json_rpc(url: str, timeout: int, payload: dict) -> dict:
    """Send a JSON-RPC request and return JSON response."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url,
            json=payload,
            headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
        )
        if resp.status_code != 200:
            raise RuntimeError('MCP HTTP ' + str(resp.status_code))

        try:
            return resp.json()
        except ValueError:
            try:
                return json.loads(resp.text)
            except json.JSONDecodeError:
                raise RuntimeError('MCP JSON response parse failed')


async def fetch_mcp_tools_sse(url: str, timeout: int, prefer_json: bool) -> list[dict]:
    """Fetch tools from MCP server via JSON or SSE stream."""
    req = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}

    last_error: Exception | None = None
    if prefer_json:
        try:
            data = await post_json_rpc(url, timeout, req)
            if 'result' in data:
                mcp_tools = (data.get('result') or {}).get('tools', [])
                return [convert_mcp_tool_to_openai(t) for t in mcp_tools]
            raise RuntimeError('MCP tools/list: no result in JSON response')
        except Exception as e:
            last_error = e

    async with httpx.AsyncClient(timeout=timeout) as client:
        # Try SSE first
        async with client.stream(
            'POST',
            url,
            json=req,
            headers={'Accept': 'text/event-stream', 'Content-Type': 'application/json'},
        ) as resp:
            if resp.status_code != 200:
                raise RuntimeError('MCP tools/list HTTP ' + str(resp.status_code))

            content_type = resp.headers.get('content-type', '')

            # Handle direct JSON response
            if 'application/json' in content_type:
                content = await resp.aread()
                data = json.loads(content)
                if 'result' in data:
                    mcp_tools = (data.get('result') or {}).get('tools', [])
                    return [convert_mcp_tool_to_openai(t) for t in mcp_tools]
                raise RuntimeError('MCP tools/list: no result in JSON response')

            # Handle SSE response
            data_lines: list[str] = []
            async for line in resp.aiter_lines():
                if not line:
                    if not data_lines:
                        continue
                    payload = '\n'.join(data_lines)
                    data_lines = []
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    if 'result' in data:
                        mcp_tools = (data.get('result') or {}).get('tools', [])
                        return [convert_mcp_tool_to_openai(t) for t in mcp_tools]
                    continue

                if line.startswith('data:'):
                    data_lines.append(line[5:].strip())

    if last_error is not None:
        raise RuntimeError('MCP tools/list: stream ended without result; last JSON error: ' + str(last_error))
    raise RuntimeError('MCP tools/list: stream ended without result')


async def call_mcp_tool_sse(
    url: str,
    timeout: int,
    tool_name: str,
    arguments: dict[str, Any],
    prefer_json: bool,
) -> dict:
    """Call MCP tool and return result via JSON or SSE stream."""
    req = {
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'tools/call',
        'params': {'name': tool_name, 'arguments': arguments},
    }

    last_error: Exception | None = None
    if prefer_json:
        try:
            data = await post_json_rpc(url, timeout, req)
            if 'error' in data:
                msg = (data['error'] or {}).get('message', 'Unknown error')
                raise RuntimeError(msg)
            if 'result' in data:
                return data.get('result', {})
            raise RuntimeError('MCP tools/call: no result in JSON response')
        except Exception as e:
            last_error = e

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            'POST',
            url,
            json=req,
            headers={'Accept': 'text/event-stream', 'Content-Type': 'application/json'},
        ) as resp:
            if resp.status_code != 200:
                raise RuntimeError('MCP tools/call HTTP ' + str(resp.status_code))

            content_type = resp.headers.get('content-type', '')

            # Handle direct JSON response
            if 'application/json' in content_type:
                content = await resp.aread()
                data = json.loads(content)
                if 'error' in data:
                    msg = (data['error'] or {}).get('message', 'Unknown error')
                    raise RuntimeError(msg)
                if 'result' in data:
                    return data.get('result', {})
                raise RuntimeError('MCP tools/call: no result in JSON response')

            # Handle SSE response
            data_lines: list[str] = []
            async for line in resp.aiter_lines():
                line = (line or '').strip()
                if not line:
                    if not data_lines:
                        continue
                    payload = '\n'.join(data_lines)
                    data_lines = []
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    if 'error' in data:
                        msg = (data['error'] or {}).get('message', 'Unknown error')
                        raise RuntimeError(msg)
                    if 'result' in data:
                        return data.get('result', {})
                    continue

                if line.startswith('data:'):
                    data_lines.append(line[5:].strip())
                elif line.startswith('{') and 'jsonrpc' in line:
                    # Tolerance: plain JSON response
                    try:
                        data = json.loads(line)
                        if 'error' in data:
                            msg = (data['error'] or {}).get('message', 'Unknown error')
                            raise RuntimeError(msg)
                        if 'result' in data:
                            return data.get('result', {})
                    except json.JSONDecodeError:
                        pass

    if last_error is not None:
        raise RuntimeError('MCP tools/call: stream ended without result; last JSON error: ' + str(last_error))
    raise RuntimeError('MCP tools/call: stream ended without result')


# --- Valves configuration class (outside Pipeline for clarity) ---

class PipelineValves(BaseModel):
    """Configuration valves for the Word MCP Pipeline."""

    MCP_SERVER_URL: str = Field(
        default='http://192.168.50.242:9000/mcp',
        description='Word MCP Server URL',
    )
    TIMEOUT: int = Field(default=60, description='HTTP timeout (seconds)')
    ENABLE_DEBUG: bool = Field(default=True, description='Enable debug logs')
    AUTO_INJECT_TOOLS: bool = Field(default=True, description='Auto inject tools')
    PREFER_JSON: bool = Field(default=True, description='Prefer JSON RPC over SSE')
    WINDOWS_PATH_PREFIX: str = Field(default='X:\\', description='Windows share drive prefix')
    LINUX_SHARE_DIR: str = Field(default='/mnt/data', description='Linux shared dir')
    FILE_SERVER_BASE_URL: str = Field(
        default='https://changdar-server.mooo.com/files',
        description='HTTP file server base URL',
    )
    KEEP_ORIGINAL_RESPONSE: bool = Field(
        default=True,
        description='Keep original assistant text and append tool results',
    )
    MAX_TOOL_CALLS_PER_TURN: int = Field(
        default=8,
        description='Max tool calls executed per turn',
    )
    STOP_ON_ERROR: bool = Field(
        default=True,
        description='Stop executing remaining tool calls after first error',
    )
    pipelines: list[str] = Field(default=['*'], description='Models this pipeline applies to')


# --- Main Pipeline class ---

class Pipeline:
    """
    Open WebUI filter pipeline for Word MCP integration.

    - on_startup: fetch tools from MCP (tools/list)
    - inlet: inject tools into request (tools + tool_choice=auto)
    - outlet: execute native assistant tool_calls only (no fallback parsing)
    """

    class Valves(PipelineValves):
        """Alias for Open WebUI compatibility."""
        pass

    def __init__(self):
        self.type = 'filter'
        self.name = 'Word MCP Smart (Native tool_calls)'
        self.valves = self.Valves()

        self.available_tools: list[dict] = []
        self.tools_loaded: bool = False

    async def on_startup(self) -> None:
        """Called when pipeline starts."""
        if self.valves.ENABLE_DEBUG:
            print('[Word MCP Pipeline] startup: fetching tools...')
        try:
            self.available_tools = await fetch_mcp_tools_sse(
                self.valves.MCP_SERVER_URL,
                self.valves.TIMEOUT,
                self.valves.PREFER_JSON,
            )
            self.tools_loaded = True
            if self.valves.ENABLE_DEBUG:
                tool_count = len(self.available_tools)
                print('[Word MCP Pipeline] tools loaded: ' + str(tool_count))
        except Exception as e:
            self.tools_loaded = False
            if self.valves.ENABLE_DEBUG:
                print('[Word MCP Pipeline] startup: failed to load tools: ' + str(e))

    async def on_shutdown(self) -> None:
        """Called when pipeline shuts down."""
        if self.valves.ENABLE_DEBUG:
            print('[Word MCP Pipeline] shutdown')

    async def inlet(self, body: dict, user: dict | None = None) -> dict:
        """Process incoming request - inject tools if needed."""
        # Reload tools if not loaded
        if not self.tools_loaded:
            try:
                self.available_tools = await fetch_mcp_tools_sse(
                    self.valves.MCP_SERVER_URL,
                    self.valves.TIMEOUT,
                    self.valves.PREFER_JSON,
                )
                self.tools_loaded = True
            except Exception as e:
                if self.valves.ENABLE_DEBUG:
                    print('[Word MCP Pipeline] inlet: tool reload failed: ' + str(e))

        # Inject tools into request
        if self.tools_loaded and self.valves.AUTO_INJECT_TOOLS and self.available_tools:
            tools = body.get('tools')
            if tools is None or (isinstance(tools, list) and not tools):
                body['tools'] = self.available_tools
            if body.get('tool_choice') is None:
                body['tool_choice'] = 'auto'

            # Inject System Prompt (avoid duplicates)
            messages = body.get('messages', [])
            base_url = (self.valves.FILE_SERVER_BASE_URL or '').rstrip('/')
            system_msg_content = TOOL_ROUTER_PROMPT.format(file_server_url=base_url)

            if isinstance(messages, list) and not has_router_prompt(messages):
                if messages and messages[0].get('role') == 'system':
                    messages[0]['content'] += '\n\n' + system_msg_content
                else:
                    messages.insert(0, {'role': 'system', 'content': system_msg_content})
                body['messages'] = messages

            if self.valves.ENABLE_DEBUG:
                tool_count = len(self.available_tools)
                print('[Word MCP Pipeline] inlet: ensured ' + str(tool_count) + ' tools & system prompt')

        return body

    async def outlet(self, body: dict, user: dict | None = None) -> dict:
        """Process outgoing response - execute tool calls if present."""
        assistant_message = get_assistant_message(body)
        if not assistant_message:
            return body

        tool_calls = extract_tool_calls_from_body(body, assistant_message)
        if not tool_calls:
            # No native tool_calls => do nothing (no fallback)
            return body

        if self.valves.ENABLE_DEBUG:
            tc_count = len(tool_calls)
            print('[Word MCP Pipeline] outlet: tool_calls detected: ' + str(tc_count))

        base_url = (self.valves.FILE_SERVER_BASE_URL or '').rstrip('/')
        executed_files: list[str] = []
        details: list[dict] = []

        max_calls = self.valves.MAX_TOOL_CALLS_PER_TURN
        for i, tc in enumerate(tool_calls[:max_calls], start=1):
            fn = tc.get('function') or {}
            name = fn.get('name')
            args_raw = fn.get('arguments')

            if not name:
                details.append({'idx': i, 'ok': False, 'error': 'missing function name'})
                continue

            try:
                arguments = parse_arguments(args_raw)
                walk_and_fix_paths(
                    arguments,
                    self.valves.WINDOWS_PATH_PREFIX,
                    self.valves.LINUX_SHARE_DIR,
                )

                out_file = guess_output_filename(arguments)
                if out_file:
                    executed_files.append(out_file)

                result = await call_mcp_tool_sse(
                    self.valves.MCP_SERVER_URL,
                    self.valves.TIMEOUT,
                    name,
                    arguments,
                    self.valves.PREFER_JSON,
                )
                details.append({'idx': i, 'ok': True, 'name': name, 'result': result})
            except Exception as e:
                details.append({'idx': i, 'ok': False, 'name': name, 'error': str(e)})
                if self.valves.STOP_ON_ERROR:
                    break

        # Dedupe filenames while preserving order
        seen: set = set()
        unique_files: list[str] = []
        for f in executed_files:
            if f and f not in seen:
                seen.add(f)
                unique_files.append(f)

        # Build user-facing output
        links_text = ''
        if unique_files and base_url:
            links = []
            for fn in unique_files:
                link = base_url + '/' + fn
                links.append('[' + fn + '](' + link + ')')
            links_text = '\n'.join(['📄 ' + lnk for lnk in links])

        # Keep original assistant response (optional), but must remove tool_calls
        original = assistant_message.get('content') or ''
        if self.valves.KEEP_ORIGINAL_RESPONSE:
            final = original.strip()
            if links_text:
                final = final + '\n\n---\n✅ **文件已生成**\n' + links_text
            else:
                final = final + '\n\n---\n✅ **操作已完成**'
            final = final.strip()
        else:
            if links_text:
                final = '✅ **文件已生成**\n' + links_text
            else:
                final = '✅ **操作已完成**'
            final = final.strip()

        # Append compact debug details (collapsible)
        if details:
            details_json = json.dumps(details, ensure_ascii=False, indent=2)
            final = final + '\n\n---\n<details>\n<summary>🔧 MCP 工具執行記錄</summary>\n\n```json\n'
            final = final + details_json
            final = final + '\n```\n</details>\n'

        assistant_message['content'] = final
        assistant_message.pop('tool_calls', None)
        return body
