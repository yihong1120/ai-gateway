"""Public API error contract shared by adapters, protocols and services."""

from fastapi import HTTPException


def error(status, code):
    return HTTPException(status, detail={'code': code})
