"""Enrich wallets with signals and score them.

Signals:
  * early   - minters + early buyers (by contract logs, order=asc)
  * degen   - buy-and-flip and mint-and-flip with realized PnL
  * smart   - blue-chip NFTs + balance (cross-chain, Ethereum mainnet)
  * kol     - curated address list + optional ENS
"""

from __future__ import annotations

import csv
import math
import os
from dataclasses import dataclass, field

from .alchemy import AlchemyClient
from .config import BLUECHIP_CONTRACTS, Settings, ZERO_ADDRESS

# Signal caps (value >= cap => signal 1.0)
BLUECHIP_CAP = float(os.getenv("BLUECHIP_CAP", "20"))       # number of blue-chip NFTs (log scale)
BLUECHIP_COLL_CAP = float(os.getenv("BLUECHIP_COLL_CAP", "4"))  # number of distinct blue-chip collections
BALANCE_CAP = float(os.getenv("BALANCE_CAP", "10"))        # native token balance (log scale)
HELD_CAP = float(os.getenv("HELD_CAP", "9"))               # collection NFTs above 1 (log scale): 10 = max
FLIP_CAP = float(os.getenv("FLIP_CAP", "10"))
PNL_CAP = float(os.getenv("PNL_CAP", "5"))  # in native token
# Team/treasury = allocation batch-minted in ONE block. Key difference from an
# open-mint whale: a team mints a big batch in one transaction/block, while an
# open-mint whale/bot accumulates a bit at a time over thousands of blocks. So we
# treat a wallet as team when its max mints in a single block >= TEAM_BATCH_MIN.
TEAM_BATCH_MIN = int(os.getenv("TEAM_BATCH_MIN", "50"))


def _log_ratio(value: float, cap: float) -> float:
    """Log normalization: 0 at value=0, ~1 at value>=cap. Compresses whales."""
    if value <= 0 or cap <= 0:
        return 0.0
    return _clamp01(math.log1p(value) / math.log1p(cap))


@dataclass
class WalletFeatures:
    address: str
    tokens_held: int = 0
    # early
    is_minter: bool = False
    is_early_buyer: bool = False
    mint_count: int = 0          # tokens minted (from 0x0)
    mint_batch: int = 0          # max mints in one block (batch-allocation signal)
    is_team: bool = False        # allocation batch-minter (team/treasury)
    # degen / pnl (within the analyzed collection)
    buys: int = 0
    sells: int = 0
    buy_and_flip: bool = False
    mint_and_flip: bool = False
    realized_pnl: float = 0.0
    # smart money (mainnet)
    bluechip_count: int = 0
    bluechip_collections: int = 0
    eth_balance: float = 0.0
    # kol
    ens: str = ""
    labels: list[str] = field(default_factory=list)
    # result
    total_score: float = 0.0

    @property
    def is_early(self) -> bool:
        return self.is_minter or self.is_early_buyer

    @property
    def profitable_flipper(self) -> bool:
        return (self.sells > 0) and (self.realized_pnl > 0)

    @property
    def num_flips(self) -> int:
        return self.sells


