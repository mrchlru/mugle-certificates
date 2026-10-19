# Timeweb App Platform

Один контейнер: сайт, карточки, касса и кабинет HR.

1. Создайте приложение из Dockerfile в корне. Порт контейнера **8080**.
2. Health check: `GET /health`.
3. Подключите managed PostgreSQL и задайте переменные.
4. Откройте сайт, войдите как HR и загрузите таблицу сертификатов в разделе «Импорт».

## Переменные

| Переменная | Зачем |
| --- | --- |
| `DATABASE_URL` | Строка PostgreSQL из панели Timeweb. `postgres://` тоже подойдёт |
| `SECRET_KEY` | Длинная случайная строка. Без неё сессии легко подделать |
| `APP_PUBLIC_URL` | `https://ваш-домен`. Этот адрес попадает в QR |
| `HTTPS_ONLY` | `true` |
| `SEED_DEMO` | `true` только для первой проверки. Для боевой базы — `false` |
| `ADMIN_LOGIN` / `ADMIN_PASSWORD` | Главный администратор. Создаётся/обновляется при каждом старте из env. Полный доступ + раздел «Доступ» и «Контроль» |
| `ADMIN_NAME` | Отображаемое имя главного администратора |
| `HR_LOGIN` / `HR_PASSWORD` | Первый вход HR, если в базе ещё нет HR |
| `MANAGER_LOGIN` / `MANAGER_PASSWORD` | Первый управляющий |
| `MANAGER_RESTAURANT` | Ресторан, который пишется в журнал рядом с его именем |
| `S3_ENDPOINT_URL` | Endpoint Object Storage (Timeweb / MinIO). Можно пустым для AWS |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | Ключи доступа к бакету |
| `S3_BUCKET` | Имя бакета для обложек карточек |
| `S3_REGION` | Регион, для Timeweb обычно `ru-1` |
| `S3_PUBLIC_BASE_URL` | Публичный URL бакета или CDN без слэша в конце |
| `S3_PREFIX` | Папка внутри бакета, по умолчанию `templates` |
| `IIKO_API_KEY` | Общий секрет с плагином iikoFront. Плагин шлёт его в заголовке `X-Iiko-Key`. Пусто = API `/api/iiko/*` выключен. Не путать с Module ID / binding.jwt портала iiko |
| `GOOGLE_SHEETS_SPREADSHEET_ID` | ID Google Таблицы учёта (из URL `/d/ID/edit`) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | JSON ключ сервисного аккаунта одной строкой. Таблицу нужно расшарить на `client_email` из JSON (редактор). На Timeweb обычно задают именно это |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Альтернатива: путь к файлу ключа внутри контейнера. Если файл недоступен, берётся `GOOGLE_SERVICE_ACCOUNT_JSON` |
| `GOOGLE_WALLET_ENABLED` | `true` — кнопка Add to Google Wallet и синк Loyalty Object. Без Issuer ID / ключа всё равно выключено |
| `GOOGLE_WALLET_ISSUER_ID` | Числовой Issuer ID из [Pay & Wallet Console](https://pay.google.com/business/console) |
| `GOOGLE_WALLET_CLASS_SUFFIX` | Суффикс Loyalty Class, по умолчанию `teams_bonus` → class id `{issuerId}.teams_bonus` |
| `GOOGLE_WALLET_SERVICE_ACCOUNT_JSON` | JSON ключ SA для Wallet API (роль Developer у Issuer). Если пусто — берётся `GOOGLE_SERVICE_ACCOUNT_JSON` |
| `GOOGLE_WALLET_SERVICE_ACCOUNT_FILE` | Файл ключа Wallet SA; иначе как у Sheets |
| `GOOGLE_WALLET_PROGRAM_NAME` | Имя программы на карте (по умолчанию «Бонусная карта», не дублировать с issuer) |
| `GOOGLE_WALLET_ISSUER_NAME` | Имя эмитента (по умолчанию «Mugle Rest») |
| `GOOGLE_WALLET_LOGO_URL` | HTTPS URL логотипа class (квадрат). Пусто = `{APP_PUBLIC_URL}/static/logo-teams.png` |

`DATA_DIR` нужен только для SQLite и локальных загрузок без S3. Файл базы тогда должен жить на постоянном диске, смонтированном в `/data`. Иначе при следующем деплое сертификаты исчезнут. Для боевого запуска берите PostgreSQL.

Обложки: задайте все `S3_*` (включая `S3_PUBLIC_BASE_URL`) — тогда баннеры уходят в Object Storage и отдаются с CDN/публичного URL, а не из контейнера. Без S3 картинки пишутся в `/data/uploads` и пропадают при деплое, если нет постоянного тома.

После деплоя: `https://ваш-домен/health` отвечает `{"status":"ok"}`.

Первые логины лежат в логе контейнера одной строкой с путём к `CREDENTIALS.txt` и в переменных, которые вы задали сами.
