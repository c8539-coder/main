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


@dataclass
class Alert:
    chain: str
    contract: str
    name: str
    kind: str                # "mint" or "buy"
    wallets: dict[str, str]  # address -> type
    ping: bool = False       # whether to ping the role (large signal)


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
    state: dict = field(default_factory=lambda: {"last_block": {}, "contracts": {}})

    # ------------------------------------------------------------------
    def _contract_name(self, chain: str, contract: str) -> str:
        try:
            m = self.clients[chain].contract_metadata(contract)
            return (m.get("name")
                    or m.get("openSeaMetadata", {}).get("collectionName")
                    or contract[:10])
        except Exception:  # noqa: BLE001
            return contract[:10]

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
        # mints_only when we don't care about buys -> lighter feed
        for t in client.transfers_since(start + 1, to_block=hex(current),
                                        mints_only=not self.track_buys):
            to = (t.get("to") or "").lower()
            if to not in self.watchlist:
                continue
            frm = (t.get("from") or "").lower()
            kind = "mint" if frm == ZERO_ADDRESS else "buy"
            rc = t.get("rawContract") or {}
            contract = (rc.get("address") or "").lower()
            if not contract:
                continue
            key = f"{chain}|{kind}|{contract}"
            entry = contracts.setdefault(
                key, {"wallets": {}, "first": now, "alerted": False, "name": None}
            )
            entry["wallets"][to] = self.watchlist[to]
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
                                dict(entry["wallets"]), ping=hit_ping))
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
