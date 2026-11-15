"""Application-owned workflow resources, bound to requests and child tasks."""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

import examples.official_document_workflow.infrastructure.storage.database as _database_imports
import examples.official_document_workflow.services.version as _version_imports
from .infrastructure.storage.analysis_results import AnalysisResults
from .infrastructure.storage.output_receipts import PostgresOutputReceipts
from .infrastructure.storage.shared_content import SharedContentCache
from .services.file_access import FileAccess
from .services.output_repair import OutputRepair
from examples.shared.runtime.modules import ModuleReference
from examples.shared.runtime.workers import WorkerHealth

_workflow_automation = ModuleReference('examples.official_document_workflow.application.automation')
_workflow_netask = ModuleReference('examples.official_document_workflow.infrastructure.eip.netask')
_workflow_automation_store = ModuleReference(
    'examples.official_document_workflow.infrastructure.storage.automation_store',
)
_workflow_state = ModuleReference('examples.official_document_workflow.infrastructure.storage.state')
_webui_files = ModuleReference('open_webui.models.files')
_webui_provider = ModuleReference('open_webui.storage.provider')

_current = ContextVar('document_workflow_resources', default=None)


@dataclass(frozen=True)
class WorkflowSettings:
    dsn: str
    schema: str
    key: Path
    shared_content_cache_enabled: bool = True
    shared_content_cache_ttl_seconds: int = 604800

    def __post_init__(self):
        if type(self.shared_content_cache_enabled) is not bool:
            raise ValueError('EIP shared content cache enabled setting must be boolean.')
        if type(self.shared_content_cache_ttl_seconds) is not int or not 60 <= self.shared_content_cache_ttl_seconds <= 30 * 86400:
            raise ValueError('EIP shared content cache TTL must be 60–2592000 seconds.')

    @classmethod
    def from_environment(cls):
        key_file = _workflow_automation_store.attribute('key_file')
        enabled = os.getenv('EIP_SHARED_CONTENT_CACHE_ENABLED', 'true').strip().lower()
        if enabled not in {'true', '1', 'yes', 'on', 'false', '0', 'no', 'off'}:
            raise ValueError('EIP shared content cache enabled setting must be boolean.')
        try:
            ttl = int(os.getenv('EIP_SHARED_CONTENT_CACHE_TTL_SECONDS', '604800'))
        except ValueError:
            raise ValueError('EIP shared content cache TTL must be 60–2592000 seconds.') from None
        return cls(*_database_imports.configuration(), key_file(), enabled in {'true', '1', 'yes', 'on'}, ttl)


class WorkflowResources:
    def __init__(self, settings):
        State = _workflow_state.attribute('State')
        self.settings = settings
        self.state = State()
        self._store, self._journal = None, None
        self._lock = RLock()
        self.health = {name: WorkerHealth() for name in ('scheduler', 'cleanup', 'outputs')}

    @property
    def store(self):
        AutomationStore = _workflow_automation_store.attribute('AutomationStore')
        with self._lock:
            if self._store is None:
                self._store = AutomationStore(self.settings.key, dsn=self.settings.dsn, schema=self.settings.schema)
            return self._store

    @property
    def journal(self):
        Journal = _workflow_state.attribute('Journal')
        with self._lock:
            if self._journal is None:
                self._journal = Journal(dsn=self.settings.dsn, schema=self.settings.schema)
            return self._journal

    @property
    def shared_content(self):
        if not self.settings.shared_content_cache_enabled:
            return None
        ORIGIN = _workflow_netask.attribute('ORIGIN')
        with self._lock:
            if not hasattr(self, '_shared_content'):
                self._shared_content = SharedContentCache(
                    lambda: self.store, f'{ORIGIN}:{self.settings.schema}', _version_imports.implementation_revision(),
                    self.settings.shared_content_cache_ttl_seconds,
                )
            return self._shared_content

    def configure_document_cache(self, processor):
        """Bind neutral content services once at the service-key entry boundary."""
        cache = self.shared_content if processor.valves.document_analysis_cache_enabled else None
        processor.content_cache = cache
        processor.evidence_cache = cache
        processor.analysis_results = self.analysis_results if cache is not None else None

    @property
    def analysis_results(self):
        ORIGIN = _workflow_netask.attribute('ORIGIN')
        with self._lock:
            if not hasattr(self, '_analysis_results'):
                self._analysis_results = AnalysisResults(lambda: self.store, f'{ORIGIN}:{self.settings.schema}')
            return self._analysis_results

    @property
    def outputs(self):
        with self._lock:
            if not hasattr(self, '_outputs'):
                self._outputs = PostgresOutputReceipts(lambda: self.store)
            return self._outputs

    def output_repair(self, publish=None):
        Files = _webui_files.attribute('Files')
        Storage = _webui_provider.attribute('Storage')
        if not hasattr(self, '_repair'):
            self._repair = OutputRepair(self.outputs, FileAccess(Files, Storage), publish)
        return self._repair

    @property
    def scheduler(self):
        AutomaticScheduler = _workflow_automation.attribute('AutomaticScheduler')
        if not hasattr(self, '_scheduler'):
            self._scheduler = AutomaticScheduler(self.store, self.journal, self.state, self.health['scheduler'])
        return self._scheduler

    def initialize(self):
        self.store
        self.journal

    @contextmanager
    def bind(self):
        token = _current.set(self)
        try:
            yield self
        finally:
            _current.reset(token)


def current_resources():
    resources = _current.get()
    if resources is None:
        raise RuntimeError('Document workflow resources are not bound to this application task.')
    return resources


class WorkflowMiddleware:
    def __init__(self, app, owner):
        self.app, self.owner = app, owner

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            await self.app(scope, receive, send)
            return
        with self.owner.state.workflow.bind():
            await self.app(scope, receive, send)
