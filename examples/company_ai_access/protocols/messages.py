"""Anthropic request fields permitted by the company device credential."""

FIELDS = frozenset({
    'model', 'messages', 'max_tokens', 'system', 'tools', 'tool_choice',
    'temperature', 'top_p', 'top_k', 'stop_sequences', 'stream', 'thinking',
    'output_config', 'context_management', 'service_tier',
})


def envelope(value):
    # Exclude provider URLs, credentials and routing overrides before transport.
    return {key: item for key, item in value.items() if key in FIELDS}
