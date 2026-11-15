"""Stateless Responses envelope for local Codex tools, never a general proxy.

Upstream hosted tools and server-stored conversation IDs are deliberately outside
this credential scope. Local function/custom calls and their replayed outputs
pass through unchanged, including call IDs and opaque reasoning content.
"""
from examples.company_ai_access.errors import error


def envelope(value):
    if (value.get('previous_response_id') or value.get('conversation') or value.get('background')
            or value.get('store') not in (None, False)):
        raise error(400, 'ai_access_stateless_responses_required')
    tools = value.get('tools', [])
    if not isinstance(tools, list):
        raise error(400, 'ai_access_invalid_tools')

    def local_tools(items, nested=False):
        for tool in items:
            if not isinstance(tool, dict):
                raise error(400, 'ai_access_invalid_tools')
            kind = tool.get('type')
            if kind == 'namespace' and not nested and isinstance(tool.get('tools'), list):
                local_tools(tool['tools'], True)
            elif kind not in {'function', 'custom'}:
                raise error(400, 'ai_access_hosted_tools_not_allowed')

    local_tools(tools)
    choice = value.get('tool_choice')
    if isinstance(choice, dict):
        if choice.get('type') == 'allowed_tools':
            choices = choice.get('tools')
            if not isinstance(choices, list):
                raise error(400, 'ai_access_invalid_tools')
            local_tools(choices)
        else:
            local_tools([choice])
    elif choice not in (None, 'auto', 'none', 'required'):
        raise error(400, 'ai_access_invalid_tools')
    inputs = value.get('input')
    if not isinstance(inputs, (str, list)):
        raise error(400, 'ai_access_invalid_input')
    if isinstance(inputs, list):
        for item in inputs:
            if not isinstance(item, dict):
                raise error(400, 'ai_access_invalid_input')
            if item.get('type', 'message') not in {
                    'message', 'function_call', 'function_call_output', 'custom_tool_call',
                    'custom_tool_call_output', 'reasoning', 'compaction'}:
                raise error(400, 'ai_access_stateless_responses_required')
            # Remote images/files could make the gateway fetch untrusted URLs.
            content = item.get('content')
            if item.get('type', 'message') == 'message' and not isinstance(content, (str, list)):
                raise error(400, 'ai_access_invalid_input')
            parts = content if isinstance(content, list) else []
            if item.get('type') in {'function_call_output', 'custom_tool_call_output'} and isinstance(item.get('output'), list):
                parts = [*parts, *item['output']]
            for part in parts:
                if not isinstance(part, dict) or part.get('type') not in {'input_text', 'output_text', 'input_image'}:
                    raise error(400, 'ai_access_invalid_input')
                if part.get('type') == 'input_image' and (
                        not isinstance(part.get('image_url'), str)
                        or not part['image_url'].startswith(('data:image/png;base64,', 'data:image/jpeg;base64,', 'data:image/webp;base64,'))):
                    raise error(400, 'ai_access_remote_content_not_allowed')
    fields = {'model', 'input', 'instructions', 'tools', 'tool_choice', 'parallel_tool_calls',
              'stream', 'max_output_tokens', 'reasoning', 'text', 'temperature', 'top_p',
              'include', 'truncation'}
    body = {key: item for key, item in value.items() if key in fields}
    # Codex does not send an output limit. Bound each model turn, including
    # thinking, rather than inheriting the provider's very large default.
    if body.get('max_output_tokens') is None:
        body['max_output_tokens'] = 8192
    body['store'] = False
    return body
