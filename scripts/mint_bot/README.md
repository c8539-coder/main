# Mint Tracker (Discord)

Watches **our tracked wallets** (`good_wallets.csv` etc.) on robinhood-mainnet
and Ethereum. When **≥ N** tracked wallets MINT or BUY the same collection in a
short window, it posts an alert to a Discord channel via webhook, with a
per-type breakdown (SMART / DEGEN / EARLY) and an optional role ping.

```
🌱 6 Wallet Minting Quantum Echoes
Smart 3 · Degen 3
0x51bc…66d2 Smart · 0xb9d8…1bac Degen · …
🔗 View Collection · 🔎 Explorer
```

## How it works

- **Mints** — every `MINT_POLL_SECONDS` it polls `alchemy_getAssetTransfers`
  (mints = transfers `from 0x0`) for new blocks on each chain, and keeps only
  mints to watchlist addresses.
- **Buys** — with an `OPENSEA_API_KEY` set, buys come from the **OpenSea Stream
  API** (websocket): strictly OpenSea sales, in real time, with the exact price
  and payment token (ETH vs WETH). Without a key it falls back to on-chain
  Seaport-confirmed sales (`SALES_ONLY`), which also catches Blur/X2Y2.
- It counts how many **distinct** tracked wallets hit the same collection within
  `MINT_WINDOW_SECONDS`. At `MINT_ALERT_MIN` wallets → a quiet alert (no ping).
  At `MINT_PING_MIN` → a second alert **with a role ping** (🔥), then again every
  `MINT_PING_STEP` more wallets.

State (last block + accumulations) is stored in `STATE_FILE` and survives a
restart. Buyers paying in WETH are marked `(W)` in the alert.

## Setup

1. In the target channel: **Channel Settings → Integrations → Webhooks → New
   Webhook → Copy URL** → set `DISCORD_WEBHOOK_URL`.
2. (Optional) For a role ping, enable Developer Mode, right-click the role →
   Copy ID → `DISCORD_ROLE_ID` (or use `everyone` / `here`).
3. Alchemy keys — the same ones the wallet collector uses (`ALCHEMY_RPC_URL`,
   `ALCHEMY_MAINNET_RPC_URL`).
4. OpenSea key (recommended for buys) → `OPENSEA_API_KEY`.

```bash
cp scripts/mint_bot/.env.example .env   # fill in webhook + keys
pip install -r scripts/mint_bot/requirements.txt
python -m scripts.mint_bot.main
python -m scripts.mint_bot.main --test  # post one sample alert and exit
```

## Watchlist

Defaults to `scripts/nft_top_wallets/out/good_wallets.csv` (columns `address`,
`type`). Multiple files / globs via `WATCHLIST_FILES`; filter by score with
`WATCHLIST_MIN_SCORE`.

## Main settings (env)

| Variable | Default | What it does |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | — | channel webhook URL (required) |
| `DISCORD_ROLE_ID` | — | role to ping, or `everyone`/`here` (optional) |
| `OPENSEA_API_KEY` | — | OpenSea Stream for strict real-time buys (recommended) |
| `MINT_ALERT_MIN` | 5 | quiet-alert threshold (no ping) |
| `MINT_PING_MIN` | 15 | wallet count that triggers a role ping |
| `MINT_PING_STEP` | 15 | re-ping every this many extra wallets |
| `TRACK_BUYS` | 1 | also alert on buys/sweeps (0 = mints only) |
| `SALES_ONLY` | 1 | on-chain fallback: confirmed sales only |
| `MINT_CHAINS` | robinhood-mainnet,eth-mainnet | chains to watch |
| `MINT_POLL_SECONDS` | 60 | mint poll interval |
| `MINT_WINDOW_SECONDS` | 21600 | per-collection accumulation window |
| `MINT_BACKFILL_BLOCKS` | 300 | blocks to scan on first start |
| `WATCHLIST_FILES` | out/good_wallets.csv | where wallets come from |

## Notes

- The `OPENSEA_API_KEY` is a secret — keep it in `.env` (gitignored), never
  commit it.
- With OpenSea enabled, the on-chain poll only handles mints; buys arrive over
  the websocket, so `MINT_POLL_SECONDS` no longer gates buy latency.
- Collection name for a buy comes from the OpenSea event; for a mint it comes
  from `getContractMetadata` (cached per contract).
- Links: **View Collection** (OpenSea) and an **Explorer** link (Etherscan for
  eth, robin.etherscan.io for robinhood).
