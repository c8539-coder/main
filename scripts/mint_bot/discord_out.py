"""Отправка алерта о минте в Discord через webhook канала.

Webhook проще бота: не нужен токен/intents, только URL вебхука канала
(Настройки канала -> Integrations -> Webhooks -> New Webhook -> Copy URL).
"""

from __future__ import annotations

from collections import Counter

import requests

# Цвета типов (в стиле дашборда)
TYPE_COLOR = {"SMART": 0x3987E5, "DEGEN": 0xD95926, "EARLY": 0x199E70}
DEFAULT_COLOR = 0xE6AD55

# Красивое отображение типов (без капса)
TYPE_LABEL = {"SMART": "Smart", "DEGEN": "Degen", "EARLY": "Early"}

# Слаг сети для OpenSea
OPENSEA_SLUG = {"eth-mainnet": "ethereum", "robinhood-mainnet": "robinhood"}


def _pretty(t: str) -> str:
    return TYPE_LABEL.get(t.upper(), t.title())


def opensea_url(chain: str, contract: str) -> str:
    slug = OPENSEA_SLUG.get(chain, chain)
    return f"https://opensea.io/assets/{slug}/{contract}"


def explorer_url(chain: str, contract: str) -> str | None:
    if chain.startswith("eth"):
        return f"https://etherscan.io/address/{contract}"
    return None


def build_embed(*, name: str, chain: str, contract: str,
                wallets: dict[str, str], hot: bool = False) -> dict:
    """Собрать Discord-embed под алерт «N кошельков минтят коллекцию».

    ``hot=True`` (крупный сигнал, с пингом роли) помечает алерт огоньком.
    """
    by_type = Counter(wallets.values())
    n = len(wallets)
    breakdown = " · ".join(f"{_pretty(t)} {c}" for t, c in by_type.most_common())
    dominant = by_type.most_common(1)[0][0] if by_type else "TRACKED"
    # ссылки
    os_url = opensea_url(chain, contract)
    exp = explorer_url(chain, contract)
    links = f"[OpenSea]({os_url})" + (f" · [Explorer]({exp})" if exp else "")
    # до 10 адресов в тело
    sample = " · ".join(
        f"`{a[:6]}…{a[-4:]}` {_pretty(t)}" for a, t in list(wallets.items())[:10]
    )
    more = f" · … и ещё {n - 10}" if n > 10 else ""
    icon = "🔥" if hot else "🌱"
    return {
        "title": f"{icon} {n} Wallet Minting {name}",
        "description": f"**{breakdown}**\n{links}\n\n{sample}{more}",
        "url": os_url,
        "color": TYPE_COLOR.get(dominant, DEFAULT_COLOR),
        "fields": [
            {"name": "Chain", "value": chain, "inline": True},
            {"name": "Contract", "value": f"`{contract}`", "inline": False},
        ],
        "footer": {"text": "Wallet mint tracker"},
    }


def post_alert(webhook_url: str, embed: dict, *, role_id: str | None = None,
               timeout: float = 15.0) -> bool:
    """Отправить embed в канал. При ``role_id`` пингует роль. True при успехе."""
    content = f"<@&{role_id}>" if role_id else ""
    payload = {
        "content": content,
        "embeds": [embed],
        "allowed_mentions": {"parse": ["roles"]} if role_id else {"parse": []},
    }
    resp = requests.post(webhook_url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return True
