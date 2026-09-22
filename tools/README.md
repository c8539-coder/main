# NFT earnings tools

Расчёт заработка (realized profit) кошельков по NFT-коллекции на Robinhood-чейне.

## Файлы
- `nft_earnings.py` — основной калькулятор (single wallet + вся коллекция → CSV).
- `probe_wallet.py` — диагностика: печатает сырые данные по одному кошельку.
- `../notebooks/nft_earnings_colab.ipynb` — то же самое в Google Colab (сеть там открыта).

## Модель
```
total_invested  = ETH+ERC20, потраченные на покупки/минты
total_sales     = ETH+ERC20, полученные за продажи
realized_profit = total_sales - total_invested
```
Цена сделки = нетто-дельта ETH+ERC20 по кошельку в транзакции (маркетплейс-агностик,
роялти/комиссии вычитаются сами). `--gas` дополнительно вычитает газ.

## Запуск
```bash
export RH_RPC="https://robinhood-mainnet.g.alchemy.com/v2/<KEY>"
# сверка по одному кошельку
python3 tools/nft_earnings.py --wallet 0xb180e3fde77c0d4499a934014f437e8d442fd8a8
# вся коллекция -> CSV
python3 tools/nft_earnings.py --collection --out earnings.csv
```

## Эталон для сверки (кошелёк 0xb180…d8a8, коллекция Robinhood Kitties)
BOUGHT 37 / SOLD 33 / HOLDING 4 · INVESTED 0.5263 · SALES 0.6556 · REALIZED +0.1292 (24.55%)

## Сеть
Требуется доступ к `robinhood-mainnet.g.alchemy.com` (и опц. `api.opensea.io` для floor).
В облачном окружении Claude Code добавь эти домены в Custom → Allowed domains.
