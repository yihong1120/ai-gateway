"""Run the standalone production API with an explicit private environment."""
import json
import os
from pathlib import Path
import sys


def main():
    config = Path.home() / '.config/company-ai-access/production.json'
    if config.is_symlink() or config.stat().st_mode & 0o077:
        raise RuntimeError('Expected a private production configuration.')
    values = json.loads(config.read_text())
    if (values['AI_ACCESS_ENVIRONMENT'] != 'production'
            or values['AI_ACCESS_LITELLM_URL'] != 'http://127.0.0.1:4000'
            or values['AI_ACCESS_ORIGIN'] != 'https://changdar-server.mooo.com'
            or values['AI_ACCESS_OIDC_AUDIENCE'] != 'visionnaire-api'):
        raise RuntimeError('Production environment mismatch.')
    os.environ.update(values)
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    root = Path(__file__).resolve().parents[4]
    if not (root / 'examples/company_ai_access' / 'app.py').is_file():
        raise RuntimeError('Company AI access source package is missing.')
    sys.path.insert(0, str(root))
    import uvicorn
    uvicorn.run('examples.company_ai_access.app:create_app', factory=True, host='127.0.0.1',
                port=8013, workers=1, proxy_headers=True, forwarded_allow_ips='127.0.0.1', access_log=False)


if __name__ == '__main__':
    main()
