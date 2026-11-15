"""WebUI invocations receive the current app's reusable content services."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from .test_official_document_processor_pipe import PIPE_MODULE as M
from examples.official_document_workflow.runtime import WorkflowResources, WorkflowSettings
from examples.official_document_workflow.services.processor import DocumentProcessor


class UploadContentResourcesTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, resources, pipe, *, task=None):
        captured = []

        async def process(current, *args):
            captured.append(current)
            return 'completed'

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(workflow=resources)))
        with patch.object(DocumentProcessor, 'process', process):
            result = await pipe.pipe({'messages': []}, __user__={'id': 'alice'},
                                     __request__=request, __task__=task)
        self.assertEqual(result, 'completed')
        return captured[0]

    async def test_each_upload_uses_current_app_cache_without_mutating_cached_pipe(self):
        pipe = M.Pipe()
        applications = [WorkflowResources(WorkflowSettings('postgresql://unused', schema, Path('/unused-key')))
                        for schema in ('first_app', 'second_app')]
        invocations = [await self.invoke(resources, pipe) for resources in applications]
        for resources, invocation in zip(applications, invocations, strict=True):
            self.assertIs(invocation.content_cache, resources.shared_content)
            self.assertIs(invocation.evidence_cache, resources.shared_content)
            self.assertIs(invocation.ocr.content_cache, resources.shared_content)
            self.assertIsNone(resources._store)
        self.assertIsNot(invocations[0].content_cache, invocations[1].content_cache)
        self.assertIsNone(pipe.content_cache)
        self.assertIsNone(pipe.evidence_cache)

    async def test_disabled_upload_and_auxiliary_tasks_do_not_initialize_shared_cache(self):
        resources = WorkflowResources(WorkflowSettings('postgresql://unused', 'disabled', Path('/unused-key')))
        pipe = M.Pipe()
        pipe.valves.document_analysis_cache_enabled = False
        invocation = await self.invoke(resources, pipe)
        self.assertIsNone(invocation.content_cache)
        self.assertIsNone(invocation.evidence_cache)
        self.assertFalse(hasattr(resources, '_shared_content'))
        pipe.valves.document_analysis_cache_enabled = True
        await self.invoke(resources, pipe, task='title_generation')
        self.assertFalse(hasattr(resources, '_shared_content'))
        self.assertIsNone(resources._store)
