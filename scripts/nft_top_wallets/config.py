"""Configuration for the top-wallets collector.

All secrets come ONLY from the environment (see .env.example). Nothing secret
is committed to code/git.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # python-dotenv is optional if vars are already in the env
    pass


# --- Zero address: transfer source = mint ---
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


# ---------------------------------------------------------------------------
# Alchemy networks
# ---------------------------------------------------------------------------
# Collection chain. The endpoint is passed whole (with the key) in
# ALCHEMY_RPC_URL, or built from ALCHEMY_NETWORK + ALCHEMY_API_KEY.
def _network_base(network: str, api_key: str) -> str:
    return f"https://{network}.g.alchemy.com"


@dataclass
class NetworkConfig:
    """One Alchemy endpoint (RPC + NFT API share a host)."""

    name: str          # e.g. "robinhood-mainnet"
    base_url: str      # e.g. "https://robinhood-mainnet.g.alchemy.com"
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
        """Build a network config from the environment.

        Priority: the full URL in ``url_var`` (e.g. ALCHEMY_RPC_URL), otherwise
        ``network`` + a shared key.
        """
        full = os.getenv(url_var, "").strip()
        if full:
            # Format: https://<net>.g.alchemy.com/v2/<key>
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
# General run configuration
# ---------------------------------------------------------------------------
@dataclass
class Settings:
    # Collection chain (Robinhood mainnet)
    chain: NetworkConfig
    # Chain for blue-chip enrichment (Ethereum mainnet); None => smart-money
    # degrades to signals within the collection chain.
    mainnet: "NetworkConfig | None"

    # Default collection contract (can be overridden via CLI)
    default_contract: str = os.getenv(
        "COLLECTION_CONTRACT", "0x116eaa62241751e0c98da43d458600c6c17cd361"
    )

    # Throttling / retries
    request_delay_s: float = float(os.getenv("REQUEST_DELAY_S", "0.12"))
    max_retries: int = int(os.getenv("MAX_RETRIES", "5"))
    backoff_base_s: float = float(os.getenv("BACKOFF_BASE_S", "1.5"))
    timeout_s: float = float(os.getenv("REQUEST_TIMEOUT_S", "30"))

    # "Early" entry: fraction of earliest-by-time acquirers flagged as early_buyer
    early_buyer_fraction: float = float(os.getenv("EARLY_BUYER_FRACTION", "0.15"))

    # Scoring weights (normalized signals 0..1)
    # Three groups: smart (blue-chip + whale balance), degen (flips), early (mint/early).
    weights: dict = field(
        default_factory=lambda: {
            "smart": float(os.getenv("W_SMART", "0.50")),
            "degen": float(os.getenv("W_DEGEN", "0.25")),
            "early": float(os.getenv("W_EARLY", "0.25")),
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
                "No Alchemy access configured. Set ALCHEMY_RPC_URL "
                "(full endpoint with key) or ALCHEMY_API_KEY + ALCHEMY_NETWORK."
            )

        mainnet = NetworkConfig.from_env(
            url_var="ALCHEMY_MAINNET_RPC_URL",
            network_var="ALCHEMY_MAINNET_NETWORK",
            default_network="eth-mainnet",
            api_key=os.getenv("ALCHEMY_MAINNET_API_KEY", api_key).strip(),
        )

        return cls(chain=chain, mainnet=mainnet)


# ---------------------------------------------------------------------------
# Curated lists (extendable)
# ---------------------------------------------------------------------------
# Blue-chip collections on Ethereum mainnet -> used to score "smart money".
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

# Paths to extendable CSV lists (address[,label]); used if present.
KOL_LIST_PATH = os.getenv(
    "KOL_LIST_PATH",
    os.path.join(os.path.dirname(__file__), "lists", "kol_wallets.csv"),
)
SMART_MONEY_LIST_PATH = os.getenv(
    "SMART_MONEY_LIST_PATH",
    os.path.join(os.path.dirname(__file__), "lists", "smart_money_wallets.csv"),
)
