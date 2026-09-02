# Deploying Sparing Horse safely

This is the operator's page: what the three containers trust, what must never be reachable, how the
image protects the host, and how to upgrade, back up and roll back. The README covers features and
the quick start; MANUAL.md covers using the app.

## 1. The trust model in one picture

```
                    the internet
                         │
          ┌──────────────┴──────────────┐
          │  your reverse proxy / tunnel │   TLS ends here. The PRIVATE box has its own
          │  (Cloudflare Tunnel + Access,│   passphrase login since 0.56.0 (§2a); a proxy
          │   Caddy, nginx, Traefik …)   │   in front of it stays the first wall.
          └───┬──────────┬──────────┬───┘
   owner only │   anyone │   anyone │
              ▼          ▼          ▼
     sparinghorse   sparinghorse-   sparinghorse-
     (private)      public          demo
     tokens, keys,  SH_READONLY=1   SH_DEMO=1
     health data,   no tokens       no tokens, its own
     the scheduler  query-only DB   synthetic database
          │  ./data  │                    │  demo-data (named volume)
          └────┬─────┘                    │
          ./secrets (private only)
```

- **The private box locks itself (0.56.0)** — a passphrase, a login page, a 30-day session cookie,
  and a first-boot page that serves nothing else until a passphrase exists (§2a). That is a second
  wall, not a reason to expose it: everything it holds — the Runalyze token, the Claude key, Suunto
  tokens, blood markers, readiness notes, a one-click full-database download — still deserves a
  proxy in front. **Never publish port 8770 to a network you do not fully trust.** The compose file
  publishes no ports at all; the proxy reaches the containers by service name over the
  `sparinghorse-edge` network.
- **The public box** serves a read-only projection over the same database file. It cannot write
  (query-only connection, every mutation refused), holds no token, and withholds the medical,
  location and personal fields server-side. It is safe to expose.
