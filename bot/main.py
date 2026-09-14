"""Entry point: Discord bot, slash commands and the background poller."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import tasks

from .config import Config
from .formatting import build_embed
from .opensea import NftEvent, OpenSeaClient, OpenSeaError
from .prices import PriceCache
from .storage import Storage, Subscription

log = logging.getLogger(__name__)

EVENT_TYPE_CHOICES = [
    app_commands.Choice(name="Продажи", value="sale"),
    app_commands.Choice(name="Листинги (выставления)", value="listing"),
    app_commands.Choice(name="Всё (продажи + листинги)", value="all"),
]

# сети OpenSea, между которыми можно выбирать
CHAIN_CHOICES = [
    app_commands.Choice(name="Ethereum", value="ethereum"),
    app_commands.Choice(name="Polygon", value="matic"),
    app_commands.Choice(name="Base", value="base"),
    app_commands.Choice(name="Arbitrum", value="arbitrum"),
    app_commands.Choice(name="Optimism", value="optimism"),
    app_commands.Choice(name="Avalanche", value="avalanche"),
]

# максимум событий на коллекцию за один опрос, чтобы не залить канал при всплеске
MAX_EVENTS_PER_POLL = 15


class NftAlertBot(discord.Client):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.storage = Storage(config.data_file)
        self.opensea = OpenSeaClient(config.opensea_api_key)
        self.prices = PriceCache()
        self._poller: tasks.Loop | None = None

    async def setup_hook(self) -> None:
        await self.storage.load()
        register_commands(self.tree, self)
        await self.tree.sync()
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
        log.info("Отслеживается коллекций: %d", len(self.storage.watched_collections()))

    # ---- poller ---------------------------------------------------------

    async def _poll_once(self) -> None:
        for slug in self.storage.watched_collections():
            try:
                await self._poll_collection(slug)
            except OpenSeaError as exc:
                log.warning("Опрос коллекции %s не удался: %s", slug, exc)
            except Exception:  # noqa: BLE001
                log.exception("Неожиданная ошибка при опросе %s", slug)

    async def _poll_collection(self, slug: str) -> None:
        subs = self.storage.subscribers_of(slug)
        if not subs:
            return
        wants_all = any(s.event_type == "all" for s in subs)
        event_type = "all" if wants_all else _common_event_type(subs)

        cursor = self.storage.get_cursor(slug)
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
        meta = self.storage.get_collection_meta(slug)
        usd = await self.prices.usd_for(event.price_symbol) if event.price else None
        embed = build_embed(event, meta=meta, usd_rate=usd)
        for sub in self.storage.subscribers_of(slug):
            if not _event_matches(event, sub):
                continue
            await self._send_to_channel(sub.channel_id, embed)

    async def _send_to_channel(self, channel_id: int, embed: discord.Embed) -> None:
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden):
                log.warning("Канал %s недоступен, пропускаю", channel_id)
                return
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            log.warning("Нет прав писать в канал %s", channel_id)
        except discord.HTTPException as exc:
            log.warning("Не удалось отправить сообщение: %s", exc)


def _common_event_type(subs: list[Subscription]) -> str:
    types = {s.event_type for s in subs}
    return next(iter(types)) if len(types) == 1 else "all"


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
        collection="Адрес контракта коллекции (0x…) или slug с OpenSea",
        chain="Сеть коллекции (по умолчанию Ethereum)",
        event_type="Какие события присылать (по умолчанию — продажи)",
        min_price="Минимальная цена, чтобы прислать алерт (в ETH/нативной валюте)",
    )
    @app_commands.choices(event_type=EVENT_TYPE_CHOICES, chain=CHAIN_CHOICES)
    async def nft_watch(
        interaction: discord.Interaction,
        collection: str,
        chain: app_commands.Choice[str] | None = None,
        event_type: app_commands.Choice[str] | None = None,
        min_price: float | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        chain_value = chain.value if chain else "ethereum"
        etype = event_type.value if event_type else "sale"

        try:
            meta = await bot.opensea.resolve_collection(collection, chain_value)
        except OpenSeaError as exc:
            await interaction.followup.send(
                f"❌ Не нашёл коллекцию по `{collection}` в сети `{chain_value}`.\n"
                f"Детали: {exc}\n\n"
                "Укажите **адрес контракта** (0x…) и правильную сеть, "
                "либо slug из ссылки `opensea.io/collection/`**`<slug>`**.",
                ephemeral=True,
            )
            return

        sub = Subscription(
            channel_id=interaction.channel_id,
            collection=meta.slug,
            event_type=etype,
            min_price=float(min_price or 0.0),
            added_by=interaction.user.id,
        )
        await bot.storage.add_subscription(sub)
        await bot.storage.set_collection_meta(
            meta.slug, meta.name, meta.image, meta.address, meta.chain
        )

        details = [f"тип: **{_etype_label(etype)}**", f"сеть: **{meta.chain}**"]
        if sub.min_price:
            details.append(f"мин. цена: **{sub.min_price:g}**")
        addr = f"\nАдрес: `{meta.address}`" if meta.address else ""
        await interaction.followup.send(
            f"✅ Слежу за **{meta.name}** в этом канале ({', '.join(details)}).{addr}\n"
            "Алерты придут при новых событиях. Проверить вид: `/nft_test`.",
            ephemeral=True,
        )

    @tree.command(name="nft_unwatch", description="Перестать отслеживать коллекцию в этом канале")
    @app_commands.describe(collection="Адрес контракта (0x…) или slug коллекции")
    async def nft_unwatch(interaction: discord.Interaction, collection: str) -> None:
        slug = _to_slug(bot, collection)
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
            meta = bot.storage.get_collection_meta(s.collection) or {}
            name = meta.get("name") or s.collection
            extra = [_etype_label(s.event_type)]
            if s.min_price:
                extra.append(f"от {s.min_price:g}")
            addr = meta.get("address")
            addr_str = f" · `{addr[:6]}…{addr[-4:]}`" if addr else ""
            lines.append(f"• **{name}** ({', '.join(extra)}){addr_str}")
        await interaction.response.send_message(
            "Отслеживаемые коллекции в этом канале:\n" + "\n".join(lines),
            ephemeral=True,
        )

    @tree.command(name="nft_test", description="Прислать последнее событие коллекции (проверка)")
    @app_commands.describe(
        collection="Адрес контракта (0x…) или slug",
        chain="Сеть коллекции (по умолчанию Ethereum)",
    )
    @app_commands.choices(chain=CHAIN_CHOICES)
    async def nft_test(
        interaction: discord.Interaction,
        collection: str,
        chain: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        chain_value = chain.value if chain else "ethereum"
        try:
            meta = await bot.opensea.resolve_collection(collection, chain_value)
            events = await bot.opensea.fetch_events(meta.slug, event_type="sale", limit=5)
        except OpenSeaError as exc:
            await interaction.followup.send(f"❌ Ошибка OpenSea: {exc}", ephemeral=True)
            return
        if not events:
            await interaction.followup.send(
                f"У коллекции **{meta.name}** не нашлось недавних продаж для примера.",
                ephemeral=True,
            )
            return
        ev = events[-1]
        usd = await bot.prices.usd_for(ev.price_symbol) if ev.price else None
        meta_dict = {"name": meta.name, "image": meta.image}
        await interaction.followup.send(
            "Пример последнего события (так будут выглядеть алерты):",
            embed=build_embed(ev, meta=meta_dict, usd_rate=usd),
            ephemeral=True,
        )

    @tree.command(name="nft_help", description="Как пользоваться NFT Alert ботом")
    async def nft_help(interaction: discord.Interaction) -> None:
        text = (
            "**NFT Alert Bot** — алерты о продажах/листингах NFT в вашем канале.\n\n"
            "**Команды:**\n"
            "• `/nft_watch collection:<0x-адрес>` — отслеживать коллекцию в этом канале\n"
            "• `/nft_unwatch collection:<адрес или slug>` — прекратить отслеживание\n"
            "• `/nft_list` — список коллекций в этом канале\n"
            "• `/nft_test collection:<0x-адрес>` — показать пример последнего события\n\n"
            "**Как указать коллекцию?** Лучше всего — **адресом контракта** (0x…) "
            "и выбрать сеть. Можно и slug из ссылки OpenSea: "
            "`opensea.io/collection/`**`<slug>`**.\n\n"
            "У `/nft_watch` есть опции: `chain` (сеть), `event_type` "
            "(продажи/листинги/всё) и `min_price` (фильтр по цене)."
        )
        await interaction.response.send_message(text, ephemeral=True)


def _to_slug(bot: NftAlertBot, collection: str) -> str:
    """Адрес -> slug (по сохранённым метаданным), иначе трактуем как slug."""
    ident = collection.strip()
    if ident.lower().startswith("0x"):
        found = bot.storage.find_slug_by_address(ident)
        if found:
            return found
    return ident.lower()


def _etype_label(etype: str) -> str:
    return {"sale": "продажи", "listing": "листинги", "all": "всё"}.get(etype, etype)


# ---- runners ------------------------------------------------------------


def build_bot(config: Config | None = None) -> NftAlertBot:
    """Собрать (но не запускать) бота. Удобно для Google Colab / тестов."""
    cfg = config or Config.from_env()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    return NftAlertBot(cfg)


def run() -> None:
    """Обычный запуск из терминала: python run.py"""
    config = Config.from_env()
    bot = build_bot(config)
    bot.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    run()
