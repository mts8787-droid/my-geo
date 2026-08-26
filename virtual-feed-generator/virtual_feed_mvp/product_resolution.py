from __future__ import annotations

import re
import threading
import urllib.request
from collections.abc import Iterable

from .countries import country_config


def _model_key(value: str) -> str:
    model = (value or "").strip().upper().split(".", 1)[0]
    return re.sub(r"[^A-Z0-9-]", "", model)


class LGSitemapResolver:
    """Country-aware model-to-PDP resolver isolated from UI and feed generation."""

    EXCLUDED_PATHS = ("/support/", "/tncs/", "/promotion/", "/microsite", "/business/")
    CATEGORY_HINTS = {
        "REF": ("/fridge-freezers/", "/geladeiras/", "/refrigeradores/"),
        "W/M": ("/laundry/", "/lavanderia/", "/maquinas-de-lavar/"),
        "LTV": ("/tvs-soundbars/", "/televisions/", "/tv/", "/tvs/"),
        "MNT": ("/monitors/", "/monitores/"),
    }
    _urls: dict[str, list[str]] = {}
    _lock = threading.Lock()

    @classmethod
    def sitemap_url(cls, country: str) -> str:
        config = country_config(country)
        if not config or not config.get("enabled"):
            raise ValueError(f"지원하지 않는 국가입니다: {country}")
        return str(config["sitemap_url"])

    @classmethod
    def _load_urls(cls, country: str) -> list[str]:
        config = country_config(country)
        if not config or not config.get("enabled"):
            raise ValueError(f"지원하지 않는 국가입니다: {country}")
        site_code = str(config["lg_site_code"]).lower()
        if site_code in cls._urls:
            return cls._urls[site_code]
        with cls._lock:
            if site_code in cls._urls:
                return cls._urls[site_code]
            request = urllib.request.Request(
                str(config["sitemap_url"]),
                headers={"User-Agent": "Mozilla/5.0 (compatible; LG-Virtual-Feed-Discovery/0.1)"},
            )
            with urllib.request.urlopen(request, timeout=25) as response:
                xml = response.read().decode("utf-8", errors="replace")
            pattern = rf"<loc>(https://www\.lg\.com/{re.escape(site_code)}/[^<]+)</loc>"
            cls._urls[site_code] = re.findall(pattern, xml, flags=re.IGNORECASE)
            return cls._urls[site_code]

    @classmethod
    def select_url(cls, sku: str, category: str, urls: Iterable[str]) -> str:
        sku_key = re.sub(r"[^a-z0-9]", "", _model_key(sku).lower())
        hints = cls.CATEGORY_HINTS.get((category or "").upper(), ())
        candidates: list[tuple[int, str]] = []
        for url in urls:
            lowered = url.lower().split("?", 1)[0]
            if any(blocked in lowered for blocked in cls.EXCLUDED_PATHS):
                continue
            slug = lowered.rstrip("/").rsplit("/", 1)[-1]
            slug_key = re.sub(r"[^a-z0-9]", "", slug)
            if not slug_key.startswith(sku_key):
                continue
            suffix = slug_key[len(sku_key):]
            if len(suffix) > 8:
                continue
            score = 100 if not suffix else 94 if suffix == "1" else 75
            if any(hint in lowered for hint in hints):
                score += 20
            if "bundle" in lowered or suffix.startswith(("ms", "fdv")):
                score -= 15
            candidates.append((score, url.split("?", 1)[0]))
        return max(candidates, default=(0, ""), key=lambda item: (item[0], -len(item[1])))[1]

    @classmethod
    def resolve(cls, country: str, sku: str, category: str) -> str:
        return cls.select_url(sku, category, cls._load_urls(country))
