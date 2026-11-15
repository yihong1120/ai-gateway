"""One held mail profile; provider errors and timeouts have explicit outcomes."""
from __future__ import annotations

import unittest

import httpx
from fastapi import HTTPException

from examples.identity.accounts.mail import Mailer
from examples.identity.settings.mail import MailSettings
from examples.shared.runtime.http_clients import HttpClientPool


class AccountMailTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_responses_and_timeout_are_classified_without_recreating_client(
        self,
    ):
        replies = iter([201, 400, 408, 500, 'timeout'])
        requests = []

        def request(request):
            requests.append(request)
            status = next(replies)
            if status == 'timeout':
                raise httpx.ReadTimeout('response lost', request=request)
            return httpx.Response(status, json={'messageId': 'provider-id'})

        pool = HttpClientPool(
            {'brevo-email': {'transport': httpx.MockTransport(request), 'timeout': 10}},
        )
        self.addAsyncCleanup(pool.close)
        mailer = Mailer(
            MailSettings(
                brevo_api_key='synthetic-mail-key', mail_from='test@example.test',
            ),
            pool,
        )
        states = [(await mailer.send({'template': 'test'})).state for _ in range(5)]
        self.assertEqual(states, ['sent', 'rejected', 'unknown', 'unknown', 'unknown'])
        self.assertEqual(len(pool._clients), 1)
        self.assertTrue(
            all(
                request.headers['api-key'] == 'synthetic-mail-key'
                for request in requests
            ),
        )

    async def test_mail_configuration_is_validated_before_transmission(self):
        pool = HttpClientPool({'brevo-email': {'timeout': 10}})
        self.addAsyncCleanup(pool.close)
        mailer = Mailer(MailSettings(brevo_api_key='', mail_from=''), pool)

        with self.assertRaises(HTTPException):
            await mailer.send({'template': 'test'})
        self.assertEqual(pool._clients, {})
