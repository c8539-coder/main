# NFT Top Wallets

Собирает холдеров NFT-коллекции через **Alchemy API**, обогащает каждый кошелёк
сигналами (early / degen / smart money / KOL) и выдаёт ранжированный список
топ-кошельков в CSV.

Логика: участие в свежем минте само по себе ничего не значит — среди холдеров много
ботов, флипперов и сибил-кластеров. Коллекция здесь это *воронка входа*, а «хорошие»
кошельки получаются **обогащением истории** каждого адреса.

## Сигналы

| Сигнал | Что считаем | Источник |
|---|---|---|
| **early** | минтеры (`from = 0x0`) + первые ~15% покупателей | `getAssetTransfers` (order=asc) по контракту |
| **degen** | buy-and-flip + mint-and-flip, число флипов, реализованный PnL | `getNFTSales` (fallback: out-трансферы) |
| **smart money** | blue-chip NFT + баланс на Ethereum mainnet (кросс-чейн) | `getNFTsForOwner`, `eth_getBalance` |
| **KOL** | join с curated-списком адресов (+ опц. ENS) | `lists/kol_wallets.csv` |

Итоговый `total_score` — взвешенная сумма нормализованных сигналов (веса в `config.py`
или через env `W_*`).

## Установка

```bash
pip install -r scripts/nft_top_wallets/requirements.txt
```

## Настройка

Скопируйте `.env.example` в `.env` и впишите Alchemy endpoint/ключ:

```bash
cp scripts/nft_top_wallets/.env.example scripts/nft_top_wallets/.env
```

> Ключ — только в `.env` (он в `.gitignore`) или в переменных окружения. В код/git не коммитить.

## Запуск

Из корня репозитория:

```bash
# smoke-тест: 50 кошельков
python -m scripts.nft_top_wallets.main --limit 50 --top 20

# полный прогон
python -m scripts.nft_top_wallets.main --top 200

# конкретный контракт, без mainnet-обогащения
python -m scripts.nft_top_wallets.main --contract 0x116e... --no-mainnet
```

CSV сохраняется в `scripts/nft_top_wallets/out/` (каталог в `.gitignore`).

### Флаги

- `--contract` — адрес контракта (по умолчанию из `COLLECTION_CONTRACT`).
- `--top N` — сколько строк показать в консоли (в CSV попадают все холдеры).
- `--limit N` — обогащать только первые N холдеров (экономия лимитов Alchemy).
- `--no-mainnet` — не ходить на eth-mainnet (smart-money=0).

## Curated-списки

`lists/kol_wallets.csv` и `lists/smart_money_wallets.csv` — пополняемые списки
`address,label` (например, экспорт меток из Nansen/Arkham). Совпадения добавляют
кошельку метку и поднимают его в скоринге.

## Ограничения

- **KOL** без off-chain данных определяется лишь частично (curated-список + ENS).
- Robinhood mainnet — свежая сеть; blue-chip репутация берётся с Ethereum mainnet по
  тому же адресу. Если `getNFTSales` там не поддержан — degen считается по out-трансферам,
  PnL=0.
- Сибил-кластеры искажают «топ»; полноценная кластеризация не входит в этот скрипт.
- Обогащение тысяч кошельков идёт минутами (rate limits Alchemy, троттлинг + бэкофф).
