"""Discover and invoke read-only MCP tools without knowing their business domain."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone

import httpx2
from jsonschema import Draft202012Validator

from company_agent.services.external_tools.config import MCPServerConfig

MAX_BYTES = 1_048_576
MAX_SCHEMA_BYTES = 65_536
MAX_CATALOG_TOOLS = 100
MAX_CATALOG_PAGES = 10
TIMEOUT_SECONDS = 20
RESERVED_ARGUMENTS = frozenset({'runtime', 'specification'})


class ExternalToolUnavailable(ValueError):
    """A safe, actionable boundary error with no upstream bodies or credentials."""


@dataclass(frozen=True)
class ToolSpec:
    server: MCPServerConfig
    name: str
    description: str
    input_schema: dict
    fingerprint: str

    @property
    def runtime_name(self):
        prefix = 'external_' + self.server.name + '_'
        normalized = re.sub(r'[^A-Za-z0-9_-]', '_', self.name)
        suffix = '' if normalized == self.name else '_' + hashlib.sha256(self.name.encode()).hexdigest()[:8]
        if len(prefix + normalized + suffix) > 64:
            suffix = '_' + hashlib.sha256(self.name.encode()).hexdigest()[:8]
            normalized = normalized[:64 - len(prefix) - len(suffix)]
        return prefix + normalized + suffix


def runtime_name(spec):
    return spec.runtime_name


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def _plain(value):
    return value.model_dump(by_alias=True, mode='json', exclude_none=True) if hasattr(value, 'model_dump') else value


def _safe_failure(error):
    if isinstance(error, ExternalToolUnavailable):
        return str(error)
    for child in getattr(error, 'exceptions', ()):
        safe = _safe_failure(child)
        if safe is not None:
            return safe
    return None


def _check_schema(schema):
    if (
        not isinstance(schema, dict) or schema.get('type') != 'object'
        or not isinstance(schema.get('properties', {}), dict)
        or RESERVED_ARGUMENTS.intersection(schema.get('properties', {}))
        or len(_json_bytes(schema)) > MAX_SCHEMA_BYTES
    ):
        raise ExternalToolUnavailable('外部工具的輸入契約無效或超過上限。')
    pending = [schema]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if '$id' in item or any(
                key in item and (not isinstance(item[key], str) or not item[key].startswith('#'))
                for key in ('$ref', '$dynamicRef')
            ):
                raise ExternalToolUnavailable('外部工具不能使用遠端 schema 參照。')
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    Draft202012Validator.check_schema(schema)


class ExternalTools:
    def __init__(self, servers, tokens, *, session_factory=None):
        self.servers = tuple(MCPServerConfig.model_validate(server) for server in servers)
        if len(self.servers) > 10 or len({server.name for server in self.servers}) != len(self.servers):
            raise ValueError('外部工具來源須唯一，最多十個。')
        self.tokens = tokens
        self.session_factory = session_factory or _sdk_session

    @asynccontextmanager
    async def _session(self, server, owner, subject):
        try:
            if not isinstance(owner, str) or not owner or not isinstance(subject, str) or not subject:
                raise ExternalToolUnavailable('外部工具需要已驗證的個人身分。')
            token = await self.tokens(owner, subject)
            if not isinstance(token, str) or not token or any(not 33 <= ord(char) <= 126 for char in token):
                raise ExternalToolUnavailable('外部工具需要本人有效的 OIDC 登入。')
            async with asyncio.timeout(TIMEOUT_SECONDS):
                async with self.session_factory(server.url, token) as session:
                    # The modern SDK discovery is explicit. No legacy fallback,
                    # replay, sampling, elicitation or server-requested callbacks.
                    await session.discover()
                    yield session, token
        except Exception as error:
            raise ExternalToolUnavailable(
                _safe_failure(error) or '外部工具連線、身分或服務暫時無法使用；未自動重試。',
            ) from None

    async def _catalog(self, session, server, token):
        tools = {}
        cursor = None
        seen_cursors = set()
        received_bytes = 0
        for _ in range(MAX_CATALOG_PAGES):
            if cursor is None:
                page = await session.list_tools()
            else:
                from mcp.types import PaginatedRequestParams

                page = await session.list_tools(params=PaginatedRequestParams(cursor=cursor))
            value = _plain(page)
            encoded = _json_bytes(value)
            received_bytes += len(encoded)
            if received_bytes > MAX_BYTES or token.encode() in encoded:
                raise ExternalToolUnavailable('外部工具目錄超過上限或含不應公開的憑證。')
            if not isinstance(value, dict) or not isinstance(value.get('tools'), list):
                raise ExternalToolUnavailable('外部工具目錄格式無效。')
            for raw in value['tools']:
                tool = _plain(raw)
                if not isinstance(tool, dict) or not isinstance(tool.get('name'), str):
                    raise ExternalToolUnavailable('外部工具目錄格式無效。')
                name = tool['name']
                if name in tools or len(tools) >= MAX_CATALOG_TOOLS:
                    raise ExternalToolUnavailable('外部工具目錄名稱重複或超過上限。')
                tools[name] = tool
            cursor = value.get('nextCursor')
            if cursor is None:
                break
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2000 or cursor in seen_cursors:
                raise ExternalToolUnavailable('外部工具目錄分頁無效。')
            seen_cursors.add(cursor)
        else:
            raise ExternalToolUnavailable('外部工具目錄超過分頁上限。')
        if not set(server.allowed_tools) <= set(tools):
            raise ExternalToolUnavailable('設定的外部工具未全部提供，未啟用此來源。')
        specs = []
        for name in server.allowed_tools:
            tool = tools[name]
            annotations = tool.get('annotations')
            if (
                not isinstance(annotations, dict)
                or annotations.get('readOnlyHint') is not True
                or annotations.get('destructiveHint') is not False
            ):
                raise ExternalToolUnavailable('外部工具必須明確宣告唯讀且不具破壞性。')
            schema = tool.get('inputSchema')
            _check_schema(schema)
            description = tool.get('description') or name
            if not isinstance(description, str) or len(description) > 8000:
                raise ExternalToolUnavailable('外部工具描述無效或超過上限。')
            fingerprint = hashlib.sha256(
                _json_bytes({
                    'server': server.model_dump(),
                    'name': name, 'description': description, 'inputSchema': schema,
                    'outputSchema': tool.get('outputSchema'), 'annotations': annotations,
                }),
            ).hexdigest()
            specs.append(ToolSpec(server, name, description, schema, fingerprint))
        return specs

    async def discover(self, owner, subject):
        specs = []
        for server in self.servers:
            async with self._session(server, owner, subject) as (session, token):
                specs.extend(await self._catalog(session, server, token))
        if len(specs) > MAX_CATALOG_TOOLS or len(
            _json_bytes([
                {'name': spec.runtime_name, 'description': spec.description, 'schema': spec.input_schema}
                for spec in specs
            ]),
        ) > MAX_BYTES:
            raise ExternalToolUnavailable('外部工具的總目錄超過上限。')
        if len({spec.runtime_name for spec in specs}) != len(specs):
            raise ExternalToolUnavailable('外部工具的執行名稱重複。')
        return specs

    async def invoke(self, owner, subject, spec, args):
        server = next((item for item in self.servers if item == spec.server), None)
        if server is None or spec.name not in server.allowed_tools:
            raise ExternalToolUnavailable('此外部工具未經設定允許。')
        try:
            if not isinstance(args, dict) or len(_json_bytes(args)) > MAX_SCHEMA_BYTES or RESERVED_ARGUMENTS.intersection(args):
                raise ValueError()
            Draft202012Validator(spec.input_schema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(args)
        except Exception:
            raise ExternalToolUnavailable('外部工具參數不符合伺服器提供的契約。') from None
        async with self._session(server, owner, subject) as (session, token):
            catalog = await self._catalog(session, server, token)
            current = next(item for item in catalog if item.name == spec.name)
            if current.fingerprint != spec.fingerprint:
                raise ExternalToolUnavailable('外部工具契約已變更，請另開工作。')
            value = _plain(await session.call_tool(spec.name, args, read_timeout_seconds=TIMEOUT_SECONDS))
            if not isinstance(value, dict) or value.get('isError', False) is not False or value.get('resultType', 'complete') != 'complete':
                raise ExternalToolUnavailable('外部工具未完成查詢；請確認來源或個人存取權限。')
            encoded = _json_bytes(value)
            if len(encoded) > MAX_BYTES or token.encode() in encoded:
                raise ExternalToolUnavailable('外部工具結果超過上限或含不應公開的憑證。')
            if value.get('structuredContent') is not None:
                result = value['structuredContent']
            else:
                content = value.get('content')
                if not isinstance(content, list) or not content or any(
                    not isinstance(item, dict) or item.get('type') != 'text' or not isinstance(item.get('text'), str)
                    for item in content
                ):
                    raise ExternalToolUnavailable('外部工具未提供可用的文字或結構化結果。')
                result = '\n'.join(item['text'] for item in content)
            return {
                'server': server.name, 'tool': spec.name,
                'queried_at': datetime.now(timezone.utc).isoformat(),
                'result': result,
            }


@asynccontextmanager
async def _sdk_session(url, token):
    # SDK imports remain optional when no external server is configured.
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(
        transport=BoundedTransport(), timeout=TIMEOUT_SECONDS,
        headers={'Authorization': 'Bearer ' + token, 'Accept-Encoding': 'identity'},
        trust_env=False, follow_redirects=False,
    ) as http:
        async with streamable_http_client(url, http_client=http, terminate_on_close=False, max_sse_event_size=MAX_BYTES) as streams:
            async with ClientSession(*streams, read_timeout_seconds=TIMEOUT_SECONDS) as session:
                yield session


class BoundedStream(httpx2.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        size = 0
        async for chunk in self.stream:
            size += len(chunk)
            if size > MAX_BYTES:
                raise ExternalToolUnavailable('外部工具回應超過讀取上限。')
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class BoundedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, transport=None):
        self.transport = transport if transport is not None else httpx2.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        response = await self.transport.handle_async_request(request)
        if 300 <= response.status_code < 400 or response.headers.get('content-encoding', 'identity') != 'identity':
            await response.aclose()
            raise ExternalToolUnavailable('外部工具不允許轉址或壓縮回應。')
        if response.status_code in {401, 403}:
            await response.aclose()
            raise ExternalToolUnavailable('外部工具需要本人有效登入與資料權限。')
        return httpx2.Response(
            response.status_code, headers=response.headers,
            stream=BoundedStream(response.stream), extensions=response.extensions,
        )

    async def aclose(self):
        await self.transport.aclose()
