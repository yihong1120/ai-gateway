"""Strict protocol arguments, preserving the SDK's public argument interface."""
from __future__ import annotations

from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase
from pydantic import ConfigDict
from pydantic import Field


class Arguments(ArgModelBase):
    model_config = ConfigDict(extra='forbid', strict=True)


class Empty(Arguments):
    pass


class NewWorkspace(Arguments):
    request_id: str = Field(min_length=1, max_length=100)


class Workspace(Arguments):
    workspace: str = Field(pattern=r'^[a-f0-9]{64}$')


class Operation(Arguments):
    operation: str = Field(pattern=r'^[a-f0-9]{32}$')


class Artifact(Workspace):
    artifact: str = Field(min_length=1, max_length=100)


TOOLS = {
    'connections_list': (Empty, '列出本人 EIP／NAS 綁定狀態，不含帳密。所有有效本人綁定預設可用。'),
    'workspace_prepare': (NewWorkspace, '建立本人全部有效 NAS／EIP 綁定的版本快照。request_id 在重試時保持相同。'),
    'operation_status': (Operation, '查本人操作與成果。結果不明不自動重送，不重新分析。'),
    'artifact_get': (Artifact, '取得本人私人成果下載連結；仍需本人網站登入，不提供密碼或公開下載。'),
}
