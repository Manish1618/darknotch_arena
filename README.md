# Darkn0tch Arena

A multi-organiser CTF platform built to run events for 1000+ players: any club
can create an organisation, run events, and let players compete in teams or on
their own. Inspired by CTF7 (events) and CTFd (scoreboard), styled with the
Gh057_Society theme (`web/static/theme/`). Change the name with `SITE_NAME`.

## Architecture

```
                         Players
                            │ HTTPS 443 (HTTP/3 too)
                 ┌──────────▼──────────┐
                 │ caddy: load balancer │  TLS (Let's Encrypt), HSTS, size limits,
                 │  /api/*  → api group │  least-connections, retries, passive
                 │  /*      → web group │  health checks, real client IP
                 └─────┬──────────┬─────┘
         network to_web│          │network to_api
        ┌──────────────▼──┐   ┌───▼───────────────────────────┐
        │ web × WEB_REPL.  │   │ api × API_REPLICAS (stateless) │
        │ nginx, static UI │   │ FastAPI, one process each      │
        └─────────────────┘   └──────┬────────────────┬────────┘
                                     │ network data (internal, no internet)
                             ┌───────▼─────┐   ┌──────▼──────┐
                             │ redis        │   │ db: Postgres │
                             │ rate limits, │   │ (+ migrate   │
                             │ board cache  │   │  job first)  │
                             └─────────────┘   └─────────────┘
```

- **Frontend group (`web`)**: unprivileged nginx serving the website files. It
  has no route to the API, Redis or the database.
- **Backend group (`api`)**: identical, stateless FastAPI containers. Any of them
  can answer any request, because everything shared lives in Postgres (accounts,
  sessions, sign-in flows) or Redis (rate limits, the scoreboard cache). Caddy
  finds every container through Docker's DNS, so `docker compose up -d --scale
  api=8` takes effect within seconds.
- **Load balancer (`caddy`)**: the only container with public ports.
  Containers that refuse connections or time out are taken out for 30 s and the
  request is retried on another one.
- **`migrate`**: runs `alembic upgrade head` once before any API container
  starts, so replicas never race to change the schema.
- **Networks**: `data` is internal (no internet, no ports). `to_web` is
  internal. `to_api` gives the API its outbound access for email and the
  sign-in providers.

### Sizing

RAM is rarely the limit; CPU is. Each API container runs one Python process,
so run about **one API container per CPU core** (`API_REPLICAS`). Keep
`API_REPLICAS × (DB_POOL_SIZE + DB_POOL_OVERFLOW)` under Postgres's 200
connections. Measure your own server before the event with the load test:

    k6 run -e BASE=https://staging.example.org -e EVENT=your-event -e VIEWERS=1500 loadtest/k6.js

It fails if the scoreboard's p95 latency goes over 0.5 s or more than 1% of
requests fail. See the comments in `loadtest/k6.js` for logged-in players.

## Sign-in

| Method | How it's protected |
|---|---|
| **Sign in with Google** | OpenID Connect authorization code flow with PKCE, state and nonce; state is single-use, expires in 10 minutes and is bound to the browser by a cookie. ID token signatures are checked against Google's published keys, along with issuer, audience, expiry and nonce. Only emails Google has verified are accepted. 2FA is still asked for if it's on. |
| **Email and password** | Sign up in one step: email, username, password. Throwaway domains are refused and the domain must accept mail. Argon2id hashes. **Forgot password** sends a 6-digit reset code to the account's email. |
| **Two-factor login (MFA)** | Any authenticator app (Google Authenticator, Authy, 2FAS...), ten single-use recovery codes, asked for after both password and Google sign-in. |

The first sign-in from a new place and a newly linked Google account each get a "was this you?" email.

### Setting up Google sign-in

In Google Cloud Console: APIs & Services, OAuth consent screen (External,
app name, your email), then Credentials, Create credentials, OAuth client ID,
Web application. Add the authorised redirect URI
`https://YOUR_DOMAIN/api/auth/oauth/google/callback`, then put the client id
and secret in `.env` as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` and run
`docker compose up -d`. Until both are set, the Google button simply doesn't show.

## Deploy

    cp .env.example .env     # fill in every value; secrets: openssl rand -hex 32
    docker compose up -d --build
    docker compose exec api python -m app.cli make-admin you@example.org   # after signing up

The app refuses to start in production without `FLAG_PEPPER`, `SECRET_KEY`,
`SMTP_HOST` and `REDIS_URL`. Behind Cloudflare, uncomment the block at the top
of the `Caddyfile`; otherwise every player looks like a few Cloudflare
addresses and the per-IP login limit would lock out a whole event.

| Setting | What it does |
|---|---|
| `DOMAIN` | Your site's name; also the only Host the app answers to |
| `SITE_NAME` | Name in the header, emails and authenticator apps |
| `DB_PASSWORD`, `REDIS_PASSWORD` | Database and Redis passwords |
| `FLAG_PEPPER` | Keys the stored flag hashes. Never change it after adding flags |
| `SECRET_KEY` | Encrypts 2FA secrets; keys recovery-code and email-code hashes. Never change it |
| `API_REPLICAS`, `WEB_REPLICAS` | Containers in each group (default 4 and 2) |
| `DB_POOL_SIZE`, `DB_POOL_OVERFLOW` | Postgres connections per API container (default 10 and 10) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_SECURITY` | Outgoing mail for codes and security notices (required). `SMTP_HOST=log` writes them to `docker compose logs api` instead, for trying the site before mail is set up |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Sign in with Google (optional) |
| `ORGANISER_MFA` | `required` (default): organisers and site admins need 2FA first |
| `BOARD_CACHE_S` | Seconds one computed scoreboard is shared by every viewer (default 3). Scoring changes clear it at once |
| `LOGIN_FAIL_IP` | Failed logins allowed per IP per 5 minutes (default 100: a lab may share one IP) |
| `MAIL_PER_IP_HOUR` | Code emails one IP may trigger per hour (default 120) |
| `SESSION_IDLE_S`, `SESSION_TTL_S` | Idle logout (12 h) and longest login (7 days) |
| `MAX_UPLOAD_MB` | Largest challenge file (default 50) |

