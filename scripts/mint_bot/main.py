"""Mint tracker: watch mints by tracked wallets and alert a Discord channel.

When >= MINT_ALERT_MIN tracked wallets mint the same collection, post an alert
to the channel (via webhook) with a per-type breakdown and an optional role ping.

Run:  python -m scripts.mint_bot.main
Config comes from the environment (see .env.example).
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
from .opensea_stream import OpenSeaStream
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
            log.warning("chain %s is not configured (no endpoint), skipping", name)
            continue
        clients[name] = AlchemyClient(net, settings)
    if not clients:  # explicit list matched nothing -> use whatever is available
        clients = {n: AlchemyClient(net, settings) for n, net in available.items()}
    return clients


def run() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        raise SystemExit("DISCORD_WEBHOOK_URL is not set (channel webhook URL).")
    role_id = os.getenv("DISCORD_ROLE_ID", "").strip() or None
    min_wallets = int(os.getenv("MINT_ALERT_MIN", "5"))
    ping_wallets = int(os.getenv("MINT_PING_MIN", "15"))
    ping_step = int(os.getenv("MINT_PING_STEP", "15"))
    track_buys = os.getenv("TRACK_BUYS", "1") not in ("0", "false", "False", "")
    sales_only = os.getenv("SALES_ONLY", "1") not in ("0", "false", "False", "")
    opensea_key = os.getenv("OPENSEA_API_KEY", "").strip()
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
        raise SystemExit(f"Empty watchlist. Check WATCHLIST_FILES={watch_paths}")

    # test mode: post one sample alert to the channel and exit
    if "--test" in sys.argv or os.getenv("MINT_TEST"):
        sample = dict(list(watchlist.items())[:6]) or {"0x0000000000000000000000000000000000000000": "SMART"}
        embed = build_embed(name="TEST — Bored Ape Yacht Club", chain="eth-mainnet",
                            contract="0xbc4ca0eda7647a8ab7c2061c2e118a18a936f13d",
                            wallets=sample, hot=bool(role_id))
        post_alert(webhook, embed, role_id=role_id)
        log.info("Test alert sent to the channel (%d wallets).", len(sample))
        return

    settings = Settings.load()
    clients = _build_clients(settings, chains)
    # With an OpenSea key, buys come from the OpenSea Stream (strict OpenSea
    # sales); the on-chain poll then only needs to catch mints.
    use_opensea = bool(opensea_key) and track_buys
    tracker = MintTracker(
        watchlist=watchlist, clients=clients, min_wallets=min_wallets,
        ping_wallets=ping_wallets, ping_step=ping_step,
        track_buys=track_buys and not use_opensea,
        sales_only=sales_only, window_seconds=window_s, backfill_blocks=backfill,
        state=_load_state(state_file),
    )

    stream: OpenSeaStream | None = None
    if use_opensea:
        def _on_sale(sale: dict) -> None:
            tracker.add_buy(sale["chain"], sale["contract"], sale["buyer"],
                            currency=sale["currency"], name=sale["collection"] or None)
        stream = OpenSeaStream(opensea_key, _on_sale, chains=chains)
        stream.start()

    if track_buys:
        buy_src = "OpenSea stream" if use_opensea else "on-chain sales"
    else:
        buy_src = "off"
    log.info("Started: %d wallets, chains=%s, mints on-chain, buys=%s, alert>=%d, ping>=%d, poll every %ds",
             len(watchlist), list(clients), buy_src, min_wallets, ping_wallets, poll_s)

    while True:
        try:
            alerts = tracker.poll()
            for a in alerts:
                embed = build_embed(name=a.name, chain=a.chain, contract=a.contract,
                                    wallets=a.wallets, hot=a.ping, kind=a.kind,
                                    currencies=a.currencies)
                try:
                    # ping the role only on a large signal (a.ping)
                    post_alert(webhook, embed, role_id=(role_id if a.ping else None))
                    log.info("Alert%s: %d wallets %s %s (%s)",
                             " (PING)" if a.ping else "",
                             len(a.wallets), "minting" if a.kind == "mint" else "buying",
                             a.name, a.chain)
                except Exception as exc:  # noqa: BLE001
                    log.error("Failed to send alert to Discord: %s", exc)
                    tracker.state["contracts"][f"{a.chain}|{a.kind}|{a.contract}"]["alerted"] = False
            _save_state(state_file, tracker.state)
        except Exception as exc:  # noqa: BLE001
            log.error("Poll loop error: %s", exc)
        time.sleep(poll_s)


if __name__ == "__main__":
    run()
