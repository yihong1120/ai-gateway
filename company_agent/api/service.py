"""One leased dispatcher with bounded, owned parallel job execution."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import uuid
from functools import partial
from pathlib import Path

import psycopg

import company_agent.api.progress as _progress_imports
import examples.official_document_workflow.infrastructure.storage.automation_store as _automation_store_imports
import examples.shared.config as _config_imports
import examples.shared.runtime.jobs as _jobs_imports
from company_agent.api.queue import WorkQueue
from company_agent.config import Root
from company_agent.config import Settings
from company_agent.runtime.execution import DocumentAgent
from company_agent.runtime.models import close_model
from company_agent.runtime.models import gateway_for_verified_user
from company_agent.runtime.models import model_failure_message
from company_agent.runtime.models import WebBilledModel
from company_agent.runtime.storage import Store
from company_agent.services.external_tools.client import ExternalToolUnavailable
from company_agent.services.files.access import FolderAccess
from company_agent.services.files.nas import NASUnavailable
from examples.identity.adapters.webui import verified_user
from examples.official_document_workflow.application.output_receipts import (
    DELETION_REVIEW_NOTICE,
)
from examples.shared.runtime.io import run_sync
from examples.shared.runtime.modules import ModuleReference
# Explicit deployment mode reuses the ordinary gateway connection,
# never the official-document exemption key or a master key.

_agent_chat = ModuleReference('company_agent.api.chat')
_agent_pipe = ModuleReference('company_agent.api.pipe')
_workflow_background_upload = ModuleReference('examples.official_document_workflow.application.background_upload')
_shared_concurrency = ModuleReference('examples.shared.runtime.concurrency')
_webui_env = ModuleReference('open_webui.env')
_webui_files = ModuleReference('open_webui.models.files')
_webui_provider = ModuleReference('open_webui.storage.provider')


class Service:
    def __init__(self, queue, identity, model_factory, settings_factory, *, max_concurrent=4, chat_publisher=None, document_outputs=None, mcp_servers=()):
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
        self.executions = {}
        self.execution_kinds = {}
        self.chat_publisher = chat_publisher
        self.document_outputs = document_outputs
        self.mcp_servers = mcp_servers
        self.external_tools = None
        self.job_handlers = {}

    def register_job_handler(self, kind, handler):
        """Register trusted host wiring; external jobs share company capacity."""
        if not isinstance(kind, str) or not kind or kind in {'company', 'document'} or not callable(handler):
            raise ValueError('需要獨立工作類型及可信執行器。')
        if kind in self.job_handlers:
            raise ValueError('工作類型已註冊。')
        self.job_handlers[kind] = handler

        return partial(self.unregister_job_handler, kind, handler)

    def unregister_job_handler(self, kind, handler):
        if self.job_handlers.get(kind) is handler:
            del self.job_handlers[kind]

    @staticmethod
    def render(result):
        markdown = _agent_pipe.attribute('markdown')

        return markdown(result)

    def result(self, owner, key):
        return self._with_document_outputs(owner, [self.queue.public(owner, key)])[0]

    def list(self, owner):
        return self._with_document_outputs(owner, self.queue.list(owner))

    def _with_document_outputs(self, owner, results):
        jobs = [item['id'] for item in results if item['kind'] == 'document']
        if jobs and self.document_outputs is not None:
            try:
                recovered = self.document_outputs(owner, jobs)
            except Exception as error:
                logging.getLogger(__name__).warning('Document catalog unavailable (%s)', type(error).__name__)
            else:
                for item in results:
                    rows = recovered.get(item['id'], [])
                    hidden = {f['id'] for f in rows if f.get('state') in {'deleting', 'delete_unknown', 'deleted'}}
                    files = {
                        f['id']: {k: v for k, v in f.items() if k != 'state'}
                        for f in rows if f['id'] not in hidden
                    }
                    files.update({f['id']: f for f in item['files'] if f['id'] not in hidden})
                    item['files'] = list(files.values())
                    if any(f.get('state') in {'deleting', 'delete_unknown'} for f in rows):
                        item.setdefault('notices', []).append(DELETION_REVIEW_NOTICE)
        return results

    _io = staticmethod(run_sync)

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
            maintenance = asyncio.create_task(self.maintain_sources())
            publisher = asyncio.create_task(self.publish_loop()) if self.chat_publisher is not None else None
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
                    DOCUMENTS = _shared_concurrency.attribute('DOCUMENTS')
                    capacities = {'company': self.max_concurrent, 'document': DOCUMENTS.limit}
                    while True:
                        eligible = [
                            kind for kind, limit in capacities.items()
                            if sum(value == kind for value in self.execution_kinds.values()) < limit
                        ]
                        if not eligible:
                            break
                        kinds = [*eligible, *(self.job_handlers if 'company' in eligible else [])]
                        row = await self._io(self.queue.claim, [owner for owner, _ in self.executions], kinds)
                        if row is None:
                            break
                        key = row['owner'], row['id']
                        self.execution_kinds[key] = 'company' if row['kind'] in self.job_handlers else row['kind']
                        self.executions[key] = asyncio.create_task(self._run_claimed(row))
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
                maintenance.cancel()
                await asyncio.gather(maintenance, return_exceptions=True)
                self.queue.snapshots.close()
                if publisher is not None:
                    publisher.cancel()
                    await asyncio.gather(publisher, return_exceptions=True)
                tasks = list(self.executions.values())
                for execution in tasks:
                    if not execution.done() and not execution.cancelling():
                        execution.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                self.executions.clear()
                self.execution_kinds.clear()

    async def maintain_sources(self):
        while True:
            try:
                await self._io(self.queue.snapshots.sweep)
            except Exception as error:
                logging.getLogger(__name__).warning('Source recovery deferred (%s)', type(error).__name__)
            await asyncio.sleep(300)

    async def publish_loop(self):
        delay = 1
        while True:
            try:
                await self.chat_publisher(self.queue, self._io)
                delay = 1
            except Exception as failure:
                logging.getLogger(__name__).warning('Chat publisher unavailable (%s)', type(failure).__name__)
                delay = min(30, delay * 2)
            await asyncio.sleep(delay)

    async def _run_claimed(self, row):
        try:
            # Stop can arrive after claim and before this task starts.
            if (await self._io(self.queue.get, row['owner'], row['id']))['cancel_requested']:
                raise asyncio.CancelledError
            if row['kind'] == 'document':
                execute = _workflow_background_upload.attribute('execute')
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
            code = 'nas_unavailable' if isinstance(failure, NASUnavailable) else (
                'external_tools_unavailable' if isinstance(failure, ExternalToolUnavailable)
                else 'model_unavailable' if model_failure_message(failure) else 'execution_failed'
            )
            description = str(failure) if isinstance(
                failure, (NASUnavailable, ExternalToolUnavailable),
            ) else model_failure_message(failure) or '執行未完成'
            if row['kind'] == 'document':
                DocumentInputError = _workflow_background_upload.attribute('DocumentInputError')
                code = 'document_input_changed' if isinstance(failure, DocumentInputError) else 'document_failed'
                description = str(failure) if isinstance(failure, DocumentInputError) else '公文處理中止；已產生的成果保留，未自動重送。'
            await self._io(self.queue.update, row['id'], status='error', progress=description, error_code=code)
            logging.getLogger(__name__).error(
                'Company Agent job %s failed (%s)', row['id'], type(failure).__name__,
                extra={'job_id': row['id'], 'error_code': code, 'error_type': type(failure).__name__},
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
        if row['kind'] in self.job_handlers:
            await self.job_handlers[row['kind']](self, row)
            return
        if row['kind'] != 'company':
            raise ValueError('此工作類型尚未啟用，未自動重送。')
        model = None
        try:
            user = await self.identity(row['owner'])
            settings = self.settings_factory(row)
            revision = hashlib.sha256(settings.model_dump_json().encode()).hexdigest()
            await self._io(self.queue.store.audit, row['owner'], row['id'], 'configuration', revision)
            model = self.model_factory(user, settings.model_name)
            if model.subject != row['subject']:
                raise ValueError('登入計費身分已變更，未執行。')
            preparation = asyncio.create_task(asyncio.to_thread(self._prepare_sources, row, settings))
            try:
                sources = await asyncio.shield(preparation)
            except asyncio.CancelledError:
                # Threads cannot be stopped midway through a private cache write.
                # Drain preparation before releasing this job to the next worker.
                await asyncio.gather(preparation, return_exceptions=True)
                raise
            agent = DocumentAgent(settings, model, store=self.queue.store, external_tools=self.external_tools)

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


class ServiceConfiguration:
    """Shared deployment paths, not an authenticated request or model session."""

    def __init__(self, state, sources):
        self.state, self.sources = state, sources

    def freeze(self):
        selected = self.configured_settings()
        self._configured = selected
        self._model_name = self.default_model(selected)
        self._connection = self.connection()

    def settings_snapshot(self):
        selected = self._configured if hasattr(self, '_configured') else self.configured_settings()
        return selected.model_copy(deep=True) if selected is not None else None

    def build_model(self, user, model_name=None):
        return self._model(user, self._connection, model_name or self._model_name)

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

        account = _automation_store_imports.store().account(owner)
        return account['generation'] if account else None

    @staticmethod
    def connection():

        # Explicit service configuration must win over repository defaults.
        # In particular, dev must never silently reuse the production key/base.
        names = ('OPENAI_API_BASE_URLS', 'OPENAI_API_KEYS')
        if any(name in os.environ for name in names):
            connection = {name: os.environ.get(name, '') for name in names}
            if not all(connection.values()):
                raise ValueError('一般使用者 LiteLLM 的位址與金鑰必須一起設定。')
        else:
            connection = _config_imports.read_service('webui')
        bases = (connection.get('OPENAI_API_BASE_URLS') or '').split(';')
        keys = (connection.get('OPENAI_API_KEYS') or '').split(';')
        if len(bases) != 1 or len(keys) != 1:
            raise ValueError('請配置單一一般使用者 LiteLLM 連線。')
        if not bases[0] or not keys[0]:
            raise ValueError('一般使用者 LiteLLM 的位址與金鑰必須一起設定。')
        return bases[0], keys[0]

    @staticmethod
    def _model(user, connection, name):
        billed = gateway_for_verified_user(user, *connection, name)
        return WebBilledModel(billed.owner, billed.subject, billed.client)

    @staticmethod
    def model(user, model_name=None):
        return ServiceConfiguration._model(
            user, ServiceConfiguration.connection(),
            model_name or ServiceConfiguration.default_model(ServiceConfiguration.configured_settings()),
        )

    def settings(self, row):
        configured_settings = self.settings_snapshot()
        roots = configured_settings.roots if configured_settings else []
        routing = configured_settings.model_dump(
            include={
                'role_models', 'purpose_models', 'model_profiles',
                'summary_trigger_tokens', 'execution_timeout_seconds',
                'orchestration_mode', 'office_execution_mode', 'model_timeout_seconds',
                'mcp_servers',
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
            model_name=getattr(self, '_model_name', None) or self.default_model(configured_settings),
            nas_binding_revision=row['payload'].get('nas_binding_revision'),
            nas_binding_revisions=row['payload'].get('nas_binding_revisions', []),
            eip_binding_revision=row['payload'].get('eip_binding_revision'),
            max_steps=40,
            compact_prompts=True,
            structured_completion=True,
            require_artifact_review=True,
            require_user_billing=True,
            require_delivery_plan=True,
            enable_web_research=True,
            enable_cad=True,
            enable_ocr=True,
            enable_spreadsheet_reports=True,
            enable_eip_read=True,
            enable_vision=True,
            enable_eip_processing=True,
        )


def create_service():
    """Build independent app resources without changing process environment."""
    DATA_DIR = _webui_env.attribute('DATA_DIR')
    publish_pending = _agent_chat.attribute('publish_pending')

    dsn = os.getenv('COMPANY_AGENT_DATABASE_URL') or os.getenv('DATABASE_URL', '')
    if not dsn.startswith('postgresql://'):
        raise ValueError('公司助理需要 PostgreSQL 連線設定。')
    state = Path(DATA_DIR) / 'company_agent'
    state.mkdir(mode=0o700, exist_ok=True)
    configuration = ServiceConfiguration(state, Path(DATA_DIR) / 'company_agent_sources')
    configuration.freeze()
    limits = configuration.settings_snapshot() or Settings(state_dir=str(state))
    store = Store(state, dsn=dsn)
    queue = WorkQueue(
        state, configuration.sources, configuration.eip_binding, store=store,
        max_pending=limits.max_pending_jobs, max_per_user=limits.max_pending_per_user,
    )
    current = Service(
        queue, verified_user, configuration.build_model, configuration.settings,
        max_concurrent=limits.max_concurrent_jobs,
        mcp_servers=limits.mcp_servers,
    )
    current.chat_publisher = partial(publish_pending, result_reader=current.result)
    return current


def configured_service():
    return _jobs_imports.service()


def service():
    current = configured_service()
    if current.task is not None and current.task.done():
        current.task.result()
    return current


async def upload_bytes(owner, ids):
    if len(ids) > 20:
        raise ValueError('附件過多，請分批。')
    result = []

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
            Files = _webui_files.attribute('Files')
            Storage = _webui_provider.attribute('Storage')

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

        self.queue.update(self.key, engine_job=event['job_id'], progress=_progress_imports.progress_text(event))


def read_output_bytes(path):
    with open(path, 'rb') as stream:
        return stream.read(20_000_001)
