"""ENS reverse-resolution через сырые eth_call (Ethereum mainnet).

Резолвим адрес -> primary ENS-имя с обязательной forward-проверкой
(reverse-запись без forward-верификации может быть подделана). Имя используется
ТОЛЬКО для отображения и НЕ участвует в скоринге.
"""

from __future__ import annotations

from Crypto.Hash import keccak as _keccak

from .alchemy import AlchemyClient

# ENS Registry (одинаков во всех сетях с ENS)
REGISTRY = "0x00000000000C2E074eC69A0dFb2997BA6C7d2e1e"

# Селекторы функций
_SEL_RESOLVER = "0x0178b8bf"  # resolver(bytes32)
_SEL_NAME = "0x691f3431"      # name(bytes32)
_SEL_ADDR = "0x3b3b57de"      # addr(bytes32)


def _keccak256(data: bytes) -> bytes:
    h = _keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def namehash(name: str) -> bytes:
    node = b"\x00" * 32
    if name:
        for label in reversed(name.split(".")):
            node = _keccak256(node + _keccak256(label.encode("utf-8")))
    return node


def _resolver_of(client: AlchemyClient, node: bytes) -> str | None:
    res = client.eth_call(REGISTRY, _SEL_RESOLVER + node.hex())
    if not res or len(res) < 42:
        return None
    addr = "0x" + res[-40:]
    return None if int(addr, 16) == 0 else addr


def _decode_abi_string(hexstr: str) -> str:
    raw = hexstr[2:] if hexstr.startswith("0x") else hexstr
    try:
        b = bytes.fromhex(raw)
    except ValueError:
        return ""
    if len(b) < 64:
        return ""
    offset = int.from_bytes(b[:32], "big")
    if offset + 32 > len(b):
        return ""
    length = int.from_bytes(b[offset:offset + 32], "big")
    start = offset + 32
    return b[start:start + length].decode("utf-8", "replace")


def reverse_name(client: AlchemyClient, address: str) -> str:
    """Primary ENS-имя адреса (с forward-проверкой) или '' если нет/не сходится."""
    try:
        addr = address.lower().replace("0x", "")
        rev_node = namehash(f"{addr}.addr.reverse")
        resolver = _resolver_of(client, rev_node)
        if not resolver:
            return ""
        name = _decode_abi_string(client.eth_call(resolver, _SEL_NAME + rev_node.hex()))
        if not name or "." not in name:
            return ""
        # forward-проверка: name -> addr должен вернуть исходный адрес
        fwd_node = namehash(name)
        fwd_resolver = _resolver_of(client, fwd_node)
        if not fwd_resolver:
            return ""
        res = client.eth_call(fwd_resolver, _SEL_ADDR + fwd_node.hex())
        if not res or len(res) < 42:
            return ""
        resolved = "0x" + res[-40:]
        return name if resolved.lower() == address.lower() else ""
    except Exception:  # noqa: BLE001 — резолвинг best-effort, не должен ронять прогон
        return ""


def resolve_many(client: AlchemyClient, addresses: list[str]) -> dict[str, str]:
    """Зарезолвить список адресов -> {address: ens_name} (пустые пропускаются)."""
    out: dict[str, str] = {}
    for addr in addresses:
        name = reverse_name(client, addr)
        if name:
            out[addr] = name
    return out
