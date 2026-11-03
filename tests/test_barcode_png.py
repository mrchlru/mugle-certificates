"""PNG штрихкода для печати бумажных сертификатов."""

from __future__ import annotations

import re
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from bonus.barcode_draw import barcode_png


def test_barcode_png_transparent_with_labels() -> None:
    """PNG содержит полоски и подписи на прозрачном фоне."""
    payload = barcode_png(
        "12345678",
        code_label="1234-5678",
        certificate_number="MR-00042",
    )
    assert payload.startswith(b"\x89PNG")
    image = Image.open(BytesIO(payload))
    assert image.mode == "RGBA"
    assert image.getpixel((0, 0))[3] == 0
    opaque = sum(
        1
        for y in range(0, image.height, 8)
        for x in range(0, image.width, 8)
        if image.getpixel((x, y))[3] > 0
    )
    assert opaque > 20


def test_hr_certificate_barcode_download(tmp_path, monkeypatch) -> None:
    """Со страницы карты HR скачивается PNG штрихкода."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'barcode.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-barcode")
    monkeypatch.setenv("SEED_DEMO", "true")
    monkeypatch.setenv("GOOGLE_SHEETS_SPREADSHEET_ID", "")
    from bonus.config import get_settings

    get_settings.cache_clear()
    import bonus.main as main

    with TestClient(main.app) as client:
        _login(client, "hr", "mugle-hr")
        page = client.get("/hr/c/1")
        assert page.status_code == 200
        assert "/hr/c/1/barcode.png" in page.text
        response = client.get("/hr/c/1/barcode.png")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/png")
        assert "attachment" in response.headers["content-disposition"]
        assert response.content.startswith(b"\x89PNG")
        number = re.search(r'filename="([^"]+)"', response.headers["content-disposition"])
        assert number is not None
        assert number.group(1).endswith("-barcode.png")


def _login(client: TestClient, login: str, password: str):
    """Логин в тестовый кабинет."""
    page = client.get("/login")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match is not None
    response = client.post(
        "/login",
        data={
            "login": login,
            "password": password,
            "csrf_token": match.group(1),
            "next": "/",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    return response
