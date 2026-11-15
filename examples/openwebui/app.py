"""Configure the WebUI environment before loading the host composition."""
from __future__ import annotations

import os

from examples.shared.config import load_service
from examples.shared.runtime.modules import ModuleReference

_configured_host = ModuleReference('examples.openwebui._host')

load_service('webui')
os.environ['PYTHON_DOTENV_DISABLED'] = '1'

webui_app = _configured_host.attribute('webui_app')
create_app = _configured_host.attribute('create_app')
