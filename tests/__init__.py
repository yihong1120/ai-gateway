"""Project test modules."""
from __future__ import annotations

import os

from .eip_webui import load_application_modules

# Live prompt evaluations explicitly use the deployed WebUI configuration.
if os.getenv('EIP_PROMPT_LIVE') != '1':
    load_application_modules()
