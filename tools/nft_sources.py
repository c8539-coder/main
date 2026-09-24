#!/usr/bin/env python3
"""Классификация КАЖДОГО перемещения NFT коллекции по источнику.

Для каждого трансфера определяет `source`:
    MINT_FREE  — минт (from=0x0), заплачено 0
    MINT_PAID  — минт (from=0x0), заплачено >0 (цена = native_в_tx / кол-во минтов в tx)
    BUY/SELL   — сделка через Seaport (есть OrderFulfilled с этим tokenId), с ценой
    TRANSFER   — обычный перевод между кошельками БЕЗ оплаты (подарок / другой маркет /
                 переброс на свой второй кошелёк)

На выходе:
    movements.csv     — построчно: time, tokenId, from, to, tx, source, price
    plus в stderr — сводка и топ-списки (бесплатные минтеры, получатели переводов).

Запуск:
    export RH_RPC="https://robinhood-mainnet.g.alchemy.com/v2/<KEY>"
    python3 tools/nft_sources.py --out movements.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nft_earnings import (  # noqa: E402
    DEFAULT_CONTRACT, DEFAULT_RH_RPC, ORDER_FULFILLED, ZERO, Rpc,
    decode_order_fulfilled, order_price_for_token, token_id_of,
)


def fetch_all_transfers(rpc: Rpc, contract: str) -> list:
    base = {
        "fromBlock": "0x0", "toBlock": "latest",
        "category": ["erc721", "erc1155"], "contractAddresses": [contract],
        "withMetadata": True, "excludeZeroValue": False, "maxCount": "0x3e8",
    }
    out, page = [], None
    while True:
        p = dict(base)
        if page:
            p["pageKey"] = page
        res = rpc("alchemy_getAssetTransfers", [p])
        out.extend(res.get("transfers", []))
        page = res.get("pageKey")
        if not page:
            break
    return out


def orders_of_tx(rpc: Rpc, tx_hash: str, cache: dict) -> list:
    if tx_hash not in cache:
        rc = rpc("eth_getTransactionReceipt", [tx_hash])
        orders = []
        for lg in rc.get("logs", []):
            tp = lg.get("topics") or []
            if tp and tp[0].lower() == ORDER_FULFILLED:
                try:
                    orders.append(decode_order_fulfilled(lg["data"]))
                except Exception:
                    pass
        cache[tx_hash] = orders
    return cache[tx_hash]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=DEFAULT_RH_RPC)
    ap.add_argument("--contract", default=DEFAULT_CONTRACT)
    ap.add_argument("--out", default="movements.csv")
    args = ap.parse_args()

    rpc = Rpc(args.rpc)
    contract = args.contract.lower()

    transfers = fetch_all_transfers(rpc, contract)
    print(f"NFT-трансферов: {len(transfers)}", file=sys.stderr)

    # цена/кол-во минтов по каждой минт-транзакции
    mint_txs = defaultdict(int)
    for t in transfers:
        if (t.get("from") or "").lower() == ZERO:
            mint_txs[t["hash"]] += 1
    mint_native = {}
    for i, h in enumerate(mint_txs, 1):
        tx = rpc("eth_getTransactionByHash", [h])
        mint_native[h] = int(tx.get("value", "0x0"), 16) / 1e18
        if i % 100 == 0:
            print(f"  минт-tx {i}/{len(mint_txs)}", file=sys.stderr)

    cache: dict = {}
    rows = []
    by_hash = defaultdict(list)
    for t in transfers:
        by_hash[t["hash"]].append(t)

    done = 0
    seen = set()
    for h, group in by_hash.items():
        orders = orders_of_tx(rpc, h, cache)
        for t in group:
            tid = token_id_of(t)
            frm = (t.get("from") or "").lower()
            to = (t.get("to") or "").lower()
            key = (h, tid, frm, to)
            if key in seen:
                continue
            seen.add(key)
            ts = (t.get("metadata") or {}).get("blockTimestamp", "")
            price = order_price_for_token(orders, contract, tid) if tid is not None else None
            if frm == ZERO:
                per = mint_native.get(h, 0.0) / max(mint_txs.get(h, 1), 1)
                source = "MINT_FREE" if per == 0 else "MINT_PAID"
                price = per
            elif price is not None:
                source = "SALE"
            else:
                source = "TRANSFER"
                price = 0.0
            rows.append({
                "time": ts, "tokenId": tid, "from": frm, "to": to,
                "tx": h, "source": source, "price": round(price, 6),
            })
        done += 1
        if done % 200 == 0:
            print(f"  ...{done}/{len(by_hash)} tx, rpc_calls={rpc.calls}", file=sys.stderr)

    rows.sort(key=lambda r: r["time"])
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["time", "tokenId", "from", "to", "tx", "source", "price"])
        w.writeheader()
        w.writerows(rows)

    # ---- сводка ----
    by_src = Counter(r["source"] for r in rows)
    print("\n===== СВОДКА ПО ИСТОЧНИКАМ =====", file=sys.stderr)
    for s, n in by_src.most_common():
        print(f"  {s:10} {n}", file=sys.stderr)

    free_minters = Counter(r["to"] for r in rows if r["source"] == "MINT_FREE")
    paid_minters = Counter(r["to"] for r in rows if r["source"] == "MINT_PAID")
    got_transfer = Counter(r["to"] for r in rows if r["source"] == "TRANSFER")
    print(f"\nБесплатных минтеров: {len(free_minters)} (NFT: {sum(free_minters.values())})", file=sys.stderr)
    for a, n in free_minters.most_common(15):
        print(f"   FREE-MINT {a}  {n} шт", file=sys.stderr)
    print(f"\nПлатных минтеров: {len(paid_minters)} (NFT: {sum(paid_minters.values())})", file=sys.stderr)
    print(f"\nПолучили просто переводом (не сделка): {len(got_transfer)} кошельков "
          f"(NFT: {sum(got_transfer.values())})", file=sys.stderr)
    for a, n in got_transfer.most_common(15):
        print(f"   TRANSFER-IN {a}  {n} шт", file=sys.stderr)

    print(f"\nГотово -> {args.out} ({len(rows)} строк), rpc_calls={rpc.calls}", file=sys.stderr)


if __name__ == "__main__":
    main()
