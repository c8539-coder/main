#!/usr/bin/env python3
"""Полный анализ ОДНОЙ NFT-коллекции одной командой (для конвейера по многим коллекциям).

Делает ОДИН тяжёлый проход по он-чейну (классификация всех перемещений) и из него
выводит все три артефакта:
    <name>_movements.csv  — каждое перемещение NFT с источником (SALE/MINT/TRANSFER)
    <name>_earnings.csv   — gross realized по кошельку (модель эталона)
    <name>_watchlist.csv  — matched-PnL + фильтр сильных кошельков (watch=1)

Запуск:
    export RH_RPC="https://robinhood-mainnet.g.alchemy.com/v2/<KEY>"
    python3 tools/analyze_collection.py --contract 0xAE42... --name kitties

Много коллекций:
    for c in 0xAAA 0xBBB 0xCCC; do python3 tools/analyze_collection.py --contract $c; done
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nft_earnings import DEFAULT_RH_RPC, ZERO, Rpc  # noqa: E402
from nft_sources import classify_movements, write_movements  # noqa: E402
from nft_watchlist import build_watchlist, write_watchlist, print_watchlist  # noqa: E402


def earnings_from_movements(rows: list) -> list:
    """Gross realized по кошельку (как в эталоне): invested = всё купленное+минт,
    sales = всё проданное через Seaport, realized = sales - invested."""
    agg = defaultdict(lambda: {"bought": 0, "sold": 0, "invested": 0.0, "sales": 0.0})
    for r in rows:
        price = float(r["price"])
        to, frm, src = r["to"], r["from"], r["source"]
        if to and to != ZERO:
            a = agg[to]
            a["bought"] += 1
            if src in ("SALE", "MINT_PAID", "MINT_FREE"):
                a["invested"] += price
        if frm and frm != ZERO:
            a = agg[frm]
            a["sold"] += 1
            if src == "SALE":
                a["sales"] += price
    out = []
    for w, a in agg.items():
        realized = a["sales"] - a["invested"]
        out.append({
            "wallet": w, "bought": a["bought"], "sold": a["sold"],
            "holding": a["bought"] - a["sold"],
            "total_invested": round(a["invested"], 4),
            "total_sales": round(a["sales"], 4),
            "avg_buy": round(a["invested"] / a["bought"], 4) if a["bought"] else 0.0,
            "avg_sale": round(a["sales"] / a["sold"], 4) if a["sold"] else 0.0,
            "realized_profit": round(realized, 4),
            "realized_pct": round(realized / a["invested"] * 100, 2) if a["invested"] else 0.0,
        })
    out.sort(key=lambda r: r["realized_profit"], reverse=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=DEFAULT_RH_RPC)
    ap.add_argument("--contract", required=True)
    ap.add_argument("--name", default=None, help="префикс файлов (по умолчанию — адрес)")
    ap.add_argument("--outdir", default="out")
    ap.add_argument("--min-trades", type=int, default=3)
    ap.add_argument("--min-winrate", type=float, default=0.5)
    args = ap.parse_args()

    contract = args.contract.lower()
    name = args.name or contract[:10]
    os.makedirs(args.outdir, exist_ok=True)
    p = lambda suf: os.path.join(args.outdir, f"{name}_{suf}")

    rpc = Rpc(args.rpc)
    print(f"=== {name} ({contract}) ===", file=sys.stderr)

    rows = classify_movements(rpc, contract)          # тяжёлый проход, один раз
    write_movements(rows, p("movements.csv"))

    earn = earnings_from_movements(rows)
    with open(p("earnings.csv"), "w", newline="") as f:
        cols = ["wallet", "bought", "sold", "holding", "total_invested", "total_sales",
                "avg_buy", "avg_sale", "realized_profit", "realized_pct"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(earn)

    watch = build_watchlist(rows, args.min_trades, args.min_winrate)
    write_watchlist(watch, p("watchlist.csv"))

    print(f"\nКошельков всего: {len(earn)}  |  rpc_calls: {rpc.calls}", file=sys.stderr)
    print_watchlist(watch, top=20)
    print(f"\nФайлы: {p('movements.csv')}, {p('earnings.csv')}, {p('watchlist.csv')}", file=sys.stderr)


if __name__ == "__main__":
    main()
