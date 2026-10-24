# Invoice Keeper — Back

Python API, scheduler, Telegram bot and durable payment history. This is an independent Git repository; the Vue application lives in the neighboring `Front` repository. All payments are demonstrations: no money moves and generated HTML receipts are explicitly non-fiscal.

## Quick start with Docker

```powershell
Copy-Item .env.example .env
docker compose up -d --build
docker compose exec api python -m app.seed
```

Set `ADMIN_LOGIN` and `ADMIN_PASSWORD` in `.env`. The example uses `admin / change-me-before-use` for local development. Both `admin` and active `user` accounts can sign in. Users can read payments, periods, reports, documents and receipts, and can confirm or correct demonstration payments. User management, payment/channel/group management, audit logs and delivery operations remain administrator-only. Explicit demo seeding creates a sample participant record, eight example expenses, and audience groups. Seeding is idempotent and never resets existing payments. No external channels are seeded.

API: `http://localhost:8000`. Health: `/health`. Start the neighboring Front repository to sign in, then open `/api/v1/docs` in that same browser. Swagger and the live `/api/v1/openapi.json` require the session cookie. The committed `openapi.json` can be read without running the application.

Compose starts PostgreSQL, API and worker. Migrations run before API startup; worker waits for a healthy API. Data and attachments use separate persistent Docker volumes. `docker compose down` keeps data. Run only one polling bot for a token.

## Local Python / SQLite

Requires Python 3.12+. SQLite is the default for local development; Docker overrides `DATABASE_URL` with PostgreSQL.

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.lock
Copy-Item .env.example .env
.venv/Scripts/python -m alembic upgrade head
.venv/Scripts/python -m app.seed
.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

In another terminal:

```powershell
.venv/Scripts/python -m app.worker
```

Do not run the local API on port 8000 while the Docker API uses that port. Local SQLite and Docker PostgreSQL are separate databases. Dependency versions are pinned in `requirements.lock`; `requirements.txt` expresses supported ranges.

## Telegram

1. Open the official https://t.me/BotFather, run `/newbot`, and create a bot. Put the issued token in `Back/.env`: `TELEGRAM_BOT_TOKEN=your_token`. This ignored file is server configuration; no token belongs in Front or in a group message.
2. From Back, run `docker compose --profile telegram up -d --build --force-recreate api worker bot`. This recreates all three services with the new token. For a local Python setup, restart API and worker, then run `python -m app.telegram_bot`.
3. In the admin UI, add a Telegram channel and click **Подключить чат**, then **Выбрать группу в Telegram**.
4. Telegram opens its group selector. Choose a group where you are an administrator and confirm adding the bot. The `startgroup` link passes the one-time code automatically; no command needs to be typed. Links expire in ten minutes. The bot must be allowed to send messages and documents. The `/connect CODE` handler remains available for older API clients; `config.chat_id` is another API option.
5. Use **Тест**, then inspect **Уведомления**. A running worker sends queued events.

Any participant in a connected target chat can use **Оплатить**, then confirm demo payment, without a web account. The confirmation button belongs to the participant who opened it. **Оплачено ранее · выбрать дату** opens a calendar for the actual payment date, including overdue invoices. A confirmation step creates the demo receipt. Paid invoices offer **Исправить дату оплаты** and **Отменить отметку**. Due dates stay unchanged; delay is recomputed. Action buttons are bound to the initiating user and chat, expire after fifteen minutes, and reject stale invoice revisions. Colored native Telegram buttons emphasize payment, receipt and cancellation. Bot callbacks cannot administer templates or users. They are validated against connected chats and a delivery for that specific invoice. Ordinary users may use the web/API payment operations; participant Telegram actions do not require a web account.

The same domain service handles web and bot confirmation. A conditional revision update permits one winner on simultaneous clicks. Demo receipts are sent as HTML files to the group and retained in the invoice card. They are labeled as demonstrations.

## Channels and targeted delivery

Default broadcast means all active configured channels. An administrator can create named audience groups containing channel IDs and select groups for a recurring payment. Repeated recipients are deduplicated. In-app events retain a complete shared journal. Groups affect delivery, not web visibility.

