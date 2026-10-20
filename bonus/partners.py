"""Рестораны-партнёры, где можно пользоваться картой."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field


class PartnerPlace(BaseModel):
    """Одна точка или сеть адресов одного бренда в городе."""

    name: str
    addresses: list[str] = Field(default_factory=list)


class PartnerCity(BaseModel):
    """Город со списком партнёров."""

    city: str
    places: list[PartnerPlace] = Field(default_factory=list)


class PartnerCatalog(BaseModel):
    """Каталог ресторанов-партнёров."""

    cities: list[PartnerCity] = Field(default_factory=list)


_DATA_FILE = Path(__file__).resolve().parent / "data" / "partner_restaurants.json"


@lru_cache
def partner_catalog() -> PartnerCatalog:
    """Загружает каталог партнёров из JSON рядом с пакетом."""
    if not _DATA_FILE.is_file():
        return PartnerCatalog()
    raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    return PartnerCatalog.model_validate(raw)


def partner_cities() -> list[PartnerCity]:
    """Список городов для UI."""
    return list(partner_catalog().cities)
