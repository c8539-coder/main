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


def _fmt_price(x: float) -> str:
    """Compact ETH amount: 0.002, 1.5, 0.00012 — no trailing zeros."""
    s = format(float(x), ".4g")
    if "e" in s or "E" in s:  # avoid scientific notation for tiny values
        s = f"{float(x):.6f}".rstrip("0").rstrip(".")
    return s


def build_embed(*, name: str, chain: str, contract: str,
                wallets: dict[str, str], hot: bool = False, kind: str = "mint",
                currencies: dict[str, str] | None = None,
                prices: dict[str, float] | None = None) -> dict:
    """Build the Discord embed for an "N wallets minting/buying a collection" alert.

    ``kind`` is "mint" or "buy". ``currencies`` maps address -> "ETH"/"WETH" and
    ``prices`` maps address -> price in ETH (per buy). ``hot=True`` (large
    signal, with a role ping) marks it with a flame.
    """
    currencies = currencies or {}
    prices = prices or {}
    by_type = Counter(wallets.values())
    n = len(wallets)
    breakdown = " · ".join(f"{_pretty(t)} {c}" for t, c in by_type.most_common())
    dominant = by_type.most_common(1)[0][0] if by_type else "TRACKED"
    os_url = opensea_url(chain, contract)
    exp = explorer_url(chain, contract)
    # real price paid (per buy). Show a single value or a min–max range.
    pay = ""
    if kind == "buy":
        vals = sorted(p for p in prices.values() if p and p > 0)
        if vals:
            lo, hi = _fmt_price(vals[0]), _fmt_price(vals[-1])
            span = lo if lo == hi else f"{lo}–{hi}"
            has_weth = "WETH" in currencies.values()
            pay = f"\n💰 {span} Ξ" + (" · incl. WETH" if has_weth else "")
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


def clean_webhook_url(url: str) -> str:
    """Normalize a Discord webhook URL from common copy/paste mistakes.

    Fixes: surrounding quotes/spaces, a trailing slash, a trailing ``/messages``
    (that path rejects POST -> 405), and a missing https scheme.
    """
    u = (url or "").strip().strip('"').strip("'").strip()
    if u.startswith("http://"):
        u = "https://" + u[len("http://"):]
    elif u and not u.startswith("https://"):
        u = "https://" + u
    u = u.rstrip("/")
    if u.endswith("/messages"):
        u = u[: -len("/messages")]
    return u


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
    url = clean_webhook_url(webhook_url)
    resp = requests.post(url, json=payload, timeout=timeout, allow_redirects=False)
    if resp.status_code >= 300:
        # surface Discord's own explanation (e.g. wrong URL shape) in the error
        raise RuntimeError(
            f"Discord webhook {resp.status_code}: {resp.text[:200]} "
            f"(url tail: …{url[-24:]})"
        )
    return True
