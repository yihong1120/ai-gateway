"""Validated business inputs, shared by service and agent adapters."""
from __future__ import annotations

from typing import Annotated
from typing import Literal

from pydantic import Field
from pydantic import model_validator

from company_agent.schemas import Strict


class ReadCad(Strict):
    root: str
    path: str
    view: Literal['summary', 'texts', 'layers', 'entities'] = 'summary'
    query: str = Field(default='', max_length=100)
    start: int = Field(default=0, ge=0, le=200000)
    limit: int = Field(
        default=10,
        ge=1,
        le=10,
        description='CAD 每頁最多 10 筆；可省略。續頁使用回傳的 next_start，不可增加 limit。',
    )


CadNumber = Annotated[float, Field(allow_inf_nan=False, ge=-1e9, le=1e9)]


class CadEntity(Strict):
    kind: Literal['line', 'circle', 'text']
    layer: str = Field(
        default='0', min_length=1, max_length=80, pattern=r'^[^<>/\\":;?*|=,\x00-\x1f]+$',
    )
    point: list[CadNumber] = Field(min_length=2, max_length=2)
    end: list[CadNumber] | None = Field(default=None, min_length=2, max_length=2)
    radius: float | None = Field(default=None, gt=0, le=1e9, allow_inf_nan=False)
    text: str = Field(default='', max_length=500, pattern=r'^[^\x00-\x1f]*$')
    height: float = Field(default=2.5, gt=0, le=1e6, allow_inf_nan=False)

    @model_validator(mode='after')
    def required_geometry(self):
        if self.kind == 'line' and (self.end is None or self.end == self.point):
            raise ValueError('Line requires distinct end')
        if self.kind == 'circle' and self.radius is None:
            raise ValueError('Circle requires radius')
        if self.kind == 'text' and not self.text.strip():
            raise ValueError('Text cannot be blank')
        return self


class CreateCad(Strict):
    name: str = Field(min_length=5, max_length=180, pattern=r'^[^/\\\x00-\x1f]+\.[dD][xX][fF]$')
    units: Literal['mm', 'm']
    entities: list[CadEntity] = Field(min_length=1, max_length=500)


class EditCad(Strict):
    root: str
    path: str
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    handle: str = Field(pattern=r'^[A-Fa-f0-9]{1,16}$')
    expected: str = Field(min_length=1, max_length=1000)
    replacement: str = Field(max_length=1000, pattern=r'^[^\x00-\x1f]*$')
    name: str = Field(min_length=5, max_length=180, pattern=r'^[^/\\\x00-\x1f]+\.[dD][xX][fF]$')
