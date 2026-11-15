"""Validated HTTP request contracts for the EIP API."""
from __future__ import annotations

from datetime import date
from typing import Annotated
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from examples.official_document_workflow.domain.limits import MAX_EIP_ATTACHMENTS

Handle = Annotated[str, Field(min_length=1, max_length=200)]


class RequestBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)


class LoginRequest(RequestBody):
    accept_http: Literal[True]
    account: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)
    enable_auto: bool = False
    # None preserves the original auto-only persistence contract.
    save_credentials: bool | None = None

    @model_validator(mode='after')
    def persistence(self):
        if self.enable_auto and self.save_credentials is False:
            raise ValueError('自動處理需要本人同意保存憑證。')
        return self


class DocumentsRequest(RequestBody):
    folder: Literal['inbox', 'sign', 'notify', 'sent']
    start: date = Field(strict=False)
    end: date = Field(strict=False)
    page: int = Field(default=0, ge=0, le=500)

    @model_validator(mode='after')
    def bounded_window(self):
        if not 0 <= (self.end - self.start).days <= 366:
            raise ValueError('document date window must be between 0 and 366 days')
        return self


class AttachmentRequest(RequestBody):
    handle: Handle


class ProcessRequest(RequestBody):
    instruction: str = Field(default='', max_length=4000)
    handles: list[Handle] = Field(min_length=1, max_length=MAX_EIP_ATTACHMENTS)

    @model_validator(mode='after')
    def unique_handles(self):
        if len(set(self.handles)) != len(self.handles):
            raise ValueError('attachment handles must be unique')
        return self


class PreviewRequest(RequestBody):
    file_id: Handle


class ConfirmRequest(RequestBody):
    confirm: Literal[True]
    token: Handle
