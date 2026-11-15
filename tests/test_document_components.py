"""Document service dependencies and per-request settings are isolated."""
from __future__ import annotations

import copy
import unittest
from unittest.mock import Mock
from unittest.mock import patch

from .test_official_document_processor_pipe import PIPE_MODULE as M
from examples.official_document_workflow.services.rendering import DocumentRendering
from examples.official_document_workflow.services.values import DocumentValues
from examples.shared.runtime.modules import ModuleReference

_workflow_webui = ModuleReference('examples.official_document_workflow.adapters.webui')
_workflow_processor = ModuleReference('examples.official_document_workflow.services.processor')


class ComponentsTests(unittest.TestCase):
    def test_request_copy_keeps_independent_services_settings_and_identity(self):
        original = M.Pipe()
        original._usage_owner = 'alice'
        clone = copy.copy(original)
        clone.valves = clone.valves.model_copy(deep=True)
        clone._usage_owner = 'bob'
        clone.valves.cloud_max_events = 3
        self.assertEqual(original._usage_owner, 'alice')
        self.assertEqual(clone.transport._usage_owner, 'bob')
        self.assertNotEqual(original.valves.cloud_max_events, clone.valves.cloud_max_events)
        self.assertIsNot(original.ocr, clone.ocr)
        self.assertIs(clone.exports.ocr, clone.ocr)
        self.assertIs(clone.sources.ocr, clone.ocr)
        self.assertIs(clone.transport.parser, clone.parser)

    def test_shared_dependencies_survive_copy_and_disable_per_request(self):
        original = M.Pipe()
        content, evidence, private, analysis = object(), object(), object(), object()
        original.content_cache = content
        original.evidence_cache = evidence
        original._eip_processing_cache = private
        original.analysis_results = analysis
        clone = copy.copy(original)
        clone.valves = clone.valves.model_copy(deep=True)
        self.assertIs(clone.exports.content_cache, content)
        self.assertIs(clone.ocr.content_cache, content)
        self.assertIs(clone.evidence_cache, evidence)
        self.assertIs(clone.analysis_results, analysis)
        clone.valves.document_analysis_cache_enabled = False
        self.assertIsNone(clone.content_cache)
        self.assertIsNone(clone.exports.content_cache)
        self.assertIsNone(clone.ocr._eip_processing_cache)
        self.assertIsNone(clone.evidence_cache)
        self.assertIsNone(clone.analysis_results)
        self.assertIs(original.content_cache, content)
        self.assertIs(original.evidence_cache, evidence)
        self.assertIs(original._eip_processing_cache, private)

    def test_facade_override_and_service_dependency_use_the_same_operation(self):
        pipe = M.Pipe()
        check = Mock(return_value=1)
        pipe._validate_pdf_page_count = check
        pipe.sources.ocr._validate_pdf_page_count('fixture')
        check.assert_called_once_with('fixture')
        del pipe._validate_pdf_page_count
        self.assertNotIn('_validate_pdf_page_count', vars(pipe.ocr))

    def test_pure_operations_have_no_request_configuration(self):
        self.assertEqual(DocumentValues._cloud_date_from_event('2026-11-08').gregorian, '2026/11/08')
        self.assertEqual(DocumentRendering.safe_filename_part('a/b', 100), 'a b')
        self.assertFalse(hasattr(DocumentValues(), 'request'))
        self.assertFalse(hasattr(DocumentRendering(), 'request'))

    def test_nested_overrides_restore_and_copied_methods_bind_to_the_new_request(self):
        original = M.Pipe()
        original._parse_cloud_calendar_payload = original._parse_cloud_calendar_payload
        clone = copy.copy(original)
        self.assertIs(clone._parse_cloud_calendar_payload.__self__, clone.parser)
        with patch.object(clone, '_parse_cloud_calendar_payload', Mock()):
            pass
        self.assertIs(clone._parse_cloud_calendar_payload.__self__, clone.parser)
        first = Mock()
        original.analyze_text = first
        with patch.object(original, 'analyze_text', Mock()):
            pass
        self.assertIs(original.analyze_text, first)

    def test_pure_value_overrides_survive_request_copy_after_domain_extraction(self):
        original = M.Pipe()
        replacement = Mock(return_value='source-specific-value')
        original._cloud_date_from_event = replacement
        clone = copy.copy(original)
        self.assertIs(clone._cloud_date_from_event, replacement)
        self.assertIs(clone.values._cloud_date_from_event, replacement)
        self.assertIsNot(clone.values, original.values)

    def test_new_service_helpers_do_not_expand_the_reviewed_pipe_abi(self):
        expose_operations = _workflow_webui.attribute('expose_operations')
        pipe = M.Pipe()
        services = {
            name: type(getattr(pipe, name)) for name in
            ('values', 'rendering', 'parser', 'ocr', 'sources', 'exports', 'transport', 'clarifications')
        }

        class FreshFacade:
            pass
        with patch.object(services['ocr'], 'new_internal_helper', lambda self: None, create=True):
            expose_operations(FreshFacade, services)
        self.assertFalse(hasattr(FreshFacade, 'new_internal_helper'))
        self.assertIn('store_output', FreshFacade._service_operations)

    def test_core_has_no_dynamic_pipe_aliases_and_copies_explicit_dependencies(self):
        DocumentProcessor = _workflow_processor.attribute('DocumentProcessor')
        core = DocumentProcessor()
        self.assertFalse(hasattr(core, '_service_operations'))
        self.assertFalse(hasattr(core, 'store_output'))
        self.assertTrue(callable(core.exports.store_output))
        clone = copy.copy(core)
        self.assertIsNot(clone.exports, core.exports)
        self.assertIs(clone.exports.sources, clone.sources)
