"""Create fresh dev encryption keys; never replace keys for encrypted data."""
import os
from pathlib import Path


def ensure_private_key(path, *, has_data):
    if path.is_symlink():
        raise RuntimeError('Dev encryption key must not be a symlink: ' + path.name)
    if path.exists():
        if len(path.read_bytes()) != 32 or path.stat().st_mode & 0o077:
            raise RuntimeError('Invalid dev encryption key length or permissions: ' + path.name)
        return
    if has_data:
        raise RuntimeError('Restore the original dev encryption key; encrypted data exists: ' + path.name)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(os.urandom(32))
        stream.flush()
        os.fsync(stream.fileno())


def ensure_keys():
    from open_webui.env import DATA_DIR
    from company_agent.runtime.storage import Store
    from psycopg import sql
    directory = Path(DATA_DIR) / 'company_agent'
    expected = Path.home() / '.local/share/ai-gateway-dev/webui/company_agent'
    if directory != expected:
        raise RuntimeError('Refusing to initialize keys outside the dev state directory.')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = Store(directory)
    key_tables = {'checkpoint.key': ('checkpoints', 'checkpoint_blobs', 'checkpoint_writes'),
                  'nas.key': ('nas_bindings',)}
    with store.mutex('dev_key_setup'):
        with store.db() as db:
            for key, tables in key_tables.items():
                has_data = False
                for table in tables:
                    if db.execute('SELECT to_regclass(%s)', (store.schema + '.' + table,)).fetchone()[0]:
                        has_data |= db.execute(sql.SQL('SELECT EXISTS(SELECT 1 FROM {})').format(sql.Identifier(table))).fetchone()[0]
                ensure_private_key(directory / key, has_data=has_data)
