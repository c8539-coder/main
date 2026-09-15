"""Обогащение кошельков сигналами и скоринг.

Сигналы:
  * early   — минтеры + ранние покупатели (по логам контракта, order=asc)
  * degen   — buy-and-flip и mint-and-flip с расчётом реализованного PnL
  * smart   — blue-chip NFT + баланс (кросс-чейн, Ethereum mainnet)
  * kol     — curated-список адресов + опц. ENS
"""

from __future__ import annotations

import csv
import math
import os
from dataclasses import dataclass, field

from .alchemy import AlchemyClient
from .config import BLUECHIP_CONTRACTS, Settings, ZERO_ADDRESS

# Нормировочные "потолки" сигналов (значение >= cap => сигнал 1.0)
BLUECHIP_CAP = float(os.getenv("BLUECHIP_CAP", "20"))       # число blue-chip NFT (лог-шкала)
BLUECHIP_COLL_CAP = float(os.getenv("BLUECHIP_COLL_CAP", "4"))  # число разных blue-chip коллекций
BALANCE_CAP = float(os.getenv("BALANCE_CAP", "10"))        # баланс в нативном токене (лог-шкала)
FLIP_CAP = float(os.getenv("FLIP_CAP", "10"))
PNL_CAP = float(os.getenv("PNL_CAP", "5"))  # в нативном токене
# Кошелёк, заминтивший >= этого числа токенов, считаем командой/трежери
# (батч-минт аллокации), а не органическим ранним участником.
TEAM_MINT_MIN = int(os.getenv("TEAM_MINT_MIN", "10"))


def _log_ratio(value: float, cap: float) -> float:
    """Лог-нормировка: 0 при value=0, ~1 при value>=cap. Сжимает «китов»."""
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
    mint_count: int = 0          # сколько токенов заминтил (from 0x0)
    is_team: bool = False        # батч-минтер аллокации (команда/трежери)
    # degen / pnl (в пределах анализируемой коллекции)
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
    # итог
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
# 1. EARLY: минтеры + ранние покупатели
# ---------------------------------------------------------------------------
def tag_early(client: AlchemyClient, contract: str, settings: Settings,
              feats: dict[str, WalletFeatures]) -> None:
    """Пройти все входящие трансферы контракта по возрастанию времени.

    Первое приобретение каждого кошелька задаёт его "ранг". Минт (from 0x0)
    => is_minter. Первые ``early_buyer_fraction`` НЕ-минтовых приобретателей
    => is_early_buyer. Кошелёк, заминтивший >= ``TEAM_MINT_MIN`` токенов
    (батч-минт аллокации), помечается ``is_team`` — это команда/трежери, а не
    органический ранний участник.
    """
    first_acq: list[tuple[str, bool]] = []  # (wallet, via_mint) в порядке появления
    seen: set[str] = set()

    for t in client.asset_transfers(contract=contract, order="asc"):
        to = (t.get("to") or "").lower()
        if not to or to == ZERO_ADDRESS:
            continue
        via_mint = client.is_mint(t)
        if via_mint:
            f = feats.setdefault(to, WalletFeatures(address=to))
            f.is_minter = True
            f.mint_count += 1
        if to not in seen:
            seen.add(to)
            first_acq.append((to, via_mint))

    # команда/трежери: батч-минтеры аллокации
    for f in feats.values():
        if f.mint_count >= TEAM_MINT_MIN:
            f.is_team = True

    # ранние покупатели: первые N% по НЕ-минтовым первым приобретениям
    non_mint_first = [w for (w, m) in first_acq if not m]
    cutoff = max(1, int(len(non_mint_first) * settings.early_buyer_fraction))
    for w in non_mint_first[:cutoff]:
        feats.setdefault(w, WalletFeatures(address=w)).is_early_buyer = True


# ---------------------------------------------------------------------------
# 2. DEGEN + PnL (в пределах коллекции)
# ---------------------------------------------------------------------------
def _sale_price(sale: dict) -> float:
    """Извлечь суммарную цену сделки из записи getNFTSales (нативный токен)."""
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
    """Посчитать покупки/продажи и реализованный PnL по продажам коллекции.

    Возвращает True, если данные о продажах доступны (getNFTSales поддержан).
    Если нет — degen считается по числу out-трансферов (флип-прокси), PnL=0.
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
                # был ли покупателем ранее => buy-and-flip; минтером => mint-and-flip
                if f.buys > 0:
                    f.buy_and_flip = True
                if f.is_minter:
                    f.mint_and_flip = True
        return True

    # fallback: out-трансферы контракта как прокси флипов
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
    """Для каждого кошелька посчитать blue-chip NFT и баланс на mainnet."""
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
        # withMetadata=false отдаёт плоский "contractAddress"; с метаданными —
        # вложенный "contract.address". Поддерживаем оба.
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
# 4. KOL (curated список + опц. ENS)
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
# 5. Скоринг
# ---------------------------------------------------------------------------
def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


# Доля blue-chip внутри группы "smart" (остальное — баланс кита)
SMART_BLUECHIP_SHARE = float(os.getenv("SMART_BLUECHIP_SHARE", "0.6"))


def signals(f: WalletFeatures) -> dict[str, float]:
    """Нормализованные сигналы 0..1 по трём группам.

    * **smart** — «умные деньги»: разнообразие blue-chip коллекций (важнее числа NFT)
      плюс баланс кита (лог-шкала). Киты входят сюда же.
    * **degen** — активность флипов в пределах коллекции.
    * **early** — минтер (0.6) или ранний покупатель (1.0).

    Сигналы непрерывные там, где возможно, чтобы топ не «слипался».
    """
    bluechip = (
        0.65 * _clamp01(f.bluechip_collections / BLUECHIP_COLL_CAP)
        + 0.35 * _log_ratio(f.bluechip_count, BLUECHIP_CAP)
    )
    whale = _log_ratio(f.eth_balance, BALANCE_CAP)
    smart = SMART_BLUECHIP_SHARE * bluechip + (1.0 - SMART_BLUECHIP_SHARE) * whale
    degen = _clamp01(f.num_flips / FLIP_CAP)
    # команда/трежери не получает early-кредита за батч-минт аллокации
    if f.is_team:
        early = 0.0
    else:
        early = 1.0 if f.is_early_buyer else (0.6 if f.is_minter else 0.0)
    return {"smart": smart, "degen": degen, "early": early}


def score_components(f: WalletFeatures, weights: dict) -> tuple[float, dict[str, float]]:
    """Вернуть (total_score 0..100, вклад каждой группы в баллах)."""
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
