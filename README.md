# Invoice Keeper — Back

Python API, scheduler, Telegram bot and durable payment history. This is an independent Git repository; the Vue application lives in the neighboring `Front` repository. All payments are demonstrations: no money moves and generated HTML receipts are explicitly non-fiscal.

## Quick start with Docker

```powershell
Copy-Item .env.example .env
docker compose up -d --build
docker compose exec api python -m app.seed
```

Set `ADMIN_LOGIN` and `ADMIN_PASSWORD` in `.env`. The example uses `admin / change-me-before-use` for local development. The web application and authenticated HTTP API are administrator-only. Explicit demo seeding creates a sample participant record, eight example expenses, and audience groups. Participant records cannot sign in to the web application. Seeding is idempotent and never resets existing payments. No external channels are seeded.

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
3. In the admin UI, add a Telegram channel and click **Подключить чат**.
4. Add the bot to the group and allow it to send messages and documents. A group administrator runs the one-time `/connect CODE` command within ten minutes. Address it explicitly as `/connect@your_bot_username CODE` so it also works with Telegram privacy mode. An alternative for API clients is creating a channel with `config.chat_id`.
5. Use **Тест**, then inspect **Уведомления**. A running worker sends queued events.

Any participant in a connected target chat can use **Оплатить**, then confirm demo payment, without a web account. The confirmation button belongs to the participant who opened it. **Уже оплачено** records an external payment without fabricating a demo receipt. Bot callbacks cannot administer templates or users. They are validated against connected chats and a delivery for that specific invoice. Web/API access requires the administrator account; participant login and old participant sessions are rejected server-side.

The same domain service handles web and bot confirmation. A conditional revision update permits one winner on simultaneous clicks. Demo receipts are sent as HTML files to the group and retained in the invoice card. They are labeled as demonstrations.

## Channels and targeted delivery

Default broadcast means all active configured channels. An administrator can create named audience groups containing channel IDs and select groups for a recurring payment. Repeated recipients are deduplicated. In-app events retain a complete shared journal. Groups affect delivery, not web visibility.

- Email: set `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`; optionally username/password and STARTTLS. Each email channel names one recipient.
- Webhook: HTTP(S) JSON, no redirects, public addresses by default. `X-Invoice-Event-ID` is stable across retries. `X-Invoice-Signature` is the hex HMAC-SHA256 of the exact request body using the channel secret. Use the event ID to deduplicate on the receiver. Private destinations are only allowed when explicitly configured with `ALLOW_PRIVATE_WEBHOOKS=true` for a trusted development setup.
- In-app: enabled automatically as the administrator's delivery journal. It has no participant portal or payment controls.

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

Telegram/SMTP live delivery needs user-provided credentials. Without them, the admin panel and delivery journal work, but participant payment through Telegram requires configuring and starting the bot. Local Git only: no remote configured or push performed.
