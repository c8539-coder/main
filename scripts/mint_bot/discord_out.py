"""Post a mint alert to Discord via a channel webhook.

A webhook is simpler than a bot: no token/intents, just the channel webhook URL
(Channel Settings -> Integrations -> Webhooks -> New Webhook -> Copy URL).
"""

from __future__ import annotations

from collections import Counter

import requests

# Type colors (matching the dashboard)
TYPE_COLOR = {"SMART": 0x3987E5, "DEGEN": 0xD95926, "EARLY": 0x199E70}
DEFAULT_COLOR = 0xE6AD55

# Display labels for types (not shouted in caps)
TYPE_LABEL = {"SMART": "Smart", "DEGEN": "Degen", "EARLY": "Early"}

# OpenSea chain slug
OPENSEA_SLUG = {"eth-mainnet": "ethereum", "robinhood-mainnet": "robinhood"}


def _pretty(t: str) -> str:
    return TYPE_LABEL.get(t.upper(), t.title())


def opensea_url(chain: str, contract: str) -> str:
    slug = OPENSEA_SLUG.get(chain, chain)
    return f"https://opensea.io/assets/{slug}/{contract}"


def explorer_url(chain: str, contract: str) -> str | None:
    """Block-explorer link for the contract address."""
    if chain.startswith("eth"):
        return f"https://etherscan.io/address/{contract}"
    if chain.startswith("robinhood"):
        return f"https://robin.etherscan.io/address/{contract}"
    return None


def build_embed(*, name: str, chain: str, contract: str,
                wallets: dict[str, str], hot: bool = False, kind: str = "mint",
                currencies: dict[str, str] | None = None) -> dict:
    """Build the Discord embed for an "N wallets minting/buying a collection" alert.

    ``kind`` is "mint" or "buy". ``currencies`` maps address -> "ETH"/"WETH" for
    buys. ``hot=True`` (large signal, with a role ping) marks it with a flame.
    """
    currencies = currencies or {}
    by_type = Counter(wallets.values())
    n = len(wallets)
    breakdown = " · ".join(f"{_pretty(t)} {c}" for t, c in by_type.most_common())
    dominant = by_type.most_common(1)[0][0] if by_type else "TRACKED"
    os_url = opensea_url(chain, contract)
    exp = explorer_url(chain, contract)
    # payment split for buys: ETH (sweep) vs WETH (accepted offer)
    pay = ""
    if kind == "buy" and currencies:
        cc = Counter(currencies.values())
        parts = []
        if cc.get("ETH"):
            parts.append(f"{cc['ETH']} Ξ")
        if cc.get("WETH"):
            parts.append(f"{cc['WETH']} WETH")
        if parts:
            pay = "\n💰 " + " · ".join(parts)
    # up to 10 addresses in the body; mark WETH buyers with (W)
    def _mark(a: str) -> str:
        return " (W)" if currencies.get(a) == "WETH" else ""
    sample = " · ".join(
        f"`{a[:6]}…{a[-4:]}` {_pretty(t)}{_mark(a)}" for a, t in list(wallets.items())[:10]
    )
    more = f" · … +{n - 10} more" if n > 10 else ""
    verb = "Minting" if kind == "mint" else "Buying"
    icon = "🔥" if hot else ("🌱" if kind == "mint" else "🛒")
    # links together at the bottom
    links = f"🔗 [View Collection]({os_url})" + (f" · 🔎 [Explorer]({exp})" if exp else "")
    return {
        # embed title = bold first line with the collection name
        "title": f"{icon} {n} Wallet {verb} {name}",
        "url": os_url,
        "description": f"{breakdown}{pay}\n\n{sample}{more}\n\n{links}",
        "color": TYPE_COLOR.get(dominant, DEFAULT_COLOR),
        "footer": {"text": "Wallet tracker"},
    }


def post_alert(webhook_url: str, embed: dict, *, role_id: str | None = None,
               timeout: float = 15.0) -> bool:
    """Send the embed to the channel. ``role_id`` pings a role, or "everyone"/
    "here" pings @everyone/@here."""
    content = ""
    allowed: dict = {"parse": []}
    if role_id:
        rid = role_id.strip().lstrip("@").lower()
        if rid == "everyone":
            content, allowed = "@everyone", {"parse": ["everyone"]}
        elif rid == "here":
            content, allowed = "@here", {"parse": ["everyone"]}
        else:
            content, allowed = f"<@&{role_id}>", {"parse": ["roles"]}
    payload = {"content": content, "embeds": [embed], "allowed_mentions": allowed}
    resp = requests.post(webhook_url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return True
