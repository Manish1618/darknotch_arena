# Security

A CTF platform is attacked by the people it is built for, so every part of it
assumes the players are skilled and some of them will try. This page lists
what an attacker might try and what stops it.

## Accounts and logins

| Attack | What stops it |
|---|---|
| Stolen password database | Passwords are argon2id hashes, rehashed automatically when the parameters get stronger. 2FA secrets are encrypted with AES-256-GCM using a key derived from `SECRET_KEY`, which is not in the database. Recovery codes and session tokens are stored only as keyed or SHA-256 hashes. |
| Password guessing | 8 failed logins per account per 5 minutes, then the account waits. 100 per IP (a whole college lab can share one IP). Common passwords, passwords under 10 characters, and passwords containing the username or email are refused. |
| Finding out who has an account | Login gives the same error and takes the same time for a wrong password and an unknown email (unknown emails are checked against a dummy hash). |
| Phished or reused password | TOTP two-factor login with a QR code. Codes are checked against a window of one step either side, and a code that has been used can't be used again. Ten single-use recovery codes are shown once when 2FA is turned on. |
| Taking over an organiser account | With `ORGANISER_MFA=required` (the default in production), nobody can create organisations or events, or touch an event's settings, challenges, flags, points or announcements, until their account has 2FA on. |
| Attacker turns 2FA off on a stolen session | Turning 2FA off needs the password and a current code. Making new recovery codes needs a code from the app. |
| Attacker keeps a stolen session alive | Turning 2FA on, turning it off, and changing the password all log out every other session. The Account page lists every session (browser, IP, last active) with a button to end it or all the others. Sessions end after 12 hours idle and 7 days at most. |
| Noticing a break-in | The Account page shows your own security log: logins, failed logins, recovery-code logins, password and 2FA changes, from which IP. |

## Sign-up and sign-in

