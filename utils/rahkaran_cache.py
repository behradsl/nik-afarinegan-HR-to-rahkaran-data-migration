"""
Invalidate Rahkaran Redis entity caches after SQL-side config/lookup changes.

Rahkaran stores CompanyConfiguration (ConfigurationValue / ConfigurationGroup)
under keys like:
  sg[.]RAHKARANRAW-...[.]entityCaches[.]...ConfigurationValueC...
  sg[.]RAHKARANRAW-...[.]entityCaches[.]...ConfigurationGroupC...

Without deleting these, the UI keeps stale Extra titles / settings.
"""
from __future__ import annotations

import json
import re
import socket
from typing import Iterable, List, Optional, Sequence, Tuple

from db_core import get_config_path

# Entity-cache fragments observed in Redis for RahkaranRaw
CONFIG_CACHE_FRAGMENTS = (
    'ConfigurationValueC',
    'ConfigurationGroupC',
)
LOOKUP_CACHE_FRAGMENTS = (
    'LookupC',
    'LookupInfoC',
    'LookupCache',
)

_DEFAULT_REDIS = {
    'enabled': True,
    'host': '127.0.0.1',
    'port': 6379,
    'db': 0,
    'socket_timeout_sec': 5,
}


def _load_app_config() -> dict:
    try:
        with open(get_config_path(), 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _database_name_from_conn(conn_str: str) -> Optional[str]:
    if not conn_str:
        return None
    match = re.search(r'Database\s*=\s*([^;]+)', conn_str, flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip()


def _redis_settings(app_config: Optional[dict] = None) -> dict:
    cfg = app_config if app_config is not None else _load_app_config()
    settings = dict(_DEFAULT_REDIS)
    raw = cfg.get('redis') or {}
    if isinstance(raw, dict):
        settings.update({k: raw[k] for k in raw if k in settings or k in ('enabled', 'host', 'port', 'db', 'socket_timeout_sec')})
    return settings


class _RedisClient:
    """Minimal Redis client (PING / SELECT / SCAN / DEL) over TCP."""

    def __init__(self, host: str, port: int, db: int = 0, timeout: float = 5.0):
        self.host = host
        self.port = int(port)
        self.db = int(db)
        self.timeout = float(timeout)
        self._sock: Optional[socket.socket] = None

    def connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        self._sock = sock
        self._call('PING')
        if self.db:
            self._call('SELECT', str(self.db))

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _send(self, parts: Sequence[str]):
        assert self._sock is not None
        chunks = [f'*{len(parts)}\r\n'.encode('utf-8')]
        for part in parts:
            data = part.encode('utf-8') if isinstance(part, str) else part
            chunks.append(f'${len(data)}\r\n'.encode('utf-8'))
            chunks.append(data)
            chunks.append(b'\r\n')
        self._sock.sendall(b''.join(chunks))

    def _readline(self) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while True:
            ch = self._sock.recv(1)
            if not ch:
                raise ConnectionError('Redis connection closed')
            buf += ch
            if len(buf) >= 2 and buf[-2:] == b'\r\n':
                return bytes(buf[:-2])

    def _read_bulk(self, length: int) -> bytes:
        assert self._sock is not None
        if length < 0:
            return b''
        data = bytearray()
        while len(data) < length + 2:
            chunk = self._sock.recv(length + 2 - len(data))
            if not chunk:
                raise ConnectionError('Redis connection closed')
            data += chunk
        return bytes(data[:length])

    def _read_reply(self):
        line = self._readline()
        if not line:
            raise ConnectionError('Empty Redis reply')
        prefix = line[:1]
        if prefix == b'+':
            return line[1:].decode('utf-8', errors='replace')
        if prefix == b'-':
            raise RuntimeError(line[1:].decode('utf-8', errors='replace'))
        if prefix == b':':
            return int(line[1:])
        if prefix == b'$':
            length = int(line[1:])
            if length < 0:
                return None
            return self._read_bulk(length)
        if prefix == b'*':
            count = int(line[1:])
            if count < 0:
                return None
            return [self._read_reply() for _ in range(count)]
        raise RuntimeError(f'Unsupported Redis reply: {line!r}')

    def _call(self, *parts: str):
        self._send(parts)
        return self._read_reply()

    def scan_iter(self, pattern: str, count: int = 200) -> Iterable[bytes]:
        cursor = b'0'
        while True:
            reply = self._call('SCAN', cursor.decode('ascii'), 'MATCH', pattern, 'COUNT', str(count))
            if not isinstance(reply, list) or len(reply) != 2:
                raise RuntimeError(f'Unexpected SCAN reply: {reply!r}')
            next_cursor = reply[0]
            keys = reply[1] or []
            if isinstance(next_cursor, bytes):
                cursor = next_cursor
            else:
                cursor = str(next_cursor).encode('ascii')
            for key in keys:
                if key is None:
                    continue
                yield key if isinstance(key, bytes) else str(key).encode('utf-8')
            if cursor == b'0':
                break

    def delete_keys(self, keys: Sequence[bytes]) -> int:
        deleted = 0
        # DEL accepts multiple keys; batch to avoid huge payloads
        batch_size = 100
        for i in range(0, len(keys), batch_size):
            batch = list(keys[i:i + batch_size])
            if not batch:
                continue
            # Build raw DEL with bytes keys
            assert self._sock is not None
            parts = [b'DEL'] + batch
            payload = [f'*{len(parts)}\r\n'.encode('utf-8')]
            for part in parts:
                payload.append(f'${len(part)}\r\n'.encode('utf-8'))
                payload.append(part)
                payload.append(b'\r\n')
            self._sock.sendall(b''.join(payload))
            result = self._read_reply()
            deleted += int(result or 0)
        return deleted


def _patterns_for(db_name: Optional[str], fragments: Sequence[str]) -> List[str]:
    patterns = []
    # Redis MATCH is case-sensitive; Rahkaran keys use UPPER(database) (e.g. RAHKARANRAW).
    db_token = db_name.upper() if db_name else None
    for frag in fragments:
        if db_token:
            # Keys: sg[.]RAHKARANRAW-...[.]entityCaches[.]...ConfigurationValueC...
            patterns.append(f'*{db_token}*{frag}*')
        else:
            patterns.append(f'*{frag}*')
    return patterns


def invalidate_rahkaran_entity_caches(
    *,
    kinds: Sequence[str] = ('configuration',),
    silent: bool = False,
) -> int:
    """
    Delete Rahkaran entity-cache keys from Redis.

    kinds:
      - 'configuration': ConfigurationValueC / ConfigurationGroupC
      - 'lookup': Lookup-related fragments (if present)
    Returns number of keys deleted.
    """
    app_config = _load_app_config()
    settings = _redis_settings(app_config)
    if not settings.get('enabled', True):
        if not silent:
            print('  -> Redis cache refresh skipped (redis.enabled=false).')
        return 0

    fragments: List[str] = []
    for kind in kinds:
        kind_l = (kind or '').lower()
        if kind_l in ('configuration', 'config', 'settings'):
            fragments.extend(CONFIG_CACHE_FRAGMENTS)
        elif kind_l in ('lookup', 'lookups'):
            fragments.extend(LOOKUP_CACHE_FRAGMENTS)

    # de-dupe preserve order
    seen = set()
    fragments = [f for f in fragments if not (f in seen or seen.add(f))]
    if not fragments:
        return 0

    db_name = _database_name_from_conn(app_config.get('dest_conn', ''))
    patterns = _patterns_for(db_name, fragments)

    try:
        with _RedisClient(
            host=str(settings.get('host') or '127.0.0.1'),
            port=int(settings.get('port') or 6379),
            db=int(settings.get('db') or 0),
            timeout=float(settings.get('socket_timeout_sec') or 5),
        ) as client:
            to_delete = []
            seen_keys = set()
            for pattern in patterns:
                for key in client.scan_iter(pattern):
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    to_delete.append(key)
            deleted = client.delete_keys(to_delete) if to_delete else 0
    except Exception as exc:
        if not silent:
            print(f'  -> Redis cache refresh failed ({exc}). UI may show stale settings until cache is cleared.')
        return 0

    if not silent:
        scope = db_name or 'all-dbs'
        print(
            f'  -> Redis entity cache refreshed ({", ".join(kinds)}): '
            f'deleted {deleted} key(s) for {scope}.'
        )
    return deleted


def invalidate_configuration_cache(**kwargs) -> int:
    return invalidate_rahkaran_entity_caches(kinds=('configuration',), **kwargs)


def invalidate_lookup_cache(**kwargs) -> int:
    return invalidate_rahkaran_entity_caches(kinds=('lookup',), **kwargs)
