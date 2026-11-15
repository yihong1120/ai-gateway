"""Shared evaluation resources, conservative cost accounting and atomic reports.

The runner does not authorize live calls. Each suite keeps its existing live,
identity, schema and budget checks before any provider operation.
"""
from __future__ import annotations

from contextlib import AsyncExitStack
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import json
import logging
import math
import os
from pathlib import Path
import tempfile
from threading import RLock

from examples.shared.runtime.database import application_database_pools
from company_agent.runtime.models import close_model

logger = logging.getLogger(__name__)
_current = ContextVar('evaluation_run', default=None)


def _amount(value):
    if not math.isfinite(value) or value < 0:
        raise ValueError('Evaluation costs must be finite and non-negative.')
    return value


class Budget:
    def __init__(self, limit):
        if not math.isfinite(limit) or limit <= 0:
            raise ValueError('Evaluation budget must be finite and positive.')
        self.limit, self.charged, self.exhausted = limit, 0.0, False
        self._lock = RLock()

    def reserve(self, amount):
        _amount(amount)
        with self._lock:
            if self.charged + amount > self.limit:
                self.exhausted = True
                raise ValueError('測試費用預算不足以安全發出下一次請求；已停止。')
            self.charged += amount

    def settle(self, reserved, cost):
        _amount(reserved)
        _amount(cost)
        with self._lock:
            self.charged += cost - reserved
            self.exhausted |= self.charged >= self.limit


class JobBudget:
    def __init__(self, parent):
        self.parent, self.charged = parent, 0.0
        self._lock = RLock()

    def reserve(self, amount):
        with self._lock:
            self.parent.reserve(amount)
            self.charged += amount

    def settle(self, reserved, cost):
        with self._lock:
            self.parent.settle(reserved, cost)
            self.charged += cost - reserved


def write_json(path, value):
    path = Path(path)
    data = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.' + path.name + '.', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class EvaluationRun:
    def __init__(self):
        self.models, self.reports = {}, {}
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.stack = AsyncExitStack()

    async def __aenter__(self):
        await self.stack.enter_async_context(application_database_pools())
        self.token = _current.set(self)
        return self

    async def __aexit__(self, kind, failure, traceback):
        errors = []
        for model in list(self.models.values()):
            try:
                await release_model(model)
            except BaseException as error:
                errors.append(error)
        try:
            await self.stack.__aexit__(kind, failure, traceback)
        except BaseException as error:
            errors.append(error)
        for path, report in self.reports.items():
            final = failure or (errors[0] if errors else None)
            report['execution'] = {
                'status': 'cancelled' if isinstance(final, BaseException) and not isinstance(final, Exception)
                          else 'error' if final else 'complete',
                'started_at': self.started_at, 'completed_at': datetime.now(timezone.utc).isoformat(),
                'error_type': type(final).__name__ if final else None,
            }
            try:
                write_json(path, report)
            except BaseException as error:
                errors.append(error)
        _current.reset(self.token)
        if failure is None and errors:
            raise BaseExceptionGroup('Evaluation cleanup failed', errors)
        if errors:
            logger.warning('Evaluation cleanup had %s errors; original failure preserved', len(errors))
        return False


def evaluation(function):
    @wraps(function)
    async def run(*args, **kwargs):
        async with EvaluationRun():
            return await function(*args, **kwargs)
    return run


def own_model(model):
    run = _current.get()
    if run is None:
        raise RuntimeError('Evaluation models require the evaluation runner.')
    run.models[id(model)] = model
    return model


async def release_model(model):
    run = _current.get()
    if run is not None:
        run.models.pop(id(model), None)
    await close_model(model)


def track_report(path, report):
    run = _current.get()
    if run is None:
        raise RuntimeError('Evaluation reports require the evaluation runner.')
    run.reports[Path(path)] = report


def own_sandbox(dsn):
    from company_agent.evals.sandbox import isolated_schemas

    run = _current.get()
    if run is None:
        raise RuntimeError('Evaluation sandboxes require the evaluation runner.')
    return run.stack.enter_context(isolated_schemas(dsn))
