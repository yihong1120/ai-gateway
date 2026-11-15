"""Read-only verification of a receipt against the authenticated WebUI file API."""

from __future__ import annotations

import httpx
from urllib.parse import quote
from dataclasses import dataclass
from .output_verification import ContentProof, OutputMismatch, verify_metadata


@dataclass(frozen=True)
class OutputVerification:
    outcome: str
    actor: str
    proof: dict


class WebUIOutputVerifier:
    def __init__(self, client):
        self.client = client

    async def verify(self, receipt):
        auth = await self.client.get('/api/v1/auths/')
        auth.raise_for_status()
        principal = auth.json()
        if principal.get('role') != 'admin' or not principal.get('id'):
            raise PermissionError('WebUI administrator authentication is required.')
        actor = str(principal['id'])
        try:
            return await self._verify_file(receipt, actor)
        except (httpx.HTTPError, ValueError, TypeError):
            return OutputVerification(
                'unavailable', actor, {'reason': 'webui_read_failed'}
            )

    async def _verify_file(self, receipt, actor):
        file_path = '/api/v1/files/' + quote(receipt.id, safe='')
        response = await self.client.get(file_path)
        if response.status_code == 404:
            return OutputVerification('not_found', actor, {'reason': 'file_not_found'})
        response.raise_for_status()
        try:
            verify_metadata(receipt, response.json())
            proof = ContentProof(receipt)
            async with self.client.stream(
                'GET', file_path + '/content?attachment=true'
            ) as content:
                content.raise_for_status()
                async for block in content.aiter_bytes(64 * 1024):
                    proof.update(block)
            return OutputVerification('verified', actor, proof.finish())
        except OutputMismatch as error:
            return OutputVerification(
                'mismatch', actor, {'reason': error.reason, **error.proof}
            )
