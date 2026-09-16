# Mint Tracker (Discord)

Следит за минтами **наших отслеживаемых кошельков** (`good_wallets.csv` и т.п.)
на robinhood-mainnet и Ethereum. Когда **≥ N** трекнутых кошельков заминтили одну
коллекцию — шлёт алерт в Discord-канал через webhook, с разбивкой по типам
(SMART / DEGEN / EARLY) и пингом роли.

```
🌱 6 Wallet Minting Quantum Echoes
SMART 3 · DEGEN 3
0x51bc…66d2 SMART · 0xb9d8…1bac DEGEN · …
```

## Как это работает

1. Каждые `MINT_POLL_SECONDS` опрашивает `alchemy_getAssetTransfers` (минты =
   трансферы `from 0x0`) по новым блокам на каждой сети.
2. Оставляет только минты на адреса из watchlist.
3. Копит по контракту, сколько **разных** наших кошельков его заминтили (в окне
   `MINT_WINDOW_SECONDS`).
4. При `MINT_ALERT_MIN` кошельках — **тихий** алерт (без пинга). Если контракт
   дорастает до `MINT_PING_MIN` — второй алерт уже **с пингом роли** (🔥).

Состояние (последний блок + накопления) хранится в `STATE_FILE` и переживает
перезапуск.

## Настройка

1. В нужном канале: **Настройки канала → Integrations → Webhooks → New Webhook →
   Copy URL**. Вставь в `DISCORD_WEBHOOK_URL`.
2. (Опц.) Для пинга роли включи Developer Mode, ПКМ по роли → Copy ID →
   `DISCORD_ROLE_ID`.
3. Ключи Alchemy — те же, что у сборщика кошельков (`ALCHEMY_RPC_URL`,
   `ALCHEMY_MAINNET_RPC_URL`).

```bash
cp scripts/mint_bot/.env.example .env   # заполнить webhook + ключи
pip install -r scripts/nft_top_wallets/requirements.txt   # requests, python-dotenv
python -m scripts.mint_bot.main
```

## Watchlist

По умолчанию `scripts/nft_top_wallets/out/good_wallets.csv` (колонки `address`,
`type`). Можно указать несколько файлов и glob через `WATCHLIST_FILES`,
и фильтровать по `total_score` через `WATCHLIST_MIN_SCORE`.

## Основные настройки (env)

| Переменная | По умолчанию | Что делает |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | — | URL вебхука канала (обязательно) |
| `DISCORD_ROLE_ID` | — | роль для пинга (опц.) |
| `MINT_ALERT_MIN` | 5 | порог тихого алерта (без пинга) |
| `MINT_PING_MIN` | 15 | с какого числа кошельков пинговать роль |
| `MINT_CHAINS` | robinhood-mainnet,eth-mainnet | какие сети следить |
| `MINT_POLL_SECONDS` | 60 | период опроса |
| `MINT_WINDOW_SECONDS` | 21600 | окно накопления по контракту (6ч) |
| `WATCHLIST_FILES` | out/good_wallets.csv | откуда брать кошельки |

## Ограничения

- Робинхуд/eth **без OpenSea-ссылок на конкретный ассет** — ссылка ведёт на
  контракт (Etherscan для eth).
- Имя коллекции берётся из `getContractMetadata` (кешируется на контракт).
- Это outbound-алерты (webhook), не полноценный бот со слэш-командами.
