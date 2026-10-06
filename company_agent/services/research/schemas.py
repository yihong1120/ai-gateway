"""Validated business inputs, shared by service and agent adapters."""
from __future__ import annotations

from pydantic import Field
from pydantic import field_validator

from company_agent.schemas import Strict


class SearchWeb(Strict):
    query: str = Field(
        min_length=1,
        max_length=300,
        description='公開網路查詢詞；不要放入密碼、個資、NAS 路徑或未授權外送的公司文件內容。',
    )
    count: int = Field(default=5, ge=1, le=5)

    @field_validator('query')
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError('Empty query')
        return value.strip()


class FetchURL(Strict):
    url: str = Field(
        min_length=1,
        max_length=2000,
        description='單一公開 HTTP(S) 網頁，不可指定內網、NAS、EIP、登入或帶憑證的連結。',
    )
