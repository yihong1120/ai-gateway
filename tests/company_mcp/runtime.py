"""Real backend HTTP plus an independently launched, credential-minimal edge."""
from __future__ import annotations

import asyncio
import contextvars
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from dataclasses import field
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route

from company_mcp.adapters.backend import endpoint
from company_mcp.config import BACKEND_PATH

ROOT = Path(__file__).resolve().parents[2]


def close_client(client):
    asyncio.run(client.aclose())


@dataclass
class Runtime:
    server: object
    thread: object
    sock: object
    port: int
    log: Path
    env: dict = field(repr=False)
    process: object = field(default=None, repr=False)
    output: object = field(default=None, repr=False)

    def start(self):
        self.output = self.log.open('a')
        executable = os.getenv('COMPANY_MCP_TEST_EDGE_PYTHON', sys.executable)
        self.process = subprocess.Popen(
            [executable, '-m', 'company_mcp.serve'], cwd=ROOT, env=self.env,
            stdin=subprocess.DEVNULL, stdout=self.output, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 15
        with httpx.Client(trust_env=False) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise AssertionError('Standalone MCP exited: ' + self.log.read_text()[-2000:])
                try:
                    if client.get(f'http://127.0.0.1:{self.port}/readyz', timeout=.3).status_code == 200:
                        return
                except Exception:
                    # Probe failures during process startup may originate from
                    # either HTTPX or its transport. Only readiness is polled;
                    # user/tool requests are never retried by this harness.
                    pass
                time.sleep(.02)
        raise AssertionError('Standalone MCP was not ready: ' + self.log.read_text()[-2000:])

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.output is not None:
            self.output.close()

    def stop_backend(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.sock.close()


def start(case, resources, verifier):
    config = resources.configuration
    app = Starlette(
        routes=[
            Route(
                BACKEND_PATH, endpoint(
                    config, verifier, Mock(return_value=resources),
                ), methods=['POST'],
            ),
        ],
    )
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    backend_port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False, ws='none'))
    context = contextvars.copy_context()
    thread = threading.Thread(target=partial(context.run, server.run, sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.02)
    case.assertTrue(server.started)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR') if key in os.environ}
    env.update(
        PYTHONPATH=str(ROOT), PYTHON_DOTENV_DISABLED='1',
        COMPANY_MCP_ENABLED='true', COMPANY_MCP_ORIGIN=config.origin, COMPANY_MCP_ISSUER=config.issuer,
        COMPANY_MCP_CLIENTS=','.join(config.clients), COMPANY_MCP_ALLOWED_CIDRS='127.0.0.0/8',
        COMPANY_MCP_BACKEND_KEY=config.backend_key, COMPANY_MCP_PORT=str(port),
        COMPANY_MCP_BACKEND_URL=f'http://127.0.0.1:{backend_port}' + BACKEND_PATH,
    )
    runtime = Runtime(server, thread, sock, port, case.base / 'independent-edge.log', env)
    case.addCleanup(runtime.stop_backend)
    case.addCleanup(runtime.stop)
    runtime.start()
    case.assertNotEqual(runtime.process.pid, os.getpid())
    return runtime
