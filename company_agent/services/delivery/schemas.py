"""Literal requirements and tool-backed statistics; never executable assertions."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic import model_validator

from company_agent.schemas import Strict
from company_agent.services.office.schemas import AnalyzeSpreadsheet
from company_agent.services.office.schemas import CellValue


Metric = Literal['sum', 'count', 'min', 'max', 'mean']


class StatisticsRequirement(AnalyzeSpreadsheet):
    key: str = Field(pattern=r'^[a-z][a-z0-9_]{0,39}$')


class Statistic(Strict):
    key: str = Field(min_length=1, max_length=40)
    metric: Metric


class CellExpectation(Strict):
    sheet: str = Field(min_length=1, max_length=31)
    cell: str = Field(pattern=r'^[A-Z]{1,2}[1-9][0-9]{0,3}$')
    expected: CellValue = None
    statistic: Statistic | None = None
    formula: bool = Field(default=False, description='true 表示精確核對 expected 原公式字串；不是驗證公式計算結果或快取。統計值不可填 true。')

    @model_validator(mode='after')
    def one_value(self):
        if self.statistic is not None and self.expected is not None:
            raise ValueError('expected 與 statistic 只能指定一種。')
        if self.formula and (self.statistic is not None or not isinstance(self.expected, str) or not self.expected.startswith('=')):
            raise ValueError('公式驗收須指定以 = 開頭的 expected 字串，不能使用 statistic。')
        return self


class TextExpectation(Strict):
    text: str = Field(
        min_length=1, max_length=500,
        description='統計文字用一個{value}並指定statistic；只有明確要求輸出字面預留符號時，用{{value}}表示字面{value}。',
    )
    statistic: Statistic | None = None

    @model_validator(mode='after')
    def placeholder(self):
        if self.statistic is not None and (self.text.count('{value}') != 1 or '{{value}}' in self.text):
            raise ValueError('統計文字須包含一個 {value}，例如 合計：{value}。')
        if self.statistic is None and re.search(r'(?<!\{)\{value\}(?!\})', self.text):
            raise ValueError('含{value}的統計文字必須指定statistic的key與metric，不能把預留符號當作驗收文字。使用者明確要求字面符號時才用{{value}}。')
        return self


class OriginalWorkbook(Strict):
    root: str = Field(min_length=1, max_length=100)
    path: str = Field(min_length=1, max_length=1000)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class ExpectedFile(Strict):
    name: str = Field(min_length=1, max_length=180, pattern=r'^[^/\\\x00-\x1f]+$')
    sheet_names: list[str] = Field(default_factory=list, max_length=20)
    cells: list[CellExpectation] = Field(default_factory=list, max_length=100)
    texts: list[TextExpectation] = Field(default_factory=list, max_length=100)
    base_source: OriginalWorkbook | None = Field(
        default=None,
        description='僅用於明確保留原簿的XLSX修改，填實際已讀來源及SHA；單一來源可省略由伺服器綁定。新建報表填null。工作表規格須符合原簿，不猜別名。',
    )

    @model_validator(mode='after')
    def format_rules(self):
        if Path(self.name).suffix.lower() == '.xlsx':
            if self.texts:
                raise ValueError('XLSX 文字與統計必須登記在 cells，不接受 texts。Word 的 texts 請放在獨立 DOCX 檔案規格。')
        elif self.cells or self.sheet_names:
            raise ValueError('只有 XLSX 可使用 cells／sheet_names；Word 等文件請使用 texts。')
        if self.base_source is not None and (Path(self.name).suffix.lower() != '.xlsx' or Path(self.base_source.path).suffix.lower() != '.xlsx'):
            raise ValueError('原簿綁定只適用於XLSX來源及XLSX修改成果。')
        targets = [(r.sheet, r.cell) for r in self.cells]
        if len(targets) != len(set(targets)):
            raise ValueError('同一檔案的儲存格規格不可重複。')
        return self


class DeliveryPlan(Strict):
    statistics: list[StatisticsRequirement] = Field(default_factory=list, max_length=20)
    files: list[ExpectedFile] = Field(default_factory=list, max_length=30)

    @model_validator(mode='after')
    def references(self):
        keys = {s.key for s in self.statistics}
        if len(keys) != len(self.statistics) or len({f.name.casefold() for f in self.files}) != len(self.files):
            raise ValueError('統計 key 與交付檔名不可重複。')
        for file in self.files:
            for rule in [*file.cells, *file.texts]:
                if rule.statistic is not None and rule.statistic.key not in keys:
                    raise ValueError('驗收規則引用未知統計 key。')
        return self


class SupersedeArtifact(Strict):
    old_id: str = Field(min_length=1, max_length=100)
    old_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    new_id: str = Field(min_length=1, max_length=100)
    new_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
