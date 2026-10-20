"""Каталог ресторанов-партнёров из JSON."""

from bonus.partners import partner_catalog, partner_cities


def test_partner_catalog_loaded() -> None:
    catalog = partner_catalog()
    assert catalog.cities
    cities = {item.city for item in catalog.cities}
    assert "Челябинск" in cities
    assert "Нижний Новгород" in cities
    assert "Санкт Петербург" in cities


def test_partner_places_have_addresses() -> None:
    total = 0
    for city in partner_cities():
        for place in city.places:
            assert place.name
            assert place.addresses
            total += len(place.addresses)
    assert total >= 40
