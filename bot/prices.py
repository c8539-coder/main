"""Tiny cached fiat-price helper (CoinGecko) to enrich alerts with USD.

Полностью необязательный: при любой ошибке возвращает None и алерт просто
показывается без USD. Курс кэшируется, чтобы не бить по API на каждое событие.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

# символ платёжного токена -> id в CoinGecko
COINGECKO_IDS = {
    "ETH": "ethereum",
    "WETH": "weth",
    "MATIC": "matic-network",
    "POL": "matic-network",
    "USDC": "usd-coin",
    "USDT": "tether",
    "DAI": "dai",
    "AVAX": "avalanche-2",
}

_CACHE_TTL = 300  # секунд
_URL = "https://api.coingecko.com/api/v3/simple/price"


class PriceCache:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, float]] = {}  # id -> (usd, fetched_at)

    async def usd_for(self, symbol: Optional[str]) -> Optional[float]:
        if not symbol:
            return None
        coin_id = COINGECKO_IDS.get(symbol.upper())
        if not coin_id:
            return None

        cached = self._cache.get(coin_id)
        now = time.time()
        if cached and now - cached[1] < _CACHE_TTL:
            return cached[0]

        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                params = {"ids": coin_id, "vs_currencies": "usd"}
                async with session.get(_URL, params=params) as resp:
                    if resp.status != 200:
                        return cached[0] if cached else None
                    data = await resp.json()
            usd = float(data.get(coin_id, {}).get("usd"))
        except (aiohttp.ClientError, ValueError, TypeError, KeyError) as exc:
            log.debug("Не удалось получить курс %s: %s", symbol, exc)
            return cached[0] if cached else None

        self._cache[coin_id] = (usd, now)
        return usd