# ---------------------------------------------------------------------------
# 1. EARLY: minters + early buyers
# ---------------------------------------------------------------------------
def tag_early(client: AlchemyClient, contract: str, settings: Settings,
              feats: dict[str, WalletFeatures]) -> None:
    """Walk all incoming contract transfers in ascending time order.

    Each wallet's first acquisition sets its "rank". A mint (from 0x0) =>
    is_minter. The first ``early_buyer_fraction`` NON-mint acquirers =>
    is_early_buyer. A wallet that batch-mints >= ``TEAM_BATCH_MIN`` tokens in one
    block is flagged ``is_team`` - team/treasury, not an organic early participant.
    """
    first_acq: list[tuple[str, bool]] = []  # (wallet, via_mint) in order of appearance
    seen: set[str] = set()
    block_run: dict[str, tuple[int, int]] = {}  # wallet -> (last block, count in it)

    for t in client.asset_transfers(contract=contract, order="asc"):
        to = (t.get("to") or "").lower()
        if not to or to == ZERO_ADDRESS:
            continue
        via_mint = client.is_mint(t)
        if via_mint:
            f = feats.setdefault(to, WalletFeatures(address=to))
            f.is_minter = True
            f.mint_count += 1
            # max mints in one block (batch): transfers come in ascending order
            blk = int(t["blockNum"], 16) if t.get("blockNum") else -1
            last_blk, run = block_run.get(to, (None, 0))
            run = run + 1 if blk == last_blk else 1
            block_run[to] = (blk, run)
            if run > f.mint_batch:
                f.mint_batch = run
        if to not in seen:
            seen.add(to)
            first_acq.append((to, via_mint))

    # team/treasury: allocation batch-minted in one block, unlike an open-mint
    # whale that accumulates a bit at a time over many blocks.
    for f in feats.values():
        if f.mint_batch >= TEAM_BATCH_MIN:
            f.is_team = True

    # early buyers: first N% of NON-mint first acquisitions
    non_mint_first = [w for (w, m) in first_acq if not m]
    cutoff = max(1, int(len(non_mint_first) * settings.early_buyer_fraction))
    for w in non_mint_first[:cutoff]:
        feats.setdefault(w, WalletFeatures(address=w)).is_early_buyer = True


# ---------------------------------------------------------------------------
# 2. DEGEN + PnL (within the collection)
# ---------------------------------------------------------------------------
def _sale_price(sale: dict) -> float:
    """Extract the total sale price from a getNFTSales record (native token)."""
    total = 0.0
    for key in ("sellerFee", "protocolFee", "royaltyFee"):
        fee = sale.get(key) or {}
        amount = fee.get("amount")
        if amount is None:
            continue
        try:
            decimals = int(fee.get("decimals", 18) or 18)
            total += int(amount) / (10 ** decimals)
        except (ValueError, TypeError):
            continue
    return total


def tag_degen(client: AlchemyClient, contract: str, settings: Settings,
              feats: dict[str, WalletFeatures]) -> bool:
    """Count buys/sells and realized PnL from collection sales.

    Returns True if sales data is available (getNFTSales supported). Otherwise
    degen is counted from out-transfers (flip proxy), PnL=0.
    """
    sales = client.nft_sales(contract)
    if sales:
        for sale in sales:
            buyer = (sale.get("buyerAddress") or "").lower()
            seller = (sale.get("sellerAddress") or "").lower()
            price = _sale_price(sale)
            if buyer:
                f = feats.setdefault(buyer, WalletFeatures(address=buyer))
                f.buys += 1
                f.realized_pnl -= price
            if seller and seller != ZERO_ADDRESS:
                f = feats.setdefault(seller, WalletFeatures(address=seller))
                f.sells += 1
                f.realized_pnl += price
                # was a buyer before => buy-and-flip; a minter => mint-and-flip
                if f.buys > 0:
                    f.buy_and_flip = True
                if f.is_minter:
                    f.mint_and_flip = True
        return True

    # fallback: contract out-transfers as a flip proxy
    for t in client.asset_transfers(contract=contract, order="asc"):
        frm = (t.get("from") or "").lower()
        if not frm or frm == ZERO_ADDRESS:
            continue
        f = feats.setdefault(frm, WalletFeatures(address=frm))
        f.sells += 1
        if f.is_minter:
            f.mint_and_flip = True
        else:
            f.buy_and_flip = True
    return False


# ---------------------------------------------------------------------------
# 3. SMART MONEY (mainnet blue-chip)
# ---------------------------------------------------------------------------
def tag_smart_money(mainnet: AlchemyClient | None, feats: dict[str, WalletFeatures],
                    only: set[str] | None = None) -> None:
    """For each wallet, count blue-chip NFTs and the mainnet balance."""
    if mainnet is None:
        return
    bluechip_addrs = list(BLUECHIP_CONTRACTS.keys())
    targets = only if only is not None else set(feats.keys())
    for addr in targets:
        f = feats[addr]
        try:
            nfts = mainnet.nfts_for_owner(addr, contracts=bluechip_addrs)
        except RuntimeError:
            nfts = []
        f.bluechip_count = len(nfts)
        # withMetadata=false returns a flat "contractAddress"; with metadata it is
        # nested "contract.address". Support both.
        collections = {
            (n.get("contractAddress") or n.get("contract", {}).get("address") or "").lower()
            for n in nfts
        }
        collections.discard("")
        f.bluechip_collections = len(collections)
        try:
            f.eth_balance = mainnet.eth_balance(addr)
        except RuntimeError:
            f.eth_balance = 0.0
        for c in collections:
            label = BLUECHIP_CONTRACTS.get(c)
            if label:
                f.labels.append(label)


