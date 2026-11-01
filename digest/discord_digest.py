#!/usr/bin/env python3
"""Дневной дайджест одного Discord-канала.

Раз в день: тянет сообщения канала за последние N часов через Discord REST,
суммаризирует через Claude API и постит дайджест в Discord-webhook (твой личный
канал). Рассчитан на запуск по расписанию (GitHub Actions cron или локальный cron).

ENV:
  DISCORD_BOT_TOKEN   — токен бота (нужны права View Channel + Read Message History,
                        включённый Message Content Intent)
  DISCORD_CHANNEL_ID  — ID канала для анализа
  DISCORD_WEBHOOK_URL — webhook твоего канала, куда слать дайджест
  ANTHROPIC_API_KEY   — ключ Claude
  MODEL               — (опц.) claude-opus-5-5 (дефолт) или claude-haiku-5-5 (дешевле)
  HOURS               — (опц.) окно в часах, по умолчанию 24
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

API = "https://discord.com/api/v10"
MODEL = os.environ.get("MODEL", "claude-opus-5-5")
HOURS = int(os.environ.get("HOURS", "24"))


def discord_get(path: str, token: str, params: dict | None = None):
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bot {token}", "User-Agent": "DigestBot/1.0"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limit
                retry = float(e.headers.get("Retry-After", "1"))
                time.sleep(retry + 0.5)
                continue
            raise RuntimeError(f"Discord {e.code}: {e.read()[:200]!r}") from e
    raise RuntimeError("discord: too many retries")


import urllib.parse  # noqa: E402


def fetch_messages(token: str, channel_id: str, hours: int) -> list[dict]:
    """Все сообщения канала за последние `hours` часов (новые Discord отдаёт первыми)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out, before = [], None
    while True:
        params = {"limit": 100}
        if before:
            params["before"] = before
        batch = discord_get(f"/channels/{channel_id}/messages", token, params)
        if not batch:
            break
        stop = False
        for m in batch:
            ts = datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00"))
            if ts < cutoff:
                stop = True
                break
            out.append(m)
        before = batch[-1]["id"]
        if stop or len(batch) < 100:
            break
    out.reverse()  # хронологический порядок
    return out


def render_transcript(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        author = (m.get("author") or {}).get("username", "?")
        ts = m["timestamp"][11:16]
        content = m.get("content", "").strip()
        # вложения/эмбеды — пометим, чтобы модель видела что был контент
        extra = []
        if m.get("attachments"):
            extra.append(f"[{len(m['attachments'])} вложений]")
        if m.get("embeds"):
            extra.append(f"[{len(m['embeds'])} эмбедов]")
        if not content and not extra:
            continue
        lines.append(f"[{ts}] {author}: {content} {' '.join(extra)}".rstrip())
    return "\n".join(lines)


SYSTEM = (
    "Ты — аналитик крипто/NFT альфа-чата. Тебе дают лог сообщений Discord-канала за день. "
    "Сделай сжатый дайджест на русском: только то, что важно для трейдера/коллекционера. "
    "Формат:\n"
    "**📌 Главное** — 3-6 ключевых пунктов (анонсы, минты, партнёрства, запуски).\n"
    "**🪙 Проекты/коллекции** — что упоминали и в каком ключе.\n"
    "**🧭 Настроение** — общий тон (буллиш/медвежий/нейтрально) и почему.\n"
    "**✅ Что сделать** — конкретные действия, если из чата следуют (застолбить минт, глянуть коллекцию, дедлайны).\n"
    "Без воды, без пересказа флуда. Если за день ничего существенного — так и скажи одной строкой."
)


def summarize(transcript: str) -> str:
    import anthropic
    client = anthropic.Anthropic()
    if not transcript.strip():
        return "За последние 24ч в канале не было содержательных сообщений."
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM,
        messages=[{"role": "user", "content":
                   f"Лог сообщений за ~{HOURS}ч:\n\n{transcript[:180000]}"}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def post_webhook(url: str, text: str, title: str):
    # Discord лимит 2000 символов на сообщение -> режем на части
    header = f"🗞️ **{title}**\n"
    chunks, cur = [], header
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 1900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for part in chunks:
        body = json.dumps({"content": part}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30).read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(float(e.headers.get("Retry-After", "1")) + 0.5)
                urllib.request.urlopen(req, timeout=30).read()
            else:
                raise
        time.sleep(0.4)


def main():
    token = os.environ["DISCORD_BOT_TOKEN"]
    channel = os.environ["DISCORD_CHANNEL_ID"]
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    msgs = fetch_messages(token, channel, HOURS)
    print(f"сообщений за {HOURS}ч: {len(msgs)}", file=sys.stderr)
    transcript = render_transcript(msgs)
    digest = summarize(transcript)
    title = f"Дайджест · {datetime.now(timezone.utc).strftime('%d.%m.%Y')} · {len(msgs)} сообщений"
    if webhook:
        post_webhook(webhook, digest, title)
        print("дайджест отправлен в webhook", file=sys.stderr)
    else:
        print(f"\n=== {title} ===\n{digest}")


if __name__ == "__main__":
    main()
