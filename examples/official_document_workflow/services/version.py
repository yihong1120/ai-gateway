"""Bind persisted work to both the installed implementation and WebUI adapter."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from examples.shared import runtime


@lru_cache(maxsize=1)
def implementation_revision():
    digest = hashlib.sha256()
    for root in (Path(__file__).resolve().parents[1], Path(runtime.__file__).resolve().parent):
        for path in sorted(root.rglob('*.py')):
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b'\0')
            digest.update(path.read_bytes())
            digest.update(b'\0')
    return digest.hexdigest()


def workflow_revision(content):
    return hashlib.sha256((content + '\0' + implementation_revision()).encode()).hexdigest()
