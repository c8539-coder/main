#!/usr/bin/env python3
"""Диагностика данных по одному кошельку в NFT-коллекции на Robinhood-чейне.

Ничего не считает — только вытаскивает СЫРЫЕ данные и печатает их, чтобы
понять форму сделок (как оформлены покупки/продажи, где лежит цена, роялти).

Запуск:
    python3 tools/probe_wallet.py

Ключи можно передать через переменные окружения (см. секцию CONFIG),
либо оставить дефолтные значения ниже.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error

# ------------------------- CONFIG -------------------------
RH_RPC = os.environ.get(
    "RH_RPC",
    "https://robinhood-mainnet.g.alchemy.com/v2/alch_DIQ82lv-_n8L5zFqkk9Ih",
)
CONTRACT = os.environ.get("CONTRACT", "0xae42d5511886590538160a3cbdb91388cf1e76a3").lower()
WALLET = os.environ.get("WALLET", "0xb180e3fde77c0d4499a934014f437e8d442fd8a8").lower()
SAMPLE_TX_COUNT = int(os.environ.get("SAMPLE_TX_COUNT", "4"))  # сколько транзакций разобрать детально
# ----------------------------------------------------------

WETH_TOPIC = None  # заполнится, если найдём


def rpc(method: str, params: list) -> dict:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RH_RPC, data=payload, headers={"Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            if "error" in data:
                raise RuntimeError(f"RPC error for {method}: {data['error']}")
            return data["result"]
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            if e.code == 429 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"HTTP {e.code} for {method}: {body}") from e
        except urllib.error.URLError as e:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Network error for {method}: {e}") from e
    raise RuntimeError(f"failed {method}")


def get_transfers(direction: str) -> list:
    """direction = 'from' (продажи/уходы) или 'to' (покупки/приходы)."""
    key = "fromAddress" if direction == "from" else "toAddress"
    params = {
        "fromBlock": "0x0",
        "toBlock": "latest",
        "contractAddresses": [CONTRACT],
        "category": ["erc721", "erc1155"],
        "withMetadata": True,
        "excludeZeroValue": False,
        "maxCount": "0x3e8",  # 1000
        key: WALLET,
    }
    out = []
    page = None
    while True:
        p = dict(params)
        if page:
            p["pageKey"] = page
        res = rpc("alchemy_getAssetTransfers", [p])
        out.extend(res.get("transfers", []))
        page = res.get("pageKey")
        if not page:
            break
    return out


def main() -> None:
    print("=" * 70)
    print(f"CONTRACT: {CONTRACT}")
    print(f"WALLET:   {WALLET}")
    print("=" * 70)

    print("\n### chainId ###")
    try:
        print("chainId =", rpc("eth_chainId", []))
    except Exception as e:
        print("!! не удалось получить chainId:", e)
        print("Если это 403 — значит из твоей сети хост тоже режется. Скажи мне.")
        return

    print("\n### NFT-трансферы КОШЕЛЬКА в этой коллекции ###")
    incoming = get_transfers("to")    # приходы = покупки/минты
    outgoing = get_transfers("from")  # уходы   = продажи/трансферы
    print(f"incoming (to wallet):   {len(incoming)}")
    print(f"outgoing (from wallet): {len(outgoing)}")

    # соберём уникальные транзакции (в хронологии), пометим направление
    tx_index: dict[str, dict] = {}
    for t in incoming:
        tx_index.setdefault(t["hash"], {"dir": "IN", "transfer": t})
    for t in outgoing:
        tx_index.setdefault(t["hash"], {"dir": "OUT", "transfer": t})

    print(f"уникальных транзакций: {len(tx_index)}")

    # ZERO address => минт
    ZERO = "0x0000000000000000000000000000000000000000"
    mints = [t for t in incoming if (t.get("from") or "").lower() == ZERO]
    print(f"из них минтов (from == 0x0): {len(mints)}")

    print("\n### Пример нескольких трансферов (сырой JSON) ###")
    for t in (incoming[:2] + outgoing[:2]):
        print(json.dumps(t, indent=2, ensure_ascii=False))
        print("-" * 40)

    # детально разберём несколько транзакций: сама tx + receipt (логи)
    print("\n### Детальный разбор нескольких транзакций (tx + receipt logs) ###")
    print("Смотрим: value самой tx (нативный ETH), и логи Transfer(ERC20)/событий маркетплейса.\n")
    sample_hashes = list(tx_index.keys())[:SAMPLE_TX_COUNT]
    for h in sample_hashes:
        info = tx_index[h]
        print("#" * 60)
        print(f"TX {h}  [{info['dir']}]  token #{info['transfer'].get('tokenId')}")
        try:
            tx = rpc("eth_getTransactionByHash", [h])
            rcpt = rpc("eth_getTransactionReceipt", [h])
        except Exception as e:
            print("  !! ошибка получения tx/receipt:", e)
            continue
        val = int(tx.get("value", "0x0"), 16) / 1e18
        print(f"  from(tx):   {tx.get('from')}")
        print(f"  to(tx):     {tx.get('to')}   <-- вероятно маркетплейс/роутер")
        print(f"  value(tx):  {val:.6f} ETH (нативный перевод внутри самой tx)")
        print(f"  logs:       {len(rcpt.get('logs', []))} шт")
        # покажем логи компактно: адрес контракта + topic0 + data(усечён)
        for lg in rcpt.get("logs", []):
            topics = lg.get("topics", [])
            t0 = topics[0] if topics else ""
            print(f"    - addr={lg.get('address')} topic0={t0[:12]}.. topics={len(topics)} data_len={len(lg.get('data','') )}")
        print()

    print("=" * 70)
    print("ГОТОВО. Скопируй ВЕСЬ вывод выше и пришли мне.")
    print("По нему я пойму, где лежат цены сделок и роялти, и напишу точный расчёт.")
    print("=" * 70)


if __name__ == "__main__":
    main()
