"""Сборка Apple pass.json и ссылки WalletPasses без подписи."""

from datetime import date
from types import SimpleNamespace
from urllib.parse import unquote

from bonus.apple_wallet import pkpass_download_url, walletpasses_url
from bonus.apple_wallet import _build_pass_json
from bonus.config import Settings
from bonus.models import CertificateStatus


def test_walletpasses_url_encodes_pkpass() -> None:
    href = walletpasses_url("https://bonus.example/c/tok/apple-wallet.pkpass")
    assert href.startswith("https://walletpass.io?u=")
    assert "apple-wallet.pkpass" in unquote(href)


def test_pkpass_download_url() -> None:
    cert = SimpleNamespace(token="abc123")
    assert (
        pkpass_download_url(cert, "https://bonus.example")
        == "https://bonus.example/c/abc123/apple-wallet.pkpass"
    )


def test_build_pass_json_from_template() -> None:
    settings = Settings(
        apple_pass_type_id="pass.com.mugle.bonus",
        apple_team_id="TEAM123",
        apple_organization_name="Mugle Rest",
    )
    template = SimpleNamespace(name="Кухня", color="#00B596")
    cert = SimpleNamespace(
        id=4,
        public_number="MR-00004",
        short_code="12345678",
        balance=250_000,
        color="#00B596",
        discount_percent=5,
        token="tok",
        expires_on=date(2026, 12, 31),
        status=CertificateStatus.active,
        template=template,
        segment=SimpleNamespace(name="Сотрудник"),
    )
    holder = SimpleNamespace(full_name="Анна")
    body = _build_pass_json(cert, holder, base_url="https://bonus.example", settings=settings)
    assert body["passTypeIdentifier"] == "pass.com.mugle.bonus"
    assert body["teamIdentifier"] == "TEAM123"
    assert body["organizationName"] == "Mugle Rest"
    assert body["logoText"] == "Кухня"
    assert body["backgroundColor"] == "rgb(0, 181, 150)"
    assert body["storeCard"]["primaryFields"][0]["value"] == "2500 ₽"
    assert body["barcode"]["message"] == "12345678"
    assert body["storeCard"]["secondaryFields"][0]["value"] == "Анна"


def test_build_pass_json_hides_holder_when_template_flag() -> None:
    settings = Settings(
        apple_pass_type_id="pass.com.mugle.bonus",
        apple_team_id="TEAM123",
        apple_organization_name="Mugle Rest",
    )
    template = SimpleNamespace(name="Бартер", color="#00B596", hide_holder=True)
    cert = SimpleNamespace(
        id=5,
        public_number="MR-00005",
        short_code="87654321",
        balance=100_000,
        color="#00B596",
        discount_percent=0,
        token="tok2",
        expires_on=None,
        status=CertificateStatus.active,
        template=template,
        segment=SimpleNamespace(name="Бартер"),
    )
    body = _build_pass_json(
        cert, SimpleNamespace(full_name="Секрет"), base_url="https://bonus.example", settings=settings
    )
    assert body["storeCard"]["secondaryFields"] == []
    assert all(field["key"] != "segment" for field in body["storeCard"]["auxiliaryFields"])
    assert body["logoText"] == "Mugle Rest"
