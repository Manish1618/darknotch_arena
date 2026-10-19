"""The Redis connection every API container shares: rate limits and the
scoreboard cache live there, so it doesn't matter which container answers a
request. None when REDIS_URL is unset (one process: development and tests)."""
import functools

import redis

from .config import settings


@functools.lru_cache(maxsize=1)
def redis_client() -> "redis.Redis | None":
    if not settings.redis_url:
        return None
    return redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=2,
                                socket_connect_timeout=2, health_check_interval=30)
