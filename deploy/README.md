# VPS deployment

Checkout layout: `/opt/invoice-keeper/Back` and `/opt/invoice-keeper/Front`.
Sources are independent Git repositories. Runtime secrets live only in `Back/.env`
and are never committed or included in Docker build contexts.

## Runtime configuration

Create `Back/.env` from `.env.example` and set real administrator, database and
integration credentials. For this host configure:

```dotenv
FRONTEND_ORIGIN=https://aezainvoice.xxxmgxxx.com
COOKIE_SECURE=true
```

Set a unique `POSTGRES_PASSWORD` before the first database start. Do not change
that value on an existing database without also rotating its database password.
Keep `.env` readable only by the deployment account (`chmod 600 .env`).

## Start and update

```sh
cd /opt/invoice-keeper/Back
git pull --ff-only
docker compose --profile telegram up -d --build

cd /opt/invoice-keeper/Front
git pull --ff-only
docker compose up -d --build
```

The backend must be started first to create its shared Docker network. Only one
bot polling process may run for the configured Telegram token. Stop the local
bot/worker before enabling their VPS counterparts.

On PostgreSQL, committed pending deliveries wake the worker immediately through
`LISTEN/NOTIFY` (migration `0011`). This covers new invoices, payment-state
updates, channel tests and manual retries. The database queue remains durable;
`WORKER_INTERVAL_SECONDS` still controls periodic reminder scans and retries and
provides a fallback if the listener disconnects. SQLite uses periodic checks.

For a new demonstration database only, explicitly seed example expenses:

```sh
cd /opt/invoice-keeper/Back
docker compose exec -T api python -m app.seed
```

## Caddy

Install Caddy as a system service. Set the domain A record to the VPS public IP
and allow inbound TCP 80/443. The checked-in Caddyfile routes `/api/*` and `/health`
to the backend and other paths to the Vue frontend, retaining one HTTPS origin.
Both application ports bind to loopback only. Caddy manages HTTPS certificates
and renewals; its storage must persist across restarts.

```sh
sudo cp /opt/invoice-keeper/Back/deploy/Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy
```

PostgreSQL and attachments persist in Docker volumes. Normal updates never use
`docker compose down -v`. Back up those volumes and `.env` separately from Git.

## Verification

Check `docker compose ps` in both checkouts, `https://aezainvoice.xxxmgxxx.com/health`,
admin login, protected API rejection without a session, and `journalctl -u caddy`.
The admin's `/api/v1/docs` is available after login. SMTP requires outbound access
to the configured mail provider port (465 for SSL or 587 for STARTTLS).
