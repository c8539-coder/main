#!/usr/bin/env python3
"""Авто-дискавери флип-коллекций на Robinhood-чейне + скоринг под стратегию
«оффер → быстрый релист чуть дороже».

Шаги:
 1) сканит ВЕСЬ чейн за окно (последние N дней) -> топ коллекций по вторичной активности;
 2) по каждому кандидату (оконно, дёшево) считает флип-метрики:
    продаж/сутки, медиана цены, % прибыльных флипов, медиана маржи и времени удержания,
    чистая маржа после комиссий;
 3) ранжирует по «flip_score» (ликвидность × прибыльность × целевой размер маржи).

Запуск:
    python3 tools/nft_discover.py --days 1 --candidates 15 --out out/flip_rank.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nft_earnings import DEFAULT_RH_RPC, ZERO, CapacityError, Rpc  # noqa: E402
from nft_sources import classify_movements  # noqa: E402

NFT_BASE = None  # заполнится из RPC url
BLOCKS_PER_DAY = 840_000  # block time ~0.1s
FEE_RATE = 0.02  # оценка комиссий маркета+роялти с двух сторон (консервативно ~2%)

# эвристика: исключаем DeFi/utility NFT (позиции ликвидности и т.п.)
SKIP_WORDS = ["position", "uniswap", "uni-v", "up-pos", "lp", "v3-pos", "v4-pos"]


def nft_meta_url(rpc_url: str) -> str:
    # https://<net>.g.alchemy.com/v2/<key>  ->  .../nft/v3/<key>
    base, key = rpc_url.rsplit("/v2/", 1)
    return f"{base}/nft/v3/{key}"


def getj(url: str) -> dict:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        import json
        return json.loads(urllib.request.urlopen(req, timeout=20).read())
    except Exception:
        return {}


def discover(rpc: Rpc, nft_base: str, days: float, max_pages: int, progress=True):
    bn = int(rpc("eth_blockNumber", []), 16)
    start = bn - int(days * BLOCKS_PER_DAY)
    total = Counter()
    sec = Counter()
    params = {"fromBlock": hex(start), "toBlock": "latest",
              "category": ["erc721", "erc1155"], "withMetadata": False,
              "excludeZeroValue": False, "maxCount": "0x3e8", "order": "desc"}
    pk = None
    pages = 0
    while pages < max_pages:
        p = dict(params)
        if pk:
            p["pageKey"] = pk
        r = rpc("alchemy_getAssetTransfers", [p])
        for t in r["transfers"]:
            a = (t.get("rawContract") or {}).get("address", "") or ""
            a = a.lower()
            if not a:
                continue
            total[a] += 1
            if (t.get("from") or "").lower() != ZERO:
                sec[a] += 1
        pk = r.get("pageKey")
        pages += 1
        if not pk:
            break
        if progress and pages % 20 == 0:
            print(f"  discover: {pages} страниц, {sum(total.values())} трансферов", file=sys.stderr)
    return sec, total, start, bn


def parse_ts(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def flip_stats(rows, days):
    """Флип-метрики из movement-строк коллекции (оконных)."""
    acq = defaultdict(list)
    disp = defaultdict(list)
    sale_prices = []
    for r in rows:
        tid = r["tokenId"]
        price = float(r["price"])
        t = parse_ts(r["time"])
        if r["source"] == "SALE":
            sale_prices.append(price)
        if r["to"] and r["to"] != ZERO and r["source"] in ("SALE", "MINT_PAID"):
            acq[(r["to"], tid)].append((t, price))
        if r["from"] and r["from"] != ZERO and r["source"] == "SALE":
            disp[(r["from"], tid)].append((t, price))
    flips = []
    for key, buys in acq.items():
        sells = disp.get(key)
        if not sells:
            continue
        for bt, bp in buys:
            later = [(sx, sp) for sx, sp in sells if sx and bt and sx > bt]
            if not later:
                continue
            sx, sp = min(later, key=lambda x: x[0])
            hold = (sx - bt).total_seconds() / 3600
            flips.append((hold, bp, sp, sp - bp))
    sales = len(sale_prices)
    out = {
        "sales": sales,
        "sales_per_day": round(sales / days, 1) if days else 0,
        "median_price": round(st.median(sale_prices), 4) if sale_prices else 0.0,
        "flips": len(flips),
        "win_rate": 0.0, "median_margin": 0.0, "median_hold_h": 0.0,
        "net_margin_est": 0.0,
    }
    if flips:
        margins = [m for _, _, _, m in flips]
        holds = [h for h, _, _, _ in flips]
        wins = [m for m in margins if m > 0]
        out["win_rate"] = round(len(wins) / len(flips), 3)
        out["median_margin"] = round(st.median(margins), 4)
        out["median_hold_h"] = round(st.median(holds), 1)
        mp = out["median_price"]
        out["net_margin_est"] = round(st.median(margins) - mp * FEE_RATE, 4)
    return out


def score(m):
    """flip_score: ликвидность × прибыльность × целевой размер нетто-маржи.
    Поощряем продажи/сутки, win-rate, нетто-маржу в диапазоне ~0.002..0.02, быстрый оборот."""
    if m["flips"] < 10 or m["net_margin_est"] <= 0:
        return 0.0
    import math
    liq = math.log10(1 + m["sales_per_day"])
    win = max(0.0, m["win_rate"] - 0.4)
    nm = m["net_margin_est"]
    band = 1.0 if 0.002 <= nm <= 0.02 else (0.5 if nm > 0 else 0.0)
    speed = 1.0 if m["median_hold_h"] and m["median_hold_h"] < 24 else 0.5
    return round(liq * win * (nm * 1000) * band * speed, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=DEFAULT_RH_RPC)
    ap.add_argument("--days", type=float, default=1.0)
    ap.add_argument("--candidates", type=int, default=15)
    ap.add_argument("--max-pages", type=int, default=120)
    ap.add_argument("--out", default="out/flip_rank.csv")
    args = ap.parse_args()

    rpc = Rpc(args.rpc)
    nft_base = nft_meta_url(args.rpc)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)

    print(f"=== DISCOVER (окно {args.days} дн) ===", file=sys.stderr)
    sec, total, start, bn = discover(rpc, nft_base, args.days, args.max_pages)
    print(f"коллекций: {len(sec)}; беру топ-{args.candidates} по вторичке", file=sys.stderr)

    results = []
    picked = 0
    for addr, _n in sec.most_common(60):
        if picked >= args.candidates:
            break
        meta = getj(f"{nft_base}/getContractMetadata?contractAddress={addr}")
        name = meta.get("name") or meta.get("openSeaMetadata", {}).get("collectionName") or addr[:10]
        sym = meta.get("symbol") or ""
        blob = f"{name} {sym}".lower()
        if any(w in blob for w in SKIP_WORDS):
            continue  # DeFi/utility NFT — не флип-кандидат
        picked += 1
        print(f"[{picked}/{args.candidates}] {name} ({addr}) — считаю флип-метрики…", file=sys.stderr)
        try:
            rows = classify_movements(rpc, addr, progress=False, from_block=start)
        except CapacityError:
            print("!! лимит Alchemy — стоп.", file=sys.stderr)
            break
        m = flip_stats(rows, args.days)
        m.update({"name": name, "symbol": sym, "contract": addr, "flip_score": score(m)})
        results.append(m)

    results.sort(key=lambda r: r["flip_score"], reverse=True)
    cols = ["flip_score", "name", "symbol", "contract", "sales_per_day", "median_price",
            "flips", "win_rate", "median_margin", "net_margin_est", "median_hold_h", "sales"]
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)

    print(f"\n{'score':>6}  {'коллекция':24}{'прод/сут':>9}{'цена':>8}{'win':>6}{'нетто-маржа':>12}{'удерж,ч':>8}")
    for r in results:
        print(f"{r['flip_score']:>6}  {r['name'][:24]:24}{r['sales_per_day']:>9}{r['median_price']:>8}"
              f"{r['win_rate']*100:>5.0f}%{r['net_margin_est']:>12}{r['median_hold_h']:>8}")
    print(f"\n-> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
