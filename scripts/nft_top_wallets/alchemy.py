"""Thin Alchemy NFT API v3 + Core JSON-RPC client.

Read-only. Retries with exponential backoff on 429/5xx/network errors, with
light throttling between requests.
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
    # Low-level requests
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
            except requests.RequestException as exc:  # network failures
                last_exc = exc
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_exc = RuntimeError(
                        f"{resp.status_code} from Alchemy: {resp.text[:200]}"
                    )
                else:
                    # 4xx (except 429) -> do not retry, request error
                    raise RuntimeError(
                        f"Alchemy {resp.status_code}: {resp.text[:300]}"
                    )
            sleep_s = self.s.backoff_base_s * (2 ** attempt)
            time.sleep(sleep_s)
        raise RuntimeError(f"Alchemy did not respond after retries: {last_exc}")

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
        """All owners of the contract -> {wallet_lower: tokens_held}."""
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
        """NFTs owned by a wallet (optionally filtered by contracts)."""
        out: list[dict] = []
        page_key: str | None = None
        contract_list = list(contracts) if contracts else None
        while True:
            params: dict[str, Any] = {"owner": owner, "withMetadata": "false", "pageSize": 100}
            if contract_list:
                # Alchemy accepts contractAddresses[] as a repeated key
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
        """Sales for the contract (if the chain supports it). May return []."""
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
            # The method may be unsupported on a newer chain -> degrade silently.
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
        """Iterate alchemy_getAssetTransfers (paginated)."""
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
        """Native token balance in ETH (float)."""
        hex_wei = self._rpc("eth_getBalance", [address, "latest"])
        return int(hex_wei, 16) / 1e18

    def eth_call(self, to: str, data: str, block: str = "latest") -> str:
        """Low-level eth_call -> hex result string ('0x' when empty)."""
        result = self._rpc("eth_call", [{"to": to, "data": data}, block])
        return result or "0x"

    def is_mint(self, transfer: dict) -> bool:
        return (transfer.get("from") or "").lower() == ZERO_ADDRESS

    def block_number(self) -> int:
        """Current block number."""
        return int(self._rpc("eth_blockNumber", []), 16)

    def mints_since(
        self,
        from_block: int,
        *,
        to_block: str = "latest",
        max_pages: int = 40,
    ) -> Iterator[dict]:
        """All mints (transfers from 0x0) starting at block ``from_block``.

        Yields alchemy_getAssetTransfers transfers with ``fromAddress = 0x0``,
        paginated. Each transfer has ``to``, ``rawContract.address`` and
        ``blockNum`` (hex).
        """
        page_key: str | None = None
        pages = 0
        while pages < max_pages:
            params: dict[str, Any] = {
                "category": ["erc721", "erc1155"],
                "order": "asc",
                "withMetadata": True,
                "excludeZeroValue": False,
                "maxCount": "0x3e8",  # 1000
                "fromAddress": ZERO_ADDRESS,
                "fromBlock": hex(from_block),
                "toBlock": to_block,
            }
            if page_key:
                params["pageKey"] = page_key
            result = self._rpc("alchemy_getAssetTransfers", [params])
            for t in result.get("transfers", []):
                yield t
            page_key = result.get("pageKey")
            pages += 1
            if not page_key:
                break
