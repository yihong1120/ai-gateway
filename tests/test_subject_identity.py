"""Provider link extraction is shared while caller authorisation stays explicit."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

import company_agent.runtime.models as _models_imports
from examples.identity.providers.subjects import linked_subject
from examples.shared.runtime.usage import verified_subject


def load_filter():
    path = Path(__file__).resolve().parents[1] / 'examples/openwebui/functions/keycloak_subject_budget_filter.py'
    specification = importlib.util.spec_from_file_location('subject_filter_source', path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module.Filter()


class LinkedSubjectTests(unittest.TestCase):
    def test_original_subject_and_provider_are_preserved_without_role_policy(self):
        original = ' unchanged '
        self.assertEqual(linked_subject({'oidc': {'sub': original}}), original)
        self.assertEqual(linked_subject({'other': MappingProxyType({'sub': 'other-sub'})}, 'other'), 'other-sub')
        self.assertEqual(linked_subject(MappingProxyType({'oidc': {'sub': 'x' * 300}})), 'x' * 300)

    def test_absent_or_malformed_provider_claims_have_no_subject(self):
        for identities in (
            None, 'forged', {}, {'oidc': None}, {'oidc': []}, {'oidc': {'sub': 7}},
            {'oidc': {'sub': ''}}, {'oidc': {'sub': '  '}},
        ):
            with self.subTest(identities=identities):
                self.assertIsNone(linked_subject(identities))

    def test_document_and_company_policies_remain_stricter_than_link_extraction(self):

        base = {'id': 'owner', 'role': 'user', 'oauth': {'oidc': {'sub': 'subject'}}}
        self.assertEqual(verified_subject(base), 'subject')
        self.assertEqual(verified_subject({key: value for key, value in base.items() if key != 'id'}), 'subject')
        for subject in (' subject ', 'x' * 256, '', None):
            user = {**base, 'oauth': {'oidc': {'sub': subject}}}
            with self.subTest(subject=subject), self.assertRaises(ValueError):
                verified_subject(user)
            with self.subTest(company_subject=subject), self.assertRaises(ValueError):
                _models_imports.gateway_for_verified_user(user, 'http://127.0.0.1:4000/v1', 'synthetic-key', 'model')
        for role in ('pending', 'guest', None):
            user = {**base, 'role': role}
            with self.subTest(role=role), self.assertRaises(ValueError):
                verified_subject(user)
            with self.subTest(company_role=role), self.assertRaises(ValueError):
                _models_imports.gateway_for_verified_user(user, 'http://127.0.0.1:4000/v1', 'synthetic-key', 'model')
        for owner in (' owner ', '', 'x' * 256, None):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                _models_imports.gateway_for_verified_user(
                    {**base, 'id': owner}, 'http://127.0.0.1:4000/v1', 'synthetic-key', 'model',
                )
        with patch('company_agent.runtime.models.GatewayModel', return_value=object()):
            model = _models_imports.gateway_for_verified_user(base, 'unused', 'unused', 'unused')
        self.assertEqual((model.owner, model.subject), ('owner', 'subject'))


class SubjectFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_dynamic_function_source_overwrites_missing_or_matching_user(self):
        filter = load_filter()
        subject = ' original subject '
        user = {'oauth': {'oidc': {'sub': subject}}}
        for supplied in (None, '', subject):
            body = {'user': supplied, 'messages': []}
            self.assertIs(await filter.inlet(body, user), body)
            self.assertEqual(body['user'], subject)

    async def test_filter_keeps_host_role_and_subject_length_policy(self):
        filter = load_filter()
        subject = 'x' * 300
        user = {'role': 'guest', 'oauth': {'oidc': {'sub': subject}}}
        self.assertEqual(await filter.inlet({}, user), {'user': subject})

    async def test_configured_provider_and_spoofing_policy_are_preserved(self):
        filter = load_filter()
        filter.valves.oidc_provider = 'custom'
        user = {'oauth': {'oidc': {'sub': 'other'}, 'custom': {'sub': 'trusted'}}}
        self.assertEqual(await filter.inlet({}, user), {'user': 'trusted'})
        with self.assertRaisesRegex(ValueError, 'The request user does not match the authenticated Keycloak subject'):
            await filter.inlet({'user': 'other'}, user)
        with self.assertRaisesRegex(ValueError, 'A verified Keycloak OIDC subject is required for model access'):
            await filter.inlet({}, {'oauth': {'oidc': {'sub': 'other'}}})

    async def test_missing_identity_never_uses_browser_user_as_fallback(self):
        filter = load_filter()
        for user in (None, {}, {'oauth': {}}, {'oauth': {'oidc': {'sub': ''}}}, {'oauth': 'invalid'}):
            with self.subTest(user=user), self.assertRaisesRegex(ValueError, 'A verified Keycloak OIDC subject is required'):
                await filter.inlet({'user': 'browser-subject'}, user)
