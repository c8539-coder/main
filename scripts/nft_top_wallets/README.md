# NFT Top Wallets

Собирает холдеров NFT-коллекции через **Alchemy API**, обогащает каждый кошелёк
сигналами (early / degen / smart money / KOL) и выдаёт ранжированный список
топ-кошельков в CSV.

Логика: участие в свежем минте само по себе ничего не значит — среди холдеров много
ботов, флипперов и сибил-кластеров. Коллекция здесь это *воронка входа*, а «хорошие»
кошельки получаются **обогащением истории** каждого адреса.

## Сигналы

Три группы (веса в `config.py` или через env `W_SMART`/`W_DEGEN`/`W_EARLY`):

| Группа | Что считаем | Источник |
|---|---|---|
| **smart** (50%) | blue-chip: **разнообразие коллекций** (важнее) + лог-число NFT; **баланс кита** (ETH); **conviction** — сколько NFT самой коллекции держит (сверх 1-й) | `getNFTsForOwner` + `eth_getBalance` на Ethereum mainnet + `tokens_held` |
| **degen** (25%) | число флипов (buy-and-flip / mint-and-flip) | `getNFTSales` (fallback: out-трансферы) |
| **early** (25%) | минтеры (`from = 0x0`, 0.6) + первые ~15% покупателей (1.0) | `getAssetTransfers` (order=asc) по контракту |

Итоговый `total_score` (0..100) — взвешенная сумма нормализованных сигналов.
Киты входят в **smart** (баланс = часть группы; доля blue-chip vs баланс —
`SMART_BLUECHIP_SHARE`, по умолчанию 0.6). Сигналы непрерывные, чтобы топ не
«слипался» в одинаковые значения. Логика скоринга — в `enrich.signals()` /
`enrich.score_components()` (используется и CLI, и дашбордом).

> На сети без `getNFTSales` (напр. robinhood-mainnet) флипы считаются по
> out-трансферам (приблизительно). PnL в скоринге не участвует.

### Early = по времени, не по цене

Сигнал `early` считается **по порядку трансферов**, а не по уплаченной цене:
кошелёк засчитывается, если он минтер (`from = 0x0`) или попал в первые
`EARLY_BUYER_FRACTION` (15%) не-минтовых покупателей по времени.

**Команда/трежери исключается.** Порог **адаптивный**: кошелёк помечается
`is_team`, если заминтил `>= max(TEAM_MINT_MIN, TEAM_SUPPLY_SHARE * весь_минт)`
(по умолчанию `max(10, 1% саплая)`). Доля от саплая не даёт ложно пометить
обычных минтеров в открытых минтах, где легально минтят помногу — команда это
только те, кто забрал заметную долю коллекции. Такой кошелёк не получает
early-кредита и убирается из лидерборда (в CSV остаётся с `is_team=1`).
Вернуть в вывод — флагом `--keep-team`.

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
