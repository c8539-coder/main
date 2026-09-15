"""Собрать HTML-дашборд из CSV, который выдаёт main.py.

Читает ранжированный CSV (top_wallets_*.csv), считает сводку и раскладку скора
на компоненты, подставляет данные в ``templates/dashboard.html`` и пишет
самодостаточный HTML в каталог out/.

Пример:
    # взять свежайший CSV из out/ автоматически
    python -m scripts.nft_top_wallets.dashboard
    # или явно
    python -m scripts.nft_top_wallets.dashboard --csv path/to/top_wallets.csv --top 50
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import statistics as st
import time
from collections import Counter

from . import enrich as E

HERE = os.path.dirname(__file__)
OUT_DIR = os.path.join(HERE, "out")
TEMPLATE = os.path.join(HERE, "templates", "dashboard.html")
PLACEHOLDER = "/*__DATA__*/{}"

# те же дефолты весов, что и в config.Settings (env-переопределяемые)
_WEIGHT_KEYS = ("bluechip", "whale", "degen", "early", "kol", "pnl")
_WEIGHT_DEFAULTS = {
    "bluechip": "0.25", "whale": "0.15", "degen": "0.15",
    "early": "0.15", "kol": "0.10", "pnl": "0.20",
}


def _weights(have_sales: bool) -> dict[str, float]:
    w = {k: float(os.getenv(f"W_{k.upper()}", _WEIGHT_DEFAULTS[k])) for k in _WEIGHT_KEYS}
    if not have_sales:
        w["pnl"] = 0.0
    return {k: v for k, v in w.items() if v > 0}


def _f(x: str) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _i(x: str) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def build_payload(csv_path: str, *, top: int = 50, collection: str = "",
                  contract: str = "", chain: str = "robinhood-mainnet",
                  enrich_chain: str = "eth-mainnet") -> dict:
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    if not rows:
        raise SystemExit(f"Пустой CSV: {csv_path}")

    have_sales = any(_f(r.get("realized_pnl", 0)) != 0 for r in rows)
    weights = _weights(have_sales)
    total_w = sum(weights.values()) or 1.0

    data = []
    for r in rows:
        bc, bcoll = _i(r["bluechip_count"]), _i(r["bluechip_collections"])
        bal, flips = _f(r["eth_balance"]), _i(r["sells"])
        mint, early = _i(r["is_minter"]), _i(r["is_early_buyer"])
        labels = [l for l in (r.get("labels") or "").split(";") if l]
        sig = {
            "bluechip": 0.65 * E._clamp01(bcoll / E.BLUECHIP_COLL_CAP)
                        + 0.35 * E._log_ratio(bc, E.BLUECHIP_CAP),
            "whale": E._log_ratio(bal, E.BALANCE_CAP),
            "degen": E._clamp01(flips / E.FLIP_CAP),
            "early": 1.0 if early else (0.6 if mint else 0.0),
            "kol": 1.0 if any(l.startswith(("KOL:", "SM:")) for l in labels) else 0.0,
            "pnl": E._clamp01(_f(r["realized_pnl"]) / E.PNL_CAP) if _f(r["realized_pnl"]) > 0 else 0.0,
        }
        contrib = {k: round(weights.get(k, 0.0) * sig[k] / total_w * 100, 2) for k in weights}
        data.append({
            "address": r["address"], "ens": r.get("ens", ""), "score": _f(r["total_score"]),
            "held": _i(r["tokens_held"]), "eth": round(bal, 3), "bc": bc, "bcoll": bcoll,
            "flips": flips, "minter": mint, "early": early, "labels": labels, "contrib": contrib,
        })
    data.sort(key=lambda d: d["score"], reverse=True)

    scores = [d["score"] for d in data]
    bals = [d["eth"] for d in data]
    hist = Counter(min(int(s // 5) * 5, 45) for s in scores)
    return {
        "meta": {
            "collection": collection or "NFT collection",
            "contract": contract or "",
            "chain": chain, "enrich_chain": enrich_chain,
            "generated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
            "no_sales": not have_sales,
            "weights": {k: round(weights[k] / total_w, 3) for k in weights},
        },
        "summary": {
            "holders": len(data), "score_max": round(max(scores), 1),
            "score_median": round(st.median(scores), 1),
            "minters": sum(d["minter"] for d in data), "early": sum(d["early"] for d in data),
            "bluechip_holders": sum(1 for d in data if d["bc"] > 0),
            "ens_found": sum(1 for d in data if d["ens"]),
            "total_eth": round(sum(bals), 1), "whales": sum(1 for d in data if d["eth"] > 10),
            "flippers": sum(1 for d in data if d["flips"] > 0),
        },
        "hist": [{"bucket": b, "count": hist.get(b, 0)} for b in range(0, 50, 5)],
        "top": data[:top],
    }


def render(payload: dict, out_path: str) -> str:
    tmpl = open(TEMPLATE, encoding="utf-8").read()
    if PLACEHOLDER not in tmpl:
        raise SystemExit(f"В шаблоне нет плейсхолдера {PLACEHOLDER!r}: {TEMPLATE}")
    body = tmpl.replace(PLACEHOLDER, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), 1)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    open(out_path, "w", encoding="utf-8").write(body)
    return out_path


def _latest_csv() -> str | None:
    files = sorted(glob.glob(os.path.join(OUT_DIR, "top_wallets_*.csv")), key=os.path.getmtime)
    return files[-1] if files else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="HTML-дашборд топ-кошельков из CSV")
    p.add_argument("--csv", help="Путь к CSV (по умолчанию — свежайший в out/)")
    p.add_argument("--top", type=int, default=50, help="Сколько кошельков показать")
    p.add_argument("--collection", default="Rare Friends Genesis")
    p.add_argument("--contract", default=os.getenv("COLLECTION_CONTRACT", ""))
    p.add_argument("--chain", default=os.getenv("ALCHEMY_NETWORK", "robinhood-mainnet"))
    p.add_argument("--enrich-chain", default="eth-mainnet")
    p.add_argument("--out", default=os.path.join(OUT_DIR, "dashboard.html"))
    args = p.parse_args(argv)

    csv_path = args.csv or _latest_csv()
    if not csv_path or not os.path.exists(csv_path):
        raise SystemExit("Не найден CSV. Сначала запустите main.py или укажите --csv.")

    payload = build_payload(
        csv_path, top=args.top, collection=args.collection,
        contract=args.contract, chain=args.chain, enrich_chain=args.enrich_chain,
    )
    out = render(payload, args.out)
    s = payload["summary"]
    print(f"[✓] Дашборд: {out}")
    print(f"    холдеров {s['holders']} | top-score {s['score_max']} | "
          f"blue-chip {s['bluechip_holders']} | ENS {s['ens_found']} | из CSV: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
