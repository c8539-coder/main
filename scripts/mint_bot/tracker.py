"""Tracker core: poll NFT transfers, keep watchlist wallets, aggregate per
contract and event kind (mint or buy/sweep).

State is JSON-serializable so it survives restarts (last processed block per
chain + wallets accumulated per contract/kind).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from scripts.nft_top_wallets.alchemy import AlchemyClient
from scripts.nft_top_wallets.config import ZERO_ADDRESS

# Seaport (OpenSea's protocol) OrderFulfilled event topic — marks a real sale.
SEAPORT_ORDER_FULFILLED = (
    "0x9d9af8e38d66c62e2c12f0225249fd9d721c54b83f48d9352c97c6cacdcb6f31"
)
# ERC20 Transfer(address,address,uint256) topic — payment token leg of a sale.
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
    ping: bool = False            # whether to ping the role (large signal)
    currencies: dict[str, str] = field(default_factory=dict)  # address -> "ETH"/"WETH" (buys)


@dataclass
class MintTracker:
    watchlist: dict[str, str]                 # address_lower -> TYPE
    clients: dict[str, AlchemyClient]         # chain name -> client
    min_wallets: int = 5                      # threshold for the quiet alert (no ping)
    ping_wallets: int = 15                    # threshold at which the role is pinged
    ping_step: int = 15                       # re-ping every +N wallets (0 = ping once)
    window_seconds: int = 6 * 3600            # window over which events per contract accrue
    backfill_blocks: int = 300                # how many blocks back to scan on first start
    track_buys: bool = True                   # also alert on buys/sweeps, not just mints
    sales_only: bool = True                   # a buy counts only if it's a confirmed Seaport sale
    state: dict = field(default_factory=lambda: {"last_block": {}, "contracts": {}})
    _weth: dict = field(default_factory=dict)  # token address -> is WETH (cached)

    # ------------------------------------------------------------------
    def _contract_name(self, chain: str, contract: str) -> str:
        try:
            m = self.clients[chain].contract_metadata(contract)
            return (m.get("name")
                    or m.get("openSeaMetadata", {}).get("collectionName")
                    or contract[:10])
        except Exception:  # noqa: BLE001
            return contract[:10]

    def _token_is_weth(self, client: AlchemyClient, addr: str) -> bool:
        """Is this ERC20 the chain's WETH? Cached by symbol lookup."""
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

    def _sale_currency(self, client: AlchemyClient, tx_hash: str | None,
                       cache: dict[str, str | None]) -> str | None:
        """Return the sale currency for a tx: 'ETH', 'WETH', or None if not a sale.

        A sale = a Seaport OrderFulfilled event. If the tx also has an ERC20
        transfer in WETH, the payment was WETH (an accepted offer); otherwise the
        buyer paid native ETH (a sweep/direct buy).
        """
        if not tx_hash:
            return None
        if tx_hash in cache:
            return cache[tx_hash]
        result: str | None = None
        try:
            r = client.tx_receipt(tx_hash)
            logs = r.get("logs", []) if r else []
            is_sale = any((lg.get("topics") or [""])[0].lower() == SEAPORT_ORDER_FULFILLED
                          for lg in logs)
            if is_sale:
                weth = False
                for lg in logs:
                    tp = lg.get("topics") or []
                    # ERC20 transfer = Transfer topic with exactly 3 topics (value in data)
                    if len(tp) == 3 and tp[0].lower() == ERC20_TRANSFER:
                        if self._token_is_weth(client, (lg.get("address") or "").lower()):
                            weth = True
                            break
                result = "WETH" if weth else "ETH"
        except Exception:  # noqa: BLE001
            result = None
        cache[tx_hash] = result
        return result

    def _poll_chain(self, chain: str, now: float) -> None:
        client = self.clients[chain]
        current = client.block_number()
        start = self.state["last_block"].get(chain)
        if start is None:
            start = max(1, current - self.backfill_blocks)
        if current <= start:
            self.state["last_block"][chain] = current
            return

        contracts = self.state["contracts"]
        sale_cache: dict[str, str | None] = {}  # tx hash -> "ETH"/"WETH"/None (per poll)
        # mints_only when we don't care about buys -> lighter feed
        for t in client.transfers_since(start + 1, to_block=hex(current),
                                        mints_only=not self.track_buys):
            to = (t.get("to") or "").lower()
            if to not in self.watchlist:
                continue
            frm = (t.get("from") or "").lower()
            kind = "mint" if frm == ZERO_ADDRESS else "buy"
            currency = None
            if kind == "buy" and self.sales_only:
                # a buy counts only if the tx is a real marketplace sale (Seaport)
                currency = self._sale_currency(client, t.get("hash"), sale_cache)
                if currency is None:
                    continue
            rc = t.get("rawContract") or {}
            contract = (rc.get("address") or "").lower()
            if not contract:
                continue
            key = f"{chain}|{kind}|{contract}"
            entry = contracts.setdefault(
                key, {"wallets": {}, "first": now, "alerted": False, "name": None, "cur": {}}
            )
            entry["wallets"][to] = self.watchlist[to]
            if currency:
                entry.setdefault("cur", {})[to] = currency
        self.state["last_block"][chain] = current

    def _collect_alerts(self) -> list[Alert]:
        """Quiet alert at >= min_wallets, a separate ping alert at >= ping_wallets.

        Each contract+kind can yield up to two alerts: the quiet one (reached the
        base threshold) and, once it grows to the ping threshold, one that pings.
        """
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
                                currencies=dict(entry.get("cur", {}))))
        return alerts

    def _prune(self, now: float) -> None:
        contracts = self.state["contracts"]
        for key in [k for k, e in contracts.items()
                    if now - e.get("first", now) > self.window_seconds]:
            del contracts[key]

    # ------------------------------------------------------------------
    def poll(self) -> list[Alert]:
        """One cycle: poll all chains, return new alerts (>= threshold)."""
        now = time.time()
        for chain in self.clients:
            self._poll_chain(chain, now)
        alerts = self._collect_alerts()
        self._prune(now)
        return alerts
