"""Explicit producer boundaries; task text is never an authorization grant."""
from __future__ import annotations

from pathlib import PurePosixPath

from company_agent.services.files.errors import RecoverableToolError


OFFICE_OUTPUTS = {
    'data': {'.xlsx'},
    'document': {'.docx', '.pptx', '.txt', '.md', '.csv'},
}


def check_assignment(settings, role, work_key, outputs):
    if settings.office_execution_mode != 'specialist':
        return
    if not work_key:
        raise RecoverableToolError('work_key_required', '專責分工須指定穩定 work_key；同一子任務不要換名稱重派。修訂或補做使用新 key 並明列前次委派 ID。')
    if len({name.casefold() for name in outputs}) != len(outputs):
        raise RecoverableToolError('duplicate_output', '同一委派的 outputs 檔名不可重複。')
    for name in outputs:
        if PurePosixPath(name).suffix.lower() not in OFFICE_OUTPUTS.get(role, set()):
            raise RecoverableToolError('wrong_producer', 'Office 產檔由 data 負責 XLSX、document 負責 Word／PPT／文字；其他角色不透過 outputs 申請產檔。EIP／CAD 使用既有領域契約。',
                                       role=role, name=name)


def check_output(ctx, name):
    if ctx.settings.office_execution_mode != 'specialist':
        return
    # EIP and CAD have dedicated, independently verified output contracts.
    if ctx.role not in {'coordinator', 'data', 'document', 'reviewer'}:
        return
    if PurePosixPath(name).suffix.lower() not in OFFICE_OUTPUTS.get(ctx.role, set()):
        raise RecoverableToolError('wrong_producer', '此角色不能產製此 Office 檔案；請交回主協調，委派正確專責角色。',
                                   role=ctx.role, name=name)
    if name not in ctx.assignment_outputs:
        raise RecoverableToolError('output_not_assigned', '只能製作本次委派 outputs 明列的檔案；唯讀任務不得自行產檔。',
                                   name=name, allowed_outputs=list(ctx.assignment_outputs))