- **The demo box** is the full console over a synthetic athlete on its own named volume. It is safe
  to expose; visitors can drive the engine but not the box (see the README's refused list).

## 2a. Access to the private console (0.56.0)

- **Upgrading an existing box to 0.56.0+:** `sh prepare_env.sh --check` in the compose directory
  previews, `sh prepare_env.sh` writes — it backs up `.env`, generates `SH_SECRET_KEY` from
  `/dev/urandom` when absent, and adds the Cloudflare Access bypass variables after checking their
  shape (`--team myteam --aud <64 hex>`, or it asks; blank means no bypass and the console asks for
  its passphrase, which also works). It never prints a secret and never overwrites a value that is
  already set. Keep a copy of the new `.env` somewhere safe: the key is what decrypts the token store.
- **First boot.** With no passphrase in the secrets store the console serves only `/setup`. Either
  open it and set one (12+ characters), or put `SH_PASSPHRASE=…` in `.env` before the first start and
  the page never appears. `/healthz`, the static assets and the icons are the only other things
  served until then.
- **Sessions.** A signed `HttpOnly; Secure; SameSite=Lax` cookie, 30 days per device. Changing the
  passphrase (Settings → Console access) signs every other device out. `SH_COOKIE_SECURE=0` only for
  a plain-http LAN box — browsers drop a Secure cookie over http, and the login page says so if it
  happens.
- **Lockout.** Five wrong passphrases lock the address for a minute, doubling per further attempt
  up to fifteen; thirty wrong ones from anywhere inside fifteen minutes make every address wait.
- **Forgotten.** `docker compose exec sparinghorse python SparingHorse.py passphrase --reset` clears
  it (every session is revoked, `/setup` comes back at once); `--set` sets one from the terminal.
- **Skipping the login behind a proxy that already authenticates.** Set `SH_TRUST_PROXY_AUTH=1` and
  one of:
  - **Cloudflare Access:** `SH_CF_ACCESS_TEAM=yourteam` (the team name, or
    `yourteam.cloudflareaccess.com`) and `SH_CF_ACCESS_AUD=` the application's audience tag from the
    Access application's overview. The `Cf-Access-Jwt-Assertion` header on every tunnelled request is
    verified against the team's published keys (RS256, issuer, audience, expiry); a request without a
    valid one falls back to the passphrase.
  - **A proxy on a dedicated network:** `SH_PROXY_CIDR=` the network the proxy speaks from; a request
    from inside it carrying `X-Forwarded-User` is trusted. Anything on that network can forge the
    header, so use this only where the proxy is the only thing there.
- **The secrets store is encrypted at rest.** Put a long random `SH_SECRET_KEY` in `.env` to keep the
  key off the `./secrets` volume; without it a random `secrets.key` (0600) is written beside the
  store on first start. Losing the key means re-entering the tokens in Settings — the passphrase is
  unaffected. The box refuses to start if the store is readable by other users.

## 2. What the image does to protect the host (0.55.2)

- **Runs unprivileged.** The container starts as root only long enough for `entrypoint.sh` to give
  the mounted `/data` and `/secrets` to the app user (uid/gid **10001** by default), then drops to
  that user with `setpriv` for the life of the process. Set `SH_UID` / `SH_GID` in the environment
  if the files must belong to a particular host user. A folder you created as root keeps working:
  the entrypoint re-owns it on every start.
- **Read-only root filesystem, no capabilities, no privilege escalation, a memory ceiling, a
  healthcheck** — the `x-hardening` block at the top of `docker-compose.yml`, merged into all three
  services. The app writes only to `/data`, `/secrets` and a tmpfs `/tmp`.
- **Pinned supply chain.** The base image is pinned by digest, and the Python dependencies are
  installed from `requirements.lock` with `--require-hashes`: a rebuild installs exactly the wheels
  that were reviewed, or fails. Bump either deliberately (see §6).
- **No third-party script host.** Leaflet is served from the image (`static/vendor/`); the
  Content-Security-Policy admits only the page's own nonce'd scripts. Map tiles still come from
  OpenStreetMap on the private box, and web fonts from Google Fonts on every box.
- **Abuse dampers.** A 64 KB request-body cap and per-address rate limits on writes, downloads and
  the demo reset (see the README's configuration table). They are limits, not authentication.

## 3. Putting a proxy in front

Whatever you use, the rule is the same: the private service is reachable only by authenticated
you; the public and demo services may be open.

**Cloudflare Tunnel + Access (what the reference deployment uses).** Run `cloudflared` on the same
Docker network (`sparinghorse-edge`) and point three public hostnames at `http://sparinghorse:8770`,
`http://sparinghorse-public:8770` and `http://sparinghorse-demo:8770`. Put a Cloudflare Access policy
on the private hostname (an email allowlist is enough). The tunnel sets `CF-Connecting-IP` and
`X-Forwarded-Proto`, which the rate limiter and the HSTS header read.

**Caddy on the host (any VPS or home server).** The console has its own login since 0.56.0; Caddy's
`basic_auth` (or `forward_auth` to an identity provider) stays a good second wall on the private
hostname, and with `forward_auth` + `SH_PROXY_CIDR` the console can trust the identity Caddy sets:

```
private.example.com {
    basic_auth {
        you  $2a$14$…bcrypt-hash-from-caddy-hash-password…
    }
    reverse_proxy sparinghorse:8770
}
public.example.com {
    reverse_proxy sparinghorse-public:8770
}
demo.example.com {
    reverse_proxy sparinghorse-demo:8770
}
```

Run Caddy on the `sparinghorse-edge` network (or add `ports: ["127.0.0.1:8770:8770"]` to the private
service and proxy to loopback — never to `0.0.0.0`). Caddy sets `X-Forwarded-For` and
`X-Forwarded-Proto` by default.

## 4. Running it

```
mkdir -p data secrets && cp .env.example .env     # fill RUNALYZE_TOKEN, SH_TZ, the optional keys
docker compose up -d --build                      # builds the image, starts all three
docker compose ps                                 # each service should read "healthy" within ~30 s
```

Then open the private console, add the token in Settings if it is not in `.env`, **Sync now**,
**Backfill all** once, add a race. `SH_TZ` must be where you train — it is the engine's clock, not
just the nightly's.

On a host where Docker needs root (a Synology, for instance), prefix the compose commands with
`sudo`. Compose reads `.env` for the whole file whatever service you name, so a root-owned `.env`
makes a non-root `docker compose` fail before it looks at any service.

## 5. Upgrading

**Every code release needs `docker compose up -d --build`.** A plain `up -d` restarts the same image
and deploys nothing. After the build, open the footer of the private page and check the version it
prints matches the release; `/healthz` on any box reports `ok`. An `.env` change needs a container
recreate (`up -d`), not a rebuild.

Database migrations are additive and run at start; a downgrade is not tested. Take a snapshot before
a major upgrade (§7).

## 6. Bumping the pinned supply chain

- **Base image:** look up the current digest (`docker manifest inspect python:3.12-slim` or the
  registry's manifest headers), replace the `FROM … @sha256:…` line, rebuild, run the suite, quote
  the digest in the commit.
- **Python dependencies:** edit `requirements.txt` (the human source), then
  `uv pip compile --python-version 3.12 --python-platform linux --generate-hashes --no-header -o requirements.lock requirements.txt`,
  rebuild, run the suite and the browser flows, and quote the version diff in the commit. CI
  installs the same lock, so a mismatch between the lock and the source fails there first.

## 7. Backups, restore, rollback

- The private box writes a consistent snapshot to `./backups` after every successful nightly
  (`sparinghorse-backup-YYYY-MM-DD.db`, `SH_BACKUP_KEEP` = 7 kept) — its own volume since 0.56.1, so
  a loss of `./data` does not take the backups with it. `SH_BACKUP_PUSH` runs a command inside the
  container after each snapshot with the file in `$SH_BACKUP_FILE` (the image has python and sh;
  for rclone, run it on the host against `./backups`). **Settings → Backup & export** downloads a
  snapshot or a portable JSON export on demand; **Settings → System** shows the newest backup's age. Copy `./data` and `./secrets` off the host on a
  schedule; the secrets store is encrypted at rest since 0.56.0 (keep `SH_SECRET_KEY` or
  `secrets.key` with the copy, or the tokens must be re-entered).
- **Restore:** `docker compose stop sparinghorse && docker compose run --rm sparinghorse python
  SparingHorse.py restore /backups/<file> && docker compose start sparinghorse`. The command refuses
  a file that is not a Sparing Horse database, keeps the previous file as
  `sparinghorse.db.pre-restore-<stamp>`, and removes the WAL sidecars; the entrypoint re-owns the
  result. (Dropping the file into `./data` by hand still works.)
- **Rollback:** `git checkout <previous tag>` and `docker compose up -d --build`. The database from a
  newer release may carry columns the older code ignores; it will not carry data the older code
  cannot read.

## 8. Reading the box

- `docker compose logs -f sparinghorse` — the app prints its own diagnostics (sync, scheduler, tz,
  secrets) to stdout; waitress logs to stderr.
- `/healthz` — liveness plus scheduler telemetry (last sync, last successful nightly, consecutive
  failures) on the private box; booleans only on the public box.
- `docker compose ps` — the health column is the same probe.
