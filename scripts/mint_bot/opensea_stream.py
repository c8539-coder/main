"""OpenSea Stream API client (websocket) — strict OpenSea sales in real time.

Subscribes to every collection's ``item_sold`` events over one websocket and
calls a callback for each sale. This is the only feed that is guaranteed to be
an OpenSea sale (the on-chain Seaport heuristic also catches Blur/X2Y2, which
share the protocol). Each sale carries the buyer, the exact price and the
payment token (ETH vs WETH), so no extra RPC is needed.

Protocol: OpenSea uses Phoenix Channels over websocket. We join the wildcard
topic ``collection:*`` and read ``item_sold`` messages. A heartbeat keeps the
socket open; the reader reconnects with backoff on any drop.

Docs: https://docs.opensea.io/reference/stream-api-overview
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

from websocket import WebSocketApp

log = logging.getLogger("mint_bot.opensea")

# vsn=1.0.0 forces the Phoenix "map" serializer: each message is a JSON object
# ({topic,event,payload,ref}). Without it the server may use the v2 serializer,
# which sends arrays ([join_ref,ref,topic,event,payload]) instead.
STREAM_URL = "wss://stream.openseabeta.com/socket/websocket?token={key}&vsn=1.0.0"
WILDCARD_TOPIC = "collection:*"

# OpenSea chain slug -> our internal chain name (for links / naming).
CHAIN_MAP = {
    "ethereum": "eth-mainnet",
    "robinhood": "robinhood-mainnet",
}


def _parse_nft_id(nft_id: str) -> tuple[str, str, str]:
    """'chain/contract/token_id' -> (chain, contract, token_id)."""
    parts = (nft_id or "").split("/")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    return "", "", ""


def _wei_to_eth(value, decimals: int) -> float:
    try:
        return int(value) / (10 ** int(decimals))
    except (TypeError, ValueError):
        return 0.0


class OpenSeaStream:
    """Background websocket that reports OpenSea sales to ``on_sale``.

    ``on_sale`` receives a dict: chain, contract, token_id, buyer, seller,
    collection, currency ("ETH"/"WETH"/symbol), price (float).
    """

    def __init__(self, api_key: str, on_sale: Callable[[dict], None],
                 chains: list[str] | None = None):
        self.api_key = api_key
        self.on_sale = on_sale
        # optional filter by our internal chain names; None = all chains
        self.chains = set(chains) if chains else None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws: WebSocketApp | None = None
        self._hb: threading.Thread | None = None
        self._ref = 0

    # ------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="opensea-stream",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    def _next_ref(self) -> str:
        self._ref += 1
        return str(self._ref)

    def _on_open(self, ws: WebSocketApp) -> None:
        ws.send(json.dumps({
            "topic": WILDCARD_TOPIC,
            "event": "phx_join",
            "payload": {},
            "ref": self._next_ref(),
        }))
        log.info("OpenSea stream connected; subscribed to %s", WILDCARD_TOPIC)

        def _heartbeat() -> None:
            while not self._stop.is_set():
                time.sleep(30)
                try:
                    ws.send(json.dumps({
                        "topic": "phoenix", "event": "heartbeat",
                        "payload": {}, "ref": self._next_ref(),
                    }))
                except Exception:  # noqa: BLE001
                    break

        self._hb = threading.Thread(target=_heartbeat, name="opensea-hb",
                                    daemon=True)
        self._hb.start()

    def _on_message(self, ws: WebSocketApp, message: str) -> None:
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            return
        # Phoenix v2 serializer sends arrays; we request v1 (objects) but guard.
        if isinstance(msg, list):
            event = msg[3] if len(msg) >= 5 else None
            payload = msg[4] if len(msg) >= 5 else {}
            msg = {"event": event, "payload": payload}
        if not isinstance(msg, dict) or msg.get("event") != "item_sold":
            return
        payload = msg.get("payload", {}).get("payload", {}) or msg.get("payload", {})
        try:
            sale = self._parse_sale(payload)
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not parse sale: %s", exc)
            return
        if sale is None:
            return
        if self.chains and sale["chain"] not in self.chains:
            return
        try:
            self.on_sale(sale)
        except Exception as exc:  # noqa: BLE001
            log.error("on_sale callback failed: %s", exc)

    def _parse_sale(self, payload: dict) -> dict | None:
        item = payload.get("item", {}) or {}
        os_chain, contract, token_id = _parse_nft_id(item.get("nft_id", ""))
        # chain may also come as an object under payload["chain"]
        if not os_chain:
            ch = payload.get("chain")
            os_chain = ch.get("name") if isinstance(ch, dict) else (ch or "")
        chain = CHAIN_MAP.get(os_chain, os_chain)
        buyer = ((payload.get("taker") or {}).get("address") or "").lower()
        seller = ((payload.get("maker") or {}).get("address") or "").lower()
        if not (contract and buyer):
            return None
        token = payload.get("payment_token", {}) or {}
        symbol = (token.get("symbol") or "ETH").upper()
        currency = "WETH" if symbol == "WETH" else ("ETH" if symbol in ("ETH", "WETH") else symbol)
        price = _wei_to_eth(payload.get("sale_price"), token.get("decimals", 18))
        collection = (payload.get("collection", {}) or {}).get("slug") or ""
        return {
            "chain": chain, "contract": contract.lower(), "token_id": token_id,
            "buyer": buyer, "seller": seller, "collection": collection,
            "currency": currency, "price": price,
        }

    def _on_error(self, ws: WebSocketApp, error) -> None:  # noqa: ANN001
        log.warning("OpenSea stream error: %s", error)

    def _on_close(self, ws: WebSocketApp, *_a) -> None:  # noqa: ANN002
        log.info("OpenSea stream closed")

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            self._ws = WebSocketApp(
                STREAM_URL.format(key=self.api_key),
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            try:
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:  # noqa: BLE001
                log.warning("OpenSea stream crashed: %s", exc)
            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)  # reconnect with backoff
