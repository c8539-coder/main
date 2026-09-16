"""Load the tracked-wallet list (address -> type).

Reads CSVs with an ``address`` and (optional) ``type`` column — the files the
dashboard/scoring produce (``good_wallets.csv``, ``combined_top_wallets.csv``,
``top_wallets_*.csv`` with ``total_score``). Multiple files are allowed.
"""

from __future__ import annotations

import csv
import glob
import os


def load_watchlist(paths: list[str], *, min_score: float = 0.0) -> dict[str, str]:
    """Build {address_lower: TYPE} from one or more CSVs.

    ``type`` comes from the ``type`` column; when absent it defaults to
    ``TRACKED``. ``min_score`` filters by ``total_score`` if that column exists.
    """
    out: dict[str, str] = {}
    files: list[str] = []
    for p in paths:
        files.extend(sorted(glob.glob(p)) if any(c in p for c in "*?[") else [p])
    for path in files:
        if not os.path.exists(path):
            continue
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                addr = (row.get("address") or "").strip().lower()
                if not addr.startswith("0x"):
                    continue
                if min_score and "total_score" in row:
                    try:
                        if float(row["total_score"]) < min_score:
                            continue
                    except ValueError:
                        pass
                out[addr] = (row.get("type") or "TRACKED").strip().upper() or "TRACKED"
    return out
