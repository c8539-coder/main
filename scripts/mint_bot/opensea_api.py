"""OpenSea REST API v2 — confirm a sale happened on OpenSea and get its price.

The Stream API's wildcard firehose does not scale to a single client, so buys
are found on-chain (filtered to our wallets) and then confirmed here: for a
wallet that just acquired an NFT, we ask OpenSea for that account's recent
sales. If OpenSea reports a sale of that collection to that wallet, it is an
OpenSea sale — and the payment token (ETH vs WETH) comes straight from the
event. Only a handful of calls per poll cycle (one per wallet that bought).

Docs: https://docs.opensea.io/reference/list_events_by_account
"""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("mint_bot.opensea")

OS_BASE = "https://api.opensea.io/api/v2"

# our internal chain name -> OpenSea API chain identifier
OS_CHAIN = {
    "eth-mainnet": "ethereum",
    "robinhood-mainnet": "robinhood",
}


class OpenSeaAPI:
    def __init__(self, api_key: str, timeout: float = 15.0):
        self.key = api_key
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"accept": "application/json", "x-api-key": api_key})

    def account_sales(self, chain: str, address: str, *, limit: int = 50) -> list[dict]:
        """Recent OpenSea sale events involving ``address`` (newest first)."""
        os_chain = OS_CHAIN.get(chain, chain)
        url = f"{OS_BASE}/events/accounts/{address}"
        params = {"event_type": "sale", "chain": os_chain, "limit": min(limit, 50)}
        try:
            r = self._session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            log.warning("OpenSea account_sales network error: %s", exc)
            return []
        if r.status_code != 200:
            log.warning("OpenSea account_sales HTTP %s: %s", r.status_code, r.text[:160])
            return []
        return r.json().get("asset_events", [])

    def buys_by_contract(self, chain: str, wallet: str, *, since: float = 0.0) -> dict[str, dict]:
        """Map contract_lower -> {"currency","price","name"} for OpenSea sales
        where ``wallet`` was the buyer (optionally only since a unix time)."""
        out: dict[str, dict] = {}
        w = wallet.lower()
        for ev in self.account_sales(chain, wallet):
            if (ev.get("buyer") or "").lower() != w:
                continue
            ts = ev.get("closing_date") or ev.get("event_timestamp") or 0
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                ts = 0.0
            if since and ts and ts < since:
                continue
            nft = ev.get("nft") or {}
            contract = (nft.get("contract") or "").lower()
            if not contract:
                continue
            pay = ev.get("payment") or {}
            symbol = (pay.get("symbol") or "ETH").upper()
            currency = "WETH" if symbol == "WETH" else ("ETH" if symbol in ("ETH", "WETH") else symbol)
            price = _amount(pay)
            name = nft.get("collection") or nft.get("name") or ""
            # keep the first (newest) sale per contract
            out.setdefault(contract, {"currency": currency, "price": price, "name": name})
        return out


def _amount(pay: dict) -> float:
    try:
        return int(pay.get("quantity")) / (10 ** int(pay.get("decimals", 18)))
    except (TypeError, ValueError):
        return 0.0
