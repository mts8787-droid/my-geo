from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def load_country_registry() -> dict[str, Any]:
    """Load the market registry once; UI labels and runtime routing share this source."""
    path = Path(__file__).with_name("country_registry.json")
    return json.loads(path.read_text(encoding="utf-8"))


def country_config(value: str) -> dict[str, Any] | None:
    """Resolve market codes, ISO codes, English/local/Korean names and aliases."""
    needle = (value or "").strip().casefold()
    if not needle:
        return None
    for item in load_country_registry().get("countries", []):
        aliases = {
            item.get("market_code", ""),
            item.get("iso_alpha2", ""),
            item.get("name_en", ""),
            item.get("name_local", ""),
            item.get("name_ko", ""),
            *item.get("aliases", []),
        }
        if needle in {str(alias).strip().casefold() for alias in aliases if alias}:
            return dict(item)
    return None


def enabled_countries() -> list[dict[str, Any]]:
    return [dict(item) for item in load_country_registry().get("countries", []) if item.get("enabled")]
