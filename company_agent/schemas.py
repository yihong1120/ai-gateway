"""Shared strict input contract; domain schemas live with their services."""
from __future__ import annotations

from pydantic import BaseModel
from pydantic import ConfigDict


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
