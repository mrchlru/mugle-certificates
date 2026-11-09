"""Журнал действий сотрудников в админке."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from bonus.models import AuditAction, AuditLog, User
from bonus.values import format_money


@dataclass(frozen=True)
class AuditView:
    """Строка журнала контроля для шаблона."""

    id: int
    created_at: datetime
    actor_name: str
    actor_login: str
    action: str
    action_label: str
    summary: str
    detail: str
    target_type: str
    target_id: int | None


_ACTION_LABELS: dict[str, str] = {
    AuditAction.staff_create.value: "Создание доступа",
    AuditAction.staff_update.value: "Изменение доступа",
    AuditAction.staff_active.value: "Включение / отключение",
    AuditAction.certificate_issue.value: "Выпуск карты",
    AuditAction.certificate_credit.value: "Начисление бонусов",
    AuditAction.certificate_debit.value: "Списание бонусов",
    AuditAction.certificate_block.value: "Блокировка карты",
    AuditAction.certificate_unblock.value: "Разблокировка карты",
    AuditAction.certificate_expiry.value: "Срок действия карты",
    AuditAction.certificate_accrual_nominal.value: "Лимит автоначисления",
    AuditAction.batch_hr_adjustments.value: "Пакетно: лимиты и начисления",
    AuditAction.debit_undo.value: "Отмена списания",
    AuditAction.credit_undo.value: "Отмена начисления",
    AuditAction.accrual_balance_repair.value: "Восстановление остатков автоначисления",
    AuditAction.import_rows.value: "Импорт карт",
    AuditAction.certificate_delete.value: "Удаление карты",
    AuditAction.template_delete.value: "Удаление шаблона",
    AuditAction.segment_delete.value: "Удаление сегмента",
    AuditAction.link_export.value: "Выгрузка ссылок",
    AuditAction.link_delivery_mark.value: "Отметка рассылки",
    AuditAction.login_unblock.value: "Разблокировка входа",
    AuditAction.sheets_dedupe.value: "Очистка дублей Google Sheets",
}


def write_audit(
    db: Session,
    *,
    actor: User | None,
    action: AuditAction,
    summary: str,
    target_type: str = "",
    target_id: int | None = None,
    detail: str = "",
) -> AuditLog:
    """Пишет одну запись в журнал контроля."""
    row = AuditLog(
        actor_id=None if actor is None else actor.id,
        action=action.value,
        target_type=target_type[:40],
        target_id=target_id,
        summary=summary.strip()[:400],
        detail=detail.strip()[:4000],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_audit(db: Session, *, limit: int = 300) -> list[AuditView]:
    """Последние записи журнала для admin и HRD."""
    rows = db.execute(
        select(AuditLog, User.full_name, User.login)
        .outerjoin(User, AuditLog.actor_id == User.id)
        .order_by(AuditLog.id.desc())
        .limit(limit)
    ).all()
    return [
        AuditView(
            id=log.id,
            created_at=log.created_at,
            actor_name=actor_name or "—",
            actor_login=actor_login or "",
            action=log.action,
            action_label=_ACTION_LABELS.get(log.action, log.action),
            summary=log.summary,
            detail=log.detail,
            target_type=log.target_type,
            target_id=log.target_id,
        )
        for log, actor_name, actor_login in rows
    ]


def credit_audit_detail(amount: int, comment: str) -> str:
    """Короткое описание начисления для журнала."""
    parts = [format_money(amount)]
    cleaned = comment.strip()
    if cleaned:
        parts.append(cleaned)
    return " · ".join(parts)


def accrual_nominal_audit_detail(segment_name: str, old_kopecks: int, new_kopecks: int) -> str:
    """Описание смены лимита начисления для журнала."""
    label = segment_name.strip() or "Часть карты"
    return (
        f"{label}: {format_money(old_kopecks)} → {format_money(new_kopecks)}"
    )


def debit_audit_detail(amount: int, comment: str) -> str:
    """Короткое описание списания для журнала."""
    parts = [f"−{format_money(amount)}"]
    cleaned = comment.strip()
    if cleaned:
        parts.append(cleaned)
    return " · ".join(parts)


def action_label(action: str) -> str:
    """Человекочитаемое название действия."""
    return _ACTION_LABELS.get(action, action)
