"""Мониторинг касс iiko: heartbeat и вычисление статусов для админки."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from bonus.config import get_settings
from bonus.models import IikoTerminal

STALE_MINUTES = 15
ERROR_FRESH_MINUTES = 60


@dataclass(frozen=True)
class TerminalStatus:
    """Статус кассы для UI."""

    key: str
    label: str
    css: str
    reason: str


def normalize_restaurant(name: str) -> str:
    """Сопоставление названий ресторанов без регистра и лишних пробелов."""
    collapsed = re.sub(r"\s+", " ", (name or "").strip().casefold())
    return collapsed


def upsert_heartbeat(db: Session, payload: dict[str, Any], client_ip: str) -> None:
    """Создаёт или обновляет запись кассы по heartbeat."""
    terminal_id = str(payload.get("terminal_id") or "").strip()[:80]
    if not terminal_id:
        raise ValueError("terminal_id обязателен")
    now = datetime.now(timezone.utc)
    row = db.scalar(select(IikoTerminal).where(IikoTerminal.terminal_id == terminal_id))
    features = payload.get("features")
    if isinstance(features, list):
        features_json = json.dumps(features, ensure_ascii=False)[:2000]
    else:
        features_json = "[]"
    last_error = _clip(str(payload.get("last_error") or ""), 300)
    last_error_at = _parse_optional_time(payload.get("last_error_at"))
    if last_error and last_error_at is None:
        last_error_at = now
    fields = {
        "restaurant": _clip(str(payload.get("restaurant") or ""), 200),
        "machine_name": _clip(str(payload.get("machine_name") or ""), 120),
        "plugin_version": _clip(str(payload.get("plugin_version") or ""), 40),
        "front_api": _clip(str(payload.get("front_api") or ""), 16),
        "front_version": _clip(str(payload.get("front_version") or ""), 40),
        "features": features_json,
        "license_ok": _optional_bool(payload.get("license_ok")),
        "payment_type_bound": _optional_bool(payload.get("payment_type_bound")),
        "payment_type_name": _clip(str(payload.get("payment_type_name") or ""), 120),
        "config_source": _clip(str(payload.get("config_source") or ""), 16),
        "pending_ops": max(0, int(payload.get("pending_ops") or 0)),
        "last_error": last_error,
        "last_error_at": last_error_at,
        "last_seen_at": now,
        "last_ip": _clip(client_ip, 64),
    }
    if row is None:
        row = IikoTerminal(terminal_id=terminal_id, first_seen_at=now, **fields)
        db.add(row)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
    db.commit()


DEBIT_BLOCKED_MESSAGE = "Списание бонусов на этой кассе отключено администратором"


def resolve_terminal(
    db: Session,
    *,
    terminal_id: str = "",
    restaurant: str = "",
) -> IikoTerminal | None:
    """Находит кассу по guid из плагина или по названию ресторана (как в debit 1.1.0)."""
    tid = (terminal_id or "").strip()
    if tid:
        return db.scalar(select(IikoTerminal).where(IikoTerminal.terminal_id == tid))
    matches = _terminals_by_restaurant_name(db, restaurant)
    if not matches:
        return None
    _sort_by_last_seen(matches)
    return matches[0]


def terminal_debit_blocked(db: Session, *, terminal_id: str = "", restaurant: str = "") -> bool:
    """True, если для этой кассы запрещено списание бонусов."""
    row = resolve_terminal(db, terminal_id=terminal_id, restaurant=restaurant)
    return bool(row and row.debit_blocked)


def set_terminal_debit_blocked(db: Session, terminal_db_id: int, blocked: bool) -> bool:
    """Включает или снимает блокировку списания на кассе."""
    row = db.get(IikoTerminal, terminal_db_id)
    if row is None:
        return False
    row.debit_blocked = blocked
    db.commit()
    return True


def forget_terminal(db: Session, terminal_id: int) -> bool:
    """Удаляет запись кассы из мониторинга."""
    row = db.get(IikoTerminal, terminal_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def build_settings_kassy_summary(db: Session) -> dict[str, Any]:
    """Краткая сводка касс iiko для блока в «Настройках»."""
    dash = build_dashboard(db)
    priority = {"bad": 4, "warn": 3, "offline": 2, "ok": 1, "missing": 0}
    connected: dict[str, dict[str, Any]] = {}
    for row in dash["rows"]:
        if row.get("kind") != "terminal":
            continue
        name = (row.get("restaurant") or "—").strip()
        key = normalize_restaurant(name)
        entry = connected.get(key)
        if entry is None:
            connected[key] = {
                "name": name,
                "terminals": 1,
                "status_key": row["status_key"],
                "status_label": row["status_label"],
                "status_css": row["status_css"],
                "status_reason": row["status_reason"],
                "last_seen_label": row["last_seen_label"],
            }
            continue
        entry["terminals"] += 1
        if priority.get(row["status_key"], 0) > priority.get(entry["status_key"], 0):
            entry["status_key"] = row["status_key"]
            entry["status_label"] = row["status_label"]
            entry["status_css"] = row["status_css"]
            entry["status_reason"] = row["status_reason"]
        if row["last_seen_label"] != "—":
            entry["last_seen_label"] = row["last_seen_label"]
    connected_list = sorted(connected.values(), key=lambda item: item["name"].casefold())
    return {
        "counts": dash["counts"],
        "connected": connected_list,
        "latest_plugin_version": dash["latest_plugin_version"],
    }


def build_dashboard(db: Session) -> dict[str, Any]:
    """Сводка и строки таблицы «Кассы»."""
    now = datetime.now(timezone.utc)
    latest = get_settings().iiko_plugin_latest_version.strip() or "1.1.0"
    terminals = list(db.scalars(select(IikoTerminal).order_by(IikoTerminal.restaurant)).all())
    rows: list[dict[str, Any]] = []
    counts = {"ok": 0, "warn": 0, "bad": 0, "offline": 0}

    for terminal in terminals:
        status = compute_terminal_status(terminal, now, latest)
        counts[status.key] = counts.get(status.key, 0) + 1
        rows.append(_terminal_row(terminal, status, now, latest))

    rows.sort(key=lambda item: (item.get("restaurant") or "", item.get("machine_name") or ""))
    return {
        "counts": {
            "total": len(terminals),
            "ok": counts.get("ok", 0),
            "warn": counts.get("warn", 0),
            "bad": counts.get("bad", 0),
            "offline": counts.get("offline", 0),
        },
        "rows": rows,
        "latest_plugin_version": latest,
    }


def compute_terminal_status(
    terminal: IikoTerminal,
    now: datetime,
    latest_version: str,
) -> TerminalStatus:
    """Вычисляет статус кассы по правилам из ТЗ."""
    last_seen = _aware(terminal.last_seen_at)
    stale = (now - last_seen) > timedelta(minutes=STALE_MINUTES)
    problem_reasons: list[str] = []
    if terminal.license_ok is False:
        problem_reasons.append("Лицензия iiko не позволяет внешнюю оплату")
    if terminal.payment_type_bound is False:
        problem_reasons.append("Тип оплаты не привязан к кассе")
    if terminal.last_error and terminal.last_error_at:
        err_at = _aware(terminal.last_error_at)
        if (now - err_at) <= timedelta(minutes=ERROR_FRESH_MINUTES):
            problem_reasons.append(terminal.last_error[:120])
    if problem_reasons:
        return TerminalStatus("bad", "Проблема", "bad", "; ".join(problem_reasons))

    warn_reasons: list[str] = []
    if terminal.pending_ops > 0:
        warn_reasons.append(f"В очереди {terminal.pending_ops} операций")
    if (terminal.config_source or "").lower() == "local":
        warn_reasons.append("Используется локальный конфиг плагина")
    if _version_outdated(terminal.plugin_version, latest_version):
        warn_reasons.append(f"Версия плагина ниже актуальной ({latest_version})")
    if warn_reasons:
        return TerminalStatus("warn", "Внимание", "warn", "; ".join(warn_reasons))

    if stale:
        label = _relative_last_seen(last_seen, now)
        return TerminalStatus(
            "offline",
            "Нет связи",
            "muted",
            f"Был в сети: {label}",
        )

    return TerminalStatus("ok", "Всё хорошо", "ok", "Касса на связи, ошибок нет")


def terminal_supports_debit_code(terminal: IikoTerminal) -> bool:
    """Проверяет, объявил ли плагин поддержку debit-code."""
    try:
        features = json.loads(terminal.features or "[]")
    except json.JSONDecodeError:
        return False
    if not isinstance(features, list):
        return False
    return any(str(item).strip().lower() == "debit-code" for item in features)


def _terminal_row(
    terminal: IikoTerminal,
    status: TerminalStatus,
    now: datetime,
    latest: str,
) -> dict[str, Any]:
    last_seen = _aware(terminal.last_seen_at)
    return {
        "kind": "terminal",
        "terminal_db_id": terminal.id,
        "restaurant": terminal.restaurant or "—",
        "machine_name": terminal.machine_name or "—",
        "status_key": status.key,
        "status_label": status.label,
        "status_css": status.css,
        "status_reason": status.reason,
        "plugin_version": terminal.plugin_version or "—",
        "version_outdated": _version_outdated(terminal.plugin_version, latest),
        "last_seen_label": _relative_last_seen(last_seen, now),
        "last_seen_exact": last_seen.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "pending_ops": terminal.pending_ops,
        "debit_code_supported": terminal_supports_debit_code(terminal),
        "debit_blocked": bool(terminal.debit_blocked),
        "last_error": terminal.last_error or "",
        "license_ok": terminal.license_ok,
        "payment_type_bound": terminal.payment_type_bound,
    }


def _terminals_by_restaurant_name(db: Session, restaurant: str) -> list[IikoTerminal]:
    """Все кассы с тем же нормализованным именем ресторана, что в теле debit."""
    norm = normalize_restaurant(restaurant)
    if not norm:
        return []
    return [
        row
        for row in db.scalars(select(IikoTerminal)).all()
        if normalize_restaurant(row.restaurant) == norm
    ]


def _sort_by_last_seen(terminals: list[IikoTerminal]) -> None:
    """Сортирует по убыванию last_seen — актуальная запись первой."""
    terminals.sort(
        key=lambda row: _aware(row.last_seen_at),
        reverse=True,
    )


def _relative_last_seen(last_seen: datetime, now: datetime) -> str:
    delta = now - last_seen
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} ч назад"
    return last_seen.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M")


def _version_outdated(current: str, latest: str) -> bool:
    cur = _parse_version(current)
    lat = _parse_version(latest)
    if cur is None or lat is None:
        return False
    return cur < lat


def _parse_version(value: str) -> tuple[int, ...] | None:
    cleaned = (value or "").strip().split("+")[0]
    parts = cleaned.split(".")
    nums: list[int] = []
    for part in parts[:4]:
        digits = "".join(ch for ch in part if ch.isdigit())
        if not digits:
            break
        nums.append(int(digits))
    return tuple(nums) if nums else None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def _parse_optional_time(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return _aware(parsed)
        except ValueError:
            return None
    return None
