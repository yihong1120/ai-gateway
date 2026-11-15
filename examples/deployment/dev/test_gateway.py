"""Regression coverage for TLS checks racing the asynchronous Nginx reload."""
from __future__ import annotations

from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from examples.deployment.dev import gateway

READY = 'HTTP/2 200\nX-AI-Gateway: dev-b\nX-AI-Environment: dev\n\n{"status":true}'


def test_reload_tolerates_stale_certificate_then_checks_verified_dev_response():
    responses = [CompletedProcess([], 60, '', 'certificate hostname mismatch'), CompletedProcess([], 0, READY, '')]
    with patch.object(gateway.subprocess, 'run', side_effect=responses) as curl, patch.object(gateway.time, 'sleep'):
        gateway.wait_gateway()
    assert curl.call_count == 2
    for call in curl.call_args_list:
        assert '--insecure' not in call.args[0] and '-k' not in call.args[0]
        assert gateway.HOST + ':443:127.0.0.1' in call.args[0]


def test_a_healthy_production_response_does_not_pass_dev_readiness():
    production = CompletedProcess([], 0, 'HTTP/2 200\n\n{"status":true}', '')
    with patch.object(gateway.subprocess, 'run', return_value=production):
        with pytest.raises(RuntimeError, match='dev gateway and app'):
            gateway.wait_gateway(timeout=0)


def test_permanent_certificate_failure_remains_an_error():
    failure = CompletedProcess([], 60, '', 'certificate hostname mismatch')
    with patch.object(gateway.subprocess, 'run', return_value=failure):
        with pytest.raises(RuntimeError, match='certificate hostname mismatch'):
            gateway.wait_gateway(timeout=0)
