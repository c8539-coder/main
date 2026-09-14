"""Minimal async client for the OpenSea API v2.

Docs: https://docs.opensea.io/reference/api-overview
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

BASE_URL = "https://api.opensea.io/api/v2"

# OpenSea event_type -> человекочитаемое действие
EVENT_LABELS = {
    "sale": "Продажа",
    "listing": "Выставлен на продажу",
    "order": "Ордер",
    "transfer": "Передача",
}


class OpenSeaError(Exception):
    """Ошибка при обращении к OpenSea (сеть, статус, парсинг)."""


@dataclass
class CollectionMeta:
    """Метаданные коллекции для отображения в алертах."""

    slug: str
    name: str
    image: Optional[str]
    address: Optional[str]
    chain: str
    opensea_url: str


@dataclass
class NftEvent:
    """Нормализованное представление события OpenSea."""

    event_type: str
    timestamp: int
    collection: str
    chain: str
    nft_name: str
    nft_image: Optional[str]
    nft_url: Optional[str]
    identifier: Optional[str]
    contract: Optional[str]
    price: Optional[float]
    price_symbol: Optional[str]
    seller: Optional[str]
    buyer: Optional[str]
    tx_hash: Optional[str]

    @property
    def unique_id(self) -> str:
        base = self.tx_hash or f"{self.contract}:{self.identifier}"
        return f"{self.event_type}:{base}:{self.timestamp}"


class OpenSeaClient:
    def __init__(self, api_key: str, timeout: float = 20.0) -> None:
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={
                    "X-API-KEY": self._api_key,
                    "Accept": "application/json",
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, path: str, params: Optional[dict] = None) -> dict:
        session = await self._get_session()
        url = f"{BASE_URL}{path}"
        for attempt in range(3):
            try:
                async with session.get(url, params=params) as resp:
                    if resp.status == 429:
                        wait = 2 ** attempt
                        log.warning("OpenSea rate limit (429), пауза %ss", wait)
                        await asyncio.sleep(wait)
                        continue
                    if resp.status == 401:
                        raise OpenSeaError(
                            "OpenSea вернул 401 — проверьте OPENSEA_API_KEY."
                        )
                    if resp.status == 404:
                        raise OpenSeaError(f"Не найдено (404): {path}")
                    if resp.status >= 400:
                        text = await resp.text()
                        raise OpenSeaError(
                            f"OpenSea вернул {resp.status}: {text[:200]}"
                        )
                    return await resp.json()
            except aiohttp.ClientError as exc:
                if attempt == 2:
                    raise OpenSeaError(f"Сетевая ошибка: {exc}") from exc
                await asyncio.sleep(2 ** attempt)
        raise OpenSeaError("Не удалось выполнить запрос к OpenSea после повторов.")

    # ---- public API -----------------------------------------------------

    async def get_collection(self, slug: str) -> dict:
        """Вернуть метаданные коллекции; кидает OpenSeaError, если её нет."""
        return await self._request(f"/collections/{slug}")

    async def collection_exists(self, slug: str) -> bool:
        try:
            await self.get_collection(slug)
            return True
        except OpenSeaError:
            return False

    async def get_contract(self, chain: str, address: str) -> dict:
        """Метаданные NFT-контракта; в ответе есть slug коллекции."""
        return await self._request(f"/chains/{chain}/contract/{address}")

    async def resolve_collection(self, identifier: str, chain: str = "ethereum") -> "CollectionMeta":
        """Принять адрес контракта (0x…) ИЛИ slug и вернуть метаданные коллекции.

        Кидает OpenSeaError, если коллекция не найдена.
        """
        ident = identifier.strip()
        if ident.lower().startswith("0x") and len(ident) >= 40:
            contract = await self.get_contract(chain, ident)
            slug = contract.get("collection")
            if not slug:
                raise OpenSeaError(
                    f"У контракта {ident} на {chain} нет коллекции на OpenSea."
                )
        else:
            slug = ident.lower()

        col = await self.get_collection(slug)
        contracts = col.get("contracts") or []
        address = contracts[0].get("address") if contracts else (
            ident if ident.lower().startswith("0x") else None
        )
        col_chain = contracts[0].get("chain") if contracts else chain
        return CollectionMeta(
            slug=slug,
            name=col.get("name") or slug,
            image=col.get("image_url"),
            address=address,
            chain=col_chain or chain,
            opensea_url=f"https://opensea.io/collection/{slug}",
        )

    async def fetch_events(
        self,
        slug: str,
        event_type: str = "sale",
        after: Optional[int] = None,
        limit: int = 50,
    ) -> list[NftEvent]:
        """Получить последние события коллекции, новые сначала.

        event_type: "sale", "listing" или "all".
        after: unix-время, вернуть только события позже него.
        """
        params: dict = {"limit": min(max(limit, 1), 50)}
        if event_type and event_type != "all":
            params["event_type"] = event_type
        if after is not None:
            params["after"] = int(after)

        data = await self._request(f"/events/collection/{slug}", params=params)
        raw_events = data.get("asset_events", []) or []
        events = [self._parse_event(raw, slug) for raw in raw_events]
        events = [e for e in events if e is not None]
        # OpenSea отдаёт новые сначала; фильтруем по after на всякий случай
        if after is not None:
            events = [e for e in events if e.timestamp > after]
        events.sort(key=lambda e: e.timestamp)  # старые -> новые для последовательной отправки
        return events

    # ---- parsing --------------------------------------------------------

    @staticmethod
    def _parse_event(raw: dict, slug: str) -> Optional[NftEvent]:
        try:
            event_type = raw.get("event_type", "unknown")
            timestamp = int(
                raw.get("event_timestamp")
                or raw.get("closing_date")
                or raw.get("created_date")
                or 0
            )
            nft = raw.get("nft") or raw.get("asset") or {}
            nft_name = nft.get("name") or f"{slug} #{nft.get('identifier', '?')}"
            price, symbol = OpenSeaClient._parse_payment(raw)

            return NftEvent(
                event_type=event_type,
                timestamp=timestamp,
                collection=slug,
                chain=raw.get("chain") or nft.get("chain") or "ethereum",
                nft_name=nft_name,
                nft_image=nft.get("image_url") or nft.get("display_image_url"),
                nft_url=nft.get("opensea_url"),
                identifier=str(nft.get("identifier")) if nft.get("identifier") is not None else None,
                contract=nft.get("contract"),
                price=price,
                price_symbol=symbol,
                seller=raw.get("seller"),
                buyer=raw.get("buyer") or raw.get("winner_account"),
                tx_hash=raw.get("transaction"),
            )
        except (TypeError, ValueError) as exc:
            log.debug("Не удалось разобрать событие: %s (%s)", exc, raw)
            return None

    @staticmethod
    def _parse_payment(raw: dict) -> tuple[Optional[float], Optional[str]]:
        payment = raw.get("payment")
        if not isinstance(payment, dict):
            return None, None
        quantity = payment.get("quantity")
        decimals = payment.get("decimals", 18)
        symbol = payment.get("symbol", "ETH")
        if quantity is None:
            return None, symbol
        try:
            value = int(quantity) / (10 ** int(decimals))
        except (TypeError, ValueError):
            return None, symbol
        return value, symbol
