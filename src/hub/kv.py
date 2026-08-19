"""KeyValueStore: сессии входа, кэш аутентификации, окна rate-limit (R-S2).

Реализации: in-memory (TTL по часам приложения) и Redis (выбор по ``HUB_REDIS_URL``).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Protocol

from hub.clock import Clock, SystemClock


class KeyValueStore(Protocol):
    async def get(self, key: str) -> Any | None:
        """Значение или ``None`` (в т.ч. после истечения TTL)."""

    async def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        """Записать JSON-сериализуемое значение с TTL в секундах (``None`` — без TTL)."""

    async def delete(self, key: str) -> None: ...

    async def count_prefix(self, prefix: str) -> int:
        """Число живых ключей с данным префиксом (для gauge активных сессий)."""

    async def rate_limit_hit(self, key: str, now: float, window: float, limit: int) -> tuple[bool, float]:
        """Скользящее окно: если в окне < ``limit`` меток — добавить ``now`` и вернуть ``(True, 0)``;
        иначе ``(False, секунды до освобождения окна)``. Отклонённые обращения не учитываются."""

    async def close(self) -> None: ...


class InMemoryKeyValueStore:
    """In-memory реализация с TTL по монотонному времени ``clock``."""

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock: Clock = clock or SystemClock()
        self._data: dict[str, tuple[Any, float | None]] = {}

    def _alive(self, key: str) -> bool:
        item = self._data.get(key)
        if item is None:
            return False
        _, expires = item
        if expires is not None and self._clock.monotonic() >= expires:
            self._data.pop(key, None)
            return False
        return True

    async def get(self, key: str) -> Any | None:
        if not self._alive(key):
            return None
        # копия через JSON, чтобы вызывающий код не менял хранимое значение
        return json.loads(json.dumps(self._data[key][0]))

    async def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        if ttl is not None and ttl <= 0:
            self._data.pop(key, None)
            return
        expires = None if ttl is None else self._clock.monotonic() + float(ttl)
        self._data[key] = (json.loads(json.dumps(value)), expires)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def count_prefix(self, prefix: str) -> int:
        return sum(1 for k in list(self._data) if k.startswith(prefix) and self._alive(k))

    async def rate_limit_hit(self, key: str, now: float, window: float, limit: int) -> tuple[bool, float]:
        stamps: list[float] = []
        if self._alive(key):
            stamps = [float(t) for t in self._data[key][0] if now - float(t) < window]
        if len(stamps) >= limit:
            oldest = min(stamps)
            return False, max(0.0, oldest + window - now)
        stamps.append(now)
        self._data[key] = (stamps, self._clock.monotonic() + float(window))
        return True, 0.0

    async def close(self) -> None:
        self._data.clear()


# Lua-скрипты: операции над окном rate-limit и подсчёт ключей выполняются атомарно на стороне Redis.
_RATE_LIMIT_LUA = """
local key, now, threshold, limit = KEYS[1], ARGV[1], ARGV[2], tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', key, '-inf', threshold)
local count = redis.call('ZCARD', key)
if count >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  local score = oldest[2]
  if type(oldest[1]) == 'table' then score = oldest[1][2] end  -- RESP3-форма [[member, score]]
  return {0, score or now}
end
redis.call('ZADD', key, now, ARGV[4])
redis.call('PEXPIRE', key, ARGV[5])
return {1, '0'}
"""

_COUNT_PREFIX_LUA = """
local seen, total, cursor = {}, 0, '0'
repeat
  local res = redis.call('SCAN', cursor, 'MATCH', ARGV[1], 'COUNT', 1000)
  cursor = res[1]
  for _, k in ipairs(res[2]) do
    if not seen[k] then
      seen[k] = true
      total = total + 1
    end
  end
until cursor == '0'
return total
"""


class RedisKeyValueStore:
    """Реализация поверх ``redis.asyncio``. Значения — JSON; окна rate-limit — sorted set.

    ``rate_limit_hit`` и ``count_prefix`` выполняются Lua-скриптами (EVALSHA) — атомарно, без гонок
    между репликами Hub.
    """

    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(url, decode_responses=True)
        self._rate_limit_script = self._redis.register_script(_RATE_LIMIT_LUA)
        self._count_prefix_script = self._redis.register_script(_COUNT_PREFIX_LUA)

    async def get(self, key: str) -> Any | None:
        raw = await self._redis.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    async def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        if ttl is not None and ttl <= 0:
            await self._redis.delete(key)
            return
        payload = json.dumps(value, ensure_ascii=False)
        if ttl is None:
            await self._redis.set(key, payload)
        else:
            await self._redis.set(key, payload, px=max(1, int(ttl * 1000)))

    async def delete(self, key: str) -> None:
        await self._redis.delete(key)

    async def count_prefix(self, prefix: str) -> int:
        total = await self._count_prefix_script(keys=[], args=[f"{prefix}*"])
        return int(total)

    async def rate_limit_hit(self, key: str, now: float, window: float, limit: int) -> tuple[bool, float]:
        member = f"{now!r}:{uuid.uuid4().hex}"
        allowed, oldest = await self._rate_limit_script(
            keys=[key],
            args=[repr(float(now)), repr(float(now - window)), int(limit), member, max(1, int(window * 1000))],
        )
        if int(allowed):
            return True, 0.0
        return False, max(0.0, float(oldest) + window - now)

    async def close(self) -> None:
        await self._redis.aclose()


def create_kv_store(redis_url: str, clock: Clock) -> KeyValueStore:
    if redis_url:
        return RedisKeyValueStore(redis_url)
    return InMemoryKeyValueStore(clock)


__all__ = ["InMemoryKeyValueStore", "KeyValueStore", "RedisKeyValueStore", "create_kv_store"]
