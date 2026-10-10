#!/usr/bin/env python3
"""Анализ удержания (retention) держателей коллекции-пропуска (Ethereum mainnet).

Отвечает на вопрос: кто купил/заминтил — ДЕРЖИТ или СЛИЛ? И отличает реальную
продажу на рынке (выход за деньги = «разочаровался/зафиксировал») от простого
перевода (подарок / переброс на свой второй кошелёк — НЕ выход).

Классификация вторичного перемещения:
    SALE      — в tx есть Seaport OrderFulfilled, Blur-маркет, или WETH пришёл продавцу
    TRANSFER  — ничего из этого (подарок / само-переброс)

Метрики: retention сейчас, churn (только продажи), время удержания до продажи,
быстрые сливы, тиры по кол-ву на руках, само-переводы, поток входов/выходов по неделям.

Запуск:
    python3 tools/nft_retention.py --rpc https://eth-mainnet.g.alchemy.com/v2/<KEY> \
        --contract 0x73c436aaba15d5495cccf31eb1ea965b9dbd3a81 --out out/velcorians_retention.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nft_earnings import ORDER_FULFILLED, ZERO, Rpc, decode_order_fulfilled  # noqa: E402
from nft_sources import fetch_receipts_orders  # noqa: E402

WETH_MAINNET = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
BLUR = {"0x000000000000ad05ccc4f10045630fb830b95127",  # Blur marketplace
        "0xb2ecfe4e4d61f8790bbb9de2d1259b9e2410cea5",  # Blur blend
        "0x39da41747a83aee658334415666f3ef92dd0d541"}  # Blur v2
BLOCKS_PER_DAY_ETH = 7200


def parse_ts(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def fetch_transfers(rpc, contract):
    base = {"fromBlock": "0x0", "toBlock": "latest", "contractAddresses": [contract],
            "category": ["erc721"], "withMetadata": True, "excludeZeroValue": False, "maxCount": "0x3e8"}
    out, pk = [], None
    while True:
        p = dict(base)
        if pk:
            p["pageKey"] = pk
        r = rpc("alchemy_getAssetTransfers", [p])
        out.extend(r["transfers"])
        pk = r.get("pageKey")
        if not pk:
            break
    out.sort(key=lambda t: (int(t["blockNum"], 16), t.get("uniqueId", "")))
    return out


def word_addr(topic):
    return "0x" + topic[-40:]


def classify_tx(rc, contract, seller):
    """Вернуть ('SALE'|'TRANSFER', price_eth) для перемещения в этой tx."""
    if not rc:
        return "TRANSFER", 0.0
    logs = rc.get("logs", [])
    addrs = {(l.get("address") or "").lower() for l in logs}
    # 1) Seaport OrderFulfilled -> цена из ордера (native + mainnet WETH)
    for l in logs:
        tp = l.get("topics") or []
        if tp and tp[0].lower() == ORDER_FULFILLED:
            try:
                offer, con = decode_order_fulfilled(l["data"])
            except Exception:
                continue
            def cur(items):
                s = 0
                for it in items:
                    if it[0] == 0 or (it[0] == 1 and it[1].lower() == WETH_MAINNET):
                        s += it[3]
                return s / 1e18
            price = max(cur(offer), cur(con))
            if price > 0:
                return "SALE", round(price, 5)
    # 2) Blur
    if addrs & BLUR:
        return "SALE", 0.0
    # 3) WETH transfer к продавцу
    for l in logs:
        if (l.get("address") or "").lower() == WETH_MAINNET:
            tp = l.get("topics") or []
            if len(tp) == 3 and tp[0].lower() == TRANSFER_TOPIC and word_addr(tp[2]).lower() == seller.lower():
                try:
                    return "SALE", round(int(l["data"], 16) / 1e18, 5)
                except Exception:
                    return "SALE", 0.0
    return "TRANSFER", 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--contract", required=True)
    ap.add_argument("--out", default="out/retention.csv")
    args = ap.parse_args()
    rpc = Rpc(args.rpc)
    contract = args.contract.lower()
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)

    tr = fetch_transfers(rpc, contract)
    print(f"трансферов: {len(tr)}", file=sys.stderr)
    now = datetime.now().astimezone()

    # receipts для вторичных tx
    sec_hashes = list({t["hash"] for t in tr if (t.get("from") or "").lower() != ZERO
                       and (t.get("to") or "").lower() != ZERO})
    orders_dummy = fetch_receipts_orders.__wrapped__ if hasattr(fetch_receipts_orders, "__wrapped__") else None
    # нам нужны сырые receipt'ы, не только ордера -> тянем сами батчем
    from nft_sources import _batch_get
    raw = _batch_get(rpc, sec_hashes, "eth_getTransactionReceipt", 25, 0.0, True, "receipts")

    # события по токену и по кошельку
    tok_owner = {}  # tokenId -> текущий владелец
    events = []     # (ts, type, token, frm, to, price)
    acq_time = {}   # (wallet,token) -> time первой покупки/минта (для hold-time)
    mint_time = {}
    for t in tr:
        tid = t.get("tokenId") or t.get("erc721TokenId")
        frm = (t.get("from") or "").lower()
        to = (t.get("to") or "").lower()
        ts = parse_ts((t.get("metadata") or {}).get("blockTimestamp", ""))
        if frm == ZERO:
            typ, price = "MINT", 0.0
        else:
            rc = raw.get(t["hash"])
            typ, price = classify_tx(rc, contract, frm)
        events.append((ts, typ, tid, frm, to, price))
        tok_owner[tid] = to
        if to != ZERO:
            acq_time[(to, tid)] = ts
        if frm == ZERO:
            mint_time[(to, tid)] = ts

    # текущие балансы
    bal = defaultdict(int)
    for tid, o in tok_owner.items():
        if o and o != ZERO:
            bal[o] += 1
    holders_now = {w for w, b in bal.items() if b > 0}

    # кто когда-либо приобрёл (mint или buy)
    acquirers = set()
    minters = set()
    for ts, typ, tid, frm, to, price in events:
        if to and to != ZERO:
            acquirers.add(to)
        if typ == "MINT":
            minters.add(to)

    # продажи / переводы и время удержания
    sales = [(ts, tid, frm, to, price) for ts, typ, tid, frm, to, price in events if typ == "SALE"]
    transfers_plain = [(ts, tid, frm, to) for ts, typ, tid, frm, to, price in events if typ == "TRANSFER" and frm != ZERO]
    # hold time: для каждой продажи — время от приобретения продавцом этого токена
    hold_hours = []
    # восстановим: последовательно пройдём события, запоминая время получения токена каждым кошельком
    last_got = {}
    quick7 = quick30 = 0
    exit_pl = []  # (entry_price?, exit_price) — упрощённо по Seaport mint цена неизвестна
    for ts, typ, tid, frm, to, price in events:
        if typ in ("MINT", "SALE", "TRANSFER") and to and to != ZERO:
            pass
        if typ == "SALE":
            got = last_got.get((frm, tid))
            if got and ts:
                h = (ts - got).total_seconds() / 3600
                hold_hours.append(h)
                if h < 24 * 7:
                    quick7 += 1
                if h < 24 * 30:
                    quick30 += 1
        # обновляем «когда получил токен»
        if to and to != ZERO:
            last_got[(to, tid)] = ts

    # реципрокные пары (вероятные само-переводы)
    pair = set((frm, to) for _, _, frm, to in transfers_plain)
    recip = set()
    for a, b in pair:
        if (b, a) in pair:
            recip.add(frozenset((a, b)))

    # минтеры: держат / продали / перевели
    minter_holds = sum(1 for w in minters if w in holders_now)
    minter_sold = len({frm for _, _, frm, _, _ in sales if frm in minters})

    # тиры по кол-ву сейчас
    tier1 = sum(1 for w in holders_now if bal[w] == 1)
    tier2 = sum(1 for w in holders_now if bal[w] >= 2)

    # вывод
    print("\n" + "=" * 60)
    print("RETENTION: Velcorians (alpha-pass)")
    print("=" * 60)
    print(f"Всего минтов:            {len([e for e in events if e[1]=='MINT'])}")
    print(f"Уникальных минтеров:     {len(minters)}")
    print(f"Уникальных приобретателей (минт+покупка): {len(acquirers)}")
    print(f"Держателей СЕЙЧАС:        {len(holders_now)}  (токенов на руках: {sum(bal.values())})")
    print(f"\n-- ВЫХОДЫ --")
    print(f"Продаж на рынке (SALE):  {len(sales)}   <- реальные выходы за деньги")
    print(f"Простых переводов:       {len(transfers_plain)}   (подарок/само-переброс, НЕ продажа)")
    print(f"Реципрокных пар (само-переброс почти точно): {len(recip)}")
    print(f"\n-- МИНТЕРЫ --")
    print(f"из {len(minters)} минтеров: держат {minter_holds} ({minter_holds*100//max(len(minters),1)}%), "
          f"продали {minter_sold} ({minter_sold*100//max(len(minters),1)}%)")
    print(f"\n-- СКОРОСТЬ ВЫХОДА (время от получения до продажи) --")
    if hold_hours:
        print(f"медиана удержания до продажи: {st.median(hold_hours)/24:.1f} дней")
        print(f"продали в течение 7 дней:  {quick7} ({quick7*100//len(sales) if sales else 0}% продаж) — быстрый слив")
        print(f"продали в течение 30 дней: {quick30}")
    print(f"\n-- ТИРЫ (держат сейчас) --")
    print(f"держат 1 пропуск:  {tier1}")
    print(f"держат 2+ (выше роль): {tier2}")
    # цены выходов
    priced = [p for _, _, _, _, p in sales if p > 0]
    if priced:
        print(f"\n-- ЦЕНЫ ПРОДАЖ --")
        print(f"сделок с ценой: {len(priced)}, медиана {st.median(priced):.4f} ETH, "
              f"мин {min(priced):.4f}, макс {max(priced):.4f}")

    # CSV по кошелькам
    rows = []
    for w in acquirers:
        rows.append({
            "wallet": w, "holds_now": bal.get(w, 0),
            "is_minter": int(w in minters),
            "sold": sum(1 for _, _, frm, _, _ in sales if frm == w),
            "transferred_out": sum(1 for _, _, frm, _ in transfers_plain if frm == w),
            "still_holding": int(w in holders_now),
        })
    rows.sort(key=lambda r: (-r["holds_now"], -r["sold"]))
    with open(args.out, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["wallet", "holds_now", "is_minter", "sold", "transferred_out", "still_holding"])
        wr.writeheader()
        wr.writerows(rows)
    print(f"\n-> {args.out} ({len(rows)} кошельков)", file=sys.stderr)


if __name__ == "__main__":
    main()
