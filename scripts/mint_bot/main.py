"""Минт-трекер: следит за минтами наших кошельков и шлёт алерт в Discord.

Когда >= MINT_ALERT_MIN отслеживаемых кошельков заминтили одну коллекцию,
постит алерт в канал (через webhook) с разбивкой по типам и пингом роли.

Запуск:  python -m scripts.mint_bot.main
Секреты и настройки — через окружение (см. .env.example).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # noqa: BLE001
    pass

from scripts.nft_top_wallets.alchemy import AlchemyClient
from scripts.nft_top_wallets.config import Settings

from .discord_out import build_embed, post_alert
from .tracker import MintTracker
from .watchlist import load_watchlist

log = logging.getLogger("mint_bot")

DEFAULT_WATCHLIST = os.path.join(
    os.path.dirname(__file__), "..", "nft_top_wallets", "out", "good_wallets.csv"
)


def _load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_block": {}, "contracts": {}}


def _save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, path)


def _build_clients(settings: Settings, chains_wanted: list[str]) -> dict[str, AlchemyClient]:
    available = {settings.chain.name: settings.chain}
    if settings.mainnet is not None:
        available.setdefault(settings.mainnet.name, settings.mainnet)
    clients: dict[str, AlchemyClient] = {}
    for name in chains_wanted:
        net = available.get(name)
        if net is None:
            log.warning("Сеть %s не сконфигурирована (нет endpoint), пропускаю", name)
            continue
        clients[name] = AlchemyClient(net, settings)
    if not clients:  # если явный список не совпал — берём всё, что есть
        clients = {n: AlchemyClient(net, settings) for n, net in available.items()}
    return clients


def run() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        raise SystemExit("Не задан DISCORD_WEBHOOK_URL (URL вебхука канала).")
    role_id = os.getenv("DISCORD_ROLE_ID", "").strip() or None
    min_wallets = int(os.getenv("MINT_ALERT_MIN", "5"))
    ping_wallets = int(os.getenv("MINT_PING_MIN", "15"))
    ping_step = int(os.getenv("MINT_PING_STEP", "15"))
    poll_s = max(15, int(os.getenv("MINT_POLL_SECONDS", "60")))
    window_s = int(os.getenv("MINT_WINDOW_SECONDS", str(6 * 3600)))
    backfill = int(os.getenv("MINT_BACKFILL_BLOCKS", "300"))
    state_file = os.getenv("STATE_FILE", "data/mint_bot_state.json")
    chains = [c.strip() for c in os.getenv(
        "MINT_CHAINS", "robinhood-mainnet,eth-mainnet").split(",") if c.strip()]
    watch_paths = [p.strip() for p in os.getenv(
        "WATCHLIST_FILES", os.path.normpath(DEFAULT_WATCHLIST)).split(",") if p.strip()]
    min_score = float(os.getenv("WATCHLIST_MIN_SCORE", "0"))

    watchlist = load_watchlist(watch_paths, min_score=min_score)
    if not watchlist:
        raise SystemExit(f"Пустой watchlist. Проверьте WATCHLIST_FILES={watch_paths}")

    # тестовый режим: отправить один пример-алерт в канал и выйти
    if "--test" in sys.argv or os.getenv("MINT_TEST"):
        sample = dict(list(watchlist.items())[:6]) or {"0x0000000000000000000000000000000000000000": "SMART"}
        embed = build_embed(name="TEST — Bored Ape Yacht Club", chain="eth-mainnet",
                            contract="0xbc4ca0eda7647a8ab7c2061c2e118a18a936f13d",
                            wallets=sample, hot=bool(role_id))
        post_alert(webhook, embed, role_id=role_id)
        log.info("Тестовый алерт отправлен в канал (%d кош.).", len(sample))
        return

    settings = Settings.load()
    clients = _build_clients(settings, chains)
    tracker = MintTracker(
        watchlist=watchlist, clients=clients, min_wallets=min_wallets,
        ping_wallets=ping_wallets, ping_step=ping_step,
        window_seconds=window_s, backfill_blocks=backfill,
        state=_load_state(state_file),
    )

    log.info("Старт: %d кошельков, сети=%s, алерт>=%d, пинг>=%d, опрос каждые %ds",
             len(watchlist), list(clients), min_wallets, ping_wallets, poll_s)

    while True:
        try:
            alerts = tracker.poll()
            for a in alerts:
                embed = build_embed(name=a.name, chain=a.chain,
                                    contract=a.contract, wallets=a.wallets, hot=a.ping)
                try:
                    # пинг роли только на крупном сигнале (a.ping)
                    post_alert(webhook, embed, role_id=(role_id if a.ping else None))
                    log.info("Алерт%s: %d кош. минтят %s (%s)",
                             " (PING)" if a.ping else "", len(a.wallets), a.name, a.chain)
                except Exception as exc:  # noqa: BLE001
                    log.error("Не отправить алерт в Discord: %s", exc)
                    tracker.state["contracts"][f"{a.chain}|{a.contract}"]["alerted"] = False
            _save_state(state_file, tracker.state)
        except Exception as exc:  # noqa: BLE001
            log.error("Ошибка в цикле опроса: %s", exc)
        time.sleep(poll_s)


if __name__ == "__main__":
    run()
