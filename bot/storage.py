"""Persistent storage of channel subscriptions and per-collection cursors.

Layout of the JSON file::

    {
      "subscriptions": {
        "<channel_id>": {
          "<collection_slug>": {
            "event_type": "sale",       # sale | listing | all
            "min_price": 0.0,           # минимальная цена в нативной валюте (ETH/…)
            "added_by": <user_id>,
            "added_at": <unix_ts>
          }
        }
      },
      "cursors": {
        "<collection_slug>": <unix_ts>  # время последнего обработанного события
      }
    }
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Subscription:
    channel_id: int
    collection: str
    event_type: str = "sale"
    min_price: float = 0.0
    added_by: int = 0
    added_at: int = field(default_factory=lambda: int(time.time()))


class Storage:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._data: dict = {"subscriptions": {}, "cursors": {}, "collections": {}}
        self._loaded = False

    # ---- disk I/O -------------------------------------------------------

    def _load_sync(self) -> None:
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                self._data = {
                    "subscriptions": loaded.get("subscriptions", {}),
                    "cursors": loaded.get("cursors", {}),
                    "collections": loaded.get("collections", {}),
                }
            except (json.JSONDecodeError, OSError):
                # повреждённый файл не должен ронять бота — начинаем с чистого
                self._data = {"subscriptions": {}, "cursors": {}, "collections": {}}
        self._loaded = True

    def _save_sync(self) -> None:
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{self._path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)  # атомарная запись

    async def load(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._load_sync)

    async def _save(self) -> None:
        await asyncio.to_thread(self._save_sync)

    # ---- subscriptions --------------------------------------------------

    async def add_subscription(self, sub: Subscription) -> None:
        async with self._lock:
            subs = self._data["subscriptions"].setdefault(str(sub.channel_id), {})
            subs[sub.collection] = {
                "event_type": sub.event_type,
                "min_price": sub.min_price,
                "added_by": sub.added_by,
                "added_at": sub.added_at,
            }
            # для новой коллекции ставим курсор на «сейчас», чтобы не спамить историей
            self._data["cursors"].setdefault(sub.collection, int(time.time()))
            await self._save()

    async def remove_subscription(self, channel_id: int, collection: str) -> bool:
        async with self._lock:
            subs = self._data["subscriptions"].get(str(channel_id), {})
            existed = collection in subs
            subs.pop(collection, None)
            if not subs:
                self._data["subscriptions"].pop(str(channel_id), None)
            # если коллекцию больше никто не смотрит — убираем и курсор
            if not self._collection_is_watched(collection):
                self._data["cursors"].pop(collection, None)
            await self._save()
            return existed

    def _collection_is_watched(self, collection: str) -> bool:
        for subs in self._data["subscriptions"].values():
            if collection in subs:
                return True
        return False

    def list_for_channel(self, channel_id: int) -> list[Subscription]:
        result = []
        for slug, meta in self._data["subscriptions"].get(str(channel_id), {}).items():
            result.append(
                Subscription(
                    channel_id=channel_id,
                    collection=slug,
                    event_type=meta.get("event_type", "sale"),
                    min_price=float(meta.get("min_price", 0.0)),
                    added_by=int(meta.get("added_by", 0)),
                    added_at=int(meta.get("added_at", 0)),
                )
            )
        return result

    def watched_collections(self) -> set[str]:
        """Все уникальные коллекции по всем каналам."""
        collections: set[str] = set()
        for subs in self._data["subscriptions"].values():
            collections.update(subs.keys())
        return collections

    def subscribers_of(self, collection: str) -> list[Subscription]:
        """Все подписки (каналы) на конкретную коллекцию."""
        result = []
        for channel_id, subs in self._data["subscriptions"].items():
            meta = subs.get(collection)
            if meta is None:
                continue
            result.append(
                Subscription(
                    channel_id=int(channel_id),
                    collection=collection,
                    event_type=meta.get("event_type", "sale"),
                    min_price=float(meta.get("min_price", 0.0)),
                    added_by=int(meta.get("added_by", 0)),
                    added_at=int(meta.get("added_at", 0)),
                )
            )
        return result

    # ---- collection metadata -------------------------------------------

    async def set_collection_meta(
        self,
        slug: str,
        name: str,
        image: Optional[str],
        address: Optional[str],
        chain: str,
    ) -> None:
        async with self._lock:
            self._data["collections"][slug] = {
                "name": name,
                "image": image,
                "address": (address or "").lower() or None,
                "chain": chain,
            }
            await self._save()

    def get_collection_meta(self, slug: str) -> Optional[dict]:
        return self._data["collections"].get(slug)

    def find_slug_by_address(self, address: str) -> Optional[str]:
        target = address.strip().lower()
        for slug, meta in self._data["collections"].items():
            if (meta.get("address") or "").lower() == target:
                return slug
        return None

    # ---- cursors --------------------------------------------------------

    def get_cursor(self, collection: str) -> Optional[int]:
        value = self._data["cursors"].get(collection)
        return int(value) if value is not None else None

    async def set_cursor(self, collection: str, ts: int) -> None:
        async with self._lock:
            current = self._data["cursors"].get(collection)
            if current is None or ts > int(current):
                self._data["cursors"][collection] = int(ts)
                await self._save()
