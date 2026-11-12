"""Тесты пилота Apple Wallet через WalletWallet."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import bonus.db as database
from bonus.app_settings import set_wallet_enabled
from bonus.catalog import ensure_default_segments, list_segments
from bonus.config import Settings, get_settings
from bonus.main import create_app
from bonus.models import Base, Certificate, WalletPass
from bonus.security import hash_password
from bonus.services import IssueDraft, issue_certificate
from bonus.wallet_payload import _expiration_rfc3339, build_walletwallet_body
from bonus.wallet_provider import CreatePassResult, WalletProviderError, WalletWalletProvider
from bonus.wallet_service import certificate_wallet_visible, serve_wallet_pass


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("SEED_DEMO", "false")
    monkeypatch.setenv("WALLETWALLET_API_KEY", "ww_live_" + "a" * 32)
    get_settings.cache_clear()
    database.configure_database("sqlite:///:memory:")
    Base.metadata.create_all(database.engine)
    from bonus.schema import ensure_schema

    ensure_schema(database.engine)
    with TestClient(create_app()) as test_client:
        db = database.open_session()
        try:
            ensure_default_segments(db)
            _issue_card(db)
        finally:
            db.close()
        yield test_client
    get_settings.cache_clear()


def _issue_card(db: Session) -> Certificate:
    segment = next(s for s in list_segments(db) if s.name == "Карта сотрудника")
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Wallet Тест",
            login="",
            password="",
            nominal=50_000,
            balance=None,
            note="",
            expires_on=None,
            segment_id=segment.id,
            segment_name=segment.name,
            company="ООО",
            ledger_sheet="employee",
        ),
        None,
        sync_external=False,
    )
    return issued.certificate


def test_expiration_rfc3339_caps_at_ten_years(monkeypatch: pytest.MonkeyPatch) -> None:
    anchor = date(2026, 3, 12)
    monkeypatch.setattr("bonus.wallet_payload.today_msk", lambda: anchor)
    capped = _expiration_rfc3339(date(2040, 1, 1))
    within = _expiration_rfc3339(date(2027, 6, 1))
    assert capped.startswith("2036-03-12")
    assert within.startswith("2027-06-01")


def test_build_walletwallet_body_barcode_short_code() -> None:
    db = MagicMock()
    cert = SimpleNamespace(
        short_code="87654321",
        public_number="MR-00099",
        balance=50000,
        discount_percent=30,
        expires_on=date(2026, 12, 31),
        segment=SimpleNamespace(name="Сотрудник"),
        token="secret-token",
        note="Правило тест",
        company="ООО Тест",
        use_days="1234567",
        id=1,
    )
    with patch("bonus.wallet_payload.list_parts", return_value=[]):
        holder = SimpleNamespace(full_name="Иван Тестов")
        body = build_walletwallet_body(
            db,
            cert,
            holder,
            base_url="https://example.test",
            show_balance=False,
            notify_on_change=False,
        )
    assert body["barcodeValue"] == "87654321"
    assert body["barcodeFormat"] == "Code128"
    assert body["barcodeValue"] != cert.token
    assert "headerFields" not in body


def test_certificate_wallet_visible_requires_flags(client: TestClient) -> None:
    db = database.open_session()
    try:
        cert = db.query(Certificate).first()
        assert cert is not None
        set_wallet_enabled(db, False)
        cert.wallet_allowed = True
        db.commit()
        assert certificate_wallet_visible(db, cert) is False
        set_wallet_enabled(db, True)
        db.refresh(cert)
        assert certificate_wallet_visible(db, cert) is True
        cert.wallet_allowed = False
        db.commit()
        assert certificate_wallet_visible(db, cert) is False
    finally:
        db.close()


def test_wallet_endpoint_404_when_disabled(client: TestClient) -> None:
    db = database.open_session()
    try:
        cert = db.query(Certificate).first()
        assert cert is not None
        set_wallet_enabled(db, False)
        cert.wallet_allowed = False
        db.commit()
        token = cert.token
    finally:
        db.close()
    response = client.get(f"/c/{token}/wallet")
    assert response.status_code == 404


@patch("bonus.wallet_service.get_wallet_provider")
def test_wallet_endpoint_pkpass_when_allowed(mock_factory: MagicMock, client: TestClient) -> None:
    provider = MagicMock()
    provider.create_pass.return_value = CreatePassResult(
        serial="ser-1",
        share_url="https://pass.walletwallet.dev/p/ser-1",
        google_save_url="",
        pkpass=b"PK",
    )
    mock_factory.return_value = provider
    db = database.open_session()
    try:
        cert = db.query(Certificate).first()
        assert cert is not None
        set_wallet_enabled(db, True)
        cert.wallet_allowed = True
        db.commit()
        token = cert.token
    finally:
        db.close()
    response = client.get(f"/c/{token}/wallet", headers={"X-Forwarded-For": "203.0.113.10"})
    assert response.status_code == 200
    assert "pkpass" in (response.headers.get("content-type") or "")
    provider.create_pass.assert_called_once()


def test_wallet_provider_share_url_without_pkpass() -> None:
    settings = Settings(walletwallet_api_key="ww_live_" + "a" * 32)
    provider = WalletWalletProvider(settings)
    with patch("bonus.wallet_provider.httpx.Client") as client_cls:
        mock_client = MagicMock()
        client_cls.return_value.__enter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{"serialNumber":"s1","shareUrl":"https://pass.example/p/s1"}'
        mock_response.json.return_value = {
            "serialNumber": "s1",
            "shareUrl": "https://pass.example/p/s1",
        }
        mock_client.post.return_value = mock_response
        cert = SimpleNamespace(
            short_code="12345678",
            public_number="MR-1",
            balance=0,
            discount_percent=0,
            expires_on=None,
            segment=None,
            token="tok",
            note="",
            company="",
            use_days="1234567",
            id=2,
        )
        with (
            patch("bonus.wallet_provider.get_settings", return_value=settings),
            patch("bonus.wallet_payload.list_parts", return_value=[]),
        ):
            result = provider.create_pass(
                MagicMock(),
                cert,
                None,
                base_url="https://x.test",
                show_balance=False,
                notify_on_change=False,
            )
        assert result.share_url.startswith("https://")
        assert result.pkpass == b""


def test_wallet_provider_error_status() -> None:
    settings = Settings(walletwallet_api_key="ww_live_" + "a" * 32)
    provider = WalletWalletProvider(settings)
    with patch("bonus.wallet_provider.httpx.Client") as client_cls:
        mock_client = MagicMock()
        client_cls.return_value.__enter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.content = b'{"error":"bad key"}'
        mock_response.json.return_value = {"error": "bad key"}
        mock_client.post.return_value = mock_response
        cert = SimpleNamespace(
            short_code="12345678",
            public_number="MR-1",
            balance=0,
            discount_percent=0,
            expires_on=None,
            segment=None,
            token="tok",
            note="",
            company="",
            use_days="1234567",
            id=2,
        )
        with patch("bonus.wallet_payload.list_parts", return_value=[]):
            with pytest.raises(WalletProviderError) as exc:
                provider.create_pass(
                    MagicMock(),
                    cert,
                    None,
                    base_url="https://x.test",
                    show_balance=False,
                    notify_on_change=False,
                )
        assert exc.value.status_code == 401


def test_serve_wallet_redirect_second_fetch(client: TestClient) -> None:
    db = database.open_session()
    try:
        cert = db.query(Certificate).first()
        assert cert is not None
        set_wallet_enabled(db, True)
        cert.wallet_allowed = True
        db.add(
            WalletPass(
                certificate_id=cert.id,
                serial="abc",
                share_url="https://pass.walletwallet.dev/p/abc",
                google_save_url="",
            )
        )
        db.commit()
        response = serve_wallet_pass(
            db,
            cert,
            base_url="https://example.test",
            client_key="203.0.113.99",
        )
        assert response.status_code == 302
    finally:
        db.close()