| Attack | What stops it |
|---|---|
| Fake or throwaway email | Throwaway inbox domains are refused and the domain must accept mail (DNS). Site admins can limit sign-up to set domains (for example `gmail.com` or the college's). Sign-up doesn't email a code, so it doesn't prove the person owns the address: a wrong address only hurts its owner, whose password resets go there. Google sign-in only accepts addresses Google has verified. |
| Mass account creation | 100 new accounts per IP per hour (a lab may share one IP). Admins can close sign-up. |
| Finding out who has an account | Login gives the same error and takes the same time for a wrong password and an unknown email. Password reset answers the same for any address and sends its email after the response. Sign-up does say when an address is taken: that's the price of one-step sign-up. |
| Flooding inboxes through the site | Reset codes: one a minute and five an hour per address, and a cap per IP per hour. |
| Hijacking a password reset | The 6-digit code goes only to the account's inbox, is stored as a keyed hash, works once for 10 minutes and dies after 5 wrong guesses. Using it ends every session, and an account with 2FA still needs its code to log in. |
| Forged or replayed "Sign in with Google" | Authorization code flow with PKCE; state and nonce are random, single-use, expire in 10 minutes and are kept server-side; the state is bound to the starting browser by a cookie (no login CSRF). The ID token's signature, issuer, audience, expiry and nonce are checked against Google's published keys. Only verified emails count. The redirect after sign-in can only stay on this site. |
| Taking over an account through Google | A Google account links to an existing account only through an email Google has verified, the owner gets an email about it, and an account with 2FA still asks for its code after the Google sign-in. |
| Unnoticed sign-ins | The first sign-in from a new IP in 90 days and a newly linked Google account each send a "was this you?" email. |

## Site admins and organisers

| Attack | What stops it |
|---|---|
| Becoming a site admin through the web | Impossible: admin is granted only on the server (`python -m app.cli make-admin`). Admin routes answer 404 to everyone else, need 2FA, and can't be used on other admins or yourself. |
| An admin quietly resetting someone's 2FA | Needs a fresh code from the admin's own authenticator, ends the person's sessions, and is written to both the admin's and the person's security logs. |
| Players reading what organisers hid | Each part of an event (scoreboard, race chart, live feed, challenge stats, solver lists, team list, solve matrix) is checked by the API route that serves it, not just hidden in the page; the solve matrix starts organisers-only. |
| A disqualified team keeping its edge | Its solves stop scoring, stop counting toward first blood and stop lowering dynamic values; it can't submit flags, buy hints, or take new members. |

## Sessions and the browser

| Attack | What stops it |
|---|---|
| Stealing the cookie with script | The session cookie is HttpOnly, Secure, SameSite=Lax and uses the `__Host-` prefix, so no script, plain-http page or subdomain can read or plant it. |
| Cross-site script injection (XSS) | Content-Security-Policy allows scripts only from the site itself, with no inline scripts. Every value from the server is HTML-escaped before it reaches the page. Challenge descriptions use a small Markdown subset that escapes first and only allows http(s) links. |
| Cross-site request forgery | Every state change needs the per-session `X-CSRF-Token` header, and any POST/PATCH/DELETE (login and register included) carrying another site's `Origin` is refused. |
| Clickjacking | `X-Frame-Options: DENY` and `frame-ancestors 'none'`. |
| Leaking data through other windows or caches | `Cross-Origin-Opener-Policy` and `Cross-Origin-Resource-Policy` are `same-origin`; API answers are `Cache-Control: no-store` so shared lab computers don't keep them; `Referrer-Policy: same-origin`; camera, microphone and location are switched off by `Permissions-Policy`. |
| Downgrade to http | HSTS for a year; Caddy redirects http to https. |
| Host-header tricks | The app answers only to `DOMAIN` (`ALLOWED_HOSTS`). |

## Flags, scoring and the game

| Attack | What stops it |
|---|---|
| Reading flags from the database or the organiser screens | Flags are stored only as HMAC-SHA256 keyed with `FLAG_PEPPER`. Organisers can add and remove flags but never read them back. The audit log never contains a flag. |
| Brute-forcing flags | 10 wrong flags a minute per player and 30 per team. Per event, organisers can also lock a team out of a challenge after N wrong flags for M minutes, take points off for every wrong flag, and cap attempts per challenge. |
| Timing attacks on flag checks | Flags are compared in constant time, against every flag of the challenge. |
| Double-solving or racing hint purchases | One solve per team per challenge and one unlock per hint per team are database constraints; buying a hint locks the team row. |
| Seeing challenges or drafts early | Challenges appear only to team members once the event has started. Drafts, other organisations' events and hidden challenges return the same 404 as things that don't exist. |
| Players using organiser tools | Every organiser route checks membership of the event's organisation and returns 404 otherwise. Organisers can't play in their own events. |
| An organiser abusing the tools | Every organiser change (settings with old and new values, challenges, flags added or removed, hints, files, point changes, lifted lockouts, announcements) is recorded with who, when and from which IP, and shown on the event's Activity tab. |

## Files and requests

| Attack | What stops it |
|---|---|
| Uploading a script and getting it run | Files are stored under random names outside the web folder and always served as `application/octet-stream` downloads, only to players who can see the challenge. |
| Huge requests to exhaust memory or disk | 1 MB for any request, 50 MB for challenge files, enforced both by Caddy and by the app (also for chunked uploads without a length). |
| Unexpected input | Strict schemas reject unknown fields; slugs, names and lengths are whitelisted. |

## Server

| Attack | What stops it |
|---|---|
| Breaking out of the app container | Runs as a non-root user with a read-only filesystem, every Linux capability dropped and `no-new-privileges`. |
| Reaching the database from outside | Postgres sits on an internal-only network; only Caddy is published (ports 80 and 443). |
| Running with weak secrets | In production the app refuses to start unless `FLAG_PEPPER` and `SECRET_KEY` are at least 32 characters and not the example values. |
| Known-vulnerable libraries | Dependencies are pinned in `requirements.txt`. `pip-audit -r requirements.txt` reported no known vulnerabilities and `bandit -r app` reported no issues on 2026-10-15. Re-run both before each event. |

## Load and addresses

| Attack | What stops it |
|---|---|
| Hiding behind a forged `X-Forwarded-For` to dodge the per-IP limits | Caddy hands the app exactly one address: the connecting client's, or, with the Cloudflare block in the `Caddyfile` turned on, the `CF-Connecting-IP` that only requests from Cloudflare's own ranges may set. Whatever a player sends in that header is replaced. |
| A whole event locked out because everyone shares Cloudflare's IPs | Same Cloudflare block: limits count the player's real address, not the proxy's. |
| Hundreds of viewers polling the public board | One computed board is shared for up to `BOARD_CACHE_S` seconds and rebuilt as soon as a solve, hint, penalty, point change or challenge edit is committed. The chart and matrix are fetched only while their tab is open, and only by viewers whose tab is visible. |
| Busy moments exhausting database connections | A pool of `DB_POOL_SIZE` + `DB_POOL_OVERFLOW` Postgres connections per API container, with dead connections detected before use; Postgres allows 200. |
| Limits that reset by hitting a different container | Login, sign-up, flag and email-code limits are counted in Redis, shared by every API container. If Redis is unreachable the API refuses with 503 rather than run without limits. |
| One broken container taking requests | Caddy stops sending to a container that refuses connections or times out, and retries the request elsewhere; `/readyz` reports one that lost the database or Redis. |
| Reaching internal services | Only Caddy publishes ports. The frontend containers can't reach the API or data; Postgres and Redis sit on an internal network with no internet route; Redis needs a password and keeps nothing on disk. |

## Not done yet

- **Per-team challenge machines and their abuse limits** come with step 4
  (challenge containers). Teams will get the same kind of pop-up when they hit
  the instance limit.
- **Postgres in tests.** The suite runs on SQLite and a fake Redis today; run
  the load test (`loadtest/k6.js`) against a staging copy on the real stack
  before each event.
- **Docker Compose and Caddy configs** are checked by reading, not by an
  automated run; bring the stack up on staging once before the event.
- **Disposable-email list** is built in and can't be complete; the DNS check
  and the emailed code are what actually prove an address.

## Reporting a problem

Please report security problems privately (GitHub: Security, then Report a
vulnerability) rather than in a public issue.
