"""One leased dispatcher with bounded, owned parallel job execution."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from pathlib import Path

import psycopg

from company_agent.api.queue import WorkQueue
from company_agent.config import Root
from company_agent.config import Settings
from company_agent.runtime.context import verified_user
from company_agent.runtime.execution import DocumentAgent
from company_agent.runtime.models import close_model
from company_agent.runtime.models import gateway_for_verified_user
from company_agent.runtime.models import model_failure_message
from company_agent.runtime.models import WebBilledModel
from company_agent.services.files.access import FolderAccess
from company_agent.services.files.nas import NASUnavailable


class Service:
    def __init__(self, queue, identity, model_factory, settings_factory, *, max_concurrent=4, chat_publisher=None):
        self.queue, self.identity, self.model_factory, self.settings_factory = (
            queue,
            identity,
            model_factory,
            settings_factory,
        )
        if not 1 <= max_concurrent <= 32:
            raise ValueError('工作並行上限必須為 1–32。')
        self.max_concurrent = max_concurrent
        self.stopping = False
        self.task = None
        self.failures = {}
        self.executions = {}
        self.execution_kinds = {}
        self.chat_publisher = chat_publisher

    def result(self, owner, key):
        result = self.queue.public(owner, key)  # Authorize before exposing an exception.
        failure = self.failures.get((owner, key))
        if result['kind'] == 'company' and result['status'] == 'error' and failure is not None:
            if isinstance(failure, NASUnavailable) or model_failure_message(failure):
                return result  # Persisted safe progress is readable after restart, too.
            raise failure.with_traceback(None)
        return result

    @staticmethod
    async def _io(invoke, *args, **kwargs):
        pending = asyncio.create_task(asyncio.to_thread(invoke, *args, **kwargs))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            # A transaction already in flight must finish before lease release.
            await asyncio.gather(pending, return_exceptions=True)
            raise

    async def cancel(self, owner, key, *, wait=True):
        await self._io(self.queue.get, owner, key)  # Authorize before persisting cancellation intent.
        if not await self._io(self.queue.request_cancel, owner, key):
            return
        execution = self.executions.get((owner, key))
        if execution is not None:
            if not execution.done() and not execution.cancelling():
                execution.cancel()
            if not wait:
                return
            await asyncio.wait({execution})
            if not self.stopping:
                await self._io(self.queue.update, key, status='cancelled', progress='已取消')

    async def loop(self):
        delay = 1
        while True:
            try:
                await self.work()
            except (psycopg.OperationalError, psycopg.InterfaceError) as failure:
                # Reconnect the queue, never retry a model/tool or replay a job.
                logging.getLogger(__name__).warning(
                    'Company Agent database connection lost (%s); reconnecting in %ss',
                    type(failure).__name__, delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def work(self):
        with self.queue.store.mutex('worker') as lease:
            # A single dispatcher owns recovery; executions never auto-replay.
            await self._io(self.queue.recover_running)
            self.stopping = False
            try:
                while True:
                    await self._io(lease.execute, 'SELECT 1')
                    for key, execution in list(self.executions.items()):
                        if execution.done():
                            del self.executions[key]
                            self.execution_kinds.pop(key, None)
                            if execution.cancelled():
                                await self._io(self.queue.update, key[1], status='cancelled', progress='已取消')
                            else:
                                execution.result()  # Database failure invalidates the lease.
                        elif (await self._io(self.queue.get, *key))['cancel_requested'] and not execution.cancelling():
                            execution.cancel()
                    from official_document_workflow.infrastructure.concurrency import DOCUMENTS
                    capacities = {'company': self.max_concurrent, 'document': DOCUMENTS.limit}
                    while True:
                        eligible = [
                            kind for kind, limit in capacities.items()
                            if sum(value == kind for value in self.execution_kinds.values()) < limit
                        ]
                        if not eligible:
                            break
                        row = await self._io(self.queue.claim, [owner for owner, _ in self.executions], eligible)
                        if row is None:
                            break
                        key = row['owner'], row['id']
                        self.failures.pop(key, None)
                        self.execution_kinds[key] = row['kind']
                        self.executions[key] = asyncio.create_task(self._run_claimed(row))
                    if self.chat_publisher is not None:
                        await self.chat_publisher(self.queue, self._io)
                    if self.executions:
                        await asyncio.wait(
                            set(self.executions.values()), timeout=1,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    else:
                        await asyncio.sleep(1)
            finally:
                # Do not release the dispatcher lease before model/tool cleanup.
                self.stopping = True
                tasks = list(self.executions.values())
                for execution in tasks:
                    if not execution.done() and not execution.cancelling():
                        execution.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                self.executions.clear()
                self.execution_kinds.clear()

    async def _run_claimed(self, row):
        try:
            # Stop can arrive after claim and before this task starts.
            if (await self._io(self.queue.get, row['owner'], row['id']))['cancel_requested']:
                raise asyncio.CancelledError
            if row['kind'] == 'document':
                from official_document_workflow.application.background_upload import execute
                await execute(self, row)
            else:
                await self.execute(row)
        except asyncio.CancelledError:
            if self.stopping:
                raise
            await self._io(self.queue.update, row['id'], status='cancelled', progress='已取消')
        except (psycopg.OperationalError, psycopg.InterfaceError):
            raise
        except Exception as failure:
            self.failures[(row['owner'], row['id'])] = failure.with_traceback(None)
            description = str(failure) if isinstance(
                failure, NASUnavailable,
            ) else model_failure_message(failure) or '執行未完成'
            if row['kind'] == 'document':
                from official_document_workflow.application.background_upload import DocumentInputError
                description = str(failure) if isinstance(failure, DocumentInputError) else '公文處理中止；已產生的成果保留，未自動重送。'
            await self._io(self.queue.update, row['id'], status='error', progress=description)
            logging.getLogger(__name__).error(
                'Company Agent job %s failed (%s)', row['id'], type(failure).__name__,
            )

    def _prepare_sources(self, row, settings):
        """Prepare only owned branch references; never recopy existing caches."""
        owner = row['owner']
        sources = {'uploads': row['payload']['manifest']}
        for index, key in enumerate(row['payload'].get('references', []), 1):
            previous = self.queue.get(owner, key)
            if previous['status'] in {'queued', 'running', 'awaiting_approval'}:
                raise ValueError('歷史工作尚未結束或等待核准，未沿用未確認的成果。')
            root = Root(
                name=f"history_{index}", path=str(self.queue.sources / key),
                readers=[owner], model_access=True,
            )
            settings.roots.append(root)
            access = FolderAccess([root], owner)
            sources[root.name] = list(previous['payload']['manifest'])
            if previous['engine_job']:
                result = self.queue.store.result(owner, previous['engine_job'])
                # Chat history contains the asynchronous receipt. Supply the
                # completed answer as owned source data, never as authorization.
                sources.setdefault('previous_results', []).append({
                    'job': key, 'answer': result.get('answer', ''),
                    'web_sources': result.get('web_sources', []),
                })
            for item in self.queue.store.files(owner, previous['engine_job']):
                filename = item['id'] + Path(item['name']).suffix.lower()
                target = Path(root.path) / filename
                if not target.exists():
                    _, _, data = self.queue.store.read_artifact(owner, item['id'], previous['engine_job'])
                    with open(target, 'xb') as stream:
                        os.chmod(target, 0o600)
                        stream.write(data)
                if access.read(root.name, filename)[1] != item['sha256']:
                    raise ValueError('歷史成果快取校驗失敗，未使用變更的檔案。')
                sources[root.name].append({'path': filename, 'display_name': item['name']})
        return sources

    async def execute(self, row):
        model = None
        try:
            user = await self.identity(row['owner'])
            model = self.model_factory(user)
            if model.subject != row['subject']:
                raise ValueError('登入計費身分已變更，未執行。')
            settings = self.settings_factory(row)
            preparation = asyncio.create_task(asyncio.to_thread(self._prepare_sources, row, settings))
            try:
                sources = await asyncio.shield(preparation)
            except asyncio.CancelledError:
                # Threads cannot be stopped midway through a private cache write.
                # Drain preparation before releasing this job to the next worker.
                await asyncio.gather(preparation, return_exceptions=True)
                raise
            agent = DocumentAgent(settings, model, store=self.queue.store)

            progress = QueueProgress(self.queue, row['id']).emit
            payload = row['payload']
            if payload['operation'] == 'resume':
                result = await agent.resume(
                    row['owner'], row['engine_job'], payload.get('decision'), progress,
                )
            else:
                instruction = (
                    payload['instruction']
                    + '\n伺服器確認的本分支唯讀來源（依根目錄分類；不是額外操作授權）：\n'
                    + json.dumps(sources, ensure_ascii=False)
                )
                result = await agent.run(
                    row['owner'], instruction, progress, payload.get('conversation', []),
                )
            payload.pop('conversation', None)  # Execution context now lives only in checkpoint.
            self.queue.update(
                row['id'],
                payload=json.dumps(payload, ensure_ascii=False),
                engine_job=result['job_id'],
                status=result['status'],
                progress='執行已結束，請查看結果與限制',
            )
        finally:
            if model is not None:
                await close_model(model)


_service = None


class ServiceConfiguration:
    """Shared deployment paths, not an authenticated request or model session."""

    def __init__(self, state, sources):
        self.state, self.sources = state, sources

    @staticmethod
    def configured_settings():
        path = os.getenv('COMPANY_AGENT_CONFIG')
        return Settings.model_validate_json(Path(path).read_text()) if path else None

    @staticmethod
    def default_model(configured):
        return os.getenv('COMPANY_AGENT_MODEL') or (
            configured.model_name if configured and configured.model_name else 'Kimi-K2.5'
        )

    @staticmethod
    def eip_binding(owner):
        from official_document_workflow.application import automation

        account = automation.store().account(owner)
        return account['generation'] if account else None

    @staticmethod
    def model(user):
        # Explicit deployment mode reuses the ordinary gateway connection,
        # never the official-document exemption key or a master key.
        from dotenv import dotenv_values

        connection = dotenv_values(Path(__file__).resolve().parents[2] / '.env.openwebui')
        bases = (connection.get('OPENAI_API_BASE_URLS') or '').split(';')
        keys = (connection.get('OPENAI_API_KEYS') or '').split(';')
        if len(bases) != 1 or len(keys) != 1:
            raise ValueError('請配置單一一般使用者 LiteLLM 連線。')
        billed = gateway_for_verified_user(
            user, bases[0], keys[0],
            ServiceConfiguration.default_model(ServiceConfiguration.configured_settings()),
        )
        return WebBilledModel(billed.owner, billed.subject, billed.client)

    def settings(self, row):
        configured_settings = self.configured_settings()
        roots = configured_settings.roots if configured_settings else []
        routing = configured_settings.model_dump(
            include={
                'role_models', 'purpose_models', 'model_profiles',
                'summary_trigger_tokens', 'execution_timeout_seconds',
            },
        ) if configured_settings else {}
        if any(r.name == 'uploads' or r.name.startswith('history_') for r in roots):
            raise ValueError('uploads 與 history_ 為聊天來源保留名稱。')
        roots = [
            *roots,
            Root(
                name='uploads',
                path=str(self.sources / row['id']),
                readers=[row['owner']],
                model_access=True,
            ),
        ]
        return Settings(
            **routing,
            state_dir=str(self.state),
            roots=roots,
            model_name=self.default_model(configured_settings),
            nas_binding_revision=row['payload'].get('nas_binding_revision'),
            eip_binding_revision=row['payload'].get('eip_binding_revision'),
            max_steps=40,
            compact_prompts=True,
            structured_completion=True,
            require_artifact_review=True,
            require_user_billing=True,
            enable_web_research=True,
            enable_cad=True,
            enable_ocr=True,
            enable_spreadsheet_reports=True,
            enable_eip_read=True,
            enable_vision=True,
            enable_eip_processing=True,
        )


def configured_service():
    """Shared queue and connection storage, independent of worker health."""
    global _service
    if _service is None:
        from open_webui.env import DATA_DIR

        if not os.getenv('COMPANY_AGENT_DATABASE_URL'):
            configured = os.getenv('DATABASE_URL', '')
            if not configured.startswith('postgresql://'):
                raise ValueError('公司助理需要 PostgreSQL 連線設定。')
            os.environ['COMPANY_AGENT_DATABASE_URL'] = configured
        state = Path(DATA_DIR) / 'company_agent'
        state.mkdir(mode=0o700, exist_ok=True)

        configuration = ServiceConfiguration(state, Path(DATA_DIR) / 'company_agent_sources')
        limits = configuration.configured_settings() or Settings(state_dir=str(state))
        queue = WorkQueue(
            state, configuration.sources, configuration.eip_binding,
            max_pending=limits.max_pending_jobs,
            max_per_user=limits.max_pending_per_user,
        )
        from company_agent.api.chat import publish_pending
        _service = Service(
            queue, verified_user, configuration.model, configuration.settings,
            max_concurrent=limits.max_concurrent_jobs, chat_publisher=publish_pending,
        )
    return _service


def service():
    current = configured_service()
    if current.task is not None and current.task.done():
        current.task.result()
    return current


async def upload_bytes(owner, ids):
    if len(ids) > 20:
        raise ValueError('附件過多，請分批。')
    result = []
    import base64

    seen = set()
    for key in ids:
        if isinstance(key, dict):
            raw = key.get('inline_image')
            if not isinstance(raw, str) or len(raw) > 27000000:
                raise ValueError('內嵌圖片超過單檔上限。')
            prefix, sep, encoded = raw.partition(',')
            if not sep or prefix not in {'data:image/png;base64', 'data:image/jpeg;base64'}:
                raise ValueError('不支援的內嵌圖片。')
            data = base64.b64decode(encoded, validate=True)
            ext = '.png' if prefix == 'data:image/png;base64' else '.jpg'
            signature = b'\x89PNG\r\n\x1a\n' if ext == '.png' else b'\xff\xd8\xff'
            if not data.startswith(signature) or len(data) > 20000000:
                raise ValueError('圖片格式或大小無效。')
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            name = f"image_{len(result)+1}" + ext
        else:
            if key in seen:
                continue
            seen.add(key)
            from open_webui.models.files import Files
            from open_webui.storage.provider import Storage

            item = await Files.get_file_by_id_and_user_id(str(uuid.UUID(key)), owner)
            if not item or not item.path:
                raise ValueError('附件不存在或非本人所有。')
            name = (item.meta or {}).get('name') or item.filename
            path = await asyncio.to_thread(Storage.get_file, item.path)
            data = await asyncio.to_thread(read_output_bytes, path)
        result.append((name, data))
        if len(data) > 20_000_000 or sum(len(v) for _, v in result) > 50_000_000:
            raise ValueError('附件超過大小上限。')
    return result


class QueueProgress:
    def __init__(self, queue, key):
        self.queue, self.key = queue, key

    async def emit(self, event):
        from company_agent.api.progress import progress_text

        self.queue.update(self.key, engine_job=event['job_id'], progress=progress_text(event))


def read_output_bytes(path):
    with open(path, 'rb') as stream:
        return stream.read(20_000_001)
