# Timeweb App Platform

Один контейнер: сайт, карточки, касса и кабинет HR.

1. Создайте приложение из Dockerfile в корне. Порт контейнера **8080**.
   Зависимости ставятся из папки `wheels/` (офлайн) — так сборка не упирается в лимит 1680s из‑за медленного PyPI.
2. Health check: `GET /health`.
3. Подключите managed PostgreSQL и задайте переменные.
4. Откройте сайт, войдите как HR и загрузите таблицу сертификатов в разделе «Импорт».

## Переменные

| Переменная | Зачем |
| --- | --- |
| `DATABASE_URL` | Строка PostgreSQL из панели Timeweb. `postgres://` тоже подойдёт |
| `ACCRUAL_SCHEDULER_ENABLED` | `true` по умолчанию. На **второй** реплике/test с той же БД — `false`, иначе две копии дерутся за lock (теперь проигравший повторяет раз в минуту) |
| `SECRET_KEY` | Длинная случайная строка. Без неё сессии легко подделать |
| `APP_PUBLIC_URL` | `https://ваш-домен`. Этот адрес попадает в QR |
| `HTTPS_ONLY` | `true` |
| `SEED_DEMO` | `true` только для первой проверки. Для боевой базы — `false` |
| `SUPPORT_TELEGRAM` | Контакт «Забыли пароль?» (`@nkatine`). Пусто — ссылку не показывать |
| `FORWARDED_ALLOW_IPS` | Кому Uvicorn доверяет `X-Forwarded-For` (по умолчанию `127.0.0.1`). На App Platform за Caddy часто нужно `172.16.0.0/12` или IP прокси из лога (`172.18.0.5`). Иначе в логах один IP у всех клиентов. **Не ставьте `*` без нужды** на публичном интернете |
| `LOGIN_ALLOW_IPS` | Опционально: IP через запятую, которые не блокируются (офис). **Ослабляет защиту** — используйте только если общий NAT мешает работе |
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
| `IIKO_PLUGIN_PAYMENT_TYPE_NAME` | Имя типа оплаты в Office (`Teams Pro Bonus`) |
| `IIKO_PLUGIN_PAYMENT_SYSTEM_KEY` | Ключ внешней ПС (`TeamsProBonus`) |
| `IIKO_PLUGIN_PAYMENT_SYSTEM_NAME` | Название ПС в списке Office (`Teams Pro`) |
| `GOOGLE_SHEETS_SPREADSHEET_ID` | ID Google Таблицы учёта (из URL `/d/ID/edit`) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | JSON ключ сервисного аккаунта одной строкой. Таблицу нужно расшарить на `client_email` из JSON (редактор). На Timeweb обычно задают именно это |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Альтернатива: путь к файлу ключа внутри контейнера. Если файл недоступен, берётся `GOOGLE_SERVICE_ACCOUNT_JSON` |
| `APPLE_WALLET_ENABLED` | `true` — .pkpass для Apple Wallet и кнопка Wallet Passes (Android). Google Wallet отключён |
| `APPLE_PASS_TYPE_ID` | Pass Type ID из Apple Developer (например `pass.com.mugle.bonus`) |
| `APPLE_TEAM_ID` | Team ID Apple Developer |
| `APPLE_ORGANIZATION_NAME` | organizationName в pass (Mugle Rest) |
| `APPLE_PASS_CERT_PEM` / `APPLE_PASS_KEY_PEM` | PEM сертификата Pass Type ID и ключа (или `*_FILE` / `APPLE_PASS_P12_FILE`) |
| `APPLE_WWDR_PEM` / `APPLE_WWDR_FILE` | Apple WWDR intermediate (желательно) |
| `WALLET_PROVIDER` | `walletwallet` (пилот) или `local` (своя подпись Apple) |
| `WALLETWALLET_API_KEY` | Ключ WalletWallet (`ww_live_…` или 32 hex). Без него кнопка Wallet не показывается |
| `APP_PUBLIC_URL` | Публичный адрес сайта (QR, ссылки на обороте pass). Картинки pass — data URI из шаблонов |
| `WALLET_ASSET_VERSION` | Зарезервировано; дефолтные PNG лежат в `static/wallet/` |
| `WALLET_DATE_LAYOUT` | Срок в pkpass: `owner_label` (по умолчанию, в подписи владельца) или `footer` |

`DATA_DIR` нужен только для SQLite и локальных загрузок без S3. Файл базы тогда должен жить на постоянном диске, смонтированном в `/data`. Иначе при следующем деплое сертификаты исчезнут. Для боевого запуска берите PostgreSQL.

Обложки: задайте все `S3_*` (включая `S3_PUBLIC_BASE_URL`) — тогда баннеры уходят в Object Storage и отдаются с CDN/публичного URL, а не из контейнера. Без S3 картинки пишутся в `/data/uploads` и пропадают при деплое, если нет постоянного тома.

После деплоя: `https://ваш-домен/health` отвечает `{"status":"ok"}`.

Первые логины лежат в логе контейнера одной строкой с путём к `CREDENTIALS.txt` и в переменных, которые вы задали сами.
