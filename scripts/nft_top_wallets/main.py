"""CLI: собрать и ранжировать топ-кошельки по NFT-коллекции.

Пример:
    python -m scripts.nft_top_wallets.main --top 200
    python -m scripts.nft_top_wallets.main --contract 0x116e... --limit 50

Секреты — только через окружение (см. .env.example).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time

from .alchemy import AlchemyClient
from .config import KOL_LIST_PATH, SMART_MONEY_LIST_PATH, Settings
from .ens import resolve_many as resolve_ens_many
from .enrich import (
    TEAM_MINT_MIN,
    TEAM_SUPPLY_SHARE,
    WalletFeatures,
    score_wallet,
    tag_degen,
    tag_early,
    tag_kol,
    tag_smart_money,
)

OUT_DIR = os.path.join(os.path.dirname(__file__), "out")

CSV_COLUMNS = [
    "address", "ens", "tokens_held", "is_minter", "is_early_buyer",
    "mint_count", "is_team",
    "buys", "sells", "buy_and_flip", "mint_and_flip", "profitable_flipper",
    "realized_pnl", "bluechip_count", "bluechip_collections", "eth_balance",
    "labels", "total_score",
]


def _row(f: WalletFeatures) -> dict:
    return {
        "address": f.address,
        "ens": f.ens,
        "tokens_held": f.tokens_held,
        "is_minter": int(f.is_minter),
        "is_early_buyer": int(f.is_early_buyer),
        "mint_count": f.mint_count,
        "is_team": int(f.is_team),
        "buys": f.buys,
        "sells": f.sells,
        "buy_and_flip": int(f.buy_and_flip),
        "mint_and_flip": int(f.mint_and_flip),
        "profitable_flipper": int(f.profitable_flipper),
        "realized_pnl": round(f.realized_pnl, 5),
        "bluechip_count": f.bluechip_count,
        "bluechip_collections": f.bluechip_collections,
        "eth_balance": round(f.eth_balance, 5),
        "labels": ";".join(sorted(set(f.labels))),
        "total_score": f.total_score,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Топ-кошельки NFT-коллекции")
    p.add_argument("--contract", help="Адрес контракта (по умолчанию из env/config)")
    p.add_argument("--top", type=int, default=200, help="Сколько кошельков вывести")
    p.add_argument(
        "--limit", type=int, default=0,
        help="Ограничить число обогащаемых кошельков (0 = все). Для smoke-теста.",
    )
    p.add_argument(
        "--no-mainnet", action="store_true",
        help="Не ходить на eth-mainnet за blue-chip (smart-money=0)",
    )
    p.add_argument(
        "--ens", type=int, default=100,
        help="Резолвить ENS-имена для top-N кошельков (0 = выключить). Только отображение.",
    )
    p.add_argument(
        "--keep-team", action="store_true",
        help="Не исключать команду/трежери (батч-минтеров) из лидерборда.",
    )
    p.add_argument("--out", default=OUT_DIR, help="Каталог для CSV")
    return p.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings.load()
    contract = (args.contract or settings.default_contract).lower()

    chain = AlchemyClient(settings.chain, settings)
    mainnet = (
        None if args.no_mainnet or settings.mainnet is None
        else AlchemyClient(settings.mainnet, settings)
    )

    print(f"[i] Сеть коллекции : {settings.chain.name}")
    print(f"[i] Контракт       : {contract}")
    print(f"[i] Mainnet enrich : {'off' if mainnet is None else settings.mainnet.name}")

    # 0) sanity: метаданные контракта
    try:
        meta = chain.contract_metadata(contract)
        name = meta.get("name") or meta.get("openSeaMetadata", {}).get("collectionName")
        print(f"[i] Коллекция      : {name or '—'} "
              f"(supply={meta.get('totalSupply')}, type={meta.get('tokenType')})")
    except Exception as exc:  # noqa: BLE001
        print(f"[!] Не удалось получить метаданные контракта: {exc}", file=sys.stderr)

    t0 = time.time()

    # 1) холдеры
    print("[1/5] Собираю холдеров…")
    holders = chain.owners_for_contract(contract)
    feats: dict[str, WalletFeatures] = {
        addr: WalletFeatures(address=addr, tokens_held=held)
        for addr, held in holders.items()
    }
    print(f"      холдеров: {len(feats)}")

    # 2) early (минтеры + ранние покупатели)
    print("[2/5] Размечаю early (минтеры + ранние покупатели)…")
    tag_early(chain, contract, settings, feats)

    # 3) degen + PnL
    print("[3/5] Считаю флипы и PnL…")
    have_sales = tag_degen(chain, contract, settings, feats)
    if not have_sales:
        print("      (!) getNFTSales недоступен на этой сети — degen по прокси, PnL=0")

    # актуализируем tokens_held для кошельков, которые всё продали (в feats попали
    # из early/degen, но не из holders) — оставляем 0, они уже не холдеры.

    # 4) smart money (mainnet)
    #    ограничиваем обогащение текущими холдерами и, при --limit, top-N по
    #    предварительному признаку, чтобы не жечь лимиты на всех.
    holder_addrs = set(holders.keys())
    enrich_targets = holder_addrs
    if args.limit and args.limit > 0:
        enrich_targets = set(list(holder_addrs)[: args.limit])
    print(f"[4/5] Обогащаю smart-money по {len(enrich_targets)} кошелькам…")
    tag_smart_money(mainnet, feats, only=enrich_targets)

    # 5) kol
    print("[5/5] Размечаю KOL/curated…")
    tag_kol(feats, KOL_LIST_PATH, SMART_MONEY_LIST_PATH)

    # скоринг: три группы — smart / degen / early
    for f in feats.values():
        score_wallet(f, settings.weights)

    # ранжируем только текущих холдеров (те, кто реально держит коллекцию)
    ranked = sorted(
        (f for a, f in feats.items() if a in holder_addrs),
        key=lambda f: f.total_score,
        reverse=True,
    )
    # команда/трежери исключается из лидерборда (в CSV остаётся, помечена is_team)
    n_team = sum(1 for f in ranked if f.is_team)
    display = ranked if args.keep_team else [f for f in ranked if not f.is_team]

    # ENS-имена для верхушки (только отображение, на скор не влияют).
    # Резолвим после ранжирования и лишь top-N, чтобы не жечь лимиты.
    if mainnet is not None and args.ens > 0 and display:
        head = display[: args.ens]
        print(f"[+] Резолвлю ENS для top-{len(head)}…")
        names = resolve_ens_many(mainnet, [f.address for f in head])
        for f in head:
            f.ens = names.get(f.address, "")
        if names:
            print(f"      найдено имён: {len(names)}")

    # вывод: в CSV пишем всех холдеров (команда помечена is_team), в лидерборд — display
    os.makedirs(args.out, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    csv_path = os.path.join(args.out, f"top_wallets_{contract[:10]}_{ts}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for f in ranked:
            writer.writerow(_row(f))

    _print_table(display[: args.top])
    if n_team:
        total_minted = sum(f.mint_count for f in feats.values())
        threshold = int(max(TEAM_MINT_MIN, TEAM_SUPPLY_SHARE * total_minted))
        note = "включена (--keep-team)" if args.keep_team else "исключена из лидерборда"
        print(f"[i] команда/трежери: {n_team} кош. (батч-минт ≥{threshold}) — {note}")
    print(f"\n[✓] Готово за {time.time() - t0:.1f}s. CSV: {csv_path}")
    print(f"    Холдеров: {len(ranked)} | в лидерборде: {len(display)} | показано: "
          f"{min(args.top, len(display))}")
    return 0


def _print_table(rows: list[WalletFeatures]) -> None:
    if not rows:
        print("Нет данных.")
        return
    print(f"\n{'#':>3}  {'score':>6}  {'address / ens':<42}  {'held':>4}  "
          f"{'flips':>5}  {'ethβ':>8}  {'bc':>3}  labels")
    print("-" * 108)
    for i, f in enumerate(rows, 1):
        tags = ",".join(t for t in (
            "TEAM" if f.is_team else "",
            "M" if f.is_minter else "",
            "E" if f.is_early_buyer else "",
            "F" if f.profitable_flipper else "",
        ) if t)
        labels = ";".join(sorted(set(f.labels)))[:30]
        who = f.ens or f.address
        print(f"{i:>3}  {f.total_score:>6.1f}  {who:<42}  {f.tokens_held:>4}  "
              f"{f.num_flips:>5}  {f.eth_balance:>8.3f}  {f.bluechip_count:>3}  "
              f"{tags} {labels}".rstrip())


if __name__ == "__main__":
    raise SystemExit(run())
