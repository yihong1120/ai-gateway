"""Validated business inputs, shared by service and agent adapters."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from company_agent.schemas import Strict


class Search(Strict):
    root: str
    query: str = Field(min_length=1, max_length=200)
    mode: Literal['name', 'content'] = 'name'
    limit: int = Field(default=10, ge=1, le=30)


class Read(Strict):
    root: str
    path: str
    start: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)


class Publish(Strict):
    artifact_id: str
    root: str
    path: str


class ReadOCR(Strict):
    root: str
    path: str
    page: int = Field(default=1, ge=1, le=100)
    start: int = Field(default=0, ge=0, le=1000)
    limit: int = Field(default=3, ge=1, le=3)


class InspectArtifact(Strict):
    artifact_id: str = Field(min_length=1, max_length=100)
    start: int = Field(
        default=0,
        ge=0,
        description='從零計數的偏移；XLSX 第1列填0、第2列填1。參數名是 start，不是 start_row。',
    )
    limit: int = Field(
        default=20, ge=1, le=50, description='讀取列/區塊數；參數名是 limit，不是 row_limit。',
    )
    sheet: str | None = Field(
        default=None,
        max_length=31,
        description='XLSX 精確工作表名稱；未知時省略，取得 sheets 清單後逐張讀取。',
    )
