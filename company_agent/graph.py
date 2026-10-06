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
from company_agent.services.files.tools import file_tools


def specialist_tools():
    return [
        *file_tools(),
        inspection_tool(),
        artifact_edit_tool(),
        *research_tools(),
        *eip_tools(),
        *vision_tools(),
    ]


def build_graph(model, saver, structured_completion=False):
    catalog = {tool.name: tool for tool in specialist_tools()}

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
    specialists = {
        role: agent(role, [catalog[name] for name in sorted(names)])
        for role, names in ROLE_TOOLS.items() if role != 'coordinator'
    }
    return agent('coordinator', delegation_tools(specialists), saver)
