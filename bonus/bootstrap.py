"""Первый запуск: пустая база получает HR, управляющего и демо-сертификат."""

from pathlib import Path

from sqlalchemy import select

from bonus.catalog import ensure_default_segments, list_segments, optimize_existing_templates
from bonus.config import Settings
from bonus.db import open_session
from bonus.models import Role, User
from bonus.security import hash_password
from bonus.services import IssueDraft, apply_due_expiries, debit_certificate, issue_certificate
from bonus.storage import s3_enabled


def bootstrap(settings: Settings) -> None:
    """Создаёт каталог данных, сегменты и стартовые входы."""
    settings.data_path().mkdir(parents=True, exist_ok=True)
    (settings.data_path() / "uploads").mkdir(parents=True, exist_ok=True)
    if s3_enabled(settings):
        print("Teams Bonus: обложки → S3", flush=True)
    else:
        print(
            "Teams Bonus: S3 выключен — обложки на локальном диске, "
            "при деплое без тома пропадут",
            flush=True,
        )
    db = open_session()
    try:
        ensure_default_segments(db)
        optimize_existing_templates(db)
        apply_due_expiries(db)
        exists = db.scalar(select(User.id).limit(1))
        if exists is not None:
            return
        hr = _staff(
            settings.hr_name,
            settings.hr_login,
            settings.hr_password,
            Role.hr,
            "",
        )
        manager = _staff(
            settings.manager_name,
            settings.manager_login,
            settings.manager_password,
            Role.manager,
            settings.manager_restaurant,
        )
        db.add_all([hr, manager])
        db.commit()
        db.refresh(hr)
        db.refresh(manager)
        lines = [
            "Teams Bonus — сертификаты",
            f"HR: {hr.login} / {settings.hr_password}",
            f"Управляющий: {manager.login} / {settings.manager_password}",
        ]
        if settings.seed_demo:
            segments = list_segments(db)
            issued = issue_certificate(
                db,
                IssueDraft(
                    full_name="Мария Соколова",
                    login="sokolova",
                    password="",
                    nominal=2_000_000,
                    balance=None,
                    note="Демонстрационный сертификат",
                    expires_on=None,
                    segment_id=segments[0].id if segments else None,
                    template_id=None,
                    color="#00A8A8",
                ),
                hr,
            )
            debit_certificate(db, issued.certificate, manager, 350_000, "Обед, стол 4")
            lines.append(f"Демо-сертификат Марии Соколовой: /c/{issued.certificate.token}")
        _write_credentials(settings.data_path() / "CREDENTIALS.txt", lines)
    finally:
        db.close()


def _staff(full_name: str, login: str, password: str, role: Role, restaurant: str) -> User:
    return User(
        full_name=full_name,
        login=login.strip().casefold(),
        password_hash=hash_password(password),
        role=role,
        restaurant=restaurant,
        is_active=True,
        must_change_password=True,
    )


def _write_credentials(path: Path, lines: list[str]) -> None:
    if path.exists():
        return
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Teams Bonus: первые входы записаны в {path}", flush=True)
