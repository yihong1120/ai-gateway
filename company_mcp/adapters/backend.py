"""Private resource authority mounted in the existing resource-owning backend."""
from __future__ import annotations

import asyncio
import json
import secrets

from pydantic import ValidationError
from starlette.responses import JSONResponse

from company_mcp.authentication import AuthorizationUnavailable
from company_mcp.authentication import principal
from company_mcp.backend import ASSERTION_HEADER
from company_mcp.backend import BackendRequest
from company_mcp.backend import Identity
from company_mcp.backend import MAX_BYTES
from company_mcp.backend import PROTOCOL
from company_mcp.storage import ResourceError
from company_mcp.tools import contract_digest
from company_mcp.tools import TOOLS
from examples.shared.runtime.io import run_sync


class ResourceAuthority:
    def __init__(self, configuration, verifier, resources_factory, *, ready=None):
        self.configuration, self.verifier = configuration, verifier
        self.resources_factory, self.ready = resources_factory, ready

    async def handle(self, request):
        configuration, verifier, resources_factory = self.configuration, self.verifier, self.resources_factory
        ready = self.ready
        # A browser cookie, forwarded owner/sub or service key alone can never
        # authorize a user operation. The original resource-bound assertion is
        # independently verified here before deriving its canonical owner.
        if (
            not request.client or request.client.host != '127.0.0.1' or request.url.query
            or 'origin' in request.headers or 'cookie' in request.headers
        ):
            return JSONResponse({'code': 'backend_access_denied'}, status_code=403)
        supplied = request.headers.get('authorization', '')
        expected = 'Bearer ' + configuration.backend_key
        if not configuration.backend_key or len(supplied) > 512 or not secrets.compare_digest(supplied.encode(), expected.encode()):
            return JSONResponse({'code': 'backend_access_denied'}, status_code=403)
        try:
            body = bytearray()
            async for part in request.stream():
                body.extend(part)
                if len(body) > MAX_BYTES:
                    return JSONResponse({'code': 'request_too_large'}, status_code=413)
            value = BackendRequest.model_validate(json.loads(body))
            if value.action == 'health':
                if ready is not None:
                    await ready()
                return JSONResponse({
                    'protocol': PROTOCOL, 'resource': configuration.resource,
                    'issuer': configuration.issuer, 'clients': list(configuration.clients), 'tools': contract_digest(),
                })
            assertion = request.headers.get(ASSERTION_HEADER, '')
            access = await verifier.verify_token(assertion)
            if access is None:
                return JSONResponse({'code': 'authentication_required'}, status_code=401)
            person = principal(access)
            if value.action == 'authorize':
                return JSONResponse(
                    Identity(
                        owner=person.owner, subject=person.subject, client=person.client,
                        expires=access.expires_at, issuer=configuration.issuer, resource=configuration.resource,
                    ).model_dump(),
                )
            resources = resources_factory()
            await resources.authorize(person)
            if value.action == 'grant':
                return JSONResponse({'authorized': True})
            if value.tool not in TOOLS:
                raise ResourceError('unknown_tool', '公司 MCP 不提供此工具。')
            if value.action == 'audit':
                await resources.audit(person, value.tool)
                return JSONResponse({'recorded': True})
            arguments = TOOLS[value.tool][0].model_validate(value.arguments)
            if value.action == 'execute':
                async with asyncio.timeout(120):
                    output = await resources.execute(person, value.tool, arguments)
                # Suppress bytes when the user logs out or a grant/source is
                # changed during a read, before they cross the process boundary.
                latest = await verifier.verify_token(assertion)
                if latest is None or principal(latest) != person:
                    raise ResourceError('authentication_required', '公司 MCP 授權已失效，未交付結果。')
                await resources.authorize(person)
                await resources.validate_result(person, value.tool, arguments, output)
                if len(json.dumps(output, ensure_ascii=False).encode()) > 128 * 1024:
                    raise ResourceError('result_too_large', '結果超過上限，請縮小查詢或使用分頁。')
                return JSONResponse(output)
            await resources.validate_result(person, value.tool, arguments, value.output)
            return JSONResponse({'valid': True})
        except ResourceError as failure:
            return JSONResponse({'code': failure.code, 'message': str(failure)}, status_code=409)
        except (ValidationError, ValueError, json.JSONDecodeError):
            return JSONResponse({'code': 'invalid_arguments', 'message': '工具參數格式或範圍不符。'}, status_code=409)
        except AuthorizationUnavailable:
            return JSONResponse({'code': 'authorization_unavailable'}, status_code=503)
        except Exception:
            return JSONResponse({'code': 'backend_unavailable'}, status_code=503)

    async def respond(self, request):
        response = await self.handle(request)
        response.headers.update({
            'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
            'X-Content-Type-Options': 'nosniff',
        })
        return response


def endpoint(configuration, verifier, resources_factory, *, ready=None):
    return ResourceAuthority(configuration, verifier, resources_factory, ready=ready).respond


def ping_store(store):
    with store.db() as db:
        db.execute('SELECT 1').fetchone()


async def check_store(store):
    await run_sync(ping_store, store)
