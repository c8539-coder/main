# Discord daily digest

Раз в день собирает сообщения одного Discord-канала за 24ч, суммаризирует через
Claude и постит дайджест в твой Discord-канал (webhook). Крутится бесплатно на
GitHub Actions — свой сервер не нужен.

## Настройка (5 шагов)

1. **Создай бота:** https://discord.com/developers/applications → New Application →
   вкладка **Bot** → Reset Token (это `DISCORD_BOT_TOKEN`). Там же включи
   **Message Content Intent**.
2. **Добавь бота в сервер** с нужным каналом (OAuth2 → URL Generator → scope `bot`,
   права **View Channel** + **Read Message History**). Нужны права администратора сервера.
3. **ID канала:** в Discord включи Developer Mode (Settings → Advanced), правый клик
   по каналу → Copy Channel ID (`DISCORD_CHANNEL_ID`).
4. **Webhook для дайджеста:** в СВО�ём канале (куда хочешь получать выводы) →
   Edit Channel → Integrations → Webhooks → New Webhook → Copy URL (`DISCORD_WEBHOOK_URL`).
5. **Anthropic ключ:** `ANTHROPIC_API_KEY`.

## Запуск на GitHub Actions

Положи секреты в репо: Settings → Secrets and variables → Actions → New repository secret:
`DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, `DISCORD_WEBHOOK_URL`, `ANTHROPIC_API_KEY`.
(Опц.) Variable `DIGEST_MODEL` = `claude-haiku-5-5`, чтобы было дешевле.

Workflow (`.github/workflows/discord-digest.yml`) должен лежать в **дефолтной ветке**,
иначе расписание не запустится. Время правится в `cron:` (UTC). Кнопка **Run workflow**
во вкладке Actions — для ручного теста.

## Локальный запуск / тест

```bash
pip install "anthropic>=0.40,<1"
export DISCORD_BOT_TOKEN=... DISCORD_CHANNEL_ID=... ANTHROPIC_API_KEY=...
# без webhook — просто печатает дайджест в консоль
python digest/discord_digest.py
```

## Стоимость

День чата (~до 50k токенов) → summary:
- `claude-opus-5-5` (умнее): ~$0.2–0.25/день
- `claude-haiku-5-5` (дешевле, для простой суммаризации хватает): ~$0.01/день
