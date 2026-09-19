"""Tracker core: aggregate mint (on-chain) and buy (OpenSea) events per
collection and alert when enough watchlist wallets hit the same collection.

Mints come from the on-chain transfer feed (transfers from 0x0). Buys come
either from the OpenSea Stream (strict OpenSea sales, via add_buy) or, as a
fallback, from confirmed on-chain Seaport sales. State is JSON-serializable so
it survives restarts. add_buy is called from the stream thread, so state
mutations are guarded by a lock.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from scripts.nft_top_wallets.alchemy import AlchemyClient
from scripts.nft_top_wallets.config import ZERO_ADDRESS

from .opensea_api import OpenSeaAPI

# Seaport (OpenSea's protocol) OrderFulfilled event topic — marks a real sale.
SEAPORT_ORDER_FULFILLED = (
    "0x9d9af8e38d66c62e2c12f0225249fd9d721c54b83f48d9352c97c6cacdcb6f31"
)
ERC20_TRANSFER = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)


@dataclass
class Alert:
    chain: str
    contract: str
    name: str
    kind: str                     # "mint" or "buy"
    wallets: dict[str, str]       # address -> type
    ping: bool = False
    currencies: dict[str, str] = field(default_factory=dict)  # address -> "ETH"/"WETH"
    prices: dict[str, float] = field(default_factory=dict)     # address -> price in ETH


@dataclass
class MintTracker:
    watchlist: dict[str, str]                 # address_lower -> TYPE
    clients: dict[str, AlchemyClient]         # chain name -> client
    min_wallets: int = 5
    ping_wallets: int = 15
    ping_step: int = 15
    window_seconds: int = 6 * 3600
    backfill_blocks: int = 300
    track_buys: bool = True                    # also alert on secondary buys, not only mints
    sales_only: bool = True                    # on-chain buy = confirmed Seaport sale only
    opensea: OpenSeaAPI | None = None          # if set, buys are confirmed OpenSea-only via REST
    state: dict = field(default_factory=lambda: {"last_block": {}, "contracts": {}})
    _weth: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------
    def _contract_name(self, chain: str, contract: str) -> str:
        client = self.clients.get(chain)
        if client is None:
            return contract[:10]
        try:
            m = client.contract_metadata(contract)
            return (m.get("name")
                    or m.get("openSeaMetadata", {}).get("collectionName")
                    or contract[:10])
        except Exception:  # noqa: BLE001
            return contract[:10]

    def _token_is_weth(self, client: AlchemyClient, addr: str) -> bool:
        if addr in self._weth:
            return self._weth[addr]
        is_weth = False
        try:
            m = client._rpc("alchemy_getTokenMetadata", [addr])
            is_weth = (m or {}).get("symbol", "").upper() == "WETH"
        except Exception:  # noqa: BLE001
            is_weth = False
        self._weth[addr] = is_weth
        return is_weth

    def _sale_currency(self, client, tx_hash, cache):
        """'ETH'/'WETH' if the tx is a Seaport sale, else None (on-chain fallback)."""
        if not tx_hash:
            return None
        if tx_hash in cache:
            return cache[tx_hash]
        result = None
        try:
            r = client.tx_receipt(tx_hash)
            logs = r.get("logs", []) if r else []
            if any((lg.get("topics") or [""])[0].lower() == SEAPORT_ORDER_FULFILLED for lg in logs):
                weth = any(
                    len(lg.get("topics") or []) == 3
                    and (lg["topics"][0].lower() == ERC20_TRANSFER)
                    and self._token_is_weth(client, (lg.get("address") or "").lower())
                    for lg in logs
                )
                result = "WETH" if weth else "ETH"
        except Exception:  # noqa: BLE001
            result = None
        cache[tx_hash] = result
        return result

    # ------------------------------------------------------------------
    def _add(self, key: str, wallet: str, currency: str | None, now: float,
             name: str | None = None, price: float | None = None) -> None:
        """Add one event to state (caller holds the lock)."""
        entry = self.state["contracts"].setdefault(
            key, {"wallets": {}, "first": now, "alerted": False, "name": name,
                  "cur": {}, "prices": {}}
        )
        if name and not entry.get("name"):
            entry["name"] = name
        entry["wallets"][wallet] = self.watchlist[wallet]
        if currency:
            entry.setdefault("cur", {})[wallet] = currency
        if price:
            entry.setdefault("prices", {})[wallet] = price

    def _poll_chain(self, chain: str, now: float) -> None:
        client = self.clients[chain]
        current = client.block_number()
        start = self.state["last_block"].get(chain)
        if start is None:
            start = max(1, current - self.backfill_blocks)
        if current <= start:
            with self._lock:
                self.state["last_block"][chain] = current
            return

        sale_cache: dict = {}
        # (key, wallet, currency, name, price)
        events: list[tuple[str, str, str | None, str | None, float | None]] = []
        # candidate buys, resolved after the scan: (wallet, contract, tx)
        buy_candidates: list[tuple[str, str, str]] = []
        # network work outside the lock
        for t in client.transfers_since(start + 1, to_block=hex(current),
                                        mints_only=not self.track_buys):
            to = (t.get("to") or "").lower()
            if to not in self.watchlist:
                continue
            frm = (t.get("from") or "").lower()
            contract = ((t.get("rawContract") or {}).get("address") or "").lower()
            if not contract:
                continue
            if frm == ZERO_ADDRESS:
                events.append((f"{chain}|mint|{contract}", to, None, None, None))
            elif self.track_buys:
                buy_candidates.append((to, contract, t.get("hash") or ""))

        events += self._resolve_buys(chain, client, buy_candidates, now, sale_cache)

        with self._lock:
            for key, wallet, currency, name, price in events:
                self._add(key, wallet, currency, now, name, price)
            self.state["last_block"][chain] = current

    def _resolve_buys(self, chain, client, candidates, now, sale_cache):
        """Turn candidate secondary transfers into confirmed buy events.

        With an OpenSea key: one REST call per buying wallet confirms the sale
        happened on OpenSea (and gives ETH/WETH + collection name). Otherwise
        fall back to on-chain Seaport confirmation.
        """
        out = []
        if self.opensea is not None:
            since = now - self.window_seconds
            os_cache: dict[str, dict] = {}
            for wallet, contract, _tx in candidates:
                buys = os_cache.get(wallet)
                if buys is None:
                    buys = self.opensea.buys_by_contract(chain, wallet, since=since)
                    os_cache[wallet] = buys
                info = buys.get(contract)
                if info:  # OpenSea confirmed this wallet bought this collection
                    out.append((f"{chain}|buy|{contract}", wallet, info["currency"],
                                info.get("name") or None, info.get("price")))
            return out
        # fallback: on-chain Seaport sale confirmation (no price available)
        for wallet, contract, tx in candidates:
            currency = None
            if self.sales_only:
                currency = self._sale_currency(client, tx, sale_cache)
                if currency is None:
                    continue
            out.append((f"{chain}|buy|{contract}", wallet, currency, None, None))
        return out

    def _collect_alerts(self) -> list[Alert]:
        alerts: list[Alert] = []
        for key, entry in self.state["contracts"].items():
            n = len(entry["wallets"])
            last_ping = entry.get("pinged_at", 0)
            hit_base = n >= self.min_wallets and not entry.get("alerted")
            if last_ping == 0:
                hit_ping = n >= self.ping_wallets
            else:
                hit_ping = self.ping_step > 0 and n >= last_ping + self.ping_step
            if not (hit_base or hit_ping):
                continue
            chain, kind, contract = key.split("|", 2)
            if not entry["name"]:
                entry["name"] = self._contract_name(chain, contract)
            entry["alerted"] = True
            if hit_ping:
                entry["pinged"] = True
                entry["pinged_at"] = n
            alerts.append(Alert(chain, contract, entry["name"], kind,
                                dict(entry["wallets"]), ping=hit_ping,
                                currencies=dict(entry.get("cur", {})),
                                prices=dict(entry.get("prices", {}))))
        return alerts

    def _prune(self, now: float) -> None:
        contracts = self.state["contracts"]
        for key in [k for k, e in contracts.items()
                    if now - e.get("first", now) > self.window_seconds]:
            del contracts[key]

    def poll(self) -> list[Alert]:
        """One cycle: poll chains for mints, return new alerts (mint + buy)."""
        now = time.time()
        for chain in self.clients:
            self._poll_chain(chain, now)
        with self._lock:
            alerts = self._collect_alerts()
            self._prune(now)
        return alerts
