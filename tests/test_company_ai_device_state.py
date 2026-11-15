"""Exact expiry boundaries and credential separation for device requests."""
from __future__ import annotations

import unittest

from examples.company_ai_access.protocols.messages import envelope
from examples.company_ai_access.services.device_state import expiration_reason
from examples.company_ai_access.services.device_state import key_error


class DeviceStateTests(unittest.TestCase):
    def device(self, **updates):
        return dict(
            status='active', expires_at=200, hard_expires_at=300,
            acked=False, delivery_until=150, **updates,
        )

    def test_delivery_expires_at_deadline_but_acknowledged_device_survives(self):
        device = self.device()
        enrollment = {'expires_at': 100, 'activation_until': 160, 'delivery_until': 150}
        self.assertIsNone(key_error(device, 149))
        self.assertIsNone(expiration_reason(device, enrollment, 149))
        self.assertEqual(key_error(device, 150), 'ai_access_activation_required')
        self.assertEqual(expiration_reason(device, enrollment, 150), 'lease_or_delivery_expired')
        device['acked'] = True
        self.assertIsNone(key_error(device, 150))
        self.assertIsNone(expiration_reason(device, None, 150))

    def test_expired_or_revoked_credentials_never_pass_delivery_grace(self):
        device = self.device()
        self.assertEqual(key_error(device, 200), 'ai_access_confirmation_required')
        self.assertEqual(expiration_reason(device, None, 300), 'confirmation_expired')
        device['status'] = 'revoked'
        self.assertEqual(key_error(device, 149), 'ai_access_invalid_key')
        self.assertIsNone(expiration_reason(device, None, 300))
        self.assertEqual(key_error(None, 149), 'ai_access_invalid_key')

    def test_anthropic_routing_overrides_are_removed_without_mutating_input(self):
        value = {
            'model': 'allowed', 'messages': [{'role': 'user', 'content': 'hello'}],
            'api_base': 'https://other.invalid', 'api_key': 'untrusted',
            'metadata': {'user_id': 'other'}, 'user': 'other', 'stream': True,
        }
        body = envelope(value)
        self.assertEqual(set(body), {'model', 'messages', 'stream'})
        self.assertIn('api_key', value)
