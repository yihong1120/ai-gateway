from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
from unittest.mock import patch

import pytest

from examples.deployment.dev.files import save


def test_private_generated_files_keep_json_and_text_formats(tmp_path):
    snapshot = tmp_path / 'private' / 'webui.json'
    save(snapshot, {'LABEL': '測試設定'})
    assert json.loads(snapshot.read_text()) == {'LABEL': '測試設定'}
    assert snapshot.stat().st_mode & 0o777 == 0o600
    configuration = snapshot.with_name('litellm.yaml')
    save(configuration, 'model_list: []\n')
    assert configuration.read_text() == 'model_list: []\n'
    assert configuration.stat().st_mode & 0o777 == 0o600


def test_concurrent_saves_each_publish_a_complete_private_snapshot(tmp_path):
    snapshot = tmp_path / 'webui.json'
    values = [{'VALUE': 'first' * 1000}, {'VALUE': 'second' * 1000}]
    ready = threading.Barrier(2)
    replace = Path.replace

    def publish(temporary, destination):
        ready.wait(timeout=5)
        return replace(temporary, destination)

    with patch.object(Path, 'replace', publish), ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda value: save(snapshot, value), values))
    assert json.loads(snapshot.read_text()) in values
    assert snapshot.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [snapshot]


def test_failed_publish_preserves_existing_snapshot_and_cleans_temporary_file(tmp_path):
    snapshot = tmp_path / 'webui.json'
    save(snapshot, {'VALUE': 'existing'})
    original = snapshot.read_bytes()
    with patch.object(Path, 'replace', side_effect=OSError('synthetic replace failure')):
        with pytest.raises(OSError, match='synthetic replace failure'):
            save(snapshot, {'VALUE': 'replacement'})
    assert snapshot.read_bytes() == original
    assert list(tmp_path.iterdir()) == [snapshot]
