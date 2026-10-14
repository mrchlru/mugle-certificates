"""Разбор GOOGLE_SERVICE_ACCOUNT_JSON из формата Timeweb."""

from bonus.sheets_sync import _parse_service_account_json


def test_parse_compact_json() -> None:
    raw = '{"type": "service_account", "project_id": "x", "private_key": "k\\n"}'
    data = _parse_service_account_json(raw)
    assert data["type"] == "service_account"
    assert data["private_key"] == "k\n"


def test_parse_timeweb_pretty_with_literal_newlines() -> None:
    """Как в экспорте Timeweb: \\n между полями, PEM-\\n внутри строки."""
    raw = (
        '"{\\n'
        '  "type": "service_account",\\n'
        '  "project_id": "demo",\\n'
        '  "private_key": "-----BEGIN PRIVATE KEY-----\\nABC\\n-----END PRIVATE KEY-----\\n",\\n'
        '  "client_email": "a@demo.iam.gserviceaccount.com"\\n'
        '}"'
    )
    data = _parse_service_account_json(raw)
    assert data["project_id"] == "demo"
    assert data["private_key"].startswith("-----BEGIN PRIVATE KEY-----\n")
    assert "\nABC\n" in data["private_key"]
