# Deploying GMG Investment Intelligence

For **GMG AI Solutions** operators. Read [`LEGAL.md`](LEGAL.md) first: the
outstanding counsel review must be complete before you take payment from
anyone.

---

## 1. Requirements

- Python 3.11+
- PostgreSQL 14+ (SQLite is fine for evaluation; do not run production on it)
- A TLS-terminating reverse proxy (nginx, Caddy) — the session cookie is set
  `Secure` in production and will not survive plain HTTP
- An SMTP account or transactional-email API for verification and reset mail

## 2. Install

```bash
git clone <your-repo> gmg && cd gmg
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Configure

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # EGX_AUTH_SECRET
```

Minimum production settings:

```ini
EGX_ENV=production
EGX_BASE_URL=https://your-domain
EGX_DATABASE_URL=postgresql+psycopg://user:password@host:5432/gmg
EGX_AUTH_SECRET=<the generated value>
EGX_COOKIE_SECURE=true
EGX_REQUIRE_EMAIL_VERIFICATION=true
EGX_ALLOW_SYNTHETIC_DATA=false
EGX_LOG_FORMAT=json
EGX_ADMIN_EMAIL=you@your-domain
EGX_EMAIL_PROVIDER=smtp
EGX_SMTP_HOST=...
EGX_SMTP_USER=...
EGX_SMTP_PASSWORD=...
EGX_EMAIL_FROM=no-reply@your-domain
```

The application **refuses to start in production without `EGX_AUTH_SECRET`**.
That is deliberate: a default signing key would let anyone forge a session.

Never commit `.env`. Never paste a key into source, a commit message or an
issue. Rotating `EGX_AUTH_SECRET` signs every user out.

## 4. Initialise

```bash
python scripts/init_db.py
python scripts/refresh_universe.py
```

The **first account registered becomes the administrator**. Register yours
immediately after go-live, before the service is publicly reachable, or set
`EGX_ADMIN_EMAIL` so only your address is promoted.

## 5. Run

```bash
gunicorn backend.api.app:app \
  -k uvicorn.workers.UvicornWorker \
  --bind 127.0.0.1:8000 --workers 4 --timeout 60
```

`systemd` unit:

```ini
[Unit]
Description=GMG Investment Intelligence
After=network.target postgresql.service

[Service]
Type=simple
User=gmg
WorkingDirectory=/srv/gmg
EnvironmentFile=/srv/gmg/.env
ExecStart=/srv/gmg/.venv/bin/gunicorn backend.api.app:app \
  -k uvicorn.workers.UvicornWorker --bind 127.0.0.1:8000 --workers 4
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/srv/gmg/data

[Install]
WantedBy=multi-user.target
```

nginx:

```nginx
server {
    listen 443 ssl http2;
    server_name your-domain;

    ssl_certificate     /etc/letsencrypt/live/your-domain/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain/privkey.pem;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
server { listen 80; server_name your-domain; return 301 https://$host$request_uri; }
```

`X-Forwarded-For` matters: rate limiting and lockout key off the client IP, and
without it every request appears to come from the proxy.

## 6. Scale beyond one node

Two things are in-process and must move to shared storage before you run more
than one application node:

- **Rate limiting** (`backend/api/auth_deps.py::rate_limit`) — point it at Redis;
  the call sites do not change.
- **The scheduler** (`EGX_SCHEDULER_ENABLED`) — enable it on exactly one node,
  or the weekly report and alert sweeps run several times.

Sessions and the quote cache are already in the database and need no change.

## 7. Connect real market data

Until you do, the platform shows stored end-of-day bars, or clearly-labelled
demonstration data, and says so on every screen.

1. Sign a licence with an EGX market-data vendor. Confirm what your licence
   permits: display to subscribers, the delay, redistribution, attribution.
2. Implement `LicensedQuoteProvider.get_quotes()` in
   `backend/market/quotes.py`. Map the vendor response onto `QuoteData`, set
   `source` to the vendor's name, and set `delayed_minutes` to the delay your
   licence **actually** grants.
3. Set `EGX_MARKET_DATA_API_KEY` and `EGX_QUOTE_DELAY_MINUTES`.
4. Verify at `/data-sources` that the provider reports as available and that
   quotes carry the right badge.

Do not shortcut step 2 by having the provider fall back to demo data. It is
written to raise instead, because a silent downgrade puts generated prices
behind a "live" label — the one failure this architecture exists to prevent.

## 8. Connect a payment gateway

1. Implement `PaymentProvider` in `backend/billing/payments.py`:
   `create_checkout()` and `verify_webhook()` — the latter **must** verify the
   signature before anything is written.
2. `register_provider("your-gateway", YourProvider)`.
3. Set `EGX_PAYMENT_PROVIDER`, `EGX_PAYMENT_API_KEY`, `EGX_PAYMENT_WEBHOOK_SECRET`.
4. Point the gateway's webhook at `POST /api/payments/webhook`.

Card details must never reach your server. Use the gateway's hosted or
tokenised flow.

## 9. Backups

```bash
pg_dump -Fc gmg > gmg-$(date +%F).dump
```

Back up daily and test a restore. The database holds accounts, subscriptions,
payment records and every user's watchlists and portfolio.

## 10. Go-live checklist

**Security**

