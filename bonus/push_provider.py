"""Отправка push-уведомлений в приложение «Спасибо»."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from bonus.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PushResult:
    """Результат отправки push."""

    ok: bool
    detail: str = ""


class PushProvider:
    """Интерфейс провайдера push."""

    def send_debit_code(
        self,
        spasibo_user_id: str,
        code: str,
        amount_kopecks: int,
        restaurant: str,
    ) -> PushResult:
        raise NotImplementedError


class LogPushProvider(PushProvider):
    """Пишет факт отправки в лог без самого кода (для разработки)."""

    def send_debit_code(
        self,
        spasibo_user_id: str,
        code: str,
        amount_kopecks: int,
        restaurant: str,
    ) -> PushResult:
        logger.info(
            "Push (log): debit code for spasibo_user_id=%s amount=%s restaurant=%s",
            spasibo_user_id,
            amount_kopecks,
            restaurant,
        )
        return PushResult(ok=True, detail="log")


class HttpPushProvider(PushProvider):
    """HTTP-провайдер; формат тела меняется в _build_payload."""

    def send_debit_code(
        self,
        spasibo_user_id: str,
        code: str,
        amount_kopecks: int,
        restaurant: str,
    ) -> PushResult:
        settings = get_settings()
        url = settings.spasibo_push_url.strip()
        if not url:
            return PushResult(ok=False, detail="SPASIBO_PUSH_URL не задан")
        payload = _build_push_payload(spasibo_user_id, code, amount_kopecks, restaurant)
        headers = {"Authorization": f"Bearer {settings.spasibo_push_key.strip()}"}
        last_error = ""
        for attempt in range(2):
            try:
                with httpx.Client(timeout=5.0) as client:
                    response = client.post(url, json=payload, headers=headers)
                if response.status_code < 300:
                    return PushResult(ok=True, detail=f"http {response.status_code}")
                last_error = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                last_error = str(exc)
        return PushResult(ok=False, detail=last_error or "push failed")


def get_push_provider() -> PushProvider:
    """Возвращает провайдер по SPASIBO_PUSH_PROVIDER."""
    mode = get_settings().spasibo_push_provider.strip().lower() or "log"
    if mode == "http":
        return HttpPushProvider()
    return LogPushProvider()


def _build_push_payload(
    spasibo_user_id: str,
    code: str,
    amount_kopecks: int,
    restaurant: str,
) -> dict[str, object]:
    """Тело запроса к API «Спасибо» — править здесь при смене контракта."""
    amount_rub = amount_kopecks // 100
    place = restaurant.strip() or "ресторан"
    return {
        "user_id": int(spasibo_user_id) if spasibo_user_id.isdigit() else spasibo_user_id,
        "title": "Код подтверждения списания",
        "body": (
            f"Код {code}: списание {amount_rub:,} бонусов, ресторан «{place}». "
            "Назовите код сотруднику только если вы сами оплачиваете."
        ).replace(",", " "),
        "type": "teams_bonus_debit_code",
    }
