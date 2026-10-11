#!/usr/bin/env python3
"""Суммарный торговый PnL одного кошелька по ВСЕМ NFT-коллекциям (any chain).

Метод — нетто-поток ETH+WETH (маркетплейс-агностик, ловит OpenSea/Blur/любой):
для каждой транзакции, где кошелёк получал/отдавал NFT, берём сколько ETH+WETH
кошелёк заплатил (покупки/минты) и получил (продажи).

    invested = Σ (ETH+WETH, ушедшие с кошелька в tx-покупках/минтах)
    proceeds = Σ (ETH+WETH, пришедшие на кошелёк в tx-продажах)
    realized = proceeds - invested   (комиссии/роялти уже вычтены — приходит нетто)

Запуск:
    python3 tools/wallet_pnl.py --rpc https://eth-mainnet.g.alchemy.com/v2/<KEY> \
        --wallet 0xbbf6...7898
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nft_earnings import ZERO, Rpc  # noqa: E402

WETH_MAINNET = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"


def get_transfers(rpc, *, from_addr=None, to_addr=None, categories, internal_ok=True):
    base = {"fromBlock": "0x0", "toBlock": "latest", "category": list(categories),
            "withMetadata": False, "excludeZeroValue": False, "maxCount": "0x3e8"}
    if from_addr:
        base["fromAddress"] = from_addr
    if to_addr:
        base["toAddress"] = to_addr
    out, pk = [], None
    while True:
        p = dict(base)
        if pk:
            p["pageKey"] = pk
        try:
            r = rpc("alchemy_getAssetTransfers", [p])
        except RuntimeError as e:
            if "internal" in str(e) and "internal" in base["category"]:
                base["category"] = [c for c in base["category"] if c != "internal"]
                continue
            raise
        out.extend(r.get("transfers", []))
        pk = r.get("pageKey")
        if not pk:
            break
    return out


def value_by_tx(rpc, wallet, weth):
    """tx -> {'in': eth+weth пришло, 'out': eth+weth ушло} по кошельку."""
    d = defaultdict(lambda: {"in": 0.0, "out": 0.0})
    cats = ["external", "internal", "erc20"]
    for t in get_transfers(rpc, to_addr=wallet, categories=cats):
        v = t.get("value")
        if not v:
            continue
        cat = t.get("category")
        addr = ((t.get("rawContract") or {}).get("address") or "").lower()
        if cat in ("external", "internal") or (cat == "erc20" and addr == weth):
            d[t["hash"]]["in"] += v
    for t in get_transfers(rpc, from_addr=wallet, categories=cats):
        v = t.get("value")
        if not v:
            continue
        cat = t.get("category")
        addr = ((t.get("rawContract") or {}).get("address") or "").lower()
        if cat in ("external", "internal") or (cat == "erc20" and addr == weth):
            d[t["hash"]]["out"] += v
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--wallet", required=True)
    ap.add_argument("--weth", default=WETH_MAINNET)
    args = ap.parse_args()
    rpc = Rpc(args.rpc)
    w = args.wallet.lower()
    weth = args.weth.lower()

    nft_in = get_transfers(rpc, to_addr=w, categories=["erc721", "erc1155"])
    nft_out = get_transfers(rpc, from_addr=w, categories=["erc721", "erc1155"])
    buy_h = {t["hash"] for t in nft_in}
    sell_h = {t["hash"] for t in nft_out}
    both = buy_h & sell_h
    buy_h -= both
    sell_h -= both
    print(f"NFT получено {len(nft_in)} / отдано {len(nft_out)} | tx-покупок {len(buy_h)}, tx-продаж {len(sell_h)}", file=sys.stderr)

    vbt = value_by_tx(rpc, w, weth)
    invested = sum(vbt.get(h, {}).get("out", 0.0) for h in buy_h)
    proceeds = sum(vbt.get(h, {}).get("in", 0.0) for h in sell_h)
    realized = proceeds - invested

    mints = sum(1 for t in nft_in if (t.get("from") or "").lower() == ZERO)
    cols = len({((t.get("rawContract") or {}).get("address") or "").lower() for t in (nft_in + nft_out)})
    bal_eth = int(rpc("eth_getBalance", [w, "latest"]), 16) / 1e18
    held = len(nft_in) - len(nft_out)

    print("=" * 52)
    print(f"WALLET {w}")
    print("=" * 52)
    print(f"коллекций:            {cols}")
    print(f"NFT куплено/минт:     {len(nft_in)} (минтов {mints})")
    print(f"NFT продано/отдано:   {len(nft_out)}")
    print(f"на руках (нетто):     {held}")
    print(f"ETH-баланс сейчас:    {bal_eth:.4f}")
    print("-" * 52)
    print(f"ВЛОЖЕНО (ETH+WETH):   {invested:.4f}")
    print(f"ПОЛУЧЕНО (ETH+WETH):  {proceeds:.4f}")
    print(f"REALIZED PnL:         {realized:+.4f} ETH"
          + (f"  ({realized/invested*100:+.1f}%)" if invested else ""))
    print(f"[rpc calls: {rpc.calls}]")


if __name__ == "__main__":
    main()
