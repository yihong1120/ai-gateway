"""Device approval, crash-safe delivery, bounded renewal and immediate denial."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
import uuid

from fastapi import HTTPException

from examples.company_ai_access.storage.registry import digest
from examples.company_ai_access.errors import error
from examples.company_ai_access.services import device_state


def challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode()


class Service:
    """Run one instance/worker per registry. No production state is inferred."""

    def __init__(self, settings, repository, litellm, directory, *, clock=time.time):
        self.settings, self.repository, self.litellm, self.directory, self.clock = (
            settings,
            repository,
            litellm,
            directory,
            clock,
        )
        self.lock = asyncio.Lock()

    def now(self):
        return int(self.clock())

    def policy(self):
        return {
            'environment': self.settings.environment,
            'portal_origin': self.settings.origin,
            'inference_base_url': self.settings.origin + '/company-ai',
            'models': list(self.settings.models),
            'default_model': self.settings.models[0],
            'codex_enabled': bool(self.settings.codex_models),
            'codex_models': list(self.settings.codex_models),
            'codex_release': self.settings.codex_release or None,
            'lease_seconds': self.settings.lease_seconds,
            'confirmation_seconds': self.settings.confirmation_seconds,
            'max_devices': self.settings.max_devices,
            'inference_network_configured': bool(self.settings.inference_cidrs),
            'windows_setup_url': self.settings.origin + '/company-ai/downloads/Setup-CompanyAI.ps1',
            'product_name': 'changdar-ai',
            'windows_installer_url': self.settings.origin + '/company-ai/downloads/changdar-ai-setup.exe',
            'mcp': self.settings.mcp_policy,
        }

    async def member(self, subject, *, fresh=False):
        async with self.lock:
            await self._member(subject, fresh=fresh)

    async def _member(self, subject, *, fresh=False):
        if not await self.directory.allowed(subject, fresh=fresh):
            # Apply local denial to all devices before attempting upstream IO.
            for device in await self.repository.list('device', subject):
                if device['status'] in {'active', 'provisioning'}:
                    await self.deny(device, 'membership_removed')
            raise error(403, 'ai_access_not_authorized')

    async def throttle(self, scope, identifier, maximum, window):
        if not (
            await self.repository.limit(
                scope + ':' + digest(identifier),
                self.now(),
                maximum=maximum,
                window=window,
            )
        ):
            raise HTTPException(
                429,
                detail={'code': 'ai_access_rate_limited'},
                headers={'Retry-After': str(window)},
            )

    async def start(self, label, proof_challenge, replace_device_id, address):
        await self.throttle('start', address, 10, 600)
        identifier = str(uuid.uuid4())
        code = '-'.join(secrets.token_hex(2).upper() for _ in range(2))
        row = {
            'id': identifier,
            'subject': '',
            'label': label,
            'challenge': proof_challenge,
            'code_hash': digest(code),
            'code': code,
            'replace_device_id': replace_device_id,
            'created_at': self.now(),
            'expires_at': self.now() + self.settings.enrollment_seconds,
            'status': 'pending',
            'device_id': None,
        }
        await self.repository.put('enrollment', row)
        return {
            'request_id': identifier,
            'user_code': code,
            'expires_at': row['expires_at'],
            'interval': 3,
            'verification_uri': self.settings.origin + self.settings.approval_path + '?request_id=' + identifier,
            'verification_uri_complete': self.settings.origin
            + self.settings.approval_path
            + '?request_id='
            + identifier
            + '&user_code='
            + code,
            'codex_enabled': bool(self.settings.codex_models),
        }

    async def enrollment(self, identifier, *, native=False):
        row = await self.repository.get('enrollment', identifier)
        deadline = row['expires_at'] if row else 0
        if row and native:
            # A human may approve just before the browser deadline. Native
            # delivery/ACK then has its own bounded activation window; the
            # expired browser code never becomes usable for a new approval.
            deadline = max(deadline, row.get('activation_until', 0), row.get('delivery_until', 0))
        if row is None or deadline <= self.now():
            raise error(410, 'ai_access_enrollment_expired')
        return row

    async def preview(self, subject, identifier):
        await self.member(subject)
        row = await self.enrollment(identifier)
        if row['subject'] not in ('', subject):
            raise error(404, 'ai_access_enrollment_not_found')
        return {
            key: row[key]
            for key in (
                'id',
                'label',
                'code',
                'status',
                'expires_at',
                'replace_device_id',
            )
        }

    async def approve(self, subject, identifier, code):
        async with self.lock:
            await self._member(subject, fresh=True)
            await self.throttle('approve', subject, 20, 600)
            row = await self.enrollment(identifier)
            if not secrets.compare_digest(row['code_hash'], digest(code)):
                raise error(400, 'ai_access_code_mismatch')
            if row['status'] != 'pending':
                raise error(409, 'ai_access_enrollment_already_approved')
            replace = row['replace_device_id']
            if replace:
                old = await self.repository.get('device', replace)
                if old is None or old['subject'] != subject:
                    raise error(404, 'ai_access_device_not_found')
            live = [
                d
                for d in (await self.repository.list('device', subject))
                if d['status'] in {'active', 'provisioning'}
                and d['id'] != replace
                and d['hard_expires_at'] > self.now()
            ]
            if len(live) >= self.settings.max_devices:
                raise error(409, 'ai_access_device_limit')
            device = {
                'id': str(uuid.uuid4()),
                'subject': subject,
                'label': row['label'],
                'status': 'provisioning',
                'created_at': self.now(),
                'hard_expires_at': self.now() + self.settings.confirmation_seconds,
                'expires_at': self.now() + self.settings.lease_seconds,
                'key_hash': None,
                'key_box': None,
                'last_seen_at': None,
                'acked': False,
                'block_synced': False,
                'reason': None,
                'replaces': replace,
                'codex_enabled': bool(self.settings.codex_models),
            }
            row.update(
                subject=subject,
                status='approved',
                device_id=device['id'],
                activation_until=self.now() + self.settings.delivery_seconds,
            )
            async with self.repository.transaction():
                await self.repository.put_many([('device', device), ('enrollment', row)])
                await self.repository.audit(self.now(), 'approved', subject, device['id'])
            return {'device_id': device['id'], 'status': 'approved'}

    def proof(self, row, verifier):
        try:
            valid = 43 <= len(verifier) <= 128 and secrets.compare_digest(challenge(verifier), row['challenge'])
        except (UnicodeError, ValueError):
            valid = False
        if not valid:
            raise error(401, 'ai_access_invalid_device_proof')

    async def poll(self, identifier, verifier):
        async with self.lock:
            row = await self.enrollment(identifier, native=True)
            self.proof(row, verifier)
            await self.throttle('poll', identifier, 30, 60)
            if row['status'] == 'pending':
                return {'status': 'pending'}
            if row['status'] == 'completed':
                raise error(410, 'ai_access_enrollment_consumed')
            await self._member(row['subject'])
            device = await self.repository.get('device', row['device_id'])
            if not device or device['status'] not in {'provisioning', 'active'}:
                raise error(410, 'ai_access_enrollment_expired')
            if row['status'] == 'approved' and row.get('activation_until', row['expires_at']) <= self.now():
                await self.deny(device, 'delivery_timeout')
                raise error(410, 'ai_access_enrollment_expired')
            if row['status'] == 'delivered' and row['delivery_until'] <= self.now():
                await self.deny(device, 'delivery_timeout')
                raise error(410, 'ai_access_enrollment_expired')
            if device['key_box'] is None:
                raw = 'sk-company-ai-' + secrets.token_urlsafe(36)
                device.update(key_box=self.repository.seal(raw), key_hash=digest(raw))
                await self.repository.put('device', device)
            raw = self.repository.open(device['key_box'])
            if device['status'] == 'provisioning':
                await self.litellm.ensure_key(device, raw)
                device['status'] = 'active'
            if row['status'] != 'delivered':
                row.update(
                    status='delivered',
                    delivery_until=min(
                        row.get('activation_until', row['expires_at']),
                        self.now() + self.settings.delivery_seconds,
                    ),
                )
            device['delivery_until'] = row['delivery_until']
            await self.repository.put_many([('device', device), ('enrollment', row)])
            return {
                'status': 'ready',
                'device_id': device['id'],
                'key': raw,
                'expires_at': device['expires_at'],
                'confirmation_due_at': device['hard_expires_at'],
                'base_url': self.policy()['inference_base_url'],
                'model': self.settings.models[0],
            }

    async def ack(self, identifier, verifier):
        async with self.lock:
            row = await self.enrollment(identifier, native=True)
            self.proof(row, verifier)
            if row['status'] == 'completed':
                return {'status': 'completed'}
            if row['status'] != 'delivered' or row['delivery_until'] <= self.now():
                raise error(409, 'ai_access_delivery_not_ready')
            await self._member(row['subject'])
            device = await self.repository.get('device', row['device_id'])
            if not device or device['status'] != 'active':
                raise error(410, 'ai_access_device_inactive')
            device['acked'] = True
            updates = [('device', device)]
            retired = []
            # A retry after an indeterminate ACK can replace an unacknowledged
            # replacement. Retire its same-owner ancestors too, atomically.
            cursor, visited = device.get('replaces'), {device['id']}
            while cursor and cursor not in visited:
                visited.add(cursor)
                old = await self.repository.get('device', cursor)
                if not old or old['subject'] != row['subject']:
                    break
                if old['status'] != 'revoked':
                    device_state.revoke(old, 'replaced', self.now())
                    updates.append(('device', old))
                    retired.append(old['id'])
                cursor = old.get('replaces')
            row['status'] = 'completed'
            updates.append(('enrollment', row))
            async with self.repository.transaction():
                await self.repository.put_many(updates)
                for identifier in retired:
                    await self.repository.audit(
                        self.now(),
                        'revoked:replaced',
                        device['subject'],
                        identifier,
                    )
                await self.repository.audit(self.now(), 'activated', device['subject'], device['id'])
            return {'status': 'completed'}

    async def deny(self, device, reason):
        if device['status'] == 'revoked':
            return
        device_state.revoke(device, reason, self.now())
        async with self.repository.transaction():
            await self.repository.put('device', device)
            await self.repository.audit(self.now(), 'revoked:' + reason, device['subject'], device['id'])

    async def revoke(self, subject, identifier, *, admin=False):
        async with self.lock:
            device = await self.repository.get('device', identifier)
            if device is None or (not admin and device['subject'] != subject):
                raise error(404, 'ai_access_device_not_found')
            if device['status'] != 'revoked':
                await self.deny(device, 'user_revoked')
            await self.sync_block(device)
            return {
                'status': 'revoked',
                'upstream_block_pending': not device['block_synced'],
            }

    async def sync_block(self, device):
        if device.get('key_box') and not device['block_synced']:
            try:
                await self.litellm.block(self.repository.open(device['key_box']))
            except HTTPException:
                return
            device['block_synced'] = True
            device['key_box'] = None
            latest = await self.repository.get('device', device['id'])
            if latest and latest['status'] == 'revoked':
                latest.update(block_synced=True, key_box=None)
                await self.repository.put('device', latest)

    async def devices(self, subject=None):
        fields = (
            'id',
            'label',
            'status',
            'created_at',
            'expires_at',
            'hard_expires_at',
            'last_seen_at',
            'reason',
            'acked',
        )
        return [
            {
                **{key: row[key] for key in fields},
                'confirmation_required': row['hard_expires_at'] <= self.now(),
                **({'subject': row['subject']} if subject is None else {}),
            }
            for row in (await self.repository.list('device', subject))
        ]

    async def authorize(self, raw):
        async with self.lock:
            device = await self.repository.by_key(raw)
            failure = device_state.key_error(device, self.now())
            if failure:
                raise error(401, failure)
            await self._member(device['subject'])
            device['last_seen_at'] = self.now()
            await self.repository.put('device', device)
            return device

    async def maintain(self):
        results = {'renewed': 0, 'denied': 0, 'unavailable': 0}
        delivered = {r['device_id']: r for r in (await self.repository.list('enrollment')) if r['device_id']}
        membership = {}
        for snapshot in await self.repository.list('device'):
            try:
                async with self.lock:
                    device = await self.repository.get('device', snapshot['id'])
                    reason = device_state.expiration_reason(device, delivered.get(device['id']), self.now())
                    if reason:
                        await self.deny(device, reason)
                        results['denied'] += 1
                if device['status'] == 'revoked':
                    await self.sync_block(device)
                    continue
                subject = device['subject']
                if subject not in membership:
                    try:
                        membership[subject] = await self.directory.allowed(subject, fresh=True)
                    except HTTPException as failure:
                        membership[subject] = failure
                allowed = membership[subject]
                if isinstance(allowed, HTTPException):
                    raise allowed
                async with self.lock:
                    device = await self.repository.get('device', snapshot['id'])
                    if not allowed:
                        for current in await self.repository.list('device', subject):
                            await self.deny(current, 'membership_removed')
                        results['denied'] += 1
                        continue
                    renew = (
                        device['status'] == 'active'
                        and device['acked']
                        and self.now() < min(device['expires_at'], device['hard_expires_at'])
                        and device['expires_at'] - self.now() <= self.settings.renew_before_seconds
                    )
                    expires = min(
                        self.now() + self.settings.lease_seconds,
                        device['hard_expires_at'],
                    )
                if renew:
                    # Directory and administrative HTTP calls do not hold the
                    # registry lock for the entire employee/device inventory.
                    await self.litellm.renew(self.repository.open(device['key_box']), expires - self.now())
                    async with self.lock:
                        current = await self.repository.get('device', device['id'])
                        # A revocation while the HTTP request was in flight wins.
                        # /key/update never changes blocked=false here.
                        if current['status'] == 'active' and self.now() < min(
                            current['expires_at'], current['hard_expires_at']
                        ):
                            current['expires_at'] = min(expires, current['hard_expires_at'])
                            async with self.repository.transaction():
                                await self.repository.put('device', current)
                                await self.repository.audit(self.now(), 'renewed', subject, current['id'])
                            results['renewed'] += 1
            except HTTPException as exc:
                results['unavailable' if exc.status_code == 503 else 'denied'] += 1
        await self.repository.prune(self.now() - 86400)
        return results

    async def maintenance_loop(self, stop):
        while not stop.is_set():
            # Exceptions are reported by the host logger without credential data.
            try:
                await self.maintain()
            except Exception:
                import logging

                logging.getLogger(__name__).error('Company AI maintenance failed; leases were not extended.')
            # Drivers may consume cancellation while releasing a connection.
            # The lifespan stop signal must still prevent another sleep/cycle.
            try:
                await asyncio.wait_for(stop.wait(), self.settings.maintenance_seconds)
            except TimeoutError:
                pass
