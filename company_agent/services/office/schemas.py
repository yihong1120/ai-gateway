"""Validated business inputs, shared by service and agent adapters."""
from __future__ import annotations

from typing import Annotated
from typing import Literal

from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

from company_agent.schemas import Strict


class Paragraph(Strict):
    text: str = Field(max_length=5000)
    heading: bool = False


class Word(Strict):
    name: str = Field(
        min_length=6,
        max_length=180,
        pattern=r'^[^/\\\x00-\x1f]+\.[dD][oO][cC][xX]$',
        description='單一檔名，含 .docx；不是路徑，不填 root/path。',
    )
    title: str = Field(max_length=200)
    paragraphs: list[Paragraph] = Field(max_length=150)


class Slide(Strict):
    title: str = Field(max_length=80)
    bullets: list[str] = Field(max_length=6)

    @field_validator('bullets')
    @classmethod
    def bounded_bullets(cls, values):
        if any(len(v) > 100 for v in values):
            raise ValueError('Each bullet must be at most 100 characters')
        return values


class Slides(Strict):
    name: str = Field(
        min_length=6,
        max_length=180,
        pattern=r'^[^/\\\x00-\x1f]+\.[pP][pP][tT][xX]$',
        description='單一檔名，含 .pptx；不是路徑。',
    )
    slides: list[Slide] = Field(
        min_length=1,
        max_length=30,
        description='每頁一個 {title, bullets} 物件。最外層只接受 name、slides；title 必須放在頁面內，不能放在最外層。',
    )


class SingleRunReplacement(Strict):
    replacement: str = Field(max_length=5000,
        description='單一文字run的替換文字，不接受CR、LF換行或tab。多段內容另製新版；不要在此欄新增段落。')

    @field_validator('replacement')
    @classmethod
    def one_run_only(cls, value):
        if any(char in value for char in ('\r', '\n', '\t')):
            raise ValueError('單一文字run不接受換行或tab；請改用單段文字，或另製新版文件。尚未執行修改。')
        return value


class Edit(SingleRunReplacement):
    root: str
    path: str
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    location: str
    expected: str = Field(max_length=5000)
    name: str


class Text(Strict):
    name: str
    content: str = Field(max_length=100_000)


CellValue = (
    Annotated[str, Field(max_length=5000)]
    | bool
    | int
    | Annotated[float, Field(allow_inf_nan=False)]
    | None
)


class SpreadsheetSheet(Strict):
    name: str = Field(min_length=1, max_length=31, pattern=r'^[^\\/*?:\[\]\x00-\x1f]+$')
    rows: list[Annotated[list[CellValue], Field(max_length=100)]] = Field(max_length=1000)

    @field_validator('name')
    @classmethod
    def valid_sheet_name(cls, value):
        if value.startswith("'") or value.endswith("'"):
            raise ValueError('Sheet names cannot start or end with an apostrophe')
        return value


class Spreadsheet(Strict):
    name: str = Field(
        min_length=6,
        max_length=180,
        pattern=r'^[^/\\\x00-\x1f]+\.[xX][lL][sS][xX]$',
        description='單一檔名，含 .xlsx，例如 result.xlsx；不得加資料夾。結果留在私人工作區，用 inspect_artifact 讀取。',
    )
    sheets: list[SpreadsheetSheet] = Field(min_length=1, max_length=20)

    @model_validator(mode='after')
    def bounded_workbook(self):
        if len({s.name.casefold() for s in self.sheets}) != len(self.sheets):
            raise ValueError('Sheet names must be unique')
        if sum(len(row) for s in self.sheets for row in s.rows) > 50_000:
            raise ValueError('Workbook exceeds 50000 cells')
        if (
            sum(len(v) for s in self.sheets for row in s.rows for v in row if isinstance(v, str))
            > 1_000_000
        ):
            raise ValueError('Workbook text exceeds 1000000 characters')
        return self


class ReadSpreadsheet(Strict):
    root: str = Field(description='已授權的來源根目錄別名，不是 artifact ID。')
    path: str = Field(
        description='來源 XLSX 相對路徑；新建私人草稿不在此處，請改用 inspect_artifact。',
    )
    sheet: str | None = Field(
        default=None,
        max_length=31,
        description='精確工作表名稱，不能填序號或多個名稱。未知時省略/null，先讀回傳 sheets 清單，再逐張讀取。',
    )
    start_row: int = Field(default=1, ge=1, le=10_000)
    row_limit: int = Field(default=20, ge=1, le=50)
    column_limit: int = Field(default=20, ge=1, le=100)


