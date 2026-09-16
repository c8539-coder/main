"""Загрузка списка отслеживаемых кошельков (address -> type).

Понимает CSV с колонками ``address`` и (опц.) ``type`` — как файлы, что выдаёт
дашборд/скоринг (``good_wallets.csv``, ``combined_top_wallets.csv``,
``top_wallets_*.csv`` с ``total_score``). Можно передать несколько файлов.
"""

from __future__ import annotations

import csv
import glob
import os


def load_watchlist(paths: list[str], *, min_score: float = 0.0) -> dict[str, str]:
    """Собрать {address_lower: TYPE} из одного или нескольких CSV.

    ``type`` берётся из колонки ``type``; если её нет — ставим ``TRACKED``.
    ``min_score`` фильтрует по ``total_score``, если колонка есть.
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
