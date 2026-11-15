"""Public API error contract shared by adapters, protocols and services."""
from __future__ import annotations

from fastapi import HTTPException


def error(status, code):
    return HTTPException(status, detail={'code': code})