class AnalyzeSpreadsheet(Strict):
    root: str
    path: str
    sheet: str = Field(min_length=1, max_length=31)
    column: int = Field(ge=1, le=100)
    start_row: int = Field(default=1, ge=1, le=10_000)
    end_row: int = Field(ge=1, le=10_000)

    @model_validator(mode='after')
    def ordered_range(self):
        if self.end_row < self.start_row:
            raise ValueError('end_row must be at least start_row')
        return self


class FormulaCell(Strict):
    sheet: str = Field(min_length=1, max_length=31)
    cell: str = Field(pattern=r'^[A-Z]{1,2}[1-9][0-9]{0,2}$')
    formula: str = Field(min_length=2, max_length=300)


class ReportChart(Strict):
    sheet: str = Field(min_length=1, max_length=31)
    kind: Literal['bar', 'line']
    title: str = Field(min_length=1, max_length=100)
    category_column: int = Field(ge=1, le=100)
    value_column: int = Field(ge=1, le=100)
    start_row: int = Field(ge=1, le=1000)
    end_row: int = Field(ge=1, le=1000)
    anchor: str = Field(default='E2', pattern=r'^[A-Z]{1,2}[1-9][0-9]{0,2}$')

    @model_validator(mode='after')
    def ordered(self):
        if self.end_row < self.start_row:
            raise ValueError('Invalid chart range')
        return self


class SpreadsheetReport(Spreadsheet):
    formulas: list[FormulaCell] = Field(default_factory=list, max_length=50)
    charts: list[ReportChart] = Field(default_factory=list, max_length=5)

    @model_validator(mode='after')
    def references(self):
        from openpyxl.utils.cell import coordinate_to_tuple
        from company_agent.services.office.worker import formula_valid

        sheets = {s.name: s for s in self.sheets}
        seen = set()
        for f in self.formulas:
            if f.sheet not in sheets or (f.sheet, f.cell) in seen:
                raise ValueError('Duplicate/missing formula target')
            seen.add((f.sheet, f.cell))
            sheet = sheets[f.sheet]
            rows = len(sheet.rows)
            cols = max(map(len, sheet.rows), default=0)
            r, c = coordinate_to_tuple(f.cell)
            if (
                r > rows
                or c > cols
                or c > 100
                or (c <= len(sheet.rows[r - 1]) and sheet.rows[r - 1][c - 1] is not None)
            ):
                raise ValueError('Formula requires an existing empty target')
            formula_valid(f.formula, rows, cols)
        for chart in self.charts:
            if chart.sheet not in sheets:
                raise ValueError('Missing chart sheet')
            sheet = sheets[chart.sheet]
            if chart.end_row > len(sheet.rows) or max(
                chart.category_column, chart.value_column,
            ) > max(map(len, sheet.rows), default=0):
                raise ValueError('Invalid chart range')
        return self


class CellChange(Strict):
    sheet: str = Field(min_length=1, max_length=31)
    cell: str = Field(pattern=r'^[A-Z]{1,2}[1-9][0-9]{0,3}$')
    expected: str | int | float | bool | None
    value: str | int | float | bool | None
    formula: bool = False
    style_from: str | None = Field(default=None, pattern=r'^[A-Z]{1,2}[1-9][0-9]{0,3}$')


class EditSpreadsheet(Strict):
    root: str
    path: str
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    name: str = Field(min_length=6, max_length=180, pattern=r'^[^/\\\x00-\x1f]+\.xlsx$')
    changes: list[CellChange] = Field(min_length=1, max_length=100)


class UnderstandImage(Strict):
    root: str
    path: str
    page: int = Field(default=1, ge=1, le=100)
    question: str = Field(min_length=1, max_length=1500)
    model_profile: str | None = Field(
        default=None, min_length=1, max_length=40,
        description='依圖片問題難度與成本，自行選擇可用清單中支援圖片的模型設定名稱。',
    )


class EditArtifact(SingleRunReplacement):
    artifact_id: str = Field(min_length=1, max_length=100)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    location: str = Field(
        min_length=1,
        max_length=200,
        description='inspect_artifact 的 blocks[].runs[].location，不是段落位置。',
    )
    expected: str = Field(max_length=5000)
    name: str = Field(
        min_length=6,
        max_length=180,
        description='單一 .docx/.pptx 檔名；沿用原交付檔名時建立新ID並取代舊稿，不同已核准檔名保留兩版。',
    )
