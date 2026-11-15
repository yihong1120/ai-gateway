"""Per-user Synology File Station access. Account roles allowed; no write API."""
from __future__ import annotations

import hashlib
import json
import os
import time
import unicodedata
import uuid
from contextlib import contextmanager
from contextlib import ExitStack
from contextlib import nullcontext
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from company_agent.services.files.access import FolderAccess
from company_agent.services.files.access import relative_parts


class NASUnavailable(ConnectionError):
    """Safe connectivity detail, without credentials or request payloads."""


def endpoint():
    url = os.getenv('COMPANY_AGENT_NAS_URL', '').rstrip('/')
    return validate_endpoint(url)


def validate_endpoint(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != 'https'
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError('NAS 尚未配置有效的 HTTPS 端點，請聯絡管理員。')
    return url


class NASClient:
    def __init__(self, url, account, password, otp=None, transport=None):
        validate_endpoint(url)
        # Keep the bound NAS identity/revision while an administrator changes
        # its verified network route (e.g. office relocation to QuickConnect).
        connect_url = os.getenv('COMPANY_AGENT_NAS_CONNECT_URL', '').rstrip('/')
        if connect_url and url == os.getenv('COMPANY_AGENT_NAS_URL', '').rstrip('/'):
            url = validate_endpoint(connect_url)
        self.http = httpx.Client(
            base_url=url, timeout=httpx.Timeout(30, connect=5, write=15, pool=5),
            follow_redirects=False, trust_env=False, transport=transport,
        )
        self.account, self.password, self.otp = account, password, otp
        self.sid = None

    @contextmanager
    def stream(self, params):
        try:
            with self.http.stream('POST', '/webapi/entry.cgi', data=params) as response:
                yield response
        except (httpx.ConnectTimeout, httpx.ConnectError) as error:
            raise NASUnavailable(
                '目前無法連線至 NAS，或 HTTPS 憑證驗證未通過；'
                '請在「帳號與連線」測試連線，並請管理員檢查搬遷後的 NAS 位址與網路。'
                '尚未取得 NAS 資料，不能判定沒有相關檔案。',
            ) from error
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise NASUnavailable(
                'NAS 回應逾時或連線中斷；本次資料未完整取得，請在「帳號與連線」測試後再查詢。',
            ) from error

    def _json(self, api, method, version=2, **args):
        allowed = {
            'SYNO.API.Auth': {'login', 'logout'},
            'SYNO.FileStation.Info': {'get'},
            'SYNO.FileStation.List': {'list_share', 'list', 'getinfo'},
        }
        if method not in allowed.get(api, set()):
            raise ValueError('NAS 操作不在唯讀允許清單。')
        params = {'api': api, 'method': method, 'version': str(version), **args}
        with self.stream(params) as r:
            r.raise_for_status()
            data = bytearray()
            deadline = time.monotonic() + 30
            for chunk in r.iter_bytes():
                if time.monotonic() > deadline:
                    raise ValueError('NAS 回應超時。')
                data.extend(chunk)
                if len(data) > 2_000_000:
                    raise ValueError('NAS 清單過大，請縮小範圍。')
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError('NAS 連線或回應格式異常；未自動重送。')
        if value.get('success') is not True:
            if api == 'SYNO.API.Auth' and method == 'login':
                raise PermissionError(
                    'NAS 登入未成功；請確認個人帳號與密碼。目前不支援每次需驗證碼的登入，請勿為此關閉雙因素驗證。',
                )
            code = (
                value.get('error', {}).get('code')
                if isinstance(value.get('error'), dict)
                else None
            )
            detail = f"（錯誤碼 {code}）" if type(code) is int else ''
            raise ValueError(f"NAS 拒絕操作或 Session 已失效{detail}；請重新測試連線。")
        return value.get('data', {})

    def __enter__(self):
        with ExitStack() as cleanup:
            cleanup.callback(self.close)
            args = {
                'account': self.account,
                'passwd': self.password,
                'session': 'FileStation',
                'format': 'cookie',
            }
            if self.otp:
                args['otp_code'] = self.otp
            self.sid = self._json('SYNO.API.Auth', 'login', 6, **args).get('sid')
            if not isinstance(self.sid, str) or not self.sid:
                raise PermissionError('NAS 未提供有效登入 Session。')
            # httpx carries DSM's id cookie for all File Station calls and logout.
            # Keep the SID out of form parameters and URLs.
            # Verify File Station connectivity, without restricting DSM roles.
            self._json('SYNO.FileStation.Info', 'get')
            cleanup.pop_all()
            return self

    def close(self):
        try:
            if self.sid:
                self._json('SYNO.API.Auth', 'logout', 6, session='FileStation')
        finally:
            self.sid = None
            self.password = None
            self.otp = None
            self.http.cookies.clear()
            self.http.close()

    def __exit__(self, *args):
        self.close()

    def shares(self):
        out = []
        for offset in range(0, 1000, 100):
            value = self._json(
                'SYNO.FileStation.List', 'list_share', offset=str(offset), limit='100',
            )
            rows = value.get('shares')
            if not isinstance(rows, list):
                raise ValueError('NAS 共享目錄清單不完整。')
            for row in rows:
                path = row.get('path', '')
                if not isinstance(path, str) or not path.startswith('/'):
                    continue
                name = path[1:]
                parts = relative_parts(name)
                if len(parts) != 1 or row.get('isdir') is not True:
                    continue
                out.append({'path': path, 'name': name})
            if offset + len(rows) >= value.get('total', 0):
                return out
            if not rows:
                break
        raise ValueError('NAS 共享目錄超過可驗證上限；未宣稱完整列出。')

    def info(self, path):
        value = self._json(
            'SYNO.FileStation.List',
            'getinfo',
            path=json.dumps([path]),
            additional=json.dumps(['real_path', 'size', 'time', 'mount_point_type']),
        )
        rows = value.get('files', [])
        if len(rows) != 1 or rows[0].get('path') != path:
            raise ValueError('NAS 檔案位置無法核對。')
        return rows[0]

    def listing(self, path, offset=0, limit=100):
        return self._json(
            'SYNO.FileStation.List',
            'list',
            folder_path=json.dumps(path),
            offset=str(offset),
            limit=str(limit),
            sort_by='name',
            sort_direction='asc',
            additional=json.dumps(['real_path', 'size', 'time', 'mount_point_type']),
        )

    def download(self, path, limit):
        params = {
            'api': 'SYNO.FileStation.Download',
            'version': '2',
            'method': 'download',
            'path': json.dumps([path]),
            'mode': 'open',
        }
        with self.stream(params) as response:
            if response.status_code != 200:
                raise ValueError('NAS 拒絕檔案下載。')
            data = bytearray()
            deadline = time.monotonic() + 30
            for chunk in response.iter_bytes():
                if time.monotonic() > deadline:
                    raise ValueError('NAS 下載超時。')
                data.extend(chunk)
                if len(data) > limit:
                    raise ValueError('NAS 檔案超過閱讀大小上限。')
            return bytes(data)


class NASVault:
    def __init__(self, store, client_factory=None):
        self.store, self.client_factory = store, client_factory or NASClient

    def cipher(self):
        return AESGCM((self.store.directory / 'nas.key').read_bytes())

    @staticmethod
    def binding_id(value):
        if not isinstance(value, str) or len(value) != 32 or any(c not in '0123456789abcdef' for c in value):
            raise ValueError('NAS 綁定識別碼無效。')
        return value

    def bindings(self, owner):
        with self.store.db() as db:
            rows = db.execute(
                'SELECT binding_id,ciphertext,revision,updated,usable FROM nas_bindings WHERE owner=%s ORDER BY binding_id',
                (owner,),
            ).fetchall()
        result = []
        for binding_id, ciphertext, revision, updated, usable in rows:
            data = self.decrypt(owner, revision, ciphertext)
            result.append({
                'binding_id': binding_id, 'account': data['account'],
                'label': data.get('label') or data['account'], 'revision': revision,
                'updated': updated, 'usable': usable, 'bound': True, 'read_only': True,
            })
            data.clear()
        return result

    def status(self, owner, binding_id=None):
        if binding_id is not None:
            self.binding_id(binding_id)
        with self.store.db() as db:
            row = db.execute(
                'SELECT revision,updated,usable,binding_id FROM nas_bindings WHERE owner=%s '
                'AND (%s::text IS NULL OR binding_id=%s) ORDER BY binding_id LIMIT 1',
                (owner, binding_id, binding_id),
            ).fetchone()
        if binding_id is not None and row is None:
            raise ValueError('NAS 綁定不存在或沒有權限。')
        return {
            'bound': bool(row),
            'revision': row[0] if row else None,
            'updated': row[1] if row else None,
            'usable': row[2] if row else False,
            'binding_id': row[3] if row else None,
            'read_only': True,
        }

    def decrypt(self, owner, revision, ciphertext):
        return json.loads(
            self.cipher().decrypt(
                ciphertext[:12], ciphertext[12:], (owner + ':' + revision).encode(),
            ),
        )

    def bind(self, owner, account, password, otp=None, *, binding_id=None, label=None):
        if (
            any(not isinstance(v, str) or not v or len(v) > 256 for v in (account, password))
            or account != account.strip()
        ):
            raise ValueError('請輸入有效的 NAS 帳號與密碼。')
        if otp is not None and (not isinstance(otp, str) or len(otp) > 16):
            raise ValueError('驗證碼格式錯誤。')
        if binding_id is not None:
            self.status(owner, binding_id)  # Authorize before any remote authentication.
        if label is not None and (not isinstance(label, str) or not label.strip() or len(label) > 80):
            raise ValueError('NAS 連線名稱須為 1–80 字。')
        url = endpoint()
        now = time.time()
        request_token = uuid.uuid4().hex
        # Rate limit before network authentication, not after rejected passwords.
        with self.store.db() as db:
            self.store.lock(db)
            prior = db.execute(
                'SELECT next_at FROM nas_login_limits WHERE owner=%s', (owner,),
            ).fetchone()
            if prior and prior[0] > now:
                raise ValueError('請等候一分鐘再測試 NAS 登入。')
            db.execute(
                'INSERT INTO nas_login_limits VALUES (%s,%s,%s) ON CONFLICT(owner) DO UPDATE SET next_at=excluded.next_at,request_token=excluded.request_token',
                (owner, now + 60, request_token),
            )
        with self.account_operation(url, account):
            with self.client_factory(url, account, password, otp) as client:
                shares = client.shares()
        revision = uuid.uuid4().hex
        plaintext = json.dumps(
            {'url': url, 'account': account, 'password': password, 'label': label or account}, ensure_ascii=False,
        ).encode()
        nonce = os.urandom(12)
        ciphertext = nonce + self.cipher().encrypt(
            nonce, plaintext, (owner + ':' + revision).encode(),
        )
        account_hash = hashlib.sha256(
            (url + '\0' + unicodedata.normalize('NFC', account).casefold()).encode(),
        ).hexdigest()
        with self.store.db() as db:
            self.store.lock(db)
            token = db.execute(
                'SELECT request_token FROM nas_login_limits WHERE owner=%s', (owner,),
            ).fetchone()
            if not token or token[0] != request_token:
                raise ValueError('綁定操作已取消或被更新。')
            other = db.execute(
                'SELECT owner,binding_id FROM nas_bindings WHERE account_hash=%s', (account_hash,),
            ).fetchone()
            if other and other[0] != owner:
                raise ValueError('此 NAS 帳號已綁定另一位使用者；請使用自己的帳號。')
            if other and binding_id is not None and other[1] != binding_id:
                raise ValueError('此帳號已有綁定，請更新原連線。')
            if binding_id is not None:
                if not db.execute(
                    'SELECT 1 FROM nas_bindings WHERE owner=%s AND binding_id=%s',
                    (owner, binding_id),
                ).fetchone():
                    raise ValueError('NAS 綁定已解除，請重新新增連線。')
            selected = binding_id or (other[1] if other else uuid.uuid4().hex)
            count = db.execute('SELECT count(*) FROM nas_bindings WHERE owner=%s', (owner,)).fetchone()[0]
            if not binding_id and not other and count >= 16:
                raise ValueError('每人最多綁定 16 組 NAS 帳號。')
            db.execute(
                'INSERT INTO nas_bindings(owner,account_hash,ciphertext,revision,updated,usable,binding_id) '
                'VALUES (%s,%s,%s,%s,%s,TRUE,%s) ON CONFLICT(owner,binding_id) DO UPDATE SET '
                'account_hash=excluded.account_hash,ciphertext=excluded.ciphertext,revision=excluded.revision,updated=excluded.updated,usable=TRUE',
                (owner, account_hash, ciphertext, revision, now, selected),
            )
        return {**self.status(owner, selected), 'bindings': self.bindings(owner), 'shares': shares}

    def unlink(self, owner, binding_id=None):
        with self.store.db() as db:
            self.store.lock(db)
            # Legacy callers with several accounts must explicitly select one.
            rows = db.execute(
                'SELECT binding_id FROM nas_bindings WHERE owner=%s ORDER BY binding_id',
                (owner,),
            ).fetchall()
            if binding_id is None:
                if len(rows) > 1:
                    raise ValueError('請選擇要解除的 NAS 連線。')
                binding_id = rows[0][0] if rows else None
            else:
                self.binding_id(binding_id)
                if binding_id not in {row[0] for row in rows}:
                    raise ValueError('NAS 綁定不存在或沒有權限。')
            db.execute('DELETE FROM nas_bindings WHERE owner=%s AND binding_id=%s', (owner, binding_id))
            db.execute(
                'INSERT INTO nas_login_limits VALUES (%s,%s,%s) ON CONFLICT(owner) DO UPDATE SET next_at=excluded.next_at,request_token=excluded.request_token',
                (owner, time.time() + 60, uuid.uuid4().hex),
            )

    def credentials(self, owner, revision, *, db=None):
        with (nullcontext(db) if db is not None else self.store.db()) as connection:
            row = connection.execute(
                'SELECT ciphertext,revision,usable FROM nas_bindings WHERE owner=%s AND revision=%s', (owner, revision),
            ).fetchone()
        if not row or not row[2]:
            raise ValueError('NAS 綁定已解除、變更或失效；請重新綁定並另開工作。')
        data = self.decrypt(owner, revision, row[0])
        if data['url'] != endpoint():
            raise ValueError('NAS 端點已變更，請重新綁定。')
        return data

    @contextmanager
    def account_operation(self, url, account):
        # DSM FileStation sessions for one account may interfere on parallel
        # login/logout. Serialize the whole lifecycle across workers, not just
        # login, without sharing credentials or retrying a remote operation.
        digest = hashlib.sha256(
            (url + '\0' + unicodedata.normalize('NFC', account).casefold()).encode(),
        ).hexdigest()
        with self.store.db() as db:
            db.execute("SET LOCAL lock_timeout='55000'; SET LOCAL statement_timeout='55000'")
            db.execute(
                'SELECT pg_advisory_xact_lock(%s)',
                (self.store.lock_id('nas-session:' + digest),),
            )
            yield db

    @contextmanager
    def session(self, owner, revision):
        data = self.credentials(owner, revision)
        try:
            with self.account_operation(data['url'], data['account']) as db:
                self.credentials(owner, revision, db=db).clear()  # May change while waiting.
                with self.client_factory(data['url'], data['account'], data['password']) as client:
                    yield client
                    self.credentials(owner, revision, db=db).clear()  # Recheck before publishing.
        finally:
            data.clear()


class NASAccess:
    def __init__(self, vault, owner, revision, max_bytes):
        self.vault, self.owner, self.revision, self.max_bytes = vault, owner, revision, max_bytes
        self.roots = None

    def mapping(self, client):
        return {
            'nas_'
            + self.revision[:8]
            + '_'
            + hashlib.sha256(s['path'].encode()).hexdigest()[:16]: s
            for s in client.shares()
        }

    def visible_roots(self):
        with self.vault.session(self.owner, self.revision) as client:
            self.roots = self.mapping(client)
        return [
            {'name': k, 'label': v['name'], 'can_publish': False, 'source': 'NAS'}
            for k, v in self.roots.items()
        ]

    def allowed(self, root, write=False):
        if write:
            raise ValueError('NAS 目前為唯讀，不提供寫入、刪除或覆寫。')
        self.vault.credentials(self.owner, self.revision)
        if self.roots is None:
            self.visible_roots()
        if root not in self.roots:
            raise ValueError('NAS 資料夾不存在或没有授權。')

    def real_base(self, client, base):
        info = client.info(base)
        extra = info.get('additional', {})
        real = extra.get('real_path')
        if (
            info.get('isdir') is not True
            or not isinstance(real, str)
            or not real.startswith('/')
            or extra.get('mount_point_type')
        ):
            raise ValueError('NAS 共享資料夾實際位置無法驗證。')
        return real.rstrip('/')

    def contained(self, real, base):
        return (
            isinstance(real, str)
            and real.startswith(base + '/')
            and not any(p in ('.', '..') for p in real.split('/'))
        )

    @contextmanager
    def operation(self, root, path=None):
        parts = relative_parts(path) if path is not None else []
        if any(p.startswith(('@', '#')) for p in parts):
            raise ValueError('不讀取 NAS 系統或回收目錄。')
        with self.vault.session(self.owner, self.revision) as client:
            mapping = self.mapping(client)
            if root not in mapping:
                raise ValueError('NAS 資料夾不存在或授權已變更。')
            base = mapping[root]['path']
            yield client, base, base + ('/' + '/'.join(parts) if parts else '')

    def read(self, root, path):
        with self.operation(root, path) as (client, base, full):
            real = self.real_base(client, base)
            before = client.info(full)
            extra = before.get('additional', {})
            if (
                before.get('isdir') is not False
                or extra.get('mount_point_type')
                or not self.contained(extra.get('real_path'), real)
                or not isinstance(extra.get('size'), int)
                or extra['size'] > self.max_bytes
            ):
                raise ValueError('NAS 僅接受大小可驗證的一般檔案。')
            data = client.download(full, self.max_bytes)
            after = client.info(full).get('additional', {})
            if (
                len(data) != extra['size']
                or any(after.get(k) != extra.get(k) for k in ('size', 'real_path'))
                or any(
                    after.get('time', {}).get(k) != extra.get('time', {}).get(k)
                    for k in ('mtime', 'ctime')
                )
            ):
                raise ValueError('NAS 檔案在讀取期間變更；請重新操作。')
        return data, hashlib.sha256(data).hexdigest()

    def listing(self, root, path='', offset=0, limit=50):
        if type(offset) is not int or not 0 <= offset <= 10000 or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('NAS 分頁範圍無效。')
        with self.operation(root, path or None) as (client, base, full):
            real = self.real_base(client, base)
            parent = client.info(full)
            extra = parent.get('additional', {})
            parent_real = extra.get('real_path')
            # An empty relative path lists the authorized share itself.
            # Descendant paths must still resolve strictly inside that share.
            allowed = parent_real == real if not path else self.contained(parent_real, real)
            if parent.get('isdir') is not True or extra.get('mount_point_type') or not allowed:
                raise ValueError('NAS 資料夾不存在或沒有授權。')
            value = client.listing(full, offset, limit)
            rows = value.get('files')
            if not isinstance(rows, list) or len(rows) > limit:
                raise ValueError('NAS 清單回應不完整。')
            result = []
            for row in rows:
                name = row.get('name', '')
                parts = relative_parts(name)
                extra = row.get('additional', {})
                if (
                    len(parts) != 1 or name.startswith(('@', '#'))
                    or row.get('path') != full + '/' + name or extra.get('mount_point_type')
                    or not self.contained(extra.get('real_path'), real)
                ):
                    continue
                result.append({
                    'name': name, 'path': (path + '/' if path else '') + name,
                    'is_directory': row.get('isdir') is True,
                })
            total = value.get('total')
            has_more = offset + len(rows) < total if type(total) is int else len(rows) == limit
            return {
                'entries': result, 'next_offset': offset + len(rows) if has_more and rows else None,
                'filtered': len(result) != len(rows),
            }

    def scan(self, root, cap=300):
        found = []
        visited = 0
        incomplete = False
        requests = 0
        with self.operation(root) as (client, base, _):
            deadline = time.monotonic() + 30
            real = self.real_base(client, base)
            pending = [('', 0)]
            while pending and visited < cap:
                prefix, depth = pending.pop(0)
                offset = 0
                while visited < cap:
                    if requests >= 20 or time.monotonic() > deadline:
                        return sorted(found, key=lambda r: r['path']), True
                    value = client.listing(
                        base + ('/' + prefix if prefix else ''), offset, min(100, cap - visited),
                    )
                    requests += 1
                    rows = value.get('files')
                    if not isinstance(rows, list):
                        raise ValueError('NAS 清單格式不完整。')
                    for row in rows:
                        visited += 1
                        name = row.get('name', '')
                        parts = relative_parts(name)
                        if len(parts) != 1 or name.startswith(('@', '#')):
                            continue
                        path = prefix + '/' + name if prefix else name
                        if row.get('path') != base + '/' + path:
                            continue
                        extra = row.get('additional', {})
                        if extra.get('mount_point_type') or not self.contained(
                            extra.get('real_path'), real,
                        ):
                            incomplete = True
                            continue
                        if row.get('isdir'):
                            if depth < 8:
                                pending.append((path, depth + 1))
                            else:
                                incomplete = True
                        else:
                            found.append(
                                {
                                    'path': path,
                                    'bytes': extra.get('size'),
                                    'mtime_ns': int(extra.get('time', {}).get('mtime', 0))
                                    * 1_000_000_000,
                                },
                            )
                    offset += len(rows)
                    if offset >= value.get('total', 0):
                        break
                    if not rows:
                        incomplete = True
                        break
                if offset < value.get('total', 0):
                    incomplete = True
            incomplete = incomplete or bool(pending)
        return sorted(found, key=lambda r: r['path']), incomplete


def binding_revisions(settings):
    return tuple(
        getattr(settings, 'nas_binding_revisions', ()) or
        ([settings.nas_binding_revision] if settings.nas_binding_revision else []),
    )


class CombinedAccess:
    def __init__(self, settings, store, owner):
        self.local = FolderAccess(settings.roots, owner, settings.max_file_bytes)
        vault = NASVault(store)
        self.remotes = [
            NASAccess(vault, owner, revision, settings.max_file_bytes)
            for revision in binding_revisions(settings)
        ]
        self.owner = owner

    def backend(self, name):
        if name.startswith('nas_'):
            candidates = [remote for remote in self.remotes if name.startswith('nas_' + remote.revision[:8] + '_')]
            if len(candidates) != 1:
                raise ValueError('此工作沒有該 NAS 來源，或來源識別碼不唯一。')
            return candidates[0]
        return self.local

    def visible_roots(self):
        roots = self.local.visible_roots()
        for remote in self.remotes:
            data = remote.vault.credentials(self.owner, remote.revision)
            label = data.get('label') or data['account']
            data.clear()
            for root in remote.visible_roots():
                roots.append({
                    **root, 'connection': label,
                    'label': label + ' / ' + root['label'],
                })
        return roots

    def allowed(self, name, write=False):
        return self.backend(name).allowed(name, write)

    def read(self, name, path):
        return self.backend(name).read(name, path)

    def scan(self, name, cap=300):
        return self.backend(name).scan(name, cap)

    def listing(self, name, path='', offset=0, limit=50):
        return self.backend(name).listing(name, path, offset, limit)

    def directory(self, name, parents=(), write=False):
        self.allowed(name, write)
        return self.local.directory(name, parents, write)

    def publish_new(self, name, path, data):
        self.allowed(name, True)
        return self.local.publish_new(name, path, data)


def file_access(settings, store, owner):
    return (
        CombinedAccess(settings, store, owner)
        if binding_revisions(settings)
        else FolderAccess(settings.roots, owner, settings.max_file_bytes)
    )
