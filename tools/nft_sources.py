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
    decode_order_fulfilled, order_price_for_token, orders_from_receipt, token_id_of,
)


import time  # noqa: E402


def batched(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _batch_get(rpc: Rpc, hashes: list, method: str, size, sleep, progress, label):
    """Общий батч-фетчер с ретраем null-результатов (rate-limit по CU отдаёт null).
    Возвращает {hash: raw_result}. Выравнивание проверяется по transactionHash/hash."""
    out = {}
    pending = list(hashes)
    total = len(pending)
    rounds = 0
    while pending and rounds < 10:
        rounds += 1
        nulls = []
        for n, chunk in enumerate(batched(pending, size), 1):
            res = rpc.batch([(method, [h]) for h in chunk])
            for j, h in enumerate(chunk):
                rc = res.get(j)
                got = (rc or {}).get("transactionHash") or (rc or {}).get("hash")
                if rc is not None and (got or "").lower() == h.lower():
                    out[h] = rc
                else:
                    nulls.append(h)
            if sleep:
                time.sleep(sleep)
            if progress and n % 50 == 0:
                print(f"  {label}: раунд {rounds}, {len(out)}/{total} (осталось {len(pending) - n * size if rounds == 1 else len(pending)})", file=sys.stderr)
        pending = nulls
        if pending:
            if progress:
                print(f"  {label}: раунд {rounds} — null'ов осталось {len(pending)}, ретрай", file=sys.stderr)
            time.sleep(min(0.5 * rounds, 4))
    for h in pending:
        out[h] = None
    return out


def fetch_receipts_orders(rpc: Rpc, hashes: list, size=25, sleep=0.0, progress=True) -> dict:
    """{hash: [orders]} с ретраем null'ов. size=25/sleep=0 держит ~28 rec/s без null (CU-лимит)."""
    raw = _batch_get(rpc, hashes, "eth_getTransactionReceipt", size, sleep, progress, "receipts")
    return {h: orders_from_receipt(rc) for h, rc in raw.items()}


def fetch_tx_values(rpc: Rpc, hashes: list, size=25, sleep=0.0, progress=True) -> dict:
    """{hash: native_value_eth} с ретраем null'ов."""
    raw = _batch_get(rpc, hashes, "eth_getTransactionByHash", size, sleep, progress, "mint-tx")
    return {h: (int((rc or {}).get("value", "0x0"), 16) / 1e18) for h, rc in raw.items()}


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


MOVEMENT_COLS = ["time", "tokenId", "from", "to", "tx", "source", "price"]


def classify_movements(rpc: Rpc, contract: str, progress=True) -> list:
    """Тяжёлый проход (один раз на коллекцию): все трансферы + Seaport-разбор +
    цены минтов -> список movement-строк с колонкой source."""
    contract = contract.lower()
    transfers = fetch_all_transfers(rpc, contract)
    if progress:
        print(f"NFT-трансферов: {len(transfers)}", file=sys.stderr)

    # минты: кол-во на tx + (батчем) нативная сумма tx
    mint_txs = defaultdict(int)
    for t in transfers:
        if (t.get("from") or "").lower() == ZERO:
            mint_txs[t["hash"]] += 1
    mint_native = fetch_tx_values(rpc, list(mint_txs)) if mint_txs else {}

    # receipt'ы нужны только для tx с НЕ-минт трансфером (у чистых минтов Seaport-ордеров нет)
    mint_only = set(mint_txs)
    for t in transfers:
        if (t.get("from") or "").lower() != ZERO:
            mint_only.discard(t["hash"])
    uniq = list({t["hash"] for t in transfers} - mint_only)
    if progress:
        print(f"tx с возможной сделкой: {len(uniq)} (минт-only пропущено: {len(mint_only)})", file=sys.stderr)
    orders_by = fetch_receipts_orders(rpc, uniq, progress=progress)

    rows = []
    seen = set()
    for t in transfers:
        h = t["hash"]
        tid = token_id_of(t)
        frm = (t.get("from") or "").lower()
        to = (t.get("to") or "").lower()
        key = (h, tid, frm, to)
        if key in seen:
            continue
        seen.add(key)
        ts = (t.get("metadata") or {}).get("blockTimestamp", "")
        price = order_price_for_token(orders_by.get(h, []), contract, tid) if tid is not None else None
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

    rows.sort(key=lambda r: r["time"])
    return rows


def write_movements(rows, path):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MOVEMENT_COLS)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=DEFAULT_RH_RPC)
    ap.add_argument("--contract", default=DEFAULT_CONTRACT)
    ap.add_argument("--out", default="movements.csv")
    args = ap.parse_args()

    rpc = Rpc(args.rpc)
    contract = args.contract.lower()

    rows = classify_movements(rpc, contract)
    write_movements(rows, args.out)

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
