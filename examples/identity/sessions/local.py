"""Atomic local sessions; profile snapshots never own mutable token lists."""
from __future__ import annotations

import hashlib
import json
import time

from fastapi import HTTPException


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def access_key(jti):
    return 'auth:local:access:' + digest(jti)


def family_key(family):
    return 'oauth:refresh-family-revoked:' + family


def refresh_key(token):
    return 'oauth:refresh-state:' + digest(token)


_REGISTER = """
local se = redis.call('GET',KEYS[5]) or '0'
local ae = redis.call('GET',KEYS[6]) or '0'
if se ~= ARGV[1] or ae ~= ARGV[2] or redis.call('EXISTS',KEYS[4]) == 1
    or redis.call('EXISTS',KEYS[8]) == 0 then return 0 end
if ARGV[7] == 'rotate' then
    local raw = redis.call('GET',KEYS[7])
    local old = raw and cjson.decode(raw) or nil
    if not old or old.status ~= 'active' or old.username ~= ARGV[8] or old.family ~= ARGV[9]
        or old.session_epoch ~= se then
        redis.call('SET',KEYS[4],'1','EX',ARGV[4])
        redis.call('INCR',KEYS[6])
        return -1
    end
    old.status = 'used'
    local ttl = redis.call('TTL',KEYS[7])
    if ttl > 0 then redis.call('SET',KEYS[7],cjson.encode(old),'EX',ttl) end
end
redis.call('EXPIRE',KEYS[8],ARGV[4])
redis.call('SET',KEYS[1],ARGV[3],'EX',ARGV[4])
redis.call('SET',KEYS[2],ARGV[5],'EX',ARGV[6])
local record = cjson.decode(ARGV[5])
redis.call('ZADD',KEYS[3],record.exp,KEYS[2])
local ttl = redis.call('TTL',KEYS[3])
if ttl < tonumber(ARGV[6]) then redis.call('EXPIRE',KEYS[3],ARGV[6]) end
return 1
"""
_CHECK_REFRESH = """
local raw = redis.call('GET',KEYS[1])
if not raw then return false end
local record = cjson.decode(raw)
if record.status ~= 'active' or record.username ~= ARGV[1] or record.family ~= ARGV[2]
    or record.session_epoch ~= (redis.call('GET',KEYS[3]) or '0')
    or redis.call('EXISTS',KEYS[2]) == 1 then return false end
return raw
"""
_REVOKED = """
local raw = redis.call('GET',KEYS[1])
if not raw then return 0 end
local record = cjson.decode(raw)
if (redis.call('GET',record.skey) or '0') ~= record.session_epoch
    or (redis.call('GET',record.akey) or '0') ~= record.access_epoch
    or redis.call('EXISTS',record.fkey) == 1 then return 1 end
return 0
"""
_SNAPSHOT = """
redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',ARGV[1])
local result = {}
local members = redis.call('ZRANGE',KEYS[1],0,-1)
for _, key in ipairs(members) do
    local raw = redis.call('GET',key)
    if raw then
        local record = cjson.decode(raw)
        if record.session_epoch == (redis.call('GET',record.skey) or '0')
            and record.access_epoch == (redis.call('GET',record.akey) or '0')
            and redis.call('EXISTS',record.fkey) == 0 then
            result[record.jti] = record.exp
        end
    else redis.call('ZREM',KEYS[1],key) end
end
return cjson.encode(result)
"""
_REVOKE_ALL = """
redis.call('INCR',KEYS[1])
redis.call('INCR',KEYS[2])
redis.call('DEL',KEYS[3],KEYS[4])
return 1
"""


class LocalSessions:
    def __init__(self, prefix):
        self.prefix = prefix

    def user_keys(self, username):
        root = self.prefix + ':sessions:' + digest(username)
        return root + ':session-epoch', root + ':access-epoch', root + ':access'

    async def epochs(self, redis, username):
        keys = self.user_keys(username)
        values = await redis.mget(*keys[:2])
        return tuple(
            value.decode() if isinstance(value, bytes) else str(value or '0')
            for value in values
        )

    async def register(
        self,
        redis,
        username,
        family,
        refresh,
        jti,
        exp,
        epochs,
        *,
        previous=None,
        refresh_seconds=2592000,
    ):
        skey, akey, index = self.user_keys(username)
        se, ae = epochs
        record = {
            'username': username,
            'family': family,
            'status': 'active',
            'session_epoch': se,
        }
        access = {
            **record,
            'access_epoch': ae,
            'jti': jti,
            'exp': exp,
            'skey': skey,
            'akey': akey,
            'fkey': family_key(family),
        }
        result = await redis.eval(
            _REGISTER,
            8,
            refresh_key(refresh),
            access_key(jti),
            index,
            family_key(family),
            skey,
            akey,
            refresh_key(previous) if previous else refresh_key(refresh),
            self.prefix + ':user_cache:' + username,
            se,
            ae,
            json.dumps(record),
            refresh_seconds,
            json.dumps(access),
            max(1, exp - int(time.time())),
            'rotate' if previous else 'issue',
            username,
            family,
        )
        if result != 1:
            raise HTTPException(
                status_code=401,
                detail='Refresh token reused'
                if previous
                else 'Session changed; sign in again',
            )

    async def validate(self, redis, token, username, family):
        skey, _, _ = self.user_keys(username)
        raw = await redis.eval(
            _CHECK_REFRESH,
            3,
            refresh_key(token),
            family_key(family),
            skey,
            username,
            family,
        )
        if not raw:
            await self.revoke_family(redis, username, family, all_access=True)
            raise HTTPException(status_code=401, detail='Refresh token reused')
        return json.loads(raw)

    async def revoke_family(self, redis, username, family, *, all_access=False):
        _, akey, _ = self.user_keys(username)
        await redis.eval(
            "redis.call('SET',KEYS[1],'1','EX',ARGV[1]); "
            "if ARGV[2]=='1' then redis.call('INCR',KEYS[2]) end; return 1",
            2,
            family_key(family),
            akey,
            2592000,
            '1' if all_access else '0',
        )

    async def revoke_all(self, redis, username):
        skey, akey, index = self.user_keys(username)
        await redis.eval(
            _REVOKE_ALL, 4, skey, akey, index, self.prefix + ':user_cache:' + username,
        )

    async def revoke_once(self, redis, username, action):
        skey, akey, index = self.user_keys(username)
        await redis.eval(
            """
if redis.call('EXISTS',KEYS[5]) == 1 then return 0 end
redis.call('INCR',KEYS[1]); redis.call('INCR',KEYS[2])
redis.call('DEL',KEYS[3],KEYS[4])
redis.call('SET',KEYS[5],'1','EX',2592000)
return 1
""",
            5,
            skey,
            akey,
            index,
            self.prefix + ':user_cache:' + username,
            'auth:account-revoked:' + action,
        )

    async def snapshot(self, redis, username):
        _, _, index = self.user_keys(username)
        value = json.loads(await redis.eval(_SNAPSHOT, 1, index, int(time.time())))
        return value if isinstance(value, dict) else {}


async def local_access_revoked(redis, jti):
    return bool(await redis.eval(_REVOKED, 1, access_key(jti)))
