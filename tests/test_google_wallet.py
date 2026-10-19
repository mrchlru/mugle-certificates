"""Сборка Loyalty Object и JWT без обращения к Google API."""

from datetime import date
from types import SimpleNamespace

import jwt

from bonus.config import Settings
from bonus.google_wallet import (
    build_loyalty_class,
    build_loyalty_object,
    class_id,
    object_id,
    wallet_enabled,
)
from bonus.models import CertificateStatus


def test_wallet_disabled_by_default() -> None:
    assert wallet_enabled(Settings(google_wallet_enabled=False)) is False


def test_class_and_object_ids() -> None:
    settings = Settings(google_wallet_issuer_id="338800000", google_wallet_class_suffix="teams_bonus")
    cert = SimpleNamespace(id=42)
    assert class_id(settings) == "338800000.teams_bonus"
    assert object_id(cert, settings) == "338800000.cert_42"


def test_build_loyalty_object_payload() -> None:
    settings = Settings(
        google_wallet_issuer_id="338800000",
        google_wallet_class_suffix="teams_bonus",
        app_public_url="https://bonus.example",
    )
    cert = SimpleNamespace(
        id=7,
        public_number="MR-00007",
        short_code="12345678",
        balance=150_050,
        color="#00a8a8",
        status=CertificateStatus.active,
        expires_on=date(2026, 12, 31),
        discount_percent=10,
        token="tok_abc",
    )
    holder = SimpleNamespace(full_name="Иван Петров")
    body = build_loyalty_object(
        cert,
        holder,
        base_url="https://bonus.example",
        settings=settings,
    )
    assert body["id"] == "338800000.cert_7"
    assert body["classId"] == "338800000.teams_bonus"
    assert body["state"] == "ACTIVE"
    assert body["accountName"] == "Иван Петров"
    assert body["accountId"] == "MR-00007"
    assert body["loyaltyPoints"]["balance"]["string"] == "1500 ₽"
    assert body["barcode"]["type"] == "CODE_128"
    assert body["barcode"]["value"] == "12345678"
    assert body["barcode"]["alternateText"] == "1234-5678"
    assert body["hexBackgroundColor"] == "#00A8A8"
    assert body["accountName"] == "Иван Петров"
    assert body["linksModuleData"]["uris"][0]["uri"] == "https://bonus.example/c/tok_abc"
    assert "validTimeInterval" in body
    headers = {m["header"] for m in body["textModulesData"]}
    assert "Номер карты" not in headers
    assert "Скидка" in headers
    assert "Действует до" in headers


def test_build_loyalty_object_blocked_and_expired() -> None:
    settings = Settings(google_wallet_issuer_id="1", google_wallet_class_suffix="x")
    blocked = SimpleNamespace(
        id=1,
        public_number="MR-00001",
        short_code="11111111",
        balance=100,
        color="#112233",
        status=CertificateStatus.blocked,
        expires_on=None,
        discount_percent=0,
        token="t1",
    )
    assert build_loyalty_object(blocked, None, settings=settings)["state"] == "INACTIVE"
    expired = SimpleNamespace(
        id=2,
        public_number="MR-00002",
        short_code="22222222",
        balance=0,
        color="#112233",
        status=CertificateStatus.active,
        expires_on=date(2020, 1, 1),
        discount_percent=0,
        token="t2",
    )
    assert build_loyalty_object(expired, None, expired=True, settings=settings)["state"] == "EXPIRED"


def test_build_loyalty_class_uses_logo() -> None:
    settings = Settings(
        google_wallet_issuer_id="99",
        google_wallet_class_suffix="teams_bonus",
        google_wallet_program_name="Бонус",
        google_wallet_issuer_name="Mugle Rest",
        google_wallet_class_color="#00B596",
        app_public_url="https://bonus.example",
    )
    body = build_loyalty_class(base_url="https://bonus.example", settings=settings)
    assert body["id"] == "99.teams_bonus"
    assert body["issuerName"] == "Mugle Rest"
    assert body["programName"] == "Бонус"
    assert body["accountNameLabel"] == "Владелец карты"
    assert body["hexBackgroundColor"] == "#00B596"
    assert body["programLogo"]["sourceUri"]["uri"] == "https://bonus.example/static/wallet-logo.png"
    # Не проставляем reviewStatus в payload class — иначе JWT/PATCH сбрасывают APPROVED в демо.
    assert "reviewStatus" not in body


def test_build_loyalty_class_rewrites_teams_bonus_env() -> None:
    settings = Settings(
        google_wallet_issuer_id="99",
        google_wallet_program_name="Teams Bonus",
        google_wallet_issuer_name="Teams Bonus",
        app_public_url="https://bonus.example",
    )
    body = build_loyalty_class(base_url="https://bonus.example", settings=settings)
    assert body["issuerName"] == "Mugle Rest"
    assert body["programName"] == "Бонус"


def test_save_jwt_claims_structure() -> None:
    """Проверяет форму claims, как у save_url (без сети)."""
    private_key = _rsa_private_pem()
    claims = {
        "iss": "wallet@example.iam.gserviceaccount.com",
        "aud": "google",
        "typ": "savetowallet",
        "iat": 1_700_000_000,
        "origins": ["https://bonus.example"],
        "payload": {
            "loyaltyObjects": [
                {
                    "id": "1.cert_1",
                    "classId": "1.teams_bonus",
                    "state": "ACTIVE",
                }
            ]
        },
    }
    token = jwt.encode(claims, private_key, algorithm="RS256")
    decoded = jwt.decode(token, options={"verify_signature": False})
    assert decoded["typ"] == "savetowallet"
    assert decoded["payload"]["loyaltyObjects"][0]["id"] == "1.cert_1"


def _rsa_private_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
