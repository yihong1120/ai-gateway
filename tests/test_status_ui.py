"""The public UI hooks preserve existing custom assets and other static routes."""
import sys
import types
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from company_agent.api.status_ui import install


def test_custom_assets_extend_existing_content_without_modifying_it(tmp_path):
    originals = {'loader.js': b'window.existingCustomization = true;', 'custom.css': b'.brand {color: blue;}'}
    for name, body in originals.items():
        (tmp_path / name).write_bytes(body)
    (tmp_path / 'other.txt').write_text('unchanged static file')
    app = FastAPI()
    app.mount('/static', StaticFiles(directory=tmp_path))
    install(app)
    with patch.dict(sys.modules, {'open_webui.config': types.SimpleNamespace(STATIC_DIR=tmp_path)}):
        with TestClient(app) as client:
            for name, body in originals.items():
                response = client.get('/static/' + name)
                assert response.status_code == 200
                assert response.content.startswith(body)
                assert len(response.content) > len(body)
                assert response.headers['cache-control'] == 'no-store'
                assert response.headers['x-content-type-options'] == 'nosniff'
                assert (tmp_path / name).read_bytes() == body
            assert client.get('/static/other.txt').text == 'unchanged static file'


def test_status_hooks_are_installed_once_and_work_with_empty_customization(tmp_path):
    app = FastAPI()
    install(app)
    install(app)
    assert sum(route.path == '/static/loader.js' for route in app.routes) == 1
    with patch.dict(sys.modules, {'open_webui.config': types.SimpleNamespace(STATIC_DIR=tmp_path)}):
        with TestClient(app) as client:
            assert client.get('/static/loader.js').status_code == 200
            assert client.get('/static/custom.css').status_code == 200
