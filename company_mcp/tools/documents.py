"""Prepare analysis/calendar operations; execution requires personal confirmation."""
from __future__ import annotations

from pydantic import Field

from .common import Workspace
from company_agent.services.eip.schemas import CalendarPrepare
from company_agent.services.eip.schemas import EIPProcess


class Plan(Workspace, CalendarPrepare):
    pass


class Analysis(Workspace, EIPProcess):
    request_id: str = Field(min_length=1, max_length=100)


TOOLS = {
    'calendar_plan_prepare': (Plan, '產生待確認的私人行事曆預覽連結，尚未寫入；必須本人瀏覽器確認。'),
    'document_analysis_prepare': (Analysis, '準備公文付費分析。先回傳本人確認頁，未確認前不呼叫模型；沿用共用分析。'),
}
