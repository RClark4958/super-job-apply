"""Persistent cache of observe() field discoveries keyed by (ATS, hostname).

observe() is the single most expensive LLM call in the fill pipeline. Most
companies reuse the same form layout indefinitely, so once we've observed
a form we can replay the field list on subsequent submissions.

Cache entry lifecycle:
    - Miss: caller runs observe(), calls ``save()``.
    - Hit: caller uses ``lookup()`` result, calls ``record_hit()`` for analytics.

Invalidation: bump ``CACHE_VERSION`` or delete the JSON file to force refresh.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


CACHE_VERSION = 1
_DEFAULT_PATH = Path("data/form_cache.json")

# Stagehand field records vary by SDK version — we normalize to a dict with
# these keys. Consumers should treat any additional keys as opaque.
FieldRecord = dict[str, Any]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_hostname(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().replace("www.", "")
    return host or "unknown"


def _cache_key(ats_platform: str | None, url: str) -> str:
    ats = ats_platform or "unknown"
    host = _normalize_hostname(url)
    return f"{ats}|{host}"


class FormCache:
    """File-backed cache of field descriptions per (ATS, hostname)."""

    def __init__(self, path: Path | str = _DEFAULT_PATH):
        self.path = Path(path)
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": CACHE_VERSION, "entries": {}}
        try:
            data = json.loads(self.path.read_text())
            if data.get("version") != CACHE_VERSION:
                logger.info(
                    f"FormCache: version mismatch ({data.get('version')} != "
                    f"{CACHE_VERSION}) — starting fresh"
                )
                return {"version": CACHE_VERSION, "entries": {}}
            return data
        except Exception as e:
            logger.warning(f"FormCache: failed to load {self.path}: {e}")
            return {"version": CACHE_VERSION, "entries": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        os.replace(tmp, self.path)

    def lookup(self, ats_platform: str | None, url: str) -> list[FieldRecord] | None:
        """Return cached field records for the (ATS, hostname), or None on miss."""
        key = _cache_key(ats_platform, url)
        entry = self._data.get("entries", {}).get(key)
        if not entry:
            return None
        return entry.get("fields")

    def save(
        self,
        ats_platform: str | None,
        url: str,
        fields: list[FieldRecord],
    ) -> None:
        """Persist observe() field records for this (ATS, hostname)."""
        if not fields:
            return  # don't cache empty results — never overwrite a valid entry with empty
        key = _cache_key(ats_platform, url)
        existing = self._data.setdefault("entries", {}).get(key, {})
        entry = {
            "ats_platform": ats_platform or "unknown",
            "hostname": _normalize_hostname(url),
            "fields": fields,
            "field_count": len(fields),
            "last_updated": _now(),
            "hit_count": existing.get("hit_count", 0),
            "update_count": existing.get("update_count", 0) + 1,
        }
        self._data["entries"][key] = entry
        self._save()

    def record_hit(self, ats_platform: str | None, url: str) -> None:
        """Increment hit counter for a cache hit. Safe to no-op on missing entry."""
        key = _cache_key(ats_platform, url)
        entry = self._data.get("entries", {}).get(key)
        if not entry:
            return
        entry["hit_count"] = entry.get("hit_count", 0) + 1
        entry["last_hit"] = _now()
        self._save()

    def stats(self) -> dict[str, Any]:
        entries = self._data.get("entries", {})
        total_hits = sum(e.get("hit_count", 0) for e in entries.values())
        total_updates = sum(e.get("update_count", 0) for e in entries.values())
        by_ats: dict[str, int] = {}
        for e in entries.values():
            ats = e.get("ats_platform", "unknown")
            by_ats[ats] = by_ats.get(ats, 0) + 1
        return {
            "entries": len(entries),
            "total_hits": total_hits,
            "total_updates": total_updates,
            "by_ats": by_ats,
        }
