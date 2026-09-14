"""Building small one-line sale notifications for Discord.

Уведомление — обычное короткое сообщение (не большая карточка-embed),
только о продажах. Текст на английском.
"""

from __future__ import annotations

from typing import Optional

from .opensea import NftEvent

# ссылки на обозреватели блокчейна по сети (chain-идентификаторы OpenSea)
EXPLORERS = {
    "ethereum": "https://etherscan.io/tx/",
    "matic": "https://polygonscan.com/tx/",
    "polygon": "https://polygonscan.com/tx/",
    "base": "https://basescan.org/tx/",
    "arbitrum": "https://arbiscan.io/tx/",
    "optimism": "https://optimistic.etherscan.io/tx/",
    "avalanche": "https://snowtrace.io/tx/",
    "blast": "https://blastscan.io/tx/",
}


def _short_addr(addr: Optional[str]) -> str:
    if not addr:
        return "—"
    if len(addr) <= 12:
        return addr
    return f"{addr[:6]}…{addr[-4:]}"


def _explorer_link(chain: str, tx_hash: Optional[str]) -> Optional[str]:
    if not tx_hash:
        return None
    base = EXPLORERS.get(chain.lower())
    return f"{base}{tx_hash}" if base else None


def _price_str(event: NftEvent, usd: Optional[float]) -> str:
    if event.price is None:
        return "?"
    symbol = (event.price_symbol or "ETH").upper()
    head = f"Ξ{event.price:g}" if symbol in ("ETH", "WETH") else f"{event.price:g} {symbol}"
    if usd:
        head += f" (${usd * event.price:,.0f})"
    return head


def build_message(event: NftEvent, usd_rate: Optional[float] = None) -> str:
    """Собрать маленькое уведомление о продаже: одна-две короткие строки.

    Пример::

        🟢 **Azuki #9605** sold for **Ξ14.2 ($39,760)**
        `0x9f2a…c41d` → `0x1b7e…88af` · <https://etherscan.io/tx/0x…>
    """
    line1 = f"🟢 **{event.nft_name}** sold for **{_price_str(event, usd_rate)}**"

    parts = []
    if event.seller or event.buyer:
        parts.append(f"`{_short_addr(event.seller)}` → `{_short_addr(event.buyer)}`")
    # ссылку оборачиваем в <>, чтобы Discord не разворачивал большое превью
    link = _explorer_link(event.chain, event.tx_hash) or event.nft_url
    if link:
        parts.append(f"<{link}>")

    if not parts:
        return line1
    return f"{line1}\n" + " · ".join(parts)
