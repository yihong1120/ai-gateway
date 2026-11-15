"""Explicit saved-chat/storage boundaries for unit tests; no host DB initialization."""
from __future__ import annotations

from types import ModuleType
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock


def modules():
    def branch(messages, identifier):
        result, seen = [], set()
        while identifier and identifier in messages and identifier not in seen:
            seen.add(identifier)
            message = messages[identifier]
            result.append(message)
            identifier = message.get('parentId')
        return list(reversed(result))

    def summary(messages):
        for index in range(len(messages) - 1, -1, -1):
            value = messages[index].get('contextSummary') or messages[index].get('context_summary')
            if isinstance(value, str) and value.strip():
                return messages[index:], value
        return messages, None

    misc = ModuleType('open_webui.utils.misc')
    misc.get_message_list = branch
    compaction = ModuleType('open_webui.utils.context_compaction')
    compaction._apply_latest_summary_checkpoint = summary
    chats = ModuleType('open_webui.models.chats')
    chats.Chats = SimpleNamespace(
        get_chat_by_id_and_user_id=AsyncMock(),
        upsert_message_to_chat_by_id_and_message_id=AsyncMock(),
        add_message_status_to_chat_by_id_and_message_id=AsyncMock(),
    )
    files = ModuleType('open_webui.models.files')
    files.Files = SimpleNamespace(get_file_by_id_and_user_id=AsyncMock())
    storage = ModuleType('open_webui.storage.provider')
    storage.Storage = SimpleNamespace(get_file=Mock())
    socket_main = ModuleType('open_webui.socket.main')
    socket_main.get_event_emitter = AsyncMock()
    socket = ModuleType('open_webui.socket')
    socket.__path__ = []
    socket.main = socket_main
    return {module.__name__: module for module in (misc, compaction, chats, files, storage, socket, socket_main)}
