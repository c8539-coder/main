"""Building Discord embeds from NFT events."""

from __future__ import annotations

from typing import Optional

import discord

from .opensea import EVENT_LABELS, NftEvent

# цвет полоски эмбеда по типу события
EVENT_COLORS = {
    "sale": discord.Color.green(),
    "listing": discord.Color.blurple(),
}

# ссылки на обозреватели блокчейна по сети
EXPLORERS = {
    "ethereum": "https://etherscan.io/tx/",
    "matic": "https://polygonscan.com/tx/",
    "polygon": "https://polygonscan.com/tx/",
    "base": "https://basescan.org/tx/",
    "arbitrum": "https://arbiscan.io/tx/",
    "optimism": "https://optimistic.etherscan.io/tx/",
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
    if not base:
        return None
    return f"{base}{tx_hash}"


def build_embed(event: NftEvent) -> discord.Embed:
    label = EVENT_LABELS.get(event.event_type, event.event_type.capitalize())
    color = EVENT_COLORS.get(event.event_type, discord.Color.light_grey())

    title = f"{label}: {event.nft_name}"
    embed = discord.Embed(
        title=title[:256],
        url=event.nft_url or None,
        color=color,
        timestamp=_utc_from_ts(event.timestamp),
    )

    if event.price is not None:
        symbol = event.price_symbol or "ETH"
        embed.add_field(name="Цена", value=f"**{event.price:g} {symbol}**", inline=True)

    embed.add_field(name="Коллекция", value=event.collection, inline=True)
    embed.add_field(name="Сеть", value=event.chain, inline=True)

    if event.event_type == "sale":
        embed.add_field(name="Покупатель", value=_short_addr(event.buyer), inline=True)
        embed.add_field(name="Продавец", value=_short_addr(event.seller), inline=True)
    elif event.seller:
        embed.add_field(name="Продавец", value=_short_addr(event.seller), inline=True)

    links = []
    if event.nft_url:
        links.append(f"[OpenSea]({event.nft_url})")
    tx_link = _explorer_link(event.chain, event.tx_hash)
    if tx_link:
        links.append(f"[Транзакция]({tx_link})")
    if links:
        embed.add_field(name="Ссылки", value=" · ".join(links), inline=False)

    if event.nft_image:
        embed.set_thumbnail(url=event.nft_image)

    embed.set_footer(text="NFT Alert Bot · OpenSea")
    return embed


def _utc_from_ts(ts: int):
    import datetime

    try:
        return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    except (ValueError, OSError, OverflowError):
        return datetime.datetime.now(tz=datetime.timezone.utc)
