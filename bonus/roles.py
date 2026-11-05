"""Роли доступа в админку и связанные проверки."""

from bonus.models import Role
from bonus.services import BonusError

HR_PANEL_ROLES: tuple[Role, ...] = (Role.admin, Role.hr, Role.hrd)
STAFF_ROLES: tuple[Role, ...] = (Role.admin, Role.hrd)
AUDIT_ROLES: tuple[Role, ...] = (Role.admin, Role.hrd)
DASHBOARD_ROLES: tuple[Role, ...] = (Role.admin, Role.hr, Role.hrd)
EXPORT_ROLES: tuple[Role, ...] = (Role.admin,)
DESK_ROLES: tuple[Role, ...] = (Role.manager, Role.hr, Role.admin, Role.hrd)
STAFF_ASSIGNABLE: tuple[Role, ...] = (Role.manager, Role.hr, Role.hrd)

_ROLE_LABELS: dict[Role, str] = {
    Role.employee: "Сотрудник",
    Role.manager: "Управляющий",
    Role.hr: "HR",
    Role.hrd: "HRD",
    Role.admin: "Администратор",
}


def role_label(role: Role) -> str:
    """Подпись роли для интерфейса."""
    return _ROLE_LABELS.get(role, role.value)


def parse_staff_role(raw: str) -> Role:
    """Разбирает роль из формы создания / правки доступа."""
    mapping = {
        "manager": Role.manager,
        "hr": Role.hr,
        "hrd": Role.hrd,
    }
    role = mapping.get(raw.strip().casefold())
    if role is None:
        raise BonusError("Неизвестная роль")
    return role


def can_manage_staff(role: Role) -> bool:
    """Может ли роль создавать и править доступы сотрудников."""
    return role in STAFF_ROLES


def can_view_audit(role: Role) -> bool:
    """Может ли роль видеть журнал действий сотрудников."""
    return role in AUDIT_ROLES
