"""Публичные HTTPS-URL картинок pass (бренд, не данные клиента)."""

from __future__ import annotations

from bonus.config import Settings, get_settings


def wallet_asset_urls(base_url: str, settings: Settings | None = None) -> dict[str, str]:
    """Абсолютные ссылки на logo / strip / icon для WalletWallet Pro."""
    cfg = settings or get_settings()
    root = (base_url or cfg.app_public_url or "").strip().rstrip("/")
    if not root:
        return {}
    version = (cfg.wallet_asset_version or "1").strip() or "1"
    suffix = f"?v={version}"
    return {
        "logo": f"{root}/static/wallet/logo.png{suffix}",
        "strip": f"{root}/static/wallet/strip.png{suffix}",
        "icon": f"{root}/static/wallet/icon.png{suffix}",
    }


def wallet_branding_ready(base_url: str, settings: Settings | None = None) -> bool:
    """Есть ли публичный базовый URL для Pro-полей."""
    cfg = settings or get_settings()
    root = (base_url or cfg.app_public_url or "").strip()
    return root.startswith("http")