A new channel becomes eligible for future releases from its connection moment: for Telegram this is the successful chat link, for email and webhook it is channel creation. Invoices released earlier are not queued for that channel automatically. Channels that already existed before this rule keep their historical delivery behavior.

Payment descriptions are part of the period projection and are included in the admin/user period API, in-app and webhook payloads, Telegram messages, plain-text/HTML email, public email invoice pages and demonstration receipts. The same field is used for recurring and one-time payments.

Forecast periods are generated for budgeting but are not released to notification channels until their initial/reminder window is reached. A channel connected before release can receive that future period; a channel connected after release does not receive it retroactively. The worker automatically releases only the current three-day reminder window. It does not broadcast all old overdue debts after downtime. An administrator can explicitly request a missed reminder with `POST /api/v1/notifications/occurrences/{occurrence_id}/remind`. The endpoint is idempotent and refuses an uncertain delivery instead of blindly resending it.

- Email: set `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, and provider credentials. `SMTP_SSL=true` enables implicit TLS (usually port 465); `SMTP_SSL=false` with `SMTP_STARTTLS=true` enables STARTTLS (usually 587). Implicit TLS takes precedence over STARTTLS. Both modes verify TLS certificates. Each email channel names one recipient.
- Webhook: HTTP(S) JSON, no redirects, public addresses by default. `X-Invoice-Event-ID` is stable across retries. `X-Invoice-Signature` is the hex HMAC-SHA256 of the exact request body using the channel secret. Use the event ID to deduplicate on the receiver. Private destinations are only allowed when explicitly configured with `ALLOW_PRIVATE_WEBHOOKS=true` for a trusted development setup.
- In-app: enabled automatically as the administrator's delivery journal. It has no participant portal or payment controls.

SMTP settings describe the **sender**, not the recipients. The VPS runs this application and connects to the sender's existing mail service; it does not need to host a mail server. For example, create a dedicated Yandex mailbox, enable mail-client access and create a Mail app password, then configure:

```dotenv
SMTP_HOST=smtp.yandex.ru
SMTP_PORT=465
SMTP_SSL=true
SMTP_STARTTLS=false
SMTP_USERNAME=your_mailbox_login
SMTP_PASSWORD=your_mail_app_password
SMTP_FROM=your_mailbox_login@yandex.ru
```

Use your real sender login, app password and address. Provider instructions: https://yandex.ru/support/yandex-360/customers/mail/ru/mail-clients/others. After changing the ignored `.env`, run `docker compose up -d --force-recreate api worker` from Back. Add an **Email через SMTP** channel in the admin panel with the recipient's address, then use **Тест** and inspect the delivery journal. Recipient passwords are never needed. Live SMTP verification requires sender credentials; adapters are tested in both TLS modes.

Delivery jobs are durable and claimed atomically. Ordinary retries and restarts do not enqueue duplicate reminders for an occurrence/channel/event. Definite failures retry with backoff up to five times. A timeout or interrupted sender has an unknown result: status becomes **Проверьте доставку**, requiring inspection before manual retry. Telegram and SMTP do not provide end-to-end exactly-once delivery guarantees.

Creation sends an immediate event for the first period; within three days of the due date this is the reminder itself. Subsequent reminders are queued when an unpaid period enters the three-day window. Paid periods are skipped. Stopping a template cancels unpaid current/future periods, preserves old debts and history, and stops its reminders. Status changes update previously sent Telegram invoice messages.

Concurrent budget requests and worker runs use conflict-safe inserts for periods and delivery events. Telegram chat-link codes are consumed atomically. Retrying a partially completed Telegram status update accepts messages that already have the requested content and continues updating the remaining messages. Stopping a template only notifies recipients about previously issued periods, without sending cancellations for every future budget period.

## Domain rules

- Every period is a distinct occurrence with amount/title/currency snapshots; unpaid old periods do not stop new ones.
- Monthly, quarterly and yearly dates retain the original day anchor. Missing days use month end, then recover the anchor later. Weekly repetition is also supported.
- Due dates and actual payment dates use `APP_TIMEZONE`. A due-day payment is on time. Paid late and outstanding overdue are distinct states. Correcting actual date recomputes delay; cancelling confirmation clears actual date and payer.
- Amounts are Decimal/Numeric; RUB, USD and EUR are summed separately. No exchange rates.
- A generated receipt is voided after cancellation or date correction; date correction issues a replacement when a demo receipt existed. Audit history remains.
- Files: PDF, PNG, JPEG, TXT up to 10 MiB; signatures checked for binary formats. Files are stored outside public static paths and downloaded through authenticated endpoints. Links permit HTTP(S) only.

## One-time payments

Administrators can create a single payment with `POST /api/v1/one-time-payments` using `title`, `description`, `amount`, `currency`, `due_date` and optional `group_ids`. The response contains `payment_type: "one_time"`. A one-time payment creates exactly one period, uses the same delivery, Telegram, email, demonstration-payment, receipt, attachment and audit flows as recurring payments, and is never regenerated by the worker, restart or budget calculation.

One-time payments are included in `/api/v1/payment-occurrences`, `/api/v1/payments/summary` and `/api/v1/payments/budget`. Filter periods with `payment_type=one_time` or `payment_type=recurring`. They are excluded from `/api/v1/recurring-payments`. An administrator can cancel an unpaid one-time payment with `DELETE /api/v1/one-time-payments/{payment_id}`; this preserves the period and history and is separate from cancelling a payment confirmation.

## Authentication and configuration

Passwords use salted PBKDF2-SHA256. Sessions use opaque random tokens, hashed in the database, HttpOnly/SameSite cookies, and seven-day expiry. Logout revokes the session. Password changes and user deletion revoke that user's sessions. Admin accounts cannot be managed through API/UI; `.env` defines the active administrator at startup. Changing admin credentials requires restarting API/worker/bot. Old admin sessions are revoked at bootstrap.

`FRONTEND_ORIGIN` is a comma-separated exact origin allowlist used by CORS and mutation origin checks. Defaults cover local Vite (5173) and Docker front (8080), both on localhost/127.0.0.1. For a future HTTPS deployment set the real origin, `COOKIE_SECURE=true`, unique credentials and trusted SMTP/webhook endpoints. Secrets are never committed or returned in channel responses. The webhook secret is preserved when toggling a channel.

## Tests and specification

```powershell
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -c "from app.main import app; import json; from pathlib import Path; Path('openapi.json').write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2), encoding='utf-8')"
```

Tests default to fresh temporary SQLite databases. To test PostgreSQL, set `TEST_DATABASE_URL` to a **separate disposable test database**: fixtures create and drop application tables there. Never point it at the working database. Tests cover permissions, dates, period independence, reminder deduplication, concurrency, receipts, attachments, totals and delivery adapters.

## Demo walkthrough

Sign in as admin → connect Telegram → use **Выставить счет** and choose recipients → inspect delivery → open the connected Telegram group as a participant → confirm payment inside the notification → receive the demonstration receipt → inspect the updated status and independent next period in the admin panel. Administrative screens issue and monitor invoices; they do not offer demo payment. The authenticated administrator API retains status/date correction and cancellation endpoints for historical corrections. Compare monthly/yearly totals by currency.

Telegram/SMTP live delivery needs user-provided credentials. Without them, the admin panel and delivery journal work, but participant payment through Telegram requires configuring and starting the bot. GitHub: https://github.com/makssssko2/HackAutumn2026Back. Deployment: https://aezainvoice.xxxmgxxx.com. Environment files remain ignored.

## Email invoice actions

SMTP messages contain both a plain-text fallback and responsive HTML in the orange/peach palette. The **Оплатить счет** button opens `/pay/:token`, without an account or admin-panel access. `FRONTEND_ORIGIN` must list the public HTTPS origin first, because it is used for email links.

Each random 256-bit capability belongs to one delivered invoice and email channel and expires after 90 days. It is persisted before SMTP sends, survives uncertain delivery and retries, is unavailable while that channel is disabled, and is revoked when its email recipient changes. Anyone holding the link can confirm that invoice and download its active demo receipt, so do not forward it to outsiders. GET is read-only, including for mail scanners. POST requires explicit confirmation and the current revision; simultaneous or repeated confirmation has one winner. Cancelled invoices cannot be paid. Future actual dates are rejected. Public endpoints never grant list, admin or other-invoice access. New styling applies to newly sent emails; existing delivered messages cannot be changed.
