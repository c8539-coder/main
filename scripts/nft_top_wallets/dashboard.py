"""Build an HTML dashboard from the CSV that main.py produces.

Reads a ranked CSV (top_wallets_*.csv), computes the summary and the score
component breakdown, injects the data into ``templates/dashboard.html`` and
writes a self-contained HTML into out/.

Example:
    # pick the newest CSV in out/ automatically
    python -m scripts.nft_top_wallets.dashboard
    # or explicitly
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

from .enrich import WalletFeatures, score_components

HERE = os.path.dirname(__file__)
OUT_DIR = os.path.join(HERE, "out")
TEMPLATE = os.path.join(HERE, "templates", "dashboard.html")
PLACEHOLDER = "/*__DATA__*/{}"

# same weight defaults as config.Settings (env-overridable): three groups
_WEIGHT_DEFAULTS = {"smart": "0.50", "degen": "0.25", "early": "0.25"}


def _weights() -> dict[str, float]:
    return {k: float(os.getenv(f"W_{k.upper()}", d)) for k, d in _WEIGHT_DEFAULTS.items()}


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
        raise SystemExit(f"Empty CSV: {csv_path}")

    have_sales = any(_f(r.get("realized_pnl", 0)) != 0 for r in rows)
    weights = _weights()

    data = []
    n_team = 0
    for r in rows:
        # team/treasury excluded from the leaderboard (flagged is_team in CSV)
        if _i(r.get("is_team", 0)):
            n_team += 1
            continue
        bc, bcoll = _i(r["bluechip_count"]), _i(r["bluechip_collections"])
        bal, flips = _f(r["eth_balance"]), _i(r["sells"])
        mint, early = _i(r["is_minter"]), _i(r["is_early_buyer"])
        labels = [l for l in (r.get("labels") or "").split(";") if l]
        f = WalletFeatures(
            address=r["address"], tokens_held=_i(r["tokens_held"]),
            is_minter=bool(mint), is_early_buyer=bool(early), sells=flips,
            bluechip_count=bc, bluechip_collections=bcoll, eth_balance=bal,
        )
        total, contrib = score_components(f, weights)
        data.append({
            "address": r["address"], "ens": r.get("ens", ""), "score": total,
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
            "weights": {k: round(weights[k] / (sum(weights.values()) or 1.0), 3) for k in weights},
        },
        "summary": {
            "holders": len(data), "score_max": round(max(scores), 1),
            "score_median": round(st.median(scores), 1),
            "minters": sum(d["minter"] for d in data), "early": sum(d["early"] for d in data),
            "bluechip_holders": sum(1 for d in data if d["bc"] > 0),
            "ens_found": sum(1 for d in data if d["ens"]),
            "total_eth": round(sum(bals), 1), "whales": sum(1 for d in data if d["eth"] > 10),
            "flippers": sum(1 for d in data if d["flips"] > 0),
            "team_excluded": n_team,
        },
        "hist": [{"bucket": b, "count": hist.get(b, 0)} for b in range(0, 50, 5)],
        "top": data[:top],
    }


def render(payload: dict, out_path: str) -> str:
    tmpl = open(TEMPLATE, encoding="utf-8").read()
    if PLACEHOLDER not in tmpl:
        raise SystemExit(f"Template has no placeholder {PLACEHOLDER!r}: {TEMPLATE}")
    title = f"{payload['meta']['collection']} Holder Intel"
    body = (
        tmpl.replace(PLACEHOLDER, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), 1)
            .replace("__TITLE__", title)
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    open(out_path, "w", encoding="utf-8").write(body)
    return out_path


def _latest_csv() -> str | None:
    files = sorted(glob.glob(os.path.join(OUT_DIR, "top_wallets_*.csv")), key=os.path.getmtime)
    return files[-1] if files else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="HTML dashboard of top wallets from a CSV")
    p.add_argument("--csv", help="CSV path (default: newest in out/)")
    p.add_argument("--top", type=int, default=50, help="How many wallets to show")
    p.add_argument("--collection", default="Rare Friends Genesis")
    p.add_argument("--contract", default=os.getenv("COLLECTION_CONTRACT", ""))
    p.add_argument("--chain", default=os.getenv("ALCHEMY_NETWORK", "robinhood-mainnet"))
    p.add_argument("--enrich-chain", default="eth-mainnet")
    p.add_argument("--out", default=os.path.join(OUT_DIR, "dashboard.html"))
    args = p.parse_args(argv)

    csv_path = args.csv or _latest_csv()
    if not csv_path or not os.path.exists(csv_path):
        raise SystemExit("No CSV found. Run main.py first or pass --csv.")

    payload = build_payload(
        csv_path, top=args.top, collection=args.collection,
        contract=args.contract, chain=args.chain, enrich_chain=args.enrich_chain,
    )
    out = render(payload, args.out)
    s = payload["summary"]
    print(f"[OK] Dashboard: {out}")
    print(f"    holders {s['holders']} | top-score {s['score_max']} | "
          f"blue-chip {s['bluechip_holders']} | ENS {s['ens_found']} | from CSV: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