### Database changes

Schema changes are Alembic migrations in `migrations/versions/`; the `migrate`
service applies them on every deploy. After changing a model:

    DATABASE_URL=sqlite:///./scratch.db alembic revision --autogenerate -m "what changed"

## Pages

| Path | What |
|---|---|
| `/` | Published events |
| `/register`, `/login`, `/reset` | Accounts: Sign in with Google, email and password, Forgot password (emailed code) |
| `/account` | 2FA, recovery codes, password, where you're logged in, security activity |
| `/e/{event}` | Event page, rules, announcements, join as a team or as a solo player |
| `/e/{event}/challenges` | Challenge board |
| `/e/{event}/scoreboard` | The live stage (orbit, gauges, capture signal, Big screen), then tabs: Standings (rows glide on overtakes), Race chart, Solve matrix, Challenges. Each shows only if the organisers allow it |
| `/dashboard` | Your organisations and events |
| `/manage/{event}` | **Control room** (status, quick actions, live counters, team or individual mode, every team with its players, disqualify, remove a player), Challenges editor, Live (announcements, points, lockouts, submissions), **Player view** (who may see each part), Settings, Activity |
| `/admin` | Site admins: overview, users (disable, log out everywhere, reset 2FA), sign-up rules (open/closed, allowed email domains, throwaway inboxes), security log, events |

## Run locally

    python -m pip install -r requirements-dev.txt
    COOKIE_SECURE=0 uvicorn app.main:app --reload     # SQLite at ./dev.db, no Redis needed
    python -m pytest -q

In development, emails (sign-up and reset codes) are printed to the server log
instead of being sent, and tables are created at startup.

## How scoring works

- Static challenges are worth their value. Dynamic ones use CTFd's formula:
  full value for the first solver, decaying quadratically to the minimum after
  `decay` solves; every solver's points follow the current value.
- First blood: the earliest solve per challenge, with an optional bonus.
- Hints may cost points; wrong flags can cost points; organisers can add or
  deduct points with a reason the team sees.
- Ranking: score, then whoever reached it first. Freeze: after `freeze_at` the
  public sees the board as of then; organisers keep seeing it live.
- Hidden challenges and disqualified teams don't count: a disqualified team's
  solves don't score, don't take first blood and don't lower dynamic values.

## Security

[SECURITY.md](SECURITY.md) lists each attack the platform is built to resist and
what stops it, plus the gaps still open.

## API

| Method | Path | Who |
|---|---|---|
| POST | /api/auth/register, /login, /logout | anyone / logged in |
| POST | /api/auth/password/forgot, /password/reset | anyone (emailed code) |
| GET | /api/auth/oauth/google/start, /callback; POST /oauth/finish | anyone |
| GET | /api/auth/me, /sessions, /activity | logged in |
| POST | /api/auth/2fa/setup, /2fa/enable, /2fa/disable, /2fa/recovery-codes, /password | logged in |
| POST/GET | /api/orgs, /orgs/{org}/members, /orgs/{org}/events | organisers |
| GET, PATCH | /api/events, /api/events/{event} | public / organisers |
| POST, GET | /api/events/{event}/teams, /teams/join, /teams/leave, /teams/me | players |
| GET, POST | /api/events/{event}/challenges[/{id}[/attempt, /solves]], /hints/{id}/unlock, /files/{id} | team members |
| GET | /api/events/{event}/scoreboard, /scoreboard/graph, /scoreboard/matrix, /notices, /announcements | per the event's Player view rules |
| … | /api/events/{event}/manage/challenges, flags, hints, files, submissions, stats, teams, awards, lockouts, announcements, audit | organisers |
| PUT | /api/events/{event}/manage/visibility | organisers |
| POST, DELETE | /manage/teams/{id}/ban, /unban, /manage/teams/{id}/members/{user} | organisers |
| GET, POST, PUT | /api/admin/overview, /users, /users/{id}/disable, /enable, /logout, /reset-2fa, /settings, /audit, /events, /events/{event}/status | site admins |
| GET | /healthz (alive), /readyz (database and Redis reachable) | containers |
