"""Shared schema binding and ToolMessage construction for company tools."""
from __future__ import annotations

import json
from functools import partial
from functools import update_wrapper
from typing import Annotated
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolArg
from langchain_core.tools import StructuredTool
from pydantic import create_model


def injected_tool(name, schema, description, invoke, input_name=None, *, handle_tool_error=False, **bindings):
    inputs = create_model(
        input_name or name + 'Input',
        __base__=schema,
        runtime=(Annotated[Any, InjectedToolArg], ...),
    )
    return StructuredTool.from_function(
        coroutine=update_wrapper(partial(invoke, **bindings), invoke) if bindings else invoke,
        name=name,
        description=description,
        args_schema=inputs,
        handle_tool_error=handle_tool_error,
    )


def tool_receipt(tool_call_id, name, result):
    return ToolMessage(
        content=json.dumps(result, ensure_ascii=False),
        tool_call_id=tool_call_id,
        id='tool-' + tool_call_id,
        name=name,
    )
