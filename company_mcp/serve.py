"""Run the standalone MCP edge under its dedicated service configuration."""
from __future__ import annotations

import uvicorn

import examples.shared.config as _config_imports
from .config import Configuration
from .settings import Settings


def main():
    _config_imports.load_service('company_mcp')
    if Configuration.from_environment(backend=False) is None:
        raise SystemExit('Company MCP is disabled in the dedicated service configuration.')
    settings = Settings.from_environment()
    uvicorn.run(
        'company_mcp.app:create_app', factory=True, host=settings.host, port=settings.port,
        workers=1, forwarded_allow_ips='127.0.0.1', access_log=False, ws='none',
    )


if __name__ == '__main__':
    main()
