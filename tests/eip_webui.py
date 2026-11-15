"""Load EIP application modules with isolated WebUI database/storage boundaries."""
from __future__ import annotations

import importlib
import sys
from types import ModuleType


def load_application_modules(first=None):
    modules = {}
    for name, symbol in (
        ('open_webui.models.files', 'Files'),
        ('open_webui.models.functions', 'Functions'),
        ('open_webui.models.users', 'Users'),
        ('open_webui.models.config', 'Config'),
        ('open_webui.storage.provider', 'Storage'),
    ):
        module = ModuleType(name)
        # Tests must explicitly supply each service they use.
        setattr(module, symbol, object())
        modules[name] = module
    modules['open_webui.models.files'].File = object()
    db = ModuleType('open_webui.internal.db')
    db.get_async_db_context = object()
    modules[db.__name__] = db
    previous = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        if first is not None:
            importlib.import_module(first)
        for name in ('application.integration', 'application.automation', 'infrastructure.eip.webui_entry'):
            importlib.import_module('examples.official_document_workflow.' + name)
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
