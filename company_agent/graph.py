"""Official LangChain agent loop, backed by LangGraph persistence and tools."""
from __future__ import annotations

from langchain.agents import create_agent

from company_agent.agents.delegation import delegation_tools
from company_agent.agents.document.revision import artifact_edit_tool
from company_agent.agents.eip.tools import eip_tools
from company_agent.agents.engineering.vision import vision_tools
from company_agent.agents.registry import ROLE_TOOLS
from company_agent.agents.research.tools import research_tools
from company_agent.agents.reviewer.tools import inspection_tool
from company_agent.runtime.completion import completion_tool
from company_agent.runtime.context import Context
from company_agent.runtime.middleware import CompanyPolicy
from company_agent.runtime.models import select_model
from company_agent.services.delivery.tools import delivery_tools
from company_agent.services.files.tools import file_tools


def specialist_tools():
    return [
        *file_tools(),
        inspection_tool(),
        artifact_edit_tool(),
        *research_tools(),
        *eip_tools(),
        *vision_tools(),
        *delivery_tools(),
    ]


def build_graph(model, saver, structured_completion=False, orchestration_mode='delegated', office_execution_mode='flexible', external_tools=()):
    if orchestration_mode not in {'delegated', 'hybrid', 'chat'}:
        raise ValueError('未知執行模式。')
    if office_execution_mode not in {'flexible', 'specialist'}:
        raise ValueError('未知 Office 分工模式。')
    if office_execution_mode == 'specialist' and not structured_completion:
        raise ValueError('專責 Office 分工必須啟用 structured_completion。')
    catalog = {tool.name: tool for tool in specialist_tools()}
    extra_names = {tool.name for tool in external_tools}
    if len(extra_names) != len(external_tools) or extra_names & catalog.keys():
        raise ValueError('外部工具名稱重複，未建立 Agent。')
    catalog.update({tool.name: tool for tool in external_tools})

    def agent(role, tools, checkpointer=None):
        if structured_completion:
            tools = [*tools, completion_tool(role)]
        return create_agent(
            model=select_model(model, role),
            tools=tools,
            middleware=[CompanyPolicy(role, structured_completion)],
            context_schema=Context,
            checkpointer=checkpointer,
            name=role,
        )

    # Default child checkpointers inherit the caller's saver with a separate
    # namespace per invocation, including repeated calls to the same role.
    if orchestration_mode == 'chat':
        return agent('coordinator', [], saver)
    specialists = {
        role: agent(role, [catalog[name] for name in sorted(names | (extra_names if role == 'knowledge' else set()))])
        for role, names in ROLE_TOOLS.items() if role != 'coordinator'
    }
    tools = [*delegation_tools(specialists), *delivery_tools()]
    if orchestration_mode == 'hybrid':
        # External writes, OCR, web, EIP and CAD retain their specialist boundaries.
        common = {
            'search_files', 'read_file', 'read_spreadsheet', 'analyze_spreadsheet',
            'create_text', 'create_word', 'create_slides', 'create_spreadsheet',
            'create_spreadsheet_report', 'edit_spreadsheet', 'edit_text_run',
            'inspect_artifact', 'edit_artifact_text_run',
        }
        if office_execution_mode == 'specialist':
            common = {'search_files', 'read_file', 'read_spreadsheet', 'inspect_artifact'}
        tools.extend(catalog[name] for name in sorted(common))
    return agent('coordinator', tools, saver)
