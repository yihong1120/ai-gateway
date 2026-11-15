"""Run a versioned identity/BFF release with the existing private BFF configuration."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import uvicorn


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    path = args.config
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise RuntimeError('BFF configuration must be a private regular file.')
    values = json.loads(path.read_text())
    environment, port = values['environment'], values['port']
    if (
        not isinstance(environment, dict) or not all(
            isinstance(k, str) and isinstance(v, str)
            for k, v in environment.items()
        )
        or type(port) is not int or not 1 <= port <= 65535
    ):
        raise RuntimeError('Invalid BFF release configuration.')
    os.environ.update(environment)
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    sys.dont_write_bytecode = True
    uvicorn.run(
        'examples.db_management.app:app', host='127.0.0.1', port=port, workers=1,
        proxy_headers=True, forwarded_allow_ips='127.0.0.1', access_log=False,
    )


if __name__ == '__main__':
    main()
