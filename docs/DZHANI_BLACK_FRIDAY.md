# Интеграция «Джани · Чёрная пятница» ↔ Teams Bonus

**Версия контракта:** v1

Витрина продажи сертификатов живёт в отдельном приложении (`07-черная пятница джани`).  
После оплаты ЮKassa она вызывает Teams Bonus для выпуска карты.

## Переменные Teams Bonus

| Переменная | Назначение |
|---|---|
| `DZHANI_API_KEY` | Ключ для `X-Dzhani-Key` (fallback: `IIKO_API_KEY`) |
| `DZHANI_DEFAULT_SEGMENT_ID` | Сегмент по умолчанию, если витрина не передала |
| `DZHANI_DEFAULT_TEMPLATE_ID` | Шаблон баннера по умолчанию |
| `DZHANI_DEFAULT_COLOR` | Цвет карты (`#b3122b`) |
| `DZHANI_DEFAULT_EXPIRES_ON` | Срок `YYYY-MM-DD` |

## API

Аутентификация: заголовок `X-Dzhani-Key`.

### `POST /api/dzhani/issue`

Идемпотентность: `Idempotency-Key` (ключ пишется в `note` как `idk:…`).

Тело (JSON):

- `nominal_rub`, `balance_rub` — рубли
- `contact` — email или телефон покупателя
- `order_id`, `note`, `expires_on`
- `segment_id`, `template_id`, `color`
- `ledger_sheet` — по умолчанию `thanks`
- `single_use` — по умолчанию `true`

Ответ: `public_number`, `short_code`, `token`, `card_url`, `balance_rub`, `status`.

### `GET /api/dzhani/card?public_number=`

Статус и баланс карты для синхронизации реестра витрины.
