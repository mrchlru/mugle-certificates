"""Выдача и снятие доступа Wallet в HR."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from bonus.audit import write_audit
from bonus.models import AuditAction, Certificate, User
from bonus.wallet_service import revoke_wallet_pass


def set_wallet_allowed(
    db: Session,
    certificate: Certificate,
    *,
    allowed: bool,
    actor: User,
) -> None:
    """Включает или выключает wallet_allowed; при снятии — revoke pass."""
    previous = bool(getattr(certificate, "wallet_allowed", False))
    if previous == allowed:
        return
    certificate.wallet_allowed = allowed
    db.commit()
    db.refresh(certificate)
    if not allowed:
        revoke_wallet_pass(db, certificate)
    action = AuditAction.wallet_access_grant if allowed else AuditAction.wallet_access_revoke
    write_audit(
        db,
        actor=actor,
        action=action,
        summary=f"{'Выдан' if allowed else 'Снят'} Wallet · {certificate.public_number}",
        target_type="certificate",
        target_id=certificate.id,
        detail="",
    )


def set_wallet_allowed_bulk(
    db: Session,
    certificate_ids: list[int],
    *,
    allowed: bool,
    actor: User,
) -> int:
    """Массовая выдача или снятие доступа."""
    unique = sorted({int(i) for i in certificate_ids if i})
    if not unique:
        return 0
    rows = db.scalars(select(Certificate).where(Certificate.id.in_(unique))).all()
    count = 0
    for certificate in rows:
        if certificate.is_deleted:
            continue
        set_wallet_allowed(db, certificate, allowed=allowed, actor=actor)
        count += 1
    return count
