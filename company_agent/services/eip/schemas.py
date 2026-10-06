"""Validated business inputs, shared by service and agent adapters."""
from __future__ import annotations

import re
from datetime import date
from datetime import datetime
from typing import Literal

from pydantic import Field
from pydantic import model_validator

from company_agent.schemas import Strict


class EIPQuery(Strict):
    folder: Literal['inbox', 'sign', 'notify', 'sent']
    start: str
    end: str
    page: int = Field(default=0, ge=0, le=500)

    @model_validator(mode='after')
    def window(self):
        if not 0 <= (date.fromisoformat(self.end) - date.fromisoformat(self.start)).days <= 366:
            raise ValueError('Date range exceeds one year')
        return self


class EIPAttachments(Strict):
    handle: str = Field(pattern=r'^[a-f0-9]{32}$')


class EIPRead(EIPAttachments):
    mode: Literal['text', 'ocr'] = 'text'
    page: int = Field(default=1, ge=1, le=100)
    start: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=5, ge=1, le=5)


class EIPProcess(Strict):
    handles: list[str] = Field(min_length=1, max_length=5)
    instruction: str = Field(default='', max_length=4000)
    model_profile: str | None = Field(
        default=None, min_length=1, max_length=40,
        description='依附件內容及指令難度與成本，自行選擇可用清單中的模型設定名稱。',
    )

    @model_validator(mode='after')
    def unique_handles(self):
        import re

        if len(set(self.handles)) != len(self.handles) or any(
            not re.fullmatch('[a-f0-9]{32}', x) for x in self.handles
        ):
            raise ValueError('Invalid attachment handles')
        return self


class CalendarQuery(Strict):
    start: str = Field(description='台北日期 YYYY-MM-DD，含當日')
    end: str = Field(description='台北日期 YYYY-MM-DD，含當日；最多 93 天')

    @model_validator(mode='after')
    def window(self):
        start, end = date.fromisoformat(self.start), date.fromisoformat(self.end)
        if start.isoformat() != self.start or end.isoformat() != self.end or not 0 <= (end-start).days <= 92:
            raise ValueError('Calendar range exceeds 93 days')
        return self


class CalendarEvent(Strict):
    subject: str = Field(min_length=1, max_length=200)
    start_date: str = Field(description='YYYY-MM-DD；使用者指定或確認的日期')
    end_date: str = Field(description='YYYY-MM-DD；全天多日包含結束當日')
    all_day: bool = Field(description='使用者指定全天才填 true；不確定時間請先問')
    start_time: str = Field(default='', description='HH:MM 台北時間；全天留空')
    end_time: str = Field(default='', description='HH:MM 台北時間；非全天必填，不自行猜結束時間')
    description: str = Field(default='', max_length=4000)

    @model_validator(mode='after')
    def valid(self):
        a, b = date.fromisoformat(self.start_date), date.fromisoformat(self.end_date)
        if (
            a.isoformat() != self.start_date
            or b.isoformat() != self.end_date
            or not 0 <= (b - a).days <= 366
        ):
            raise ValueError('需有效起訖日期，最長一年')
        if not self.subject.strip() or any(
            ord(c) < 32 and c not in '\r\n\t' for c in self.subject + self.description
        ):
            raise ValueError('行程文字無效')
        if any(
            v.lstrip().startswith(('=', '+', '-', '@')) for v in (self.subject, self.description)
        ):
            raise ValueError('CSV 文字不可使用試算表公式前綴')
        if self.all_day:
            if self.start_time or self.end_time:
                raise ValueError('全天行程不可同時指定時間')
        else:
            for t in (self.start_time, self.end_time):
                if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', t):
                    raise ValueError('請提供完整 HH:MM 起訖時間')
            if datetime.fromisoformat(
                self.end_date + 'T' + self.end_time,
            ) <= datetime.fromisoformat(self.start_date + 'T' + self.start_time):
                raise ValueError('結束時間必須晚於開始')
        return self


class CalendarPrepare(Strict):
    events: list[CalendarEvent] = Field(min_length=1, max_length=20)

    @model_validator(mode='after')
    def distinct(self):
        keys = [e.model_dump_json() for e in self.events]
        if len(keys) != len(set(keys)):
            raise ValueError('請移除重複行程')
        return self
