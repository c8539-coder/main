"""Building minimalist Discord embeds from NFT events."""

from __future__ import annotations

import datetime
from typing import Optional

import discord

from .opensea import NftEvent

# цвет полоски эмбеда по типу события (текст алертов — на английском)
EVENT_STYLE = {
    "sale": ("🟢", "Sale", discord.Color.from_str("#2ecc71")),
    "listing": ("🔵", "Listing", discord.Color.from_str("#3498db")),
}
DEFAULT_STYLE = ("⚪", "Event", discord.Color.light_grey())

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


def _fmt_price(event: NftEvent, usd: Optional[float]) -> Optional[str]:
    if event.price is None:
        return None
    symbol = (event.price_symbol or "ETH").upper()
    if symbol in ("ETH", "WETH"):
        head = f"Ξ {event.price:g}"
    else:
        head = f"{event.price:g} {symbol}"
    if usd:
        total = usd * event.price
        head += f"  ·  ${total:,.0f}"
    return head


def build_embed(
    event: NftEvent,
    meta: Optional[dict] = None,
    usd_rate: Optional[float] = None,
) -> discord.Embed:
    icon, label, color = EVENT_STYLE.get(event.event_type, DEFAULT_STYLE)

    embed = discord.Embed(
        title=event.nft_name[:256],
        url=event.nft_url or None,
        color=color,
        timestamp=_utc_from_ts(event.timestamp),
    )

    # шапка = коллекция (имя + иконка)
    col_name = (meta or {}).get("name") or event.collection
    col_icon = (meta or {}).get("image")
    embed.set_author(
        name=col_name[:256],
        icon_url=col_icon or None,
        url=f"https://opensea.io/collection/{event.collection}",
    )

    # компактное тело: событие, цена, участники
    lines = [f"{icon} **{label}**"]
    price_line = _fmt_price(event, usd_rate)
    if price_line:
        lines.append(f"**{price_line}**")

    if event.event_type == "sale" and (event.seller or event.buyer):
        lines.append(
            f"`{_short_addr(event.seller)}` → `{_short_addr(event.buyer)}`"
        )
    elif event.seller:
        lines.append(f"from `{_short_addr(event.seller)}`")

    tx_link = _explorer_link(event.chain, event.tx_hash)
    if tx_link:
        lines.append(f"[View transaction]({tx_link})")

    embed.description = "\n".join(lines)

    if event.nft_image:
        embed.set_thumbnail(url=event.nft_image)

    embed.set_footer(text=f"OpenSea · {event.chain}")
    return embed


def _utc_from_ts(ts: int):
    try:
        return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    except (ValueError, OSError, OverflowError):
        return datetime.datetime.now(tz=datetime.timezone.utc)