# ---------------------------------------------------------------------------
# 4. KOL (curated list + optional ENS)
# ---------------------------------------------------------------------------
def _load_address_labels(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path or not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if not row or row[0].strip().startswith("#"):
                continue
            addr = row[0].strip().lower()
            if not addr.startswith("0x"):
                continue
            out[addr] = row[1].strip() if len(row) > 1 else "KOL"
    return out


def tag_kol(feats: dict[str, WalletFeatures], kol_path: str,
            smart_path: str) -> None:
    kol = _load_address_labels(kol_path)
    smart = _load_address_labels(smart_path)
    for addr, f in feats.items():
        if addr in kol:
            f.labels.append(f"KOL:{kol[addr]}")
        if addr in smart:
            f.labels.append(f"SM:{smart[addr]}")


# ---------------------------------------------------------------------------
# 5. Scoring
# ---------------------------------------------------------------------------
def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


# Shares within the "smart" group: blue-chip, holdings of this collection
# (conviction), remainder is whale ETH balance. First two must sum to <= 1.
SMART_BLUECHIP_SHARE = float(os.getenv("SMART_BLUECHIP_SHARE", "0.5"))
SMART_HELD_SHARE = float(os.getenv("SMART_HELD_SHARE", "0.25"))


def signals(f: WalletFeatures) -> dict[str, float]:
    """Normalized 0..1 signals for the three groups.

    * **smart** - smart money / whales: blue-chip collection diversity (more than
      raw NFT count), ETH balance (whale), and **conviction** - how many NFTs of
      this collection the wallet holds (floor buyers supporting the price).
    * **degen** - flip activity within the collection.
    * **early** - minter (0.6) or early buyer (1.0).

    Signals are continuous where possible so the top does not clump.
    """
    bluechip = (
        0.65 * _clamp01(f.bluechip_collections / BLUECHIP_COLL_CAP)
        + 0.35 * _log_ratio(f.bluechip_count, BLUECHIP_CAP)
    )
    whale = _log_ratio(f.eth_balance, BALANCE_CAP)
    # conviction: holds MANY NFTs of this collection (above the first one)
    conviction = _log_ratio(max(f.tokens_held - 1, 0), HELD_CAP)
    whale_share = max(0.0, 1.0 - SMART_BLUECHIP_SHARE - SMART_HELD_SHARE)
    smart = (
        SMART_BLUECHIP_SHARE * bluechip
        + SMART_HELD_SHARE * conviction
        + whale_share * whale
    )
    degen = _clamp01(f.num_flips / FLIP_CAP)
    # team/treasury gets no early credit for batch-minting the allocation
    if f.is_team:
        early = 0.0
    else:
        early = 1.0 if f.is_early_buyer else (0.6 if f.is_minter else 0.0)
    return {"smart": smart, "degen": degen, "early": early}


def score_components(f: WalletFeatures, weights: dict) -> tuple[float, dict[str, float]]:
    """Return (total_score 0..100, each group's point contribution)."""
    sigs = signals(f)
    active = {k: weights.get(k, 0.0) for k in sigs}
    total_w = sum(active.values()) or 1.0
    total = round(sum(active[k] * sigs[k] for k in sigs) / total_w * 100, 2)
    contrib = {k: round(active[k] * sigs[k] / total_w * 100, 2) for k in sigs}
    return total, contrib


def score_wallet(f: WalletFeatures, weights: dict) -> float:
    total, _ = score_components(f, weights)
    f.total_score = total
    return total
