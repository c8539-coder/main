"""Тонкий клиент Alchemy NFT API v3 + Core JSON-RPC.

Только чтение. Ретраи с экспоненциальным бэкоффом на 429/5xx/сетевые ошибки,
мягкий троттлинг между запросами.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Iterator

import requests

from .config import NetworkConfig, Settings, ZERO_ADDRESS


class AlchemyClient:
    def __init__(self, network: NetworkConfig, settings: Settings):
        self.net = network
        self.s = settings
        self._session = requests.Session()
        self._last_call = 0.0

    # ------------------------------------------------------------------
    # Низкоуровневые запросы
    # ------------------------------------------------------------------
    def _throttle(self) -> None:
        wait = self.s.request_delay_s - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _request(self, method: str, url: str, **kwargs: Any) -> dict:
        last_exc: Exception | None = None
        for attempt in range(self.s.max_retries):
            self._throttle()
            try:
                resp = self._session.request(
                    method, url, timeout=self.s.timeout_s, **kwargs
                )
            except requests.RequestException as exc:  # сетевые сбои
                last_exc = exc
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_exc = RuntimeError(
                        f"{resp.status_code} от Alchemy: {resp.text[:200]}"
                    )
                else:
                    # 4xx (кроме 429) — не ретраим, ошибка запроса
                    raise RuntimeError(
                        f"Alchemy {resp.status_code}: {resp.text[:300]}"
                    )
            sleep_s = self.s.backoff_base_s * (2 ** attempt)
            time.sleep(sleep_s)
        raise RuntimeError(f"Alchemy не ответил после ретраев: {last_exc}")

    def _nft_get(self, endpoint: str, params: dict) -> dict:
        return self._request("GET", f"{self.net.nft_url}/{endpoint}", params=params)

    def _rpc(self, method: str, params: list) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        data = self._request("POST", self.net.rpc_url, json=payload)
        if "error" in data:
            raise RuntimeError(f"RPC {method} error: {data['error']}")
        return data.get("result")

    # ------------------------------------------------------------------
    # NFT API
    # ------------------------------------------------------------------
    def contract_metadata(self, contract: str) -> dict:
        data = self._nft_get(
            "getContractMetadata", {"contractAddress": contract}
        )
        return data

    def owners_for_contract(self, contract: str) -> dict[str, int]:
        """Все владельцы контракта -> {wallet_lower: tokens_held}."""
        owners: dict[str, int] = {}
        page_key: str | None = None
        while True:
            params = {
                "contractAddress": contract,
                "withTokenBalances": "true",
            }
            if page_key:
                params["pageKey"] = page_key
            data = self._nft_get("getOwnersForContract", params)
            for entry in data.get("owners", []):
                addr = (entry.get("ownerAddress") or "").lower()
                if not addr:
                    continue
                held = sum(
                    int(tb.get("balance", 1)) for tb in entry.get("tokenBalances", [])
                )
                owners[addr] = owners.get(addr, 0) + max(held, 1)
            page_key = data.get("pageKey")
            if not page_key:
                break
        return owners

    def nfts_for_owner(self, owner: str, contracts: Iterable[str] | None = None) -> list[dict]:
        """Список NFT кошелька (опц. отфильтрованный по contracts)."""
        out: list[dict] = []
        page_key: str | None = None
        contract_list = list(contracts) if contracts else None
        while True:
            params: dict[str, Any] = {"owner": owner, "withMetadata": "false", "pageSize": 100}
            if contract_list:
                # Alchemy принимает contractAddresses[] повторяющимся ключом
                params["contractAddresses[]"] = contract_list
            if page_key:
                params["pageKey"] = page_key
            data = self._nft_get("getNFTsForOwner", params)
            out.extend(data.get("ownedNfts", []))
            page_key = data.get("pageKey")
            if not page_key:
                break
        return out

    def nft_sales(self, contract: str, *, limit: int = 1000) -> list[dict]:
        """Продажи по контракту (если поддержано сетью). Может вернуть []."""
        out: list[dict] = []
        page_key: str | None = None
        try:
            while True:
                params: dict[str, Any] = {
                    "contractAddress": contract,
                    "limit": min(limit, 100),
                    "order": "asc",
                }
                if page_key:
                    params["pageKey"] = page_key
                data = self._nft_get("getNFTSales", params)
                out.extend(data.get("nftSales", []))
                page_key = data.get("pageKey")
                if not page_key or len(out) >= limit:
                    break
        except RuntimeError:
            # Метод может быть не поддержан на новой сети — деградируем молча.
            return out
        return out

    # ------------------------------------------------------------------
    # Core API (RPC)
    # ------------------------------------------------------------------
    def asset_transfers(
        self,
        *,
        contract: str | None = None,
        from_address: str | None = None,
        to_address: str | None = None,
        categories: list[str] | None = None,
        order: str = "asc",
        max_pages: int = 50,
    ) -> Iterator[dict]:
        """Итератор по alchemy_getAssetTransfers (постранично)."""
        categories = categories or ["erc721", "erc1155"]
        page_key: str | None = None
        pages = 0
        while pages < max_pages:
            params: dict[str, Any] = {
                "category": categories,
                "order": order,
                "withMetadata": True,
                "excludeZeroValue": False,
                "maxCount": "0x3e8",  # 1000
            }
            if contract:
                params["contractAddresses"] = [contract]
            if from_address:
                params["fromAddress"] = from_address
            if to_address:
                params["toAddress"] = to_address
            if page_key:
                params["pageKey"] = page_key
            result = self._rpc("alchemy_getAssetTransfers", [params])
            for t in result.get("transfers", []):
                yield t
            page_key = result.get("pageKey")
            pages += 1
            if not page_key:
                break

    def eth_balance(self, address: str) -> float:
        """Баланс нативного токена в ETH (float)."""
        hex_wei = self._rpc("eth_getBalance", [address, "latest"])
        return int(hex_wei, 16) / 1e18

    def eth_call(self, to: str, data: str, block: str = "latest") -> str:
        """Низкоуровневый eth_call -> hex-строка результата ('0x' при пустом)."""
        result = self._rpc("eth_call", [{"to": to, "data": data}, block])
        return result or "0x"

    def is_mint(self, transfer: dict) -> bool:
        return (transfer.get("from") or "").lower() == ZERO_ADDRESS