- [ ] `EGX_AUTH_SECRET` set to a fresh random value, not shared with any other environment
- [ ] `EGX_COOKIE_SECURE=true` and HTTPS enforced end to end
- [ ] `EGX_ENV=production`
- [ ] `EGX_ALLOW_SYNTHETIC_DATA=false`
- [ ] `EGX_REQUIRE_EMAIL_VERIFICATION=true`
- [ ] Admin account registered and its password stored in a password manager
- [ ] `.env` permissions `600`, owned by the service user, not in git
- [ ] Reverse proxy sets `X-Forwarded-For` and the security headers above
- [ ] Database not reachable from the public internet

**Data**

- [ ] Demonstration data deleted (`SELECT count(*) FROM price_history WHERE source LIKE 'SYNTHETIC%'` returns 0)
- [ ] Universe refreshed from a real source
- [ ] Licensed market-data provider connected, or the demo labelling accepted and visible
- [ ] `/data-sources` reviewed and accurate

**Legal — see [`LEGAL.md`](LEGAL.md)**

- [ ] **Egyptian counsel has reviewed every legal document and confirmed what authorisation, if any, the business model requires**
- [ ] Company name, address and contact details correct on every legal page
- [ ] No claim of FRA licensing anywhere
- [ ] Prohibited language audit clean (see `LEGAL.md` §6)

**Billing**

- [ ] Payment gateway connected and its webhook signature verified, **or** manual mode understood and the pricing page's wording confirmed accurate
- [ ] Refund policy operationally achievable
- [ ] Tax and invoicing handling agreed with your accountant

**Operations**

- [ ] Backups running and a restore tested
- [ ] Scheduler enabled on exactly one node
- [ ] Email delivery verified end to end (register a test account and reset its password)
- [ ] Error monitoring in place

## 11. Containers

The repository ships a production `Dockerfile`, a `docker-compose.yml` for a
single VPS, and configs for Fly.io and Railway. All three platforms build the
same image.

### The image

Two stages, so the runtime carries no compiler and no build cache. It runs as a
non-root user (`uid 10001`), writes only to `/app/data`, and its `HEALTHCHECK`
calls `/api/health` — which reports the real quote provider and whether payments
are actually processed, so a green container is one that can answer for itself.

`docker-entrypoint.sh` refuses to start in production without
`EGX_AUTH_SECRET`, refuses outright with `EGX_ALLOW_SYNTHETIC_DATA=true` in
production, and warns when `EGX_COOKIE_SECURE` is not set. Failing at boot beats
serving with a forgeable session cookie.

```bash
docker build -t gmg .
docker run --env-file .env -p 8000:8000 gmg
```

### A VPS with Compose

```bash
cp .env.example .env          # fill it in, including POSTGRES_PASSWORD
docker compose up -d --build
```

Three services: the application, PostgreSQL, and **a separate scheduler
container**. That separation matters. The scheduler runs the weekly report, the
daily snapshot, the alert sweep and subscription expiry; running it inside the
web workers would generate the weekly report once per worker and email every
subscriber four copies. The web containers therefore set
`EGX_SCHEDULER_ENABLED=false`, and the scheduler runs one replica — never two.

The app binds to `127.0.0.1:8000` only. Put nginx in front:

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/gmg
sudo ln -s /etc/nginx/sites-available/gmg /etc/nginx/sites-enabled/
sudo certbot --nginx -d your-domain
sudo nginx -t && sudo systemctl reload nginx
```

`deploy/nginx.conf` sets HSTS, a tight Content-Security-Policy (the app loads no
third-party scripts, fonts, frames or analytics, so it can afford to be tight),
an edge rate limit on the auth routes, and — importantly — `X-Forwarded-For`.
The application keys rate limiting and account lockout off that header; without
it every request appears to come from the proxy and the limits protect nothing.

### Fly.io

```bash
fly launch --no-deploy --copy-config
fly postgres create --name gmg-db && fly postgres attach gmg-db
fly secrets set EGX_AUTH_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
fly secrets set EGX_BASE_URL="https://<your-app>.fly.dev" \
                EGX_SMTP_HOST=... EGX_SMTP_USER=... EGX_SMTP_PASSWORD=... \
                EGX_EMAIL_FROM=... EGX_ADMIN_EMAIL=...
fly deploy
fly scale count scheduler=1     # exactly one, never more
```

`fly postgres attach` sets `DATABASE_URL`, which does **not** name a driver.
SQLAlchemy needs one, so set `EGX_DATABASE_URL` yourself with the
`postgresql+psycopg://` scheme rather than relying on the attached value.

### Railway

Point Railway at the repo; `railway.json` selects the Dockerfile and the health
check. Add a PostgreSQL plugin, then set `EGX_AUTH_SECRET`, `EGX_BASE_URL` and
`EGX_DATABASE_URL` (again with the `postgresql+psycopg://` scheme). Railway
injects `$PORT`, which the start command already honours.

Railway runs one service per deployment, so add a **second service from the same
repo** with the start command `python scripts/run_server.py --scheduler-only`
and `EGX_SCHEDULER_ENABLED=true`, left at one replica.

### Whichever platform

The scheduler is the one component that must not be scaled horizontally. The web
tier can run as many replicas as you like: sessions and the quote cache live in
the database. Only the in-process rate limiter is per-node — move it to Redis
(`backend/api/auth_deps.py::rate_limit`) before running several web nodes, or
lean on the edge limit in nginx.

---

## 12. Health

`GET /api/health` returns status and mode. The admin panel at `/admin` shows
user and subscription counts, quote-provider status, email delivery outcomes,
data-source health and the audit log.
