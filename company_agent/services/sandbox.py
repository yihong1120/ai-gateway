"""Shared offline container execution for CAD, Office reports and page rendering."""
from __future__ import annotations

import json
import os
import selectors
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


def run_bounded(command, container):
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
    )
    buffers = {}
    deadline = time.monotonic() + 60
    try:
        with selectors.DefaultSelector() as selector:
            for pipe in (process.stdout, process.stderr):
                selector.register(pipe, selectors.EVENT_READ)
                buffers[pipe] = bytearray()
            while selector.get_map():
                if time.monotonic() > deadline:
                    raise ValueError('隔離處理超過 60 秒上限。')
                for event, _ in selector.select(timeout=0.2):
                    pipe = event.fileobj
                    chunk = os.read(pipe.fileno(), 4096)
                    if not chunk:
                        selector.unregister(pipe)
                        continue
                    buffers[pipe].extend(chunk)
                    if len(buffers[pipe]) > 30000:
                        raise ValueError('隔離工具回應超過上限；請縮小讀取範圍。')
        process.wait(timeout=max(0.1, deadline - time.monotonic()))
        if process.returncode:
            raise subprocess.CalledProcessError(
                process.returncode, command,
                output=bytes(buffers[process.stdout]), stderr=bytes(buffers[process.stderr]),
            )
        return bytes(buffers[process.stdout])
    finally:
        # Exact random container created for this call only, never another job.
        if process.poll() is None:
            try:
                subprocess.run(
                    ['docker', 'rm', '-f', container],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            finally:
                process.kill()
                process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


def sandbox_worker(image, request, *, data=None, suffix, output=None, entrypoint=(), tmpfs_mb=64):
    """Internal callers provide fixed image, entrypoint and output names."""
    with tempfile.TemporaryDirectory(prefix='company-sandbox-') as directory:
        root = Path(directory)
        incoming, outgoing = root / 'input', root / 'output'
        incoming.mkdir(mode=0o700)
        outgoing.mkdir(mode=0o700)
        if data is not None:
            (incoming / ('source' + suffix)).write_bytes(data)
        (incoming / 'request.json').write_text(
            json.dumps(request, ensure_ascii=False, allow_nan=False), encoding='utf-8',
        )
        container = 'company-sandbox-' + uuid.uuid4().hex
        command = [
            'docker', 'run', '--rm', '--pull', 'never', '--name', container,
            '--network', 'none', '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--memory', '2g', '--memory-swap', '2g',
            '--cpus', '1', '--pids-limit', '128', '--user', f"{os.getuid()}:{os.getgid()}",
            '--ulimit', 'fsize=30000000:30000000',
            '--tmpfs', f"/tmp:rw,noexec,nosuid,size={tmpfs_mb}m",
            '--mount', f"type=bind,src={incoming},dst=/input,readonly",
            '--mount', f"type=bind,src={outgoing},dst=/out",
        ]
        command += ['--entrypoint', entrypoint[0], image, *entrypoint[1:]] if entrypoint else [image]
        result = json.loads(run_bounded(command, container))
        if output is None:
            return result
        target = outgoing / output
        if target.is_symlink() or not target.is_file() or not 0 < target.stat().st_size <= 20_000_000:
            raise ValueError('未產生有效的隔離處理成果。')
        return result, target.read_bytes()
