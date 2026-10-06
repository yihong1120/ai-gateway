"""Gemini compatibility for LiteLLM tool schemas and signed conversation history."""
from __future__ import annotations

import re
from copy import deepcopy

from langchain_core.messages import AIMessage
from langchain_core.messages import ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

SEPARATOR = '__thought__'


def valid_provider_id(value):
    if not isinstance(value, str) or not value:
        return False
    if len(value) <= 128:
        return True
    base, separator, signature = value.partition(SEPARATOR)
    return bool(
        separator and 0 < len(base) <= 128 and len(value) <= 32768
        and re.fullmatch(r'[A-Za-z0-9+/=_-]+', signature),
    )


def signed_history(messages):
    """Project copies only; tool receipts/checkpoints keep stable canonical IDs."""
    ids = {}
    output = []
    for message in messages:
        if isinstance(message, AIMessage) and message.tool_calls:
            providers = message.response_metadata['provider_tool_ids_by_call']
            for call in message.tool_calls:
                provider = providers.get(call['id'], '')
                _, separator, signature = provider.partition(SEPARATOR)
                suffix = separator + signature if valid_provider_id(provider) else ''
                ids[call['id']] = call['id'] + suffix
            message = message.model_copy(
                update={
                    'tool_calls': [
                        {**call, 'id': ids[call['id']]} for call in message.tool_calls
                    ],
                },
            )
        elif isinstance(message, ToolMessage) and message.tool_call_id in ids:
            message = message.model_copy(update={'tool_call_id': ids[message.tool_call_id]})
        output.append(message)
    return output


BOUNDS = frozenset({'maxItems'})


def gemini_schema(schema):
    """Avoid Vertex constraint compilation failures without changing argument types.

    Bounds are documented for the model and still enforced by original Pydantic
    tool schemas. Walk schema nodes only: a property named `maximum` is data.
    """
    result = deepcopy(schema)
    bounds = {key: result.pop(key) for key in BOUNDS if key in result}
    if bounds:
        note = 'Server-enforced limits: ' + ', '.join(
            f"{key}={value}" for key, value in sorted(bounds.items())
        ) + '.'
        result['description'] = (result.get('description', '') + ' ' + note).strip()
    for key in ('properties', '$defs', 'definitions'):
        if key in result:
            result[key] = {name: gemini_schema(value) for name, value in result[key].items()}
    for key in ('items', 'additionalProperties'):
        if isinstance(result.get(key), dict):
            result[key] = gemini_schema(result[key])
    for key in ('anyOf', 'oneOf', 'allOf', 'prefixItems'):
        if key in result:
            result[key] = [gemini_schema(value) for value in result[key]]
    return result


def provider_tools(tools, model_name):
    if not model_name.lower().split('/')[-1].startswith('gemini-'):
        return tools
    converted = []
    for tool in tools:
        definition = deepcopy(convert_to_openai_tool(tool))
        function = definition.get('function', {})
        if 'parameters' in function:
            function['parameters'] = gemini_schema(function['parameters'])
        converted.append(definition)
    return converted
