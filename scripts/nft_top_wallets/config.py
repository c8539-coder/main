"""Конфигурация сборщика топ-кошельков.

Все секреты берутся ТОЛЬКО из окружения (см. .env.example). В код/git ничего
секретного не попадает.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # python-dotenv необязателен, если переменные уже в env
    pass


# --- Нулевой адрес: source транзакции = mint ---
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


# ---------------------------------------------------------------------------
# Сети Alchemy
# ---------------------------------------------------------------------------
# Основная сеть коллекции. Endpoint передаётся целиком (вместе с ключом) в
# ALCHEMY_RPC_URL, либо собирается из ALCHEMY_NETWORK + ALCHEMY_API_KEY.
def _network_base(network: str, api_key: str) -> str:
    return f"https://{network}.g.alchemy.com"


@dataclass
class NetworkConfig:
    """Один Alchemy-эндпоинт (RPC + NFT API живут на одном хосте)."""

    name: str          # напр. "robinhood-mainnet"
    base_url: str      # напр. "https://robinhood-mainnet.g.alchemy.com"
    api_key: str

    @property
    def rpc_url(self) -> str:
        return f"{self.base_url}/v2/{self.api_key}"

    @property
    def nft_url(self) -> str:
        return f"{self.base_url}/nft/v3/{self.api_key}"

    @classmethod
    def from_env(
        cls,
        *,
        url_var: str,
        network_var: str,
        default_network: str,
        api_key: str,
    ) -> "NetworkConfig | None":
        """Собрать конфиг сети из окружения.

        Приоритет: полный URL из ``url_var`` (напр. ALCHEMY_RPC_URL), иначе
        ``network`` + общий ключ.
        """
        full = os.getenv(url_var, "").strip()
        if full:
            # Формат: https://<net>.g.alchemy.com/v2/<key>
            base = full.split("/v2/")[0].split("/nft/")[0].rstrip("/")
            key = api_key
            if "/v2/" in full:
                key = full.split("/v2/")[1].split("/")[0] or api_key
            net = base.split("//")[-1].split(".")[0]
            if not key:
                return None
            return cls(name=net, base_url=base, api_key=key)

        network = os.getenv(network_var, default_network).strip()
        if not api_key:
            return None
        return cls(name=network, base_url=_network_base(network, api_key), api_key=api_key)


# ---------------------------------------------------------------------------
# Общая конфигурация запуска
# ---------------------------------------------------------------------------
@dataclass
class Settings:
    # Основная сеть коллекции (Robinhood mainnet)
    chain: NetworkConfig
    # Сеть для blue-chip обогащения (Ethereum mainnet); None => smart-money
    # деградирует до сигналов внутри основной сети.
    mainnet: "NetworkConfig | None"

    # Контракт коллекции по умолчанию (можно переопределить CLI)
    default_contract: str = os.getenv(
        "COLLECTION_CONTRACT", "0x116eaa62241751e0c98da43d458600c6c17cd361"
    )

    # Троттлинг / ретраи
    request_delay_s: float = float(os.getenv("REQUEST_DELAY_S", "0.12"))
    max_retries: int = int(os.getenv("MAX_RETRIES", "5"))
    backoff_base_s: float = float(os.getenv("BACKOFF_BASE_S", "1.5"))
    timeout_s: float = float(os.getenv("REQUEST_TIMEOUT_S", "30"))

    # "Ранний" вход: доля первых по времени приобретателей, помечаемых early_buyer
    early_buyer_fraction: float = float(os.getenv("EARLY_BUYER_FRACTION", "0.15"))

    # Веса скоринга (нормализованные сигналы 0..1)
    weights: dict = field(
        default_factory=lambda: {
            "bluechip": float(os.getenv("W_BLUECHIP", "0.30")),
            "degen": float(os.getenv("W_DEGEN", "0.20")),
            "early": float(os.getenv("W_EARLY", "0.15")),
            "kol": float(os.getenv("W_KOL", "0.15")),
            "pnl": float(os.getenv("W_PNL", "0.20")),
        }
    )

    @classmethod
    def load(cls) -> "Settings":
        api_key = os.getenv("ALCHEMY_API_KEY", "").strip()

        chain = NetworkConfig.from_env(
            url_var="ALCHEMY_RPC_URL",
            network_var="ALCHEMY_NETWORK",
            default_network="robinhood-mainnet",
            api_key=api_key,
        )
        if chain is None:
            raise SystemExit(
                "Не задан доступ к Alchemy. Укажите ALCHEMY_RPC_URL "
                "(полный endpoint с ключом) или ALCHEMY_API_KEY + ALCHEMY_NETWORK."
            )

        mainnet = NetworkConfig.from_env(
            url_var="ALCHEMY_MAINNET_RPC_URL",
            network_var="ALCHEMY_MAINNET_NETWORK",
            default_network="eth-mainnet",
            api_key=os.getenv("ALCHEMY_MAINNET_API_KEY", api_key).strip(),
        )

        return cls(chain=chain, mainnet=mainnet)


# ---------------------------------------------------------------------------
# Curated-списки (можно расширять)
# ---------------------------------------------------------------------------
# Blue-chip коллекции на Ethereum mainnet — по ним считаем "smart money".
BLUECHIP_CONTRACTS: dict[str, str] = {
    "0xbc4ca0eda7647a8ab7c2061c2e118a18a936f13d": "BAYC",
    "0x60e4d786628fea6478f785a6d7e704777c86a7c6": "MAYC",
    "0xb47e3cd837ddf8e4c57f05d70ab865de6e193bbb": "CryptoPunks",
    "0xed5af388653567af2f388e6224dc7c4b3241c544": "Azuki",
    "0xbd3531da5cf5857e7cfaa92426877b022e612cf8": "PudgyPenguins",
    "0x5af0d9827e0c53e4799bb226655a1de152a425a5": "Milady",
    "0x23581767a106ae21c074b2276d25e5c3e136a68b": "Moonbirds",
    "0x8a90cab2b38dba80c64b7734e58ee1db38b8992e": "Doodles",
    "0x49cf6f5d44e70224e2e23fdcdd2c053f30ada28b": "CloneX",
    "0x1a92f7381b9f03921564a437210bb9396471050c": "CoolCats",
}

# Пути к пополняемым CSV-спискам (address[,label]); используются, если существуют.
KOL_LIST_PATH = os.getenv(
    "KOL_LIST_PATH",
    os.path.join(os.path.dirname(__file__), "lists", "kol_wallets.csv"),
)
SMART_MONEY_LIST_PATH = os.getenv(
    "SMART_MONEY_LIST_PATH",
    os.path.join(os.path.dirname(__file__), "lists", "smart_money_wallets.csv"),
)
