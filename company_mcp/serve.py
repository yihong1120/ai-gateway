"""Run the standalone MCP edge under its dedicated service configuration."""
def main():
    from examples.shared.config import load_service
    load_service('company_mcp')
    from .config import Configuration
    from .settings import Settings
    if Configuration.from_environment(backend=False) is None:
        raise SystemExit('Company MCP is disabled in the dedicated service configuration.')
    settings = Settings.from_environment()
    import uvicorn
    uvicorn.run('company_mcp.app:create_app', factory=True, host=settings.host, port=settings.port,
                workers=1, forwarded_allow_ips='127.0.0.1', access_log=False, ws='none')


if __name__ == '__main__':
    main()
