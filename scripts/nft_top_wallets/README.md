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
| **smart money** | blue-chip NFT: **разнообразие коллекций** (важнее) + лог-число NFT | `getNFTsForOwner` по blue-chip контрактам на Ethereum mainnet |
| **whale** | нативный баланс (лог-шкала) | `eth_getBalance` на mainnet |
| **degen** | buy-and-flip + mint-and-flip, число флипов, реализованный PnL | `getNFTSales` (fallback: out-трансферы, PnL=0) |
| **early** | минтеры (`from = 0x0`, 0.6) + первые ~15% покупателей (1.0) | `getAssetTransfers` (order=asc) по контракту |
| **KOL** | join с curated-списком адресов | `lists/kol_wallets.csv` |

Итоговый `total_score` (0..100) — взвешенная сумма нормализованных сигналов
(веса в `config.py` или через env `W_*`). Сигналы непрерывные там, где можно
(blue-chip, баланс, флипы), чтобы топ не «слипался» в одинаковые значения.

> **Если сеть без `getNFTSales`** (напр. robinhood-mainnet), PnL посчитать нельзя,
> поэтому его вес **перераспределяется** на остальные сигналы — иначе шкала теряет
> пятую часть диапазона. Флипы в этом случае считаются по out-трансферам.

**ENS.** Для top-N кошельков резолвится primary ENS-имя (reverse + forward-проверка,
`--ens N`). Имя идёт **только в отображение** и на скор не влияет.

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
- `--no-mainnet` — не ходить на eth-mainnet (smart-money/whale=0).
- `--ens N` — резолвить ENS-имена для top-N кошельков (0 = выключить; по умолчанию 100).

## Дашборд (HTML)

Наглядный ранжированный дашборд собирается из CSV:

```bash
# берёт свежайший CSV из out/ автоматически
python -m scripts.nft_top_wallets.dashboard
# или явно
python -m scripts.nft_top_wallets.dashboard --csv scripts/nft_top_wallets/out/top_wallets_*.csv --top 50
```

Самодостаточный `out/dashboard.html`: KPI, распределение скора, разбор каждого
кошелька по 5 компонентам (наведи на полосу composition), фильтры и сортировка.
Шаблон — `templates/dashboard.html` (данные подставляются в плейсхолдер).

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
