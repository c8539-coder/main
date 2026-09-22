#!/usr/bin/env python3
"""Расчёт заработка (realized profit) кошельков по NFT-коллекции.

Модель (реверс-инжиниринг эталонного скриншота Robinhood Kitties):
    total_invested  = сумма ETH+ERC20, потраченная кошельком на ПОКУПКИ и МИНТЫ
    total_sales     = сумма ETH+ERC20, полученная кошельком за ПРОДАЖИ
    realized_profit = total_sales - total_invested
    realized_pct    = realized_profit / total_invested * 100
    holding         = сколько NFT осталось на руках
    (holding_value / unrealized / potential — считаются, если задан --floor)

Метод определения цены — МАРКЕТПЛЕЙС-АГНОСТИК:
для каждой транзакции, где кошелёк получил/отдал NFT этой коллекции, берём
НЕТТО-ДЕЛЬТУ движения ETH (native) и ERC20-токенов по этому же кошельку в этой
же транзакции. Так автоматически учитываются WETH-расчёты, а роялти/комиссии
маркетплейса вычитаются сами (продавцу на кошелёк падает уже нетто-сумма).
Опция --gas дополнительно вычитает газ в транзакциях, где кошелёк — отправитель.

Запуск (один кошелёк, режим сверки с эталоном):
    python3 tools/nft_earnings.py --wallet 0xb180...d8a8

Запуск (вся коллекция -> CSV):
    python3 tools/nft_earnings.py --collection --out earnings.csv

Ключи/эндпоинты — через переменные окружения или флаги (см. --help).
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


class Rpc:
    def __init__(self, url: str):
        self.url = url
        self.calls = 0

    def __call__(self, method: str, params: list):
        self.calls += 1
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, data=payload, headers={"Content-Type": "application/json"})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=40) as resp:
                    data = json.loads(resp.read())
                if "error" in data:
                    raise RuntimeError(f"RPC error {method}: {data['error']}")
                return data["result"]
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"HTTP {e.code} {method}: {e.read()[:200]!r}") from e
            except urllib.error.URLError as e:
                if attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"NET {method}: {e}") from e
        raise RuntimeError(f"failed {method}")


def wei(hexstr) -> int:
    if hexstr in (None, "0x", ""):
        return 0
    return int(hexstr, 16)


def get_transfers(rpc: Rpc, *, contract=None, from_addr=None, to_addr=None, categories) -> list:
    """Обёртка над alchemy_getAssetTransfers с пагинацией."""
    base = {
        "fromBlock": "0x0",
        "toBlock": "latest",
        "category": categories,
        "withMetadata": True,
        "excludeZeroValue": False,
        "maxCount": "0x3e8",
    }
    if contract:
        base["contractAddresses"] = [contract]
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


def wallet_value_delta(rpc: Rpc, wallet: str) -> dict:
    """tx_hash -> нетто-дельта стоимости для кошелька (в ETH-эквиваленте по номиналу).

    Суммируем native (external+internal) и erc20 переводы. Возвращаем по каждой
    транзакции {'in': сумма_прихода, 'out': сумма_ухода} по кошельку.
    ВНИМАНИЕ: ERC20 суммируются по номиналу (1 WETH = 1 ETH). Экзотические токены
    в цене сделок будут искажать — но для NFT-маркетов расчёт обычно в ETH/WETH.
    """
    delta: dict[str, dict[str, float]] = defaultdict(lambda: {"in": 0.0, "out": 0.0})
    cats = ["external", "internal", "erc20"]
    for t in get_transfers(rpc, to_addr=wallet, categories=cats):
        v = t.get("value")
        if v:
            delta[t["hash"]]["in"] += float(v)
    for t in get_transfers(rpc, from_addr=wallet, categories=cats):
        v = t.get("value")
        if v:
            delta[t["hash"]]["out"] += float(v)
    return delta


def analyze_wallet(rpc: Rpc, contract: str, wallet: str, *, subtract_gas=False) -> dict:
    wallet = wallet.lower()
    nft_in = get_transfers(rpc, contract=contract, to_addr=wallet, categories=["erc721", "erc1155"])
    nft_out = get_transfers(rpc, contract=contract, from_addr=wallet, categories=["erc721", "erc1155"])

    # направление по транзакциям (одна tx может нести несколько токенов)
    acquire_hashes = {t["hash"] for t in nft_in}
    dispose_hashes = {t["hash"] for t in nft_out}
    # трансфер и туда и сюда в одной tx (редко) — не считаем ни покупкой, ни продажей
    both = acquire_hashes & dispose_hashes
    acquire_hashes -= both
    dispose_hashes -= both

    vdelta = wallet_value_delta(rpc, wallet)

    gas_by_tx: dict[str, float] = {}
    if subtract_gas:
        for h in acquire_hashes | dispose_hashes:
            tx = rpc("eth_getTransactionByHash", [h])
            if (tx.get("from") or "").lower() != wallet:
                continue
            rcpt = rpc("eth_getTransactionReceipt", [h])
            gas_by_tx[h] = wei(rcpt.get("gasUsed")) * wei(tx.get("effectiveGasPrice") or tx.get("gasPrice")) / 1e18

    total_invested = 0.0
    total_sales = 0.0
    buys, sells = [], []
    for h in acquire_hashes:
        cost = vdelta.get(h, {}).get("out", 0.0)  # ушло с кошелька = заплатил
        cost += gas_by_tx.get(h, 0.0)
        total_invested += cost
        buys.append((h, cost))
    for h in dispose_hashes:
        proceeds = vdelta.get(h, {}).get("in", 0.0)  # пришло = получил
        proceeds -= gas_by_tx.get(h, 0.0)
        total_sales += proceeds
        sells.append((h, proceeds))

    # counts по количеству NFT-трансферов (одна tx может нести несколько токенов)
    bought_n = len(nft_in)
    sold_n = len(nft_out)
    holding = bought_n - sold_n

    realized = total_sales - total_invested
    return {
        "wallet": wallet,
        "bought": bought_n,
        "sold": sold_n,
        "holding": holding,
        "total_invested": round(total_invested, 4),
        "total_sales": round(total_sales, 4),
        "avg_buy": round(total_invested / bought_n, 4) if bought_n else 0.0,
        "avg_sale": round(total_sales / sold_n, 4) if sold_n else 0.0,
        "realized_profit": round(realized, 4),
        "realized_pct": round(realized / total_invested * 100, 2) if total_invested else 0.0,
        "rpc_calls": rpc.calls,
    }


def all_wallets(rpc: Rpc, contract: str) -> list[str]:
    """Все адреса, когда-либо державшие NFT коллекции (из всех трансферов)."""
    seen = set()
    for t in get_transfers(rpc, contract=contract, categories=["erc721", "erc1155"]):
        for a in (t.get("from"), t.get("to")):
            if a and a.lower() != ZERO:
                seen.add(a.lower())
    return sorted(seen)


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
        print(f"HOLDING VALUE   {round(hv,4)}  (floor {floor})")
        print(f"POTENTIAL       {round(res['realized_profit'] + hv, 4)}")
    print(f"[rpc calls: {res['rpc_calls']}]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=DEFAULT_RH_RPC)
    ap.add_argument("--contract", default=DEFAULT_CONTRACT)
    ap.add_argument("--wallet", help="один кошелёк (режим сверки)")
    ap.add_argument("--collection", action="store_true", help="вся коллекция")
    ap.add_argument("--out", default="earnings.csv")
    ap.add_argument("--floor", type=float, default=None, help="floor price для holding value")
    ap.add_argument("--gas", action="store_true", help="вычитать газ (для tx, где кошелёк отправитель)")
    args = ap.parse_args()

    rpc = Rpc(args.rpc)
    contract = args.contract.lower()

    if args.wallet:
        res = analyze_wallet(rpc, contract, args.wallet, subtract_gas=args.gas)
        print_wallet(res, args.floor)
        return

    if args.collection:
        wallets = all_wallets(rpc, contract)
        print(f"Кошельков в коллекции: {len(wallets)}", file=sys.stderr)
        rows = []
        for i, w in enumerate(wallets, 1):
            try:
                rows.append(analyze_wallet(rpc, contract, w, subtract_gas=args.gas))
            except Exception as e:
                print(f"  !! {w}: {e}", file=sys.stderr)
            if i % 10 == 0:
                print(f"  ...{i}/{len(wallets)}", file=sys.stderr)
        rows.sort(key=lambda r: r["realized_profit"], reverse=True)
        cols = ["wallet", "bought", "sold", "holding", "total_invested",
                "total_sales", "avg_buy", "avg_sale", "realized_profit", "realized_pct"]
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"Готово -> {args.out} ({len(rows)} строк)", file=sys.stderr)
        return

    ap.error("укажи --wallet ADDRESS или --collection")


if __name__ == "__main__":
    main()
