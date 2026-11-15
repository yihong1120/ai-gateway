"""Single-worker Open WebUI launcher with the local EIP router installed."""
from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from examples.shared.config import load_service
from examples.shared.runtime.modules import ModuleReference

_open_webui = ModuleReference('open_webui')


def main():
    load_service('webui')
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    open_webui = _open_webui.load()

    os.environ['FROM_INIT_PY'] = 'true'
    if not os.getenv('WEBUI_SECRET_KEY'):
        key_file = Path.cwd() / '.webui_secret_key'
        if not key_file.exists():
            raise RuntimeError(
                'WEBUI_SECRET_KEY must be configured; refusing to replace the existing login key',
            )
        os.environ['WEBUI_SECRET_KEY'] = key_file.read_text()
    # `open-webui serve` initializes this path before importing open_webui.env.
    # This launcher installs the EIP router first, so keep the same persistent
    # package data directory unless an operator supplied DATA_DIR explicitly.
    # Without it, Open WebUI falls back to a sibling `site-packages/data`
    # directory and a correctly bound EIP account appears unconfigured.
    if not os.getenv('DATA_DIR'):
        os.environ['DATA_DIR'] = str(
            Path(open_webui.__file__).resolve().parent / 'data',
        )
    # Uvicorn loads the application after its environment has been configured.
    uvicorn.run(
        'examples.openwebui.app:create_app',
        factory=True,
        host='127.0.0.1',
        port=8080,
        workers=1,
        forwarded_allow_ips='127.0.0.1',
        ws_per_message_deflate=os.getenv(
            'UVICORN_WS_PER_MESSAGE_DEFLATE', 'True',
        ).lower()
        == 'true',
    )


if __name__ == '__main__':
    main()
