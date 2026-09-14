"""Loading and validating configuration from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    discord_token: str
    opensea_api_key: str
    poll_interval_seconds: int
    data_file: str
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        discord_token = os.getenv("DISCORD_TOKEN", "").strip()
        opensea_api_key = os.getenv("OPENSEA_API_KEY", "").strip()

        missing = []
        if not discord_token:
            missing.append("DISCORD_TOKEN")
        if not opensea_api_key:
            missing.append("OPENSEA_API_KEY")
        if missing:
            raise SystemExit(
                "Отсутствуют обязательные переменные окружения: "
                + ", ".join(missing)
                + ".\nСкопируйте .env.example в .env и заполните значения."
            )

        try:
            poll = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
        except ValueError:
            poll = 60
        poll = max(poll, 15)  # не бомбим API чаще раза в 15 секунд

        return cls(
            discord_token=discord_token,
            opensea_api_key=opensea_api_key,
            poll_interval_seconds=poll,
            data_file=os.getenv("DATA_FILE", "data/subscriptions.json").strip(),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        )
