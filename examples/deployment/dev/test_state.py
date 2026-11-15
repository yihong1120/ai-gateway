import pytest
from examples.deployment.dev.state import ensure_private_key


def test_fresh_key_is_private_and_survives_repeated_setup(tmp_path):
    key = tmp_path / 'checkpoint.key'
    ensure_private_key(key, has_data=False)
    original = key.read_bytes()
    assert len(original) == 32
    assert key.stat().st_mode & 0o777 == 0o600
    ensure_private_key(key, has_data=True)
    assert key.read_bytes() == original


def test_missing_key_for_encrypted_data_requires_restore(tmp_path):
    key = tmp_path / 'checkpoint.key'
    with pytest.raises(RuntimeError, match='Restore the original'):
        ensure_private_key(key, has_data=True)
    assert not key.exists()


def test_invalid_existing_key_is_not_replaced(tmp_path):
    key = tmp_path / 'nas.key'
    key.write_bytes(b'existing-invalid-key')
    key.chmod(0o600)
    with pytest.raises(RuntimeError, match='Invalid'):
        ensure_private_key(key, has_data=False)
    assert key.read_bytes() == b'existing-invalid-key'


def test_symlink_cannot_redirect_key_creation(tmp_path):
    key = tmp_path / 'nas.key'
    target = tmp_path / 'elsewhere'
    key.symlink_to(target)
    with pytest.raises(RuntimeError, match='symlink'):
        ensure_private_key(key, has_data=False)
    assert not target.exists()
