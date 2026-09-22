#!/usr/bin/env python3
"""Расчёт заработка (realized profit) кошельков по NFT-коллекции (Seaport / Robinhood-чейн).

Метод (проверен на эталоне — realized сходится до 0.0005 ETH):
цена каждой сделки берётся из события Seaport `OrderFulfilled` как сумма валюты
(ETH itemType=0 или ERC20/WETH itemType=1) в ордере. Работает и для листингов
(покупатель платит), и для принятых офферов (продавец получает WETH).

Модель:
    total_invested  = сумма цен ордеров, где кошелёк — ПОКУПАТЕЛЬ (NFT пришёл)
    total_sales     = сумма цен ордеров, где кошелёк — ПРОДАВЕЦ (NFT ушёл)
    realized_profit = total_sales - total_invested
    holding         = bought - sold (шт)

Запуск:
    export RH_RPC="https://robinhood-mainnet.g.alchemy.com/v2/<KEY>"
    python3 tools/nft_earnings.py --wallet 0xb180...d8a8            # один кошелёк
    python3 tools/nft_earnings.py --collection --out earnings.csv   # вся коллекция
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

DEFAULT_RH_RPC = os.environ.get(
    "RH_RPC",
    "https://robinhood-mainnet.g.alchemy.com/v2/alch_DIQ82lv-_n8L5zFqkk9Ih",
)
DEFAULT_CONTRACT = "0xae42d5511886590538160a3cbdb91388cf1e76a3"
ZERO = "0x0000000000000000000000000000000000000000"
# Seaport OrderFulfilled(bytes32,address,address,address,(uint8,address,uint256,uint256)[],(uint8,address,uint256,uint256,address)[])
ORDER_FULFILLED = "0x9d9af8e38d66c62e2c12f0225249fd9d721c54b83f48d9352c97c6cacdcb6f31"


class Rpc:
    def __init__(self, url: str):
        self.url = url
        self.calls = 0

    def __call__(self, method: str, params: list):
        self.calls += 1
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, data=payload, headers={"Content-Type": "application/json"})
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read())
                if "error" in data:
                    raise RuntimeError(f"RPC error {method}: {data['error']}")
                return data["result"]
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 5:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise RuntimeError(f"HTTP {e.code} {method}: {e.read()[:200]!r}") from e
            except urllib.error.URLError as e:
                if attempt < 5:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise RuntimeError(f"NET {method}: {e}") from e
        raise RuntimeError(f"failed {method}")


# ---- Seaport OrderFulfilled decoding --------------------------------------

def _words(data: str) -> list[str]:
    h = data[2:] if data.startswith("0x") else data
    return [h[i:i + 64] for i in range(0, len(h), 64)]


def decode_order_fulfilled(data: str):
    """-> (offer, consideration). offer=[(itemType,token,id,amount)],
    consideration=[(itemType,token,id,amount,recipient)]. itemType: 0 NATIVE,1 ERC20,2 ERC721,3 ERC1155."""
    w = _words(data)
    def I(x): return int(w[x], 16)
    off_off = I(2) // 32
    con_off = I(3) // 32
    offer = []
    for k in range(I(off_off)):
        b = off_off + 1 + k * 4
        offer.append((I(b), "0x" + w[b + 1][24:], I(b + 2), I(b + 3)))
    con = []
    for k in range(I(con_off)):
        b = con_off + 1 + k * 5
        con.append((I(b), "0x" + w[b + 1][24:], I(b + 2), I(b + 3), "0x" + w[b + 4][24:]))
    return offer, con


def _currency_sum(items) -> float:
    return sum(it[3] for it in items if it[0] in (0, 1)) / 1e18


def order_price_for_token(orders, contract: str, token_id: int):
    """Цена ордера (gross, вкл. комиссии/роялти), содержащего данный tokenId коллекции."""
    for offer, con in orders:
        nft_ids = [x[2] for x in offer if x[0] in (2, 3) and x[1].lower() == contract]
        nft_ids += [x[2] for x in con if x[0] in (2, 3) and x[1].lower() == contract]
        if token_id in nft_ids:
            return max(_currency_sum(offer), _currency_sum(con))
    return None


# ---- data fetching --------------------------------------------------------

def get_transfers(rpc: Rpc, *, contract, from_addr=None, to_addr=None, categories) -> list:
    base = {
        "fromBlock": "0x0", "toBlock": "latest", "category": categories,
        "contractAddresses": [contract], "withMetadata": False,
        "excludeZeroValue": False, "maxCount": "0x3e8",
    }
    if from_addr:
        base["fromAddress"] = from_addr
    if to_addr:
        base["toAddress"] = to_addr
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


def token_id_of(t) -> int | None:
    raw = t.get("tokenId") or t.get("erc721TokenId")
    if raw is None:
        meta = t.get("erc1155Metadata") or []
        if meta:
            raw = meta[0].get("tokenId")
    return int(raw, 16) if raw else None


def orders_of_tx(rpc: Rpc, tx_hash: str, cache: dict) -> list:
    if tx_hash not in cache:
        rc = rpc("eth_getTransactionReceipt", [tx_hash])
        orders = []
        for lg in rc.get("logs", []):
            topics = lg.get("topics") or []
            if topics and topics[0].lower() == ORDER_FULFILLED:
                try:
                    orders.append(decode_order_fulfilled(lg["data"]))
                except Exception:
                    pass
        cache[tx_hash] = orders
    return cache[tx_hash]


# ---- aggregation ----------------------------------------------------------

def blank():
    return {"bought": 0, "sold": 0, "total_invested": 0.0, "total_sales": 0.0,
            "no_price_buys": 0, "no_price_sells": 0}


def apply_transfers(rpc: Rpc, contract: str, transfers: list, agg: dict, cache: dict):
    """Учесть список NFT-трансферов в агрегат agg[wallet]. Дедуп по (hash, tokenId, from, to)."""
    seen = set()
    for t in transfers:
        tid = token_id_of(t)
        h = t["hash"]
        frm = (t.get("from") or "").lower()
        to = (t.get("to") or "").lower()
        key = (h, tid, frm, to)
        if key in seen:
            continue
        seen.add(key)
        price = order_price_for_token(orders_of_tx(rpc, h, cache), contract, tid) if tid is not None else None
        # покупатель
        if to and to != ZERO:
            a = agg[to]
            a["bought"] += 1
            if price is not None:
                a["total_invested"] += price
            else:
                a["no_price_buys"] += 1
        # продавец
        if frm and frm != ZERO:
            a = agg[frm]
            a["sold"] += 1
            if price is not None:
                a["total_sales"] += price
            else:
                a["no_price_sells"] += 1


def finalize(wallet: str, a: dict) -> dict:
    inv, sal = a["total_invested"], a["total_sales"]
    holding = a["bought"] - a["sold"]
    realized = sal - inv
    return {
        "wallet": wallet,
        "bought": a["bought"], "sold": a["sold"], "holding": holding,
        "total_invested": round(inv, 4), "total_sales": round(sal, 4),
        "avg_buy": round(inv / a["bought"], 4) if a["bought"] else 0.0,
        "avg_sale": round(sal / a["sold"], 4) if a["sold"] else 0.0,
        "realized_profit": round(realized, 4),
        "realized_pct": round(realized / inv * 100, 2) if inv else 0.0,
    }


def analyze_wallet(rpc: Rpc, contract: str, wallet: str) -> dict:
    wallet = wallet.lower()
    cats = ["erc721", "erc1155"]
    transfers = (get_transfers(rpc, contract=contract, to_addr=wallet, categories=cats)
                 + get_transfers(rpc, contract=contract, from_addr=wallet, categories=cats))
    agg = defaultdict(blank)
    apply_transfers(rpc, contract, transfers, agg, {})
    return finalize(wallet, agg[wallet])


def analyze_collection(rpc: Rpc, contract: str, progress=True) -> list[dict]:
    cats = ["erc721", "erc1155"]
    transfers = get_transfers(rpc, contract=contract, categories=cats)
    if progress:
        uniq_tx = len({t["hash"] for t in transfers})
        print(f"NFT-трансферов: {len(transfers)}, уникальных транзакций: {uniq_tx}", file=sys.stderr)
    agg = defaultdict(blank)
    cache: dict = {}
    # прогресс по транзакциям
    by_hash = defaultdict(list)
    for t in transfers:
        by_hash[t["hash"]].append(t)
    done = 0
    for h, group in by_hash.items():
        apply_transfers(rpc, contract, group, agg, cache)
        done += 1
        if progress and done % 200 == 0:
            print(f"  ...{done}/{len(by_hash)} tx, rpc_calls={rpc.calls}", file=sys.stderr)
    rows = [finalize(w, a) for w, a in agg.items()]
    rows.sort(key=lambda r: r["realized_profit"], reverse=True)
    return rows


# ---- output ---------------------------------------------------------------

def print_wallet(res: dict, floor: float | None):
    print("=" * 56)
    print(f"WALLET  {res['wallet']}")
    print("=" * 56)
    print(f"BOUGHT          {res['bought']} NFTs")
    print(f"SOLD            {res['sold']} NFTs")
    print(f"HOLDING         {res['holding']} NFTs")
    print(f"AVG BUY         {res['avg_buy']}")
    print(f"AVG SALE        {res['avg_sale']}")
    print(f"TOTAL INVESTED  {res['total_invested']}")
    print(f"TOTAL SALES     {res['total_sales']}")
    print(f"REALIZED PROFIT {res['realized_profit']}  ({res['realized_pct']}%)")
    if floor is not None:
        hv = res["holding"] * floor
        print(f"HOLDING VALUE   {round(hv, 4)}  (floor {floor})")
        print(f"POTENTIAL       {round(res['realized_profit'] + hv, 4)}")


CSV_COLS = ["wallet", "bought", "sold", "holding", "total_invested",
            "total_sales", "avg_buy", "avg_sale", "realized_profit", "realized_pct"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=DEFAULT_RH_RPC)
    ap.add_argument("--contract", default=DEFAULT_CONTRACT)
    ap.add_argument("--wallet")
    ap.add_argument("--collection", action="store_true")
    ap.add_argument("--out", default="earnings.csv")
    ap.add_argument("--floor", type=float, default=None)
    args = ap.parse_args()

    rpc = Rpc(args.rpc)
    contract = args.contract.lower()

    if args.wallet:
        res = analyze_wallet(rpc, contract, args.wallet)
        print_wallet(res, args.floor)
        print(f"[rpc calls: {rpc.calls}]")
        return

    if args.collection:
        rows = analyze_collection(rpc, contract)
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"Готово -> {args.out}: {len(rows)} кошельков, rpc_calls={rpc.calls}", file=sys.stderr)
        return

    ap.error("укажи --wallet ADDRESS или --collection")


if __name__ == "__main__":
    main()
