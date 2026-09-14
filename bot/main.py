"""Entry point: Discord bot, slash commands and the background poller."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import tasks

from .config import Config
from .formatting import build_embed
from .opensea import NftEvent, OpenSeaClient, OpenSeaError
from .storage import Storage, Subscription

log = logging.getLogger(__name__)

EVENT_TYPE_CHOICES = [
    app_commands.Choice(name="Продажи", value="sale"),
    app_commands.Choice(name="Листинги (выставления)", value="listing"),
    app_commands.Choice(name="Всё (продажи + листинги)", value="all"),
]

# сколько событий за один опрос максимум обрабатывать на коллекцию,
# чтобы не залить канал при всплеске активности
MAX_EVENTS_PER_POLL = 15


class NftAlertBot(discord.Client):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.storage = Storage(config.data_file)
        self.opensea = OpenSeaClient(config.opensea_api_key)
        self._poller: tasks.Loop | None = None

    async def setup_hook(self) -> None:
        await self.storage.load()
        register_commands(self.tree, self)
        await self.tree.sync()
        # запускаем поллер с настроенным интервалом
        self._poller = tasks.loop(seconds=self.config.poll_interval_seconds)(
            self._poll_once
        )
        self._poller.before_loop(self._before_poll)
        self._poller.start()

    async def _before_poll(self) -> None:
        await self.wait_until_ready()

    async def close(self) -> None:
        if self._poller:
            self._poller.cancel()
        await self.opensea.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Бот вошёл как %s (id=%s)", self.user, getattr(self.user, "id", "?"))
        watched = self.storage.watched_collections()
        log.info("Отслеживается коллекций: %d", len(watched))

    # ---- poller ---------------------------------------------------------

    async def _poll_once(self) -> None:
        collections = self.storage.watched_collections()
        for slug in collections:
            try:
                await self._poll_collection(slug)
            except OpenSeaError as exc:
                log.warning("Опрос коллекции %s не удался: %s", slug, exc)
            except Exception:  # noqa: BLE001
                log.exception("Неожиданная ошибка при опросе %s", slug)

    async def _poll_collection(self, slug: str) -> None:
        cursor = self.storage.get_cursor(slug)
        # какие типы событий вообще нужны кому-то из подписчиков
        subs = self.storage.subscribers_of(slug)
        if not subs:
            return
        wants_all = any(s.event_type == "all" for s in subs)
        event_type = "all" if wants_all else _common_event_type(subs)

        events = await self.opensea.fetch_events(
            slug, event_type=event_type, after=cursor, limit=50
        )
        if not events:
            return

        events = events[-MAX_EVENTS_PER_POLL:]
        max_ts = cursor or 0
        for event in events:
            max_ts = max(max_ts, event.timestamp)
            await self._dispatch_event(slug, event)

        if max_ts:
            await self.storage.set_cursor(slug, max_ts)

    async def _dispatch_event(self, slug: str, event: NftEvent) -> None:
        embed = build_embed(event)
        for sub in self.storage.subscribers_of(slug):
            if not _event_matches(event, sub):
                continue
            channel = self.get_channel(sub.channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(sub.channel_id)
                except (discord.NotFound, discord.Forbidden):
                    log.warning("Канал %s недоступен, пропускаю", sub.channel_id)
                    continue
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                log.warning("Нет прав писать в канал %s", sub.channel_id)
            except discord.HTTPException as exc:
                log.warning("Не удалось отправить сообщение: %s", exc)


def _common_event_type(subs: list[Subscription]) -> str:
    types = {s.event_type for s in subs}
    if len(types) == 1:
        return next(iter(types))
    return "all"


def _event_matches(event: NftEvent, sub: Subscription) -> bool:
    if sub.event_type != "all" and event.event_type != sub.event_type:
        return False
    if sub.min_price and (event.price is None or event.price < sub.min_price):
        return False
    return True


# ---- slash commands -----------------------------------------------------


def register_commands(tree: app_commands.CommandTree, bot: NftAlertBot) -> None:
    @tree.command(name="nft_watch", description="Отслеживать коллекцию NFT в этом канале")
    @app_commands.describe(
        collection="Slug коллекции на OpenSea (из URL: opensea.io/collection/<slug>)",
        event_type="Какие события присылать (по умолчанию — продажи)",
        min_price="Минимальная цена события, чтобы прислать алерт (в ETH/нативной валюте)",
    )
    @app_commands.choices(event_type=EVENT_TYPE_CHOICES)
    async def nft_watch(
        interaction: discord.Interaction,
        collection: str,
        event_type: app_commands.Choice[str] | None = None,
        min_price: float | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        slug = collection.strip().lower()
        etype = event_type.value if event_type else "sale"

        if not await bot.opensea.collection_exists(slug):
            await interaction.followup.send(
                f"❌ Коллекция `{slug}` не найдена на OpenSea.\n"
                "Slug берётся из URL коллекции: "
                "`opensea.io/collection/`**`<slug>`**",
                ephemeral=True,
            )
            return

        sub = Subscription(
            channel_id=interaction.channel_id,
            collection=slug,
            event_type=etype,
            min_price=float(min_price or 0.0),
            added_by=interaction.user.id,
        )
        await bot.storage.add_subscription(sub)

        details = [f"тип: **{_etype_label(etype)}**"]
        if sub.min_price:
            details.append(f"мин. цена: **{sub.min_price:g}**")
        await interaction.followup.send(
            f"✅ Теперь слежу за **{slug}** в этом канале ({', '.join(details)}).\n"
            "Алерты придут при новых событиях.",
            ephemeral=True,
        )

    @tree.command(name="nft_unwatch", description="Перестать отслеживать коллекцию в этом канале")
    @app_commands.describe(collection="Slug коллекции, которую нужно убрать")
    async def nft_unwatch(interaction: discord.Interaction, collection: str) -> None:
        slug = collection.strip().lower()
        removed = await bot.storage.remove_subscription(interaction.channel_id, slug)
        if removed:
            await interaction.response.send_message(
                f"🗑️ Больше не слежу за **{slug}** в этом канале.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"ℹ️ Коллекция **{slug}** и так не отслеживалась в этом канале.",
                ephemeral=True,
            )

    @tree.command(name="nft_list", description="Показать коллекции, отслеживаемые в этом канале")
    async def nft_list(interaction: discord.Interaction) -> None:
        subs = bot.storage.list_for_channel(interaction.channel_id)
        if not subs:
            await interaction.response.send_message(
                "В этом канале пока нет отслеживаемых коллекций. "
                "Добавьте через `/nft_watch`.",
                ephemeral=True,
            )
            return
        lines = []
        for s in subs:
            extra = [f"{_etype_label(s.event_type)}"]
            if s.min_price:
                extra.append(f"от {s.min_price:g}")
            lines.append(f"• **{s.collection}** ({', '.join(extra)})")
        await interaction.response.send_message(
            "Отслеживаемые коллекции в этом канале:\n" + "\n".join(lines),
            ephemeral=True,
        )

    @tree.command(name="nft_test", description="Прислать последнее событие коллекции (проверка)")
    @app_commands.describe(collection="Slug коллекции на OpenSea")
    async def nft_test(interaction: discord.Interaction, collection: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        slug = collection.strip().lower()
        try:
            events = await bot.opensea.fetch_events(slug, event_type="sale", limit=5)
        except OpenSeaError as exc:
            await interaction.followup.send(f"❌ Ошибка OpenSea: {exc}", ephemeral=True)
            return
        if not events:
            await interaction.followup.send(
                f"У коллекции **{slug}** не нашлось недавних продаж для примера.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "Пример последнего события (так будут выглядеть алерты):",
            embed=build_embed(events[-1]),
            ephemeral=True,
        )

    @tree.command(name="nft_help", description="Как пользоваться NFT Alert ботом")
    async def nft_help(interaction: discord.Interaction) -> None:
        text = (
            "**NFT Alert Bot** — алерты о продажах/листингах NFT в вашем канале.\n\n"
            "**Команды:**\n"
            "• `/nft_watch collection:<slug>` — начать отслеживать коллекцию в этом канале\n"
            "• `/nft_unwatch collection:<slug>` — прекратить отслеживание\n"
            "• `/nft_list` — список коллекций в этом канале\n"
            "• `/nft_test collection:<slug>` — показать пример последнего события\n\n"
            "**Где взять slug?** В ссылке на коллекцию OpenSea: "
            "`opensea.io/collection/`**`<slug>`** — например `boredapeyachtclub`.\n\n"
            "У `/nft_watch` есть опции: тип событий (продажи/листинги/всё) и "
            "минимальная цена для фильтра."
        )
        await interaction.response.send_message(text, ephemeral=True)


def _etype_label(etype: str) -> str:
    return {"sale": "продажи", "listing": "листинги", "all": "всё"}.get(etype, etype)


# ---- runner -------------------------------------------------------------


def run() -> None:
    config = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    bot = NftAlertBot(config)
    bot.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    run()
