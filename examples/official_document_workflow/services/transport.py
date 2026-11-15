"""ModelTransport responsibilities for the shared document processor."""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlsplit

import httpx

from examples.shared.runtime.concurrency import model_request
from examples.shared.runtime.usage import request_metadata
from examples.official_document_workflow.application.prompts import (
    INSTRUCTION_PLANNER_PROMPT,
    PENDING_REMINDER_RESOLUTION_PROMPT,
)
from examples.official_document_workflow.domain.documents import (
    CloudDocumentExtraction,
    CloudExtractionError,
    CloudRateLimited,
    CloudSemanticSource,
    CloudTemporaryFailure,
    ProcessingError,
)
from examples.official_document_workflow.infrastructure.storage.processing_cache import (
    active_requests,
)

logger = logging.getLogger(__name__)


from .components import ConfiguredService


class ModelTransport(ConfiguredService):
    def __init__(self, request, parser, emit_status):
        super().__init__(request)
        self.parser, self.emit_status = parser, emit_status

    def content_identity(self):
        """Semantic settings for reusable service-key evidence selection."""
        return (
            self._official_document_litellm_connection()[0],
            self.valves.cloud_ocr_text_model,
            self.valves.cloud_ocr_text_reasoning_effort,
            self.valves.cloud_ocr_text_max_output_tokens,
        )

    @model_request
    async def _cloud_instruction_plan_payload(self, current_message: str) -> dict[str, Any]:
        """Call the approved service key for a strictly structured JobPlan.

        This is deliberately a small text-only request.  The user message is
        the complete input: no previous chat content, document text, file IDs,
        user identity, or EIP credentials are sent to the model.
        """
        try:
            base_url, api_key = self._official_document_litellm_connection()
            request_body = {
                'model': self.valves.instruction_plan_model,
                'litellm_metadata': request_metadata(),
                'input': [
                    {
                        'role': 'developer',
                        'content': [
                            {'type': 'input_text', 'text': INSTRUCTION_PLANNER_PROMPT},
                        ],
                    },
                    {
                        'role': 'user',
                        'content': [
                            {
                                'type': 'input_text',
                                'text': (f'<current-user-instruction>\n{current_message}\n</current-user-instruction>'),
                            },
                        ],
                    },
                ],
                'temperature': 0,
                'max_output_tokens': self.valves.instruction_plan_max_output_tokens,
                'text': {'format': {'type': 'json_object'}},
                'store': False,
            }
            timeout = httpx.Timeout(self.valves.instruction_plan_timeout_seconds, connect=10.0)
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    f'{base_url}/responses',
                    json=request_body,
                    headers={'Authorization': f'Bearer {api_key}'},
                )
            if response.status_code != 200:
                raise ValueError(
                    f'instruction planner returned HTTP {response.status_code}',
                )
            return self.parser.values._cloud_json_object(self.parser.values._responses_output_text(response.json()))
        except ValueError:
            raise
        except (CloudExtractionError, httpx.HTTPError, ValueError, TypeError, OSError) as exc:
            # Never log the user's raw instruction, model payload, or service
            # key.  A failed planner must not silently apply a default plan.
            logger.info('Controlled instruction planner failed: %s', type(exc).__name__)
            raise ValueError('instruction planner unavailable') from exc

    @model_request
    async def _cloud_pending_reminder_choice_payload(
        self,
        current_message: str,
        deadline_options: tuple[str, ...],
    ) -> dict[str, Any]:
        """Call the approved text model with no document/OCR/chat history."""

        option_text = '\n'.join(f'{index}: {date_value}' for index, date_value in enumerate(deadline_options))
        try:
            base_url, api_key = self._official_document_litellm_connection()
            request_body = {
                'model': self.valves.instruction_plan_model,
                'litellm_metadata': request_metadata(),
                'input': [
                    {
                        'role': 'developer',
                        'content': [
                            {
                                'type': 'input_text',
                                'text': (
                                    f'{PENDING_REMINDER_RESOLUTION_PROMPT}\n'
                                    '<verified-deadline-options>\n'
                                    f'{option_text}\n'
                                    '</verified-deadline-options>'
                                ),
                            },
                        ],
                    },
                    {
                        'role': 'user',
                        'content': [
                            {
                                'type': 'input_text',
                                'text': (f'<current-user-answer>\n{current_message}\n</current-user-answer>'),
                            },
                        ],
                    },
                ],
                'temperature': 0,
                'max_output_tokens': self.valves.instruction_plan_max_output_tokens,
                'text': {'format': {'type': 'json_object'}},
                'store': False,
            }
            timeout = httpx.Timeout(self.valves.instruction_plan_timeout_seconds, connect=10.0)
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    f'{base_url}/responses',
                    json=request_body,
                    headers={'Authorization': f'Bearer {api_key}'},
                )
            if response.status_code != 200:
                raise ValueError(
                    f'pending reminder resolver returned HTTP {response.status_code}',
                )
            return self.parser.values._cloud_json_object(self.parser.values._responses_output_text(response.json()))
        except ValueError:
            raise
        except (CloudExtractionError, httpx.HTTPError, ValueError, TypeError, OSError) as exc:
            logger.info('Pending reminder resolver failed: %s', type(exc).__name__)
            raise ValueError('pending reminder resolver unavailable') from exc

    @staticmethod
    def _official_document_litellm_connection() -> tuple[str, str]:
        """Return only the dedicated loopback service key for this Pipe.

        This intentionally never falls back to ``OPENAI_API_KEYS``.  Those
        general Open WebUI keys are used for interactive user traffic and may
        be subject to a per-user budget.  Failing closed here prevents an
        environment mistake from silently charging a document request to the
        uploading user's daily allowance.
        """
        base_url = (
            os.getenv(
                'OFFICIAL_DOCUMENT_LITELLM_BASE_URL',
                'http://127.0.0.1:4000/v1',
            )
            .strip()
            .rstrip('/')
        )
        api_key = os.getenv('OFFICIAL_DOCUMENT_LITELLM_API_KEY', '').strip()
        try:
            parsed = urlsplit(base_url)
            port = parsed.port
        except ValueError as exc:
            raise CloudExtractionError('invalid official-document LiteLLM base URL') from exc

        if not (
            parsed.scheme == 'http'
            and parsed.hostname == '127.0.0.1'
            and port == 4000
            and parsed.path == '/v1'
            and not parsed.query
            and not parsed.fragment
            and not parsed.username
            and not parsed.password
            and api_key
        ):
            raise CloudExtractionError('official-document LiteLLM service key is unavailable')
        return base_url, api_key

    async def _cloud_ocr_text_document_extraction(
        self,
        source: CloudSemanticSource,
    ) -> CloudDocumentExtraction:
        from examples.official_document_workflow.domain.calendar.contracts import (
            SINGLE_ANALYSIS_SCHEMA,
        )

        payload = await self.analyze_text(source, schema=SINGLE_ANALYSIS_SCHEMA)
        try:
            return self.parser._parse_cloud_ocr_text_payload(payload, self.valves.cloud_ocr_text_model, source)
        except (CloudExtractionError, ValueError, TypeError) as exc:
            logger.info(
                'Required text cloud extraction failed: %s',
                str(exc) if isinstance(exc, CloudExtractionError) else type(exc).__name__,
            )
            raise ProcessingError('雲端公文欄位判讀未完成') from exc

    @model_request
    async def analyze_text(
        self,
        source: CloudSemanticSource,
        *,
        prompt: str | None = None,
        schema: dict[str, Any] | None = None,
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Run the one required cloud extraction against complete local text.

        This intentionally sends neither the source PDF nor a partial OCR
        prefix.  The local source carries page/block layout boundaries so the
        model can distinguish an A4 visual wrap from a genuine section break,
        while bypassing the provider's expensive PDF/vision ingestion.  The
        model remains the sole authority for document semantics; this method
        accepts results only after literal evidence is found in ``source``.
        """
        model = self.valves.cloud_ocr_text_model
        try:
            from examples.official_document_workflow.domain.calendar.contracts import (
                require_tentative_schedule_decision,
            )

            if schema is not None:
                schema = require_tentative_schedule_decision(schema)
            base_url, api_key = self._official_document_litellm_connection()
            request_body = {
                'model': model,
                'litellm_metadata': request_metadata(),
                'input': [
                    {
                        'role': 'developer',
                        'content': [
                            {
                                'type': 'input_text',
                                'text': prompt
                                if prompt is not None
                                else self.parser.values._cloud_ocr_text_prompt(source.kind),
                            },
                        ],
                    },
                    {
                        'role': 'user',
                        'content': [
                            {
                                'type': 'input_text',
                                'text': (
                                    f'<untrusted-local-document-text source="{source.kind}">\n'
                                    f'{source.text}\n'
                                    '</untrusted-local-document-text>'
                                ),
                            },
                        ],
                    },
                ],
                'temperature': 0,
                'max_output_tokens': max_output_tokens or self.valves.cloud_ocr_text_max_output_tokens,
                'text': {'format': {'type': 'json_object'}},
                # The source is already stored in Open WebUI's file store;
                # do not create a second provider-side retained conversation.
                'store': False,
            }
            if model.startswith('gemini-'):
                request_body['reasoning'] = {'effort': self.valves.cloud_ocr_text_reasoning_effort}
                if schema is not None:
                    request_body['text']['format'] = {
                        'type': 'json_schema',
                        'name': 'document_result',
                        'schema': schema,
                        'strict': True,
                    }
            cache = active_requests.get()
            if cache is not None:
                cache_key = cache.key('model', base_url, schema, request_body)
                cached = await cache.get(cache_key)
                if cached is not None:
                    await self.emit_status(cache.emitter, '沿用相同文件與設定的判讀結果，正在核對原文證據。')
                    return cached
            timeout = httpx.Timeout(self.valves.cloud_ocr_text_timeout_seconds, connect=10.0)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
                for attempt in range(2):
                    response = await client.post(
                        f'{base_url}/responses',
                        json=request_body,
                        headers={'Authorization': f'Bearer {api_key}'},
                    )
                    if response.status_code == 429:
                        raise CloudRateLimited(
                            '雲端模型達到速率或額度限制（HTTP 429）；本次判讀未完成，未反覆重送或據此匯入行事曆。'
                        )
                    if response.status_code in (408, 500, 502, 503, 504):
                        raise CloudTemporaryFailure('雲端模型服務暫時無法完成請求；來源保留，未據此產生行程。')
                    if response.status_code != 200:
                        raise CloudExtractionError(f'LiteLLM returned HTTP {response.status_code}')
                    response_body = response.json()
                    if response_body.get('status') == 'incomplete':
                        reason = (response_body.get('incomplete_details') or {}).get('reason')
                        if (
                            max_output_tokens is None
                            and reason == 'max_output_tokens'
                            and attempt == 0
                            and request_body['max_output_tokens'] < 6_000
                        ):
                            # Retry only confirmed output truncation, once. Never
                            # parse a partial JSON or supply it as source evidence.
                            request_body['max_output_tokens'] = min(
                                request_body['max_output_tokens'] * 2,
                                6_000,
                            )
                            continue
                        raise ProcessingError('雲端回應未完整產生；已保留來源，未以截斷內容產生或匯入行程。')
                    result = self.parser.values._cloud_json_object(
                        self.parser.values._responses_output_text(response_body)
                    )
                    if cache is not None:
                        cache.put(cache_key, result)
                    return result
        except ProcessingError:
            raise
        except httpx.TransportError as exc:
            raise CloudTemporaryFailure('雲端模型連線中斷或逾時；來源保留，未據此產生行程。') from exc
        except (CloudExtractionError, httpx.HTTPError, ValueError, TypeError, OSError) as exc:
            # Never log source text, API key, request body, or raw provider
            # output. There is deliberately no alternate semantic path.
            if isinstance(exc, CloudExtractionError):
                logger.info('Required text cloud extraction failed: %s', str(exc))
            else:
                logger.info('Required text cloud extraction failed: %s', type(exc).__name__)
            raise ProcessingError('雲端公文欄位判讀未完成') from exc
