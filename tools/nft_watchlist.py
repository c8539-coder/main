#!/usr/bin/env python3
"""Watchlist сильнейших кошельков коллекции для последующего трекинга.

Читает movements.csv (вывод nft_sources.py) и считает ЧЕСТНЫЙ matched-PnL
по каждому токену: profit = цена_продажи - цена_покупки, только для токенов
с настоящей себестоимостью (куплен через Seaport или платный минт).
Transfer-fed токены (пришли переводом, себестоимость на другом кошельке) в
профит НЕ идут — учитываются отдельно как флаг мусора.

Метрики на кошелёк:
    clean_trades   — завершённых сделок с реальной себестоимостью (купил->продал)
    clean_profit   — суммарный matched-профит по ним, ETH
    win_rate       — доля прибыльных сделок
    avg_profit     — средний профит на сделку
    invested       — вложено в токены с себестоимостью
    roi            — clean_profit / invested
    holdings       — держит сейчас (купленных, ещё не проданных)
    transfer_flips — продал того, что пришло переводом (мусорный сигнал)
    last_seen      — последняя активность

`watch=1` — прошёл строгий фильтр «сильный органический трейдер».

Запуск:  python3 tools/nft_watchlist.py --in movements.csv --out watchlist.csv
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict

ZERO = "0x0000000000000000000000000000000000000000"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="movements.csv")
    ap.add_argument("--out", default="watchlist.csv")
    ap.add_argument("--min-trades", type=int, default=3, help="мин. сделок для watch=1")
    ap.add_argument("--min-winrate", type=float, default=0.5, help="мин. win-rate для watch=1")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.inp)))
    for r in rows:
        r["price"] = float(r["price"])

    # события по (wallet, tokenId): acquire/dispose в хронологии
    # ERC721 уникален -> у кошелька в моменте 0/1 экземпляр, чередование acq/disp
    acq_ev = defaultdict(list)   # (wallet, tid) -> [(time, source, price)]
    disp_ev = defaultdict(list)  # (wallet, tid) -> [(time, source, price)]
    transfers = [r for r in rows if r["source"] == "TRANSFER"]

    for r in rows:
        tid = r["tokenId"]
        to, frm = r["to"], r["from"]
        if to and to != ZERO:
            acq_ev[(to, tid)].append((r["time"], r["source"], r["price"]))
        if frm and frm != ZERO:
            disp_ev[(frm, tid)].append((r["time"], r["source"], r["price"]))

    # флаги мусора
    pair = set((r["from"], r["to"]) for r in transfers)
    reciprocal = set()
    for a, b in pair:
        if (b, a) in pair:
            reciprocal.add(a); reciprocal.add(b)

    W = defaultdict(lambda: {
        "clean_trades": 0, "wins": 0, "clean_profit": 0.0, "invested": 0.0,
        "transfer_flips": 0, "holdings": 0, "held_cost": 0.0,
        "mint_paid": 0, "bought": 0, "transfer_in": 0, "last_seen": "",
    })

    wallets = set(w for (w, _t) in acq_ev) | set(w for (w, _t) in disp_ev)
    for w in wallets:
        # соберём все токены, которых касался кошелёк
        tids = set(t for (ww, t) in acq_ev if ww == w) | set(t for (ww, t) in disp_ev if ww == w)
        for tid in tids:
            evs = [("A", *e) for e in acq_ev.get((w, tid), [])] + \
                  [("D", *e) for e in disp_ev.get((w, tid), [])]
            evs.sort(key=lambda x: x[1])  # по времени
            open_cost = None  # себестоимость текущего лота, None если нет / transfer
            open_is_clean = False
            for kind, tm, src, price in evs:
                if tm > W[w]["last_seen"]:
                    W[w]["last_seen"] = tm
                if kind == "A":
                    open_cost = price
                    open_is_clean = src in ("SALE", "MINT_PAID")
                    if src == "SALE":
                        W[w]["bought"] += 1
                    elif src == "MINT_PAID":
                        W[w]["mint_paid"] += 1
                    else:
                        W[w]["transfer_in"] += 1
                else:  # D
                    if src == "SALE":
                        if open_is_clean and open_cost is not None:
                            profit = price - open_cost
                            W[w]["clean_trades"] += 1
                            W[w]["clean_profit"] += profit
                            W[w]["invested"] += open_cost
                            if profit > 0:
                                W[w]["wins"] += 1
                        elif open_cost is not None and not open_is_clean:
                            W[w]["transfer_flips"] += 1
                    open_cost = None
                    open_is_clean = False
            # осталось на руках (открытый clean-лот)
            if open_cost is not None and open_is_clean:
                W[w]["holdings"] += 1
                W[w]["held_cost"] += open_cost

    out = []
    for w, d in W.items():
        ct = d["clean_trades"]
        wr = d["wins"] / ct if ct else 0.0
        roi = d["clean_profit"] / d["invested"] if d["invested"] else 0.0
        secondary = (d["bought"] == 0 and d["mint_paid"] == 0 and d["transfer_in"] > 0)
        watch = int(ct >= args.min_trades and wr >= args.min_winrate
                    and d["clean_profit"] > 0 and not secondary and w not in reciprocal)
        out.append({
            "wallet": w,
            "watch": watch,
            "clean_profit": round(d["clean_profit"], 4),
            "clean_trades": ct,
            "win_rate": round(wr, 3),
            "avg_profit": round(d["clean_profit"] / ct, 4) if ct else 0.0,
            "roi": round(roi, 3),
            "invested": round(d["invested"], 4),
            "holdings": d["holdings"],
            "held_cost": round(d["held_cost"], 4),
            "bought": d["bought"],
            "mint_paid": d["mint_paid"],
            "transfer_in": d["transfer_in"],
            "transfer_flips": d["transfer_flips"],
            "is_reciprocal": int(w in reciprocal),
            "likely_secondary": int(secondary),
            "last_seen": d["last_seen"][:10],
        })
    out.sort(key=lambda r: (r["watch"], r["clean_profit"]), reverse=True)

    cols = ["wallet", "watch", "clean_profit", "clean_trades", "win_rate", "avg_profit",
            "roi", "invested", "holdings", "held_cost", "bought", "mint_paid",
            "transfer_in", "transfer_flips", "is_reciprocal", "likely_secondary", "last_seen"]
    with open(args.out, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=cols)
        wr.writeheader()
        wr.writerows(out)

    watched = [r for r in out if r["watch"]]
    print(f"Всего кошельков: {len(out)}")
    print(f"Прошли фильтр watch=1 (сделок>={args.min_trades}, win>={args.min_winrate}, "
          f"профит>0, не secondary, не reciprocal): {len(watched)}")
    print(f"\n{'#':>2} {'wallet':44}{'profit':>9}{'trades':>7}{'win':>6}{'roi':>7}{'hold':>5}  last")
    for i, r in enumerate(watched[:30], 1):
        print(f"{i:>2} {r['wallet']:44}{r['clean_profit']:>9.4f}{r['clean_trades']:>7}"
              f"{r['win_rate']*100:>5.0f}%{r['roi']*100:>6.0f}%{r['holdings']:>5}  {r['last_seen']}")
    print(f"\n-> {args.out}")


if __name__ == "__main__":
    main()
