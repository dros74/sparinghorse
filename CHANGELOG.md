# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(pre-1.0: the minor version moves for features, the patch version for fixes).

> **Status — the training-plan engine is under heavy, active development.** Its behaviour, levers and
> outputs may change between releases as the model matures. Versions are checkpoints on a moving
> target, not a stable API.

## [0.60.1] - 2026-09-02

### Fixed

- **The limits block rendered in the week card's number gutter** — one word per line down a 30 px
  column — on every opened week since 0.58.0. The card is a three-column grid and the block was
  its last direct child; it now spans the grid. Seen first on the live plan after the deploy —
  the browser suite had never opened the block. It measures it now.

## [0.60.0] - 2026-09-02

### Added

- **The console says which Cloudflare Access team and audience tag it sees.** Configuring the login
  bypass needed two values from a dashboard that keeps moving them (on an iPad, with no developer
  tools, there was no other way to get the tag). Now every request through Access leaves its team
  and 64-hex audience on record — unverified claims, display only; an unverifiable token is still
  refused — shown in Settings → System and by `python SparingHorse.py access-seen`.
  `prepare_env.sh --from-container` reads them out of the running console and asks you to confirm
  the team name before writing, because a stranger's token would print a stranger's team. Deploy once
  without the bypass, open the private site through Access once, run the script, `docker compose up -d`.
  Nothing on the public or demo box. `det/access-seen`.

## [0.59.0] - 2026-09-02

_The engine symmetry release (ENGINE_SYMMETRY_PROPOSAL §5, decided §12): the engine has braked on
evidence and accelerated on a schedule; from this release a scheduled brake can be cancelled by
evidence — and only cancelled, never replaced by gas._

### Added

- **§C — "this deload isn't owed" (decisions (b), (c), (d)).** The periodizer keeps laying its
  positional down weeks. A governor judges the one that contains today — its block has fully
  elapsed, so the read is a measurement — and publishes it on the week as `deload`: `absorbed_frac`
  (the block's actual km over its bar, P2's `intent_km`), the load ratio against the regime's cap,
  fatigue against fitness, and whether a fresh positive check-in exists (B's token). When **all
  four** hold the deload is not owed: the week is demoted to a **level** week — the volume the
  trajectory carried, governor-capped like any week, quality kept off; never a build spike. The
  load-bearing gate is the athlete's word: a busy week and an illness week are identical in the
  activity stream, so silence keeps the deload. **Owner-confirmed first** — the week card offers
  "retire it / keep it" and the answer is a write-once ledger row (`POST /api/plan/deload`) — and
  **automatic** once the ledger holds ten scored 28-day forecasts with a median under-prediction of
  20 % or more, still under every cap; the engine's own decision is then banked the same way, so a
  token expiring mid-week cannot resurrect a deload. Goldens: additive only (the `deload` read on
  every positional down week); nothing fires without a token, and the CLI passes none.

- **§P2 — both denominators, published (symmetry decision (a)).** Every laid week carries `bar`:
  `intent_km` (the as-laid ramp bar — the governor basis, what C's test reads) and `sheet_km` (the
  session sheet — the adherence basis, what the athlete reads day to day), with `training_km`, the
  ratio and a `note` wherever they diverge: a race week's sheet includes the race; a frozen week's
  bar is what was asked when the week was first laid while its sheet is what was run; a live week
  beyond 10 % is labelled. The week card prints the pair where they diverge. Additive: goldens gain
  the block and lose nothing. Nothing reads it yet.
- **§B — readiness as a bounded permission token (decision (d)).** The readiness payload carries
  `permission`: a grant only on a check-in at most 48 h old declaring legs good and sleep not poor,
  with no stop symptom and no red evidence on the card (a medical hold, HRV below its band, a red
  verdict). Silence, "ok" or "heavy" grant nothing; absent telemetry never counts for or against.
  Private only. **No behaviour change:** the engine reads it nowhere, and `det/permission` carries a
  tripwire that goes red the day it does — C must be seen, not slipped in.

## [0.58.1] - 2026-09-02

### Fixed

- **The taper's marathon-pace touch is no longer called "tempo" (§TT3, PRODUCT_PLAN §4.6).** Since
  §TT the touch runs at the race's pace — the marathon zone for a marathon — but it kept the kind
  `tempo`, a name that means threshold. Two consequences: the plan called a session tempo that was
  not one, and the effort monitor graded it against the *threshold* band (`KIND_ZONE`), so a
  correctly-run marathon-pace session read "too easy". The kind is now `race_pace` when the zone
  is marathon (every shorter race keeps `tempo` at threshold); it counts as structured, not as
  hard — marathon pace is exempt from the hard share by design, so the 0 % it contributes to the
  20 % taper cap was never the defect, the label was. Goldens: only the `kind` strings of those
  sessions change (diff quoted in the commit). The page labels it "Race-pace touch".
- **Three engine questions recorded as decided-not-now** in ENGINE_SCIENCE §10.8: the straddle-week
  residual, `NEAR_CEILING_ACWR` with the three down weeks, and Davis's race-week short touch.

## [0.58.0] - 2026-09-02

### Added

- **The body's limits as one object per week (§LIMITS, PRODUCT_PLAN §4.2).** Every laid week now
  publishes `limits`: for each governor axis — the load ratio (ACWR), the long-run step, the
  damage-equivalent km per week and per session, the near-ceiling streak, the fitness gain per
  week — its ceiling, what the week was laid at, the headroom, whether it **bound** the week, and
  the evidence basis of the ceiling (**L** literature, **A** fitted to this athlete, **S**
  structural, from ENGINE_SCIENCE §10). `binding` names the axis that held the week, or the
  tightest one. The week card renders it as a strip ("This week is held by the long-run step"), and
  the Today page carries the binding limit under the why-line. Additive: no prescription moved —
  the goldens changed by 6,877 added lines and none removed, all inside `limits`.
- **An injury-risk read, not a probability.** The long-run-step axis carries a `risk` read against
  the one cohort behind it (Nielsen et al., Aarhus, n≈5000: sharp longest-run jumps predicted injury
  where weekly-mileage jumps did not): whether this week's long run sits within the +10 % step. No
  percentage is printed — one athlete with one injury event cannot calibrate one, and the decision
  record says the plan must not pretend to. The cohort's hazard ratios join the read when they are
  transcribed from the primary paper into ENGINE_SCIENCE.
- **The ledger scores more (§4.3).** Weekly fitness checkpoints at **7 and 14 days** beside the 28;
  **prescription rows** (the session sheet the plan standing at the week's start asked for, against
  what was run — decision (a): the sheet is the adherence denominator, the intent bar rides in the
  payload); **readiness calls** (the verdict a stored check-in implies — stop → red, heavy legs or
  poor sleep → amber — against that day's planned and run km); and **override rows** when a week was
  run at ≥ 1.5 × its intent with no race in it (decision (e)), with the week's check-in mix. All
  write-once. The track-record card shows bias by horizon, prescription adherence, green vs
  amber/red completion rates and the banked overrides; the public box publishes the calibration and
  the override count, never a week's own km.

- **Distance units (U5).** Settings → Distance units (`SH_UNITS`): `km` or `mi`. A display choice
  made at the edge — the engine plans in kilometres and every API stays metric; the page converts
  distances, paces and the server's own pace strings, on every box. The browser suite's new `units`
  phase sets miles and demands that no kilometre survives on Today, the dashboard or the runs
  explorer.

### Tests

- `det/limits` (structure, the hard-cap inequalities, the risk read, caution publishing only ACWR,
  L/A/S bases; lived weeks carry no ceiling and are skipped); `det/track-record` (e) — the 7/14-day
  checkpoints, prescription and override rows, readiness calls, write-once on all of them; the
  goldens rewritten with the additive block (6,889 added lines, none removed); the `units` browser
  phase.

## [0.57.0] - 2026-09-02

### Added

- **A Today page (§5.1).** `/today` is the daily surface: the readiness verdict, today's session
  with its paces and heart-rate bands, the check-in, and one line saying *why this session* —
  rendered from the plan's own fields (the phase and week it sits in, what it is, the fitness
  component it builds) — and nothing else. The installed app now starts there
  (`manifest.start_url`); the dashboard header links to it and it links back; on a phone the tab
  bar hands the other tabs to the dashboard. The binding-limit line joins it when 0.58.0 publishes
  the limits block.
- **A section rail on wide screens (review U6).** One link per dashboard section, fixed at the
  left from 1180 px up; a link opens a closed section on the way. The phone keeps its tab bar.

### Changed

- **The dashboard is plan-first (§5.2).** Order: the objective, the plan (the current week open,
  as before), the readiness card, the latest run — then the analytics. Every analytics section
  (shape, effort, zones, fitness & fatigue, weekly volume, drift, track record, health markers) is
  now a collapsible `<details>`, **closed on a fresh device and remembered per device**. The plan,
  the readiness card and the latest run stay open.
- **The showcase opens on the track record.** On the public and demo boxes the ledger — the
  engine's own forecasts scored against what happened — is the first section, open. It is the
  product's argument; it used to be the last thing on the page.
- **Two themes, following the system (decoration decision).** Daylight and Charcoal; the theme
  follows `prefers-color-scheme` unless a choice is saved, and one button cycles auto → charcoal →
  daylight. The third theme, Aurora, and the three 30×9 px swatches are gone (review U4 named the
  swatches the smallest control on every page).

### Removed

- **The weather header widget and its geocode proxy** (decoration decision). `SH_WEATHER_CITIES`,
  the `weather_cities` setting and city picker, `/api/weather`, `/api/geocode` and two outbound
  dependencies (Open-Meteo forecast and geocoding) on every box. The aerobic-efficiency card's
  temperature panel is unrelated and stays — that is the athlete's own run data.

### Tests

- The browser driver asserts the Today page (card, check-in, why line, nothing else), the rail
  (≥8 links; a link opens a closed section), a fresh device's closed analytics, ledger-first on
  the public box, and runs axe on `/today`; it starts each load with the sections remembered open
  so the flows can read inside them. The PWA det pins `start_url` = `/today` and two theme
  colours; the settings and demo-guard dets no longer know a weather setting.

## [0.56.1] - 2026-09-02

### Fixed

- **The page reads (review U2, U3, U4).** The 2026-09-02 review ran axe-core on every surface and
  measured the type: unlabelled check-in selects and date inputs, no main landmark and 134 nodes
  outside any landmark, about 200 text nodes under 11 px on the desktop dashboard (29 at 8.5 px),
  white on the Charcoal accent at 2.6:1, the motto at 3.7:1, the "not set" badges at 3.7:1 at 9 px,
  a 13 px stop-symptom checkbox as the smallest target on the readiness card, and 15 px ignore/delete
  anchors. Now:
  - **Landmarks and names.** The page has a `main`, a `banner` and a named region per section; the
    check-in selects, the race-date and away-day inputs and the theme swatches carry accessible
    names; the self-test page has a `main` too.
  - **A type floor.** Nothing renders under 11 px; 85 stylesheet rules that sat between 8 and
    10.5 px moved to 11, and 39 rules that carry information — session lines, the week's sessions,
    help and error text, zones, chips with numbers, tooltips, the ACWR explainer — moved to 12.
  - **Contrast in all three themes.** Daylight's muted text darkened to clear 4.5:1 on every surface;
    dark ink on the Charcoal and Aurora accent buttons (white was 2.6:1 and 4.2:1); text-safe
    `--accent-ink` / `--warn-ink` tokens for the motto, the objective date, the "not set" badge and
    the demo button, so the paint hues stay for fills and gradients; the map's empty state and the
    self-test page's button and links re-inked. `det/readiness-contrast` now checks the tokens per
    theme.
  - **Targets.** The stop-symptom control is a full 32 px row with an 18 px box; ignore and delete
    are buttons with a 24 px box (they were `href="#"` anchors); the calendar arrows and the swatches
    are taller. `det/touch-targets` pins the first two.
  - **Enforced in the browser.** axe-core 4.10 is vendored into `test/` and the driver runs it on
    the dashboard in all three themes, the Settings modal, the run browser, the first-run card, the
    public box in all three themes, the public-full fixture and the demo: no critical violation and
    no colour-contrast violation, or the flow fails.
- **The one hard-coded `en-US` date** (review U5) — the weekly-chart label — follows the browser's
  locale like every other date on the page.

### Added

- **Backups on their own volume, a push hook, and a restore command (review F3).** Snapshots used to
  sit beside the live database in the same bind mount. `SH_BACKUP_DIR` (compose: `./backups`) puts
  them on their own volume, `SH_BACKUP_KEEP` (7) bounds them, `SH_BACKUP_PUSH` runs a command inside
  the container after each snapshot with the file in `$SH_BACKUP_FILE`, and
  `python SparingHorse.py restore <snapshot>` puts one back: it refuses anything that is not a
  Sparing Horse database, keeps the previous file beside the restored one, and removes the WAL
  sidecars. The restore drill is recorded in PROJECT_LOG §125. `det/backup-export` drives all of it
  on a temp directory.
- **A System block in Settings (review O1).** Last sync, the nightly's outcome and failures in a
  row, the watch push, the newest backup and its age, where backups go — the telemetry that lived
  only in `docker logs`. `GET /api/system`, private-only; `det/scheduler-health` pins it.

## [0.56.0] - 2026-09-02

### Added

- **The private console locks itself (§AUTH, review S1).** Until now the box that holds the
  Runalyze token, the Claude key, the Suunto tokens, the blood markers, the readiness notes and a
  one-click full-database download had no authentication of its own: it was protected only by
  whatever sat in front of the container, and one published port was the whole exposure. Now:
  - **An owner passphrase** (12+ characters, scrypt-hashed in the secrets store), a **login page**,
    and a signed `HttpOnly; Secure; SameSite=Lax` session cookie that lasts 30 days per device.
    Every page and every `/api` route needs it; `/healthz`, the static assets, the icons, the
    manifest and the service worker do not (an anonymous `/healthz` gets the same booleans the
    public box serves).
  - **A first-boot page.** With no passphrase set the console serves only `/setup` — fail closed,
    even behind a proxy. `SH_PASSPHRASE` in the environment sets it non-interactively so there is
    never an open window; `python SparingHorse.py passphrase --set | --reset` inside the container
    changes or clears it, and takes effect at once.
  - **Lockout.** Five wrong passphrases lock the address for a minute, doubling per further
    attempt up to fifteen; thirty wrong ones from anywhere inside fifteen minutes make every
    address wait. The right passphrase is refused while locked.
  - **Change passphrase and log out** in Settings → Console access; a change signs every other
    device out and keeps the one that made it.
  - **A verified proxy bypass** (`SH_TRUST_PROXY_AUTH=1`): a request that carries a **Cloudflare
    Access JWT** is checked against the team's published signing keys (RS256, issuer, the
    application's audience tag, expiry) and skips the login; so does `X-Forwarded-User` from an
    address inside `SH_PROXY_CIDR` for a proxy on a dedicated network. Neither is on unless set;
    the passphrase remains the fallback when the proxy is not there.
  - The public and demo boxes have no login: they hold nothing.
- **The AI layer has switches, and says what it sends (§S5, review S5).** A Claude key alone used
  to enable all four features, and the check-in judgment shipped the athlete's own words about
  their body to a third party. Settings → AI features now carries three switches, each naming
  exactly what leaves the box: **plan narration** (the computed plan summary and the athlete
  context — on by default), **goal parsing and race advice** (the typed goal, the objectives list —
  on by default), and **check-in judgment and free-text adjustments** (the check-in note, energy and
  sleep answers, HRV state, today's session, anything typed into "Tell the horse" — **off until
  switched on**). Off, the deterministic readiness gate and the stop-symptom catch still run; the
  buttons say why they are disabled; the routes answer 403 and make no call. A disclosure beside
  the key field lists the same.

### Security

- **The secrets store is encrypted at rest (review S6).** Values in `secrets.db` — the Runalyze
  token, the Claude key, the Suunto credentials and OAuth tokens, now the passphrase hash and the
  session key — are Fernet-encrypted (AES-128-CBC + HMAC-SHA256, `cryptography`). The key comes
  from `SH_SECRET_KEY` (scrypt-derived; keeps the key off the volume) or, absent that, from a random
  `secrets.key` written once beside the store with mode 0600. Rows written by earlier releases are
  encrypted in place at boot. The box **refuses to start** if the store is readable by other users.
  `det/secrets` now reads the raw row and the file's bytes.
- `cryptography` is the project's first compiled dependency (Fernet, and RSA verification of the
  Access JWT); its wheels are in `requirements.lock`.

### Tests

- `det/auth` drives the whole thing through the real routes with an injected clock: fresh box →
  `/setup` only; setup signs in; logout; the lockout and its lifting; a passphrase change signing
  the other device out; the open-redirect guard on `next`; `X-Forwarded-User` from inside and
  outside the CIDR; a Cloudflare Access JWT signed with a key generated in the test, then
  tampered, wrong-audience, expired, wrong-issuer and unknown-key copies, and nothing passing with
  the flag off; and the guard inert on the public and demo boxes. `det/ai-gates` counts calls to a
  fake client. The battery runs with the guard off for its own requests; the browser driver signs
  in on every private flow; the CI image job proves a fresh container answers 401 until a passphrase
  is set and 200 with the session cookie.

### Documentation

- README, MANUAL §12, DEPLOY.md: first boot, the passphrase, the proxy bypass, `SH_SECRET_KEY`, the
  AI switches, and what to expect on the deploy.

## [0.55.2] - 2026-09-02

### Security

- **The container runs unprivileged, on a read-only root, with no capabilities and a memory ceiling
  (§S4, review S4).** The image used to run as root with the live database bind-mounted read-write
  — a compromise of the *public* box was a root shell over the real data and the nightly backups
  beside it. Now `entrypoint.sh` starts as root only long enough to give `/data` and `/secrets` to
  the app user (uid 10001; `SH_UID`/`SH_GID` to choose) and drops to it with `setpriv` for the life
  of the process; a `USER` line alone would have booted the next deploy into "attempt to write a
  readonly database" on any host whose mounts are root-owned. Compose merges one `x-hardening`
  block into all three services: `read_only` (the app writes only to the mounts and a tmpfs
  `/tmp`), `cap_drop: ALL` plus the four the entrypoint's first second needs, `no-new-privileges`,
  `mem_limit: 512m` (the app sits at ~80 MB, the self-test child peaks at ~180 MB on a real-size
  database — measured), and a `healthcheck` on `/healthz` that the Dockerfile also declares, so
  `docker ps` finally shows healthy/unhealthy. The demo's secrets-store path moves to the tmpfs
  (it holds nothing, but the status read opens the file).
- **Pinned supply chain.** `FROM python:3.12-slim` is pinned by digest, and the wheels are installed
  from a new `requirements.lock` with `--require-hashes` — 28 packages, every hash listed, generated
  with `uv` for Python 3.12 on Linux. `anthropic` is constrained to the 0.x line the LLM layer was
  written and exercised against; the unconstrained resolution would have pulled 1.3.0, a major the
  code has never run on, on the next rebuild. CI installs the same lock, and a new CI job builds the
  image, runs it exactly as compose does, waits for healthy, and asserts the server's uid, the
  read-only root, the database's owner, the CSP and the vendored Leaflet from inside the container.
- **No third-party script host in the Content-Security-Policy (review S8).** `script-src` allowed
  any script from `unpkg.com` as a host source, so an HTML injection anywhere could have loaded an
  arbitrary npm package despite the nonce. Leaflet 1.9.4 is now served from `static/vendor/` (the
  exact unpkg bytes — the SRI pins in `app.js` still verify them, same-origin or not), `unpkg.com`
  is gone from `script-src`, `style-src` and `img-src`, and `script-src` carries `'strict-dynamic'`:
  only the nonce'd tags and the scripts they create may run, whatever host an injected tag names.
  The map also works with the outside world cut off. Two more headers: `Permissions-Policy`
  (geolocation, camera, microphone, payment all denied) on every response, and
  `Strict-Transport-Security` only when the request came over TLS — behind the tunnel or a proxy
  that says `X-Forwarded-Proto: https` — never on plain http, where a browser that cached it for a
  LAN hostname could not reach the box again.

### Documentation

- **`DEPLOY.md`** — the operator's page: the three-box trust model as a diagram, the sentence
  "never publish 8770", a Cloudflare Tunnel + Access and a Caddy `basic_auth` example, what the image
  does to the host, upgrade, supply-chain bumps, backup, restore and rollback. Linked from the README
  and the manual.

### Tests

- `det/csp-worker` now asserts the whole policy shape (no unpkg, `'strict-dynamic'`, the vendored
  file served, `Permissions-Policy`, HSTS only over TLS); `det/image-completeness` asserts the image
  shape (digest-pinned base, hash-pinned lock, privilege-dropping entrypoint, HEALTHCHECK, vendored
  Leaflet); a new browser phase, `map`, drives the private console over the `--demo` fixture and
  requires Leaflet to render from this origin with a clean console.

## [0.55.1] - 2026-09-02

### Security

- **The anonymous surface got dampers (§ABUSE, review S2).** The 2026-09-02 review stored a
  2,000,035-byte "note" on the private box and found the demo's reseed (about a second of CPU) and
  its full-database download (a `VACUUM INTO` per call) open to anyone in a loop. Now: a request
  body over **64 KB** answers JSON 413 before it is read (`SH_MAX_BODY_BYTES`; the browser
  self-check's report gets eight times that, and Werkzeug's own 1 MB ceiling backstops a body that
  arrives without a length); every state-changing request draws from a **per-address token bucket**
  (120/min, `SH_RATE_POST` — a bulk entry of lab values is a real burst; a loop is still held to two a second), backup and export downloads from a smaller one (10/min,
  `SH_RATE_EXPENSIVE`), the demo reset from a smaller one still (6/min, `SH_RATE_RESET`), and the
  reseed itself runs at most **once every ten seconds across all callers**. The address is
  `CF-Connecting-IP` behind the tunnel, `X-Forwarded-For` behind a plain proxy, the socket
  otherwise — a damper, not authentication. Plain GETs are never limited. `SH_RATE_LIMIT=0` turns
  it off; the self-test battery runs with it off and `det/abuse-limits` turns it on for itself.
- **The demo hands out less (review S2, S9).** `GET /api/backup/db` and `/api/export/json` are
  refused on the demo box — the data is synthetic, the CPU and bandwidth are not — and so is
  `POST /api/health`: a lab value and its free-text note are rendered to the *next* visitor's Body
  tab, the same defacement surface as `house_name`. The read stays open so the tab renders. Three
  more settings join the refused list: `tz` (it moves the **whole process clock** via `tzset()`, so
  one visitor could make "today" wrong for everyone until the reset), `weather_cities` (points
  outbound Open-Meteo calls wherever a stranger says) and `athlete_context` (prose shown back in
  Settings and injected into every prompt). The day preferences, manual LTHR and age stay playable.
- **The public box no longer serves the whole diary by number (§PV, review S3).**
  `/api/activity/<id>` — and its `/profile` and `/structure` — answered for **every** id on the
  public box, each row with its start time and title, while `/healthz` went to the trouble of
  reducing the nightly's timestamp to a boolean because the household routine is private. Now the
  public box serves a run by id only when the page itself points at it: the latest run, a run the
  current plan or block log references, or one inside the last 14 days
  (`PUBLIC_ACTIVITY_WINDOW_DAYS`). Everything else is 404 there. And the public activity payload
  carries the **date only** — `date_time` and `title` are withheld by allowlist and registered in
  `_PV_WITHHELD`; the card reads "Tue 1 Sept" on the public box and "Tue 1 Sept · 18:30" on the
  private console. `det/public-activity-gate` drives all three routes; `runPublicFull` drives it in a
  browser over a fixture that has a plan.

### Fixed

- **The public objectives strip rendered live controls (review U1).** The date pickers and the
  "remove" buttons were built into the row template before the read-only early return, so the
  public showcase's first clickable thing was a control that answered 403. On the public box the
  date is now text and there is no button; the browser flow asserts it.
- **Eight concurrent clicks on Generate wrote eight identical plan versions (review A3).** The
  route is serialised (`_plan_lock`, like `_sync_lock`), and a regeneration that reproduces the
  latest saved plan — timestamps aside — writes no row and answers `unchanged: true`. A changed
  input (a moved race, a new sync, a new day) still writes a new row; the nightly never dedupes,
  because `for_date` is what the staleness label reads. `det/plan-generate-dedupe`.
- **Every demo reset wrote the plan twice.** `demo_reset` called `regenerate` (which saves) and then
  `save_plan` again — two identical rows per reseed, every hour since §DEMO landed.
- **The demo-guard det cleared the instance's own day preferences.** Its "allowances" loop POSTed
  empty `rest_day_rank` / `long_run_day` values to prove they are writable in demo, and on an inline
  battery run (`SparingHorse.py selftest` on a real database) that write landed. The values are put
  back now.

### Documentation

- README: the demo's refused list and the public box's run-by-id rule; the abuse-limit env vars in
  the configuration table. MANUAL §11: the two new privacy rules.

## [0.55.0] - 2026-09-02

### Fixed

- **The demo athlete was running through the middle of the Lake (§DEMO2).** The route generator drew
  a harmonic blob around a centre point and repeated it to reach each run's distance. That is a fine
  way to draw a closed curve and a hopeless way to draw a route: a 3.2 km circuit is about 1 km
  across, Central Park is 830 m wide, so every long run crossed the Lake, the Reservoir and both
  flanking avenues. The shape of a route is a claim about ground, so it is no longer drawn at all.
  `DEMO_LOOP_UV` is a real 9.14 km park circuit SOLVED against the park: the corridor between the
  park boundary and every pond, lake and reservoir inside it was measured from public map data
  (OpenStreetMap, the same source as the tiles under it) and the route is the path that stays in it —
  threading inside The Pool where the wall leaves no room, swinging around Harlem Meer at the north
  as the real drive does, and passing the Reservoir on both flanks. It keeps 43 m from the park wall
  and 46 m from the nearest water.
- **…and so is the line the map actually draws.** Clearing the water is not enough on its own: the
  map renders a 120-sample downsample, and its chords cut every corner the route turns. An
  intermediate solve was entirely on land and still put 75 of 116 drawn tracks in the water. Three
  things fixed that — the corridor solve now maximises daylight instead of hugging the flank, the
  sampler lands on the route's corners rather than at even intervals (which is also what the real
  downsampler does, picking the nearest recorded point), and the walk is scaled so the DRAWN polyline
  measures the distance printed beside it (it was reading 3.4% short on the longest runs; it is now
  within 0.6%). ⚠ Past ~2.6 laps there are fewer than 14 samples per circuit and no 14-gon holds a
  9 km loop's corners — a marathon drawn this way cuts into the Reservoir. That is the downsampler's
  resolution and is equally true of a real lapped marathon; the demo athlete's longest run is 22.5 km.

### Added

- **The demo athlete now has long runs, and therefore a durability card (§DEMO2).** The durability
  panel reads long-run aerobic decoupling and needs runs of at least `DURABILITY_MIN_KM` (16 km) that
  carry a decoupling value. The synthetic athlete had neither: the longest run of the whole 24-week
  block was 15.8 km — just under the floor — and `raw` held only the fields the planner consumes, so
  no run carried decoupling at all. The one card whose entire subject is the long run had no long run
  to read, and said so honestly on the public demo. `seed_synthetic_db(demo=True)` now builds a full
  marathon block (45 → 70 km/wk, 15 long runs over the floor, longest 22.5 km) and writes the rest of
  the Runalyze payload: decoupling that rises with distance and falls as the athlete adapts, a
  seasonal temperature, and a 1–5 subjective feel. The card now reads *durable · improving* over 15
  charted long runs.
- **The efficiency card has a trend and weather to plot it against.** Same cause: with a fixed pace
  per zone the synthetic athlete finished 24 weeks of building at exactly the pace they started
  (`r = -0.004`, a dead-flat line), and with no `temperature` field the §EF weather panel — the one
  that exists to show that heat is plotted and never subtracted — had nothing in it. The demo profile
  improves 5% at the same heart rate across the block and carries a seasonal temperature: +0.9%/30d
  over 93 runs, 6–25 °C.
- **Demo elevation comes from the ground now.** A sine against the sample index gave every run the
  same two bumps whatever its distance. Elevation is tied to position on the loop instead, so a
  lapped long run climbs the same 26 m hill once per lap — which is what a lapped run's trace
  actually looks like.
- **`det/demo-route`** — the invented route is a closed, simple, park-sized circuit; every vertex is
  inside the park and at least 15 m from every lake and pond; the 120-sample line the map DRAWS is
  clear too at every distance the demo produces; and re-centring the demo moves it without changing
  any distance. Checked against simplified park and water polygons carried in the battery. Seen to
  fail on the original bug: restoring the 3.2 km blob reports *"the route runs -72 m from Reservoir —
  that is the water"*.
- **`seed --demo`** — the CLI can now build the public demo's athlete locally, for screenshots.

### Changed

- `det/demo-track`'s "it fits somewhere real" bound was 2.5 km, sized for the old 3.2 km circuit. It
  now derives the bound from the loop's own span, so it tracks any future re-solve of the route.
- The shared fixture is untouched: `seed_synthetic_db(db)` with no `demo=True` is byte-identical, and
  all ten goldens are unchanged. The demo profile is opt-in for exactly that reason.

## [0.54.0] - 2026-09-02

### Added

- **A tune-up race is a session now (§TT2).** `select_chain` has returned `tune_ups` — upcoming B/C
  races that are deliberately kept OUT of the periodization chain — since §6q, and the plan has
  reported them ever since as a legend line ("Tune-ups before the peak: …"). Nothing ever laid one.
  Only a `taper` phase built a race spec, so a B/C race day drew whatever the distributor happened to
  put there. On the default seed fixture that was a **14.4 km, 9x3min VO₂ interval session on the
  morning of a tune-up 10k** — precisely the failure §RACE was written to fix for marathon morning,
  fixed there and left standing for every other race the athlete enters.
  Tune-ups are now laid wherever they fall, through the existing `_place_race` machinery: the race
  replaces the day, is charged at `RACE_KM` and its own zone, and its load is carried into the week
  header, the projection and the block's end state. The block is still never periodized toward it —
  phases and the A-race chain are unchanged, which is what keeps a tune-up a tune-up.
- **A tune-up clears its own run-up (`TUNEUP_CLEAR_DAYS = 2`).** The race *is* that week's hard
  session; quality work inside the two days before it is dropped, row and TRIMP together. A workout
  stacked into the run-up makes the result measure fatigue rather than fitness, which defeats the
  only reason a B/C race is on the calendar. Quality further out survives, and **A-races are
  untouched** — their taper already is the run-up.

### Changed

- Five golden plans move. The default seed fixture carries a B-priority "Tune-up 10k", so the
  scenarios that use it now lay it. The only structural change is the race day itself; every other
  changed day is a 0.2–0.4 km trim as the (slightly lighter) race load propagates through
  `end_ctl`/`end_atl`. No session changed kind and no week gained or lost a run.
- ⚠ A tune-up adds **ungoverned** load to whatever week it falls in — the race is laid after the
  governor has sized the week, exactly as an A-race is. On the `caution` fixture that takes a
  rebuild week from 20.2 km / TRIMP 166 to 26.4 km / TRIMP 239. This is the §RACE trade restated: the
  governor bounds the training it controls, and a race the athlete has entered is told the truth
  about rather than wished away.

## [0.53.2] - 2026-09-01

### Fixed

- **The service worker has never run, on any deployment (§CSP).** `worker-src` was never declared in
  the Content-Security-Policy, so the browser fell back to `script-src` — a nonce plus unpkg, and a
  nonce cannot be attached to a worker script. `navigator.serviceWorker.register("/sw.js")` has
  therefore been **blocked since the CSP landed** (`git log -S worker-src` finds nothing before this),
  which means the PWA layer this app ships — `sw.js`, `manifest.webmanifest`, the touch icons — has
  never once installed. It stayed invisible because the registration `.catch()`es silently; it took a
  console error on a demo page load to surface it.

  One directive: `worker-src 'self'`. Same-origin only — it grants the worker exactly what
  `default-src 'self'` already grants everything else, and nothing in the policy loosens. Verified in
  a real browser: the worker now reaches **active**, and the page's console is clean.

- **`det/csp-worker`** — the directive is present and same-origin, the shell still *asks* for a
  worker and `/sw.js` is still served (a "fix" that quietly stopped registering would satisfy the
  header check while leaving the feature just as dead), and the rest of the policy — nonce'd scripts,
  no objects, no framing, `connect-src 'self'` — is unchanged, so this cannot become the commit that
  opened the CSP up.

## [0.53.1] - 2026-09-01

### Added

- **The demo's runs have routes (§DEMO).** An empty map panel reads as a broken app rather than as
  an absent input, and the route view is one of the better things here — so the demo now generates a
  GPS track and pace/HR/elevation/cadence curves for every synthetic run, written as the same
  `trackcache` rows the real downsampler would have produced. Nothing else had to change: the map and
  the profile panel read that table and simply start working.

  **Invented data is only honest if it is internally consistent**, so the generated route *is* the
  distance printed beside it — measured on the sphere over the drawn polyline, within 0.1%. A viewer
  who reads "15.8 km" under a loop that measures 3 km has caught the app lying about something small.

  ⚠ **Laps, not one enormous loop.** Scaling a single circuit to 15.8 km is arithmetically fine and
  visually absurd: the loop comes out ~5 km across — wider than the island the first version drew it
  over. Repeating a ~3.2 km circuit is what a runner actually does, keeps the track somewhere
  plausible, and keeps the distance exact. A marathon and a 5 k now occupy roughly the same park
  (1.2 km and 0.85 km of map respectively).

  Routes are deterministic per activity, so a run keeps its route across the hourly reset — nothing
  looks faker than a track that changes while you watch it — and differ between runs. The centre is
  `SH_DEMO_ROUTE_CENTER`, defaulting to Central Park: recognisable enough that it reads as demo data,
  and nowhere near whoever is hosting it.

- **`det/demo-track`** — the drawn route matches the stated distance, the bounding box stays under
  2.5 km at any distance (so a regression back to one huge loop fails), routes are stable per run and
  distinct between runs, and every series carries the downsampler's full 120 samples. Seen to fail on
  both bugs the generator actually had.

### Fixed

- **The demo no longer refuses `/api/activity/<id>/profile` and `/map`.** 0.53.0 answered them with
  an honest "no route in the demo" message, which was the right answer while there was no data and
  the wrong one now there is.
- **Every generated track was ~1% short of its stated distance.** The scale was computed from a
  perimeter that summed *around* the loop — including a wrap-around segment from the last point back
  to the first. With laps the path is an open polyline and that segment does not exist, so the route
  was over-measured and came out under-length.

## [0.53.0] - 2026-09-01

### Added

- **A public demo: the full private console over a synthetic athlete (§DEMO).** The honest problem
  with showing this project to anyone was that a screenshot cannot demonstrate an engine — you have
  to be able to push it and watch it push back. `SH_DEMO=1` runs the **complete private console**
  against a synthetic athlete, so a stranger can regenerate the plan, post a check-in, move the race,
  set rest days and see the governors actually respond.

  This is deliberately **not** the `SH_READONLY` public view, and close to its opposite: read-only
  serves the *public* projection (medical sections withheld, inputs hidden) over the *real* database,
  while demo serves *everything* over a database that belongs to nobody.

  **It seeds and restores itself.** On first boot against an empty volume it seeds the synthetic
  athlete and generates a plan; every hour (`SH_DEMO_RESET_EVERY_S`) it restores that state, and a
  banner offers **Reset now**. Reset re-runs `seed_synthetic_db` inside the app's own connection
  rather than restoring a file copy — a file-level restore of an open SQLite database works until it
  doesn't. Measured across repeated resets: 3 plans, 115 activities, 440 KB, no growth.

  **It carries no credentials** — no Runalyze token, no Claude key, and no secrets-store mount — and
  it gets **its own volume** (`./demo`, never the shared `./data`). No barrier inside the app is as
  good as not mounting the real database.

  `_demo_guard` refuses four families, each for its own reason:

  | Refused | Why |
  |---|---|
  | `POST /api/secrets` | a public box must never accept an API key from a stranger, nor hold one to leak |
  | `POST /api/sync`, `/api/suunto/*` | outbound calls against someone's real account |
  | `POST /api/selftest/run` | an anonymous POST that burns ~3 minutes of CPU is a denial-of-service primitive |
  | `private_url`, `house_url`, `house_name` | the only settings rendered back to the *next* visitor — the defacement and open-redirect surface |

  Everything else really runs. ⚠ Status reads stay open (`GET /api/secrets`, which returns
  configured-flags and never values, and `GET /api/suunto/status`): blocking the whole path 403'd the
  demo's own Settings panel on first paint — found by loading the page, not by reading the guard.

- **`SparingHorse.py demo-bake`** — the plan explainer is the one feature a demo cannot run live,
  because it needs a Claude key and a public host should not hold one. Bake the narration once
  wherever the key already lives; the demo serves that text **flagged as a sample**. With nothing
  baked it says plainly that the AI layer isn't wired up on the demo box and that everything else on
  screen is computed by the deterministic engine — which is true, and a better thing for a visitor to
  read than an invented explanation.

- **`det/demo-guard`** — drives the real routes with the flag on: the refusals are 403, the
  plan-shaping settings and status reads are not, and the whole guard is inert when `SH_DEMO` is
  unset. ⚠ The identity-settings tooth names its three keys **literally** rather than looping over
  `DEMO_BLOCKED_SETTINGS`: the first cut looped over the constant, so deleting a key from it deleted
  the test for that key and the revert passed. Measured, then fixed.

### Fixed

- **The demo banner rendered on the private and public boxes too.** `.demobar{display:flex}` is a
  class selector and `[hidden]` is a UA rule, so the class outranked the attribute and the banner was
  never actually hidden — a browser flow caught it by noticing the *public* page telling a visitor
  the plan "regenerates for real". Fixed twice over: `.demobar[hidden]{display:none}` restates it at
  class specificity, and the markup is now injected server-side **only when `SH_DEMO` is set**, so
  demo copy is not in the private or public DOM at all. Verified across all three modes.

- **The demo's first page load hit four failures a visitor would have seen** — a 403 on the Settings
  panel's secrets status, a 403 on the auto-sync the page fires on load, and 502s on a run's profile
  and map. The page now skips auto-sync in demo mode (there is no account to sync from), and the
  profile/map routes answer honestly: a synthetic athlete has summary figures but no GPS track, and
  inventing one would put a fake person on a real map. Load is now clean — zero 4xx/5xx.

## [0.52.1] - 2026-09-01

### Fixed

- **The per-session biomechanical ceiling was policing the wrong bout, and froze whole blocks
  (§AARHUS).** §3.1's per-session step exists to implement one finding: among novice runners, sharp
  jumps in the **longest run** predicted injury while weekly-mileage jumps did not. It was written as
  that finding "generalised past the long run" onto the damage axis — and the generalisation reached
  too far. `EQ_KM_FACTOR` prices interval running at **3.5** against a long run's **1.0**, so a 5.8 km
  interval session scores **eq 9.8** while the same week's 7.0 km long run scores **7.0**. Across a
  full block the largest-eq bout was an **interval session in 60 of 102 laid weeks** (long 18,
  progression 12, tempo 8, race 4). The ceiling spent its time governing a short rep session.

  That closed a loop. An interval session's size is a fixed share of the week, so: the week grows →
  the interval session grows with it → the session ceiling rejects → the week cannot grow. The
  ceiling then re-derived itself from the bout it had just limited and **locked at 12.74 for fourteen
  straight weeks**. Re-running the search's own accept/reject test, `session_eq` rejected at **52 of
  102 margins** — more than every other governor combined. The affected block froze at a 17.9 km
  build peak and projected the athlete **detraining from CTL 55 to 15**.

  Structured interval sessions are now exempt from the **per-session step**. The step measures
  sustained bouts, which is what the source finding measured.

  ⚠ **Narrowed, not made free.** Interval work still carries its full 3.5-weighted damage into the
  §3.1 **week** ceiling, so the week's total damage budget is untouched. What changed is only which
  bout the per-session step measures its jump against — and `det/session-step` now asserts both
  halves, so relaxing it by the back door fails.

  ⚠ **The actuals side needed no equivalent and deliberately has none.** `_recent_session_eq` scores
  a logged *day* by one interpolated factor from its whole-day pace, never by rep zones, so it never
  carried the 3.5 anchor: measured on real data the seed reads 14.61 / 13.54 / 17.81 / 13.93 against
  long runs of 11.3 / 10.1 / 12.3 / 13.6 km — the long runs, at about 1.3 apiece. It was already the
  largest sustained bout.

  **Result on the frozen fixture:** build peak **17.9 → 61.8 km**, 8 building weeks spanning 30.9 km,
  and the block carries its fitness into the taper instead of decaying into it. The `§TP-RATCHET`
  tripwire that asserted the freeze went red exactly as written, and the two real assertions it was
  holding — the build-peak headroom check and "race fitness well above the taper trough" — are
  restored verbatim.

  **On a real block the change is modest**, because real activities already anchor the seed: 1064 →
  1099 km (+3.3%), peak weeks 71.1 / 73.7 / 75.9 km (inside the 65–80 km/wk tier the plan is
  calibrated for), hard share unchanged at 11.5–12.4% against a 25% cap, no week eq-capped.

- **`det/session-step`'s docstring stated the defect as the contract.** It read that
  damage-equivalent km "lets the same rule cover an interval session, where the damage is not in the
  distance" — which is exactly the generalisation that broke the governor. Rewritten, and its seed
  tightened from 6.0 to 5.0: at 6.0 the cap (7.80) sat above the fixture's natural long-run bout
  (7.36), so the binding assertion would have passed while proving nothing.

## [0.52.0] - 2026-09-01

### Added

- **The Kenyan-style progression run (§PROG).** The engine had two structures — `intervals` and
  `continuous` — and no way to express a run that ramps across zones, so the session Davis uses six
  times in Breeze and calls *"unparalleled for building efficiency and relaxation at fast paces"*
  could not be prescribed at all. It now can.

  The run opens slower than an easy run, climbs over the first three or four kilometres, holds
  80–88% of 5k pace until two kilometres to go, then lifts to 92–95% for the last couple of
  minutes. There is **no warm-up block** — the easy ramp *is* the warm-up, which is the whole point
  of the session — and a cool-down follows, as in the book's own "12 km Kenyan-style progression
  run + 2 km easy cool-down".

  ⭐ **The zones already encoded those percentages; nothing had to be invented.** Read as a share of
  5k *speed*, this engine's marathon zone is **85.2% 5k** — the middle of the 80–88% cruise band —
  and its threshold zone is **92.5%**, exactly the closing 92–95%. So the run is
  easy → marathon → threshold on zones that were already priced, with shares .34/.50/.16 taken from
  the book's own 12 km description.

  A rendered example, at a week near the middle of the 65–80 km/wk tier:

  | segment | zone | | |
  |---|---|---|---|
  | settle in — start slower than easy | easy | 21 min | 3.0 km |
  | cruise, fast and relaxed | marathon | 31 min | 5.1 km |
  | close it down — fast finish | threshold | 10 min | 1.8 km |
  | easy cool-down | easy | 10 min | 1.4 km |

  ⚠ **Only the closing segment is hard.** `_hard_share` counts work reps whose zone is in
  `HARD_ZONES`, so the easy and marathon thirds correctly do not register — which is exactly why
  Davis files this as a Group A high-end aerobic session and lets a lower-mileage athlete run one
  every 7–14 days. A progression week asks **0.096** of weekly TRIMP in hard zones.

  `PROG_FRAC` = **0.18** is calibrated on the midpoint of the tier Table 7.12 is about (~72 km/wk),
  where it lays **11.8 km** inside that table's 11–13 km band; it reads 10.9 km at 65 km/wk and 13.2
  at 80. Held at .18 rather than .20 so a progression week's total structured slice stays inside the
  **0.25 sanity bound `det/components` already enforces** — sizing the session to fit an existing
  bound is the right way round; raising a bound to fit a new session is how bounds stop meaning
  anything.

- **The general-phase pool is three families now**, and the rotation runs 4 VO₂ : 2 threshold : 2
  progression = 50/25/25, against Breeze's own general-phase 12 : 5 : 5 ≈ 55/23/23. The second row
  of the cycle pairs a progression run with a cruise tempo, which is Breeze's week 2 exactly.

- **`det/progression-run`** — the run climbs, carries no warm-up block, holds its third/half/sixth
  shape (read off the laid minutes, not the constants), puts only its closing segment in
  `HARD_ZONES`, lands in the 11–13 km band at the tier midpoint, and scales with weekly volume.
  ⚠ The zone mapping is asserted as a claim about **pace** rather than a pair of names: the cruise
  zone must read inside 80–88% of 5k and the finish inside 92–95%, so a future zone recalibration
  fails this rather than silently changing what the session is. Four reverts each seen to fail.

### Known gaps

- The taper's sharpening sessions are still labelled "tempo" and resolve to the **marathon** zone,
  contributing 0% to a 20% hard cap. Davis names a progression run as a good default for the final
  lead-in, so the structure to fix it now exists — but changing what lands ten days before a race
  is its own decision and is not bundled here.

## [0.51.0] - 2026-09-01

### Added

- **The general phase stops prescribing the same week twice (§MIX).** Every base week in the
  assertive path carried ONE structure — a short VO₂ touch — on the same weekday, all block long.
  The `threshold` zone was defined, priced (`TRIMP_PER_MIN` 2.55, `EQ_KM_FACTOR` 2.5) and given a
  pace, and **never once prescribed**: measured across a 19-week block, 250 min at interval, 294 at
  marathon, **0 at threshold**. Davis's Breeze general phase — the tier this engine is calibrated
  against — runs 1.8 non-long quality sessions a week across three structures, 12 VO₂ : 5 threshold
  : 5 progression.

  The general phase now draws **two** quality sessions a week from a pool, on a **deterministic**
  four-week cycle keyed to the quality-week ordinal — so a down week does not advance the rotation,
  the same inputs still regenerate the same plan, and every golden stays diffable. A fresh block
  now reads:

  | week | quality |
  |---|---|
  | 3 | short VO₂ touch + VO₂ reps (3 min) |
  | 5 | short VO₂ touch + cruise tempo |
  | 6 | VO₂ reps (3 min) + short VO₂ touch |
  | 7 | cruise tempo + VO₂ reps (3 min) |

  **Threshold replaces an interval — it is never a third hard session**, so a week's hard count is
  fixed at two and only the mix moves. The cycle runs 6 VO₂ : 2 threshold = 75/25, against Breeze's
  own 12:5 ≈ 70/30 once its progression runs (not yet built) are set aside.

  ⚠ **`BASE_THR_FRAC` = 0.06 is derived, not tuned.** Threshold costs **0.176 eq-km per TRIMP**
  against interval's **0.223** at this engine's zone paces, so 0.05 × 0.223/0.176 = 0.063 — the
  slice at which swapping an interval slot for a threshold one is **EQ-neutral by construction**.
  The §3.1 biomechanical ceilings see the same week either way, and "replaces" is true on the
  damage axis rather than merely in the session count. Both slots count as hard: a base week now
  asks 0.10–0.11 of weekly TRIMP in `HARD_ZONES` against a 0.15 cap.

- **Strides survive the specific phases.** Breeze carries 4 × 20 sec through week 18; this engine
  hardcoded `"strides": 0` in build and peak, so the cheapest neuromuscular touch there is vanished
  exactly when the plan got specific. ⚠ Not regime-gated, so caution moves too — deliberately, on
  the precedent the strides *slot* rule already set. Verified on the caution golden: `strides` is
  the only key that changes, sessions and km byte-identical. A stride marker rides an easy run that
  was already prescribed; it adds no TRIMP and no kilometre.

### Fixed

- **A quality session may start the week when the week's first run follows a rest day.** The slot
  walk began at the second run day because the first is "the first run back" — true on every stock
  layout, where it means Monday, the day after the Sunday long run. It stops being true the moment
  a rest-day preference pushes the first run later: with rest on Monday the first run is Tuesday,
  which follows a *rest* day and sits two days off the long run.

  This was load-bearing, not a tidy-up. On a 5-run week shaped Tue/Wed/Thu/Sat/Sun the only pair of
  quality days ≥ 2 apart that also avoids the day before the long run is Tue + Thu — and Tuesday was
  slot 0. The second session found no slot and was dropped **silently**: the shape asked for two,
  the week laid one, and nothing said so. Measured before the fix: 2 of 2 laid on a 6-run week, 1 of
  2 on 4- and 5-run weeks. Byte-identical for anyone who has set no day preference — every stock
  layout starts on Monday.

- **`det/components`** asserted the base phase carries the single VO₂ touch — which was the defect
  itself, frozen as a contract. It now asserts the pool, the pair and the per-kind fractions.
- **`det/quality-mix`** — two sessions a week from the pool, the cycle keyed to quality weeks (not
  calendar weeks), at most one threshold per week, the swap eq-neutral within 10%, no two
  consecutive quality weeks alike, hard share under the phase cap, caution's single cruise tempo
  untouched. Four reverts each seen to fail on their own tooth.

### Known gaps

- **Progression runs are not built.** Breeze uses six Kenyan-style progression runs, five of them in
  the general phase, and the engine has no structure that ramps across zones within one session.
  Until it does, the general-phase pool holds two families rather than three, and the taper's
  "tempo" sessions still resolve to the marathon zone.
- Threshold is a **general-phase** structure here, as in Breeze, whose supportive and specific
  phases are marathon-pace dominated. A block already past its base phase will see little change.

## [0.50.2] - 2026-09-01

### Fixed

- **The search that decides what is actually prescribed had the same omission (§STRAD2).** 0.50.1
  gave the straddling week's *intent* search the §3.1 biomechanical ceilings. The search that fixes
  the **remainder** — the sessions the athlete is really told to run — still went out without them,
  so `_bio_on` stayed False there too and `_peak_governs` re-armed the per-day peak ACWR brake
  §PRO17 stood down. Measured on a deployed 0.50.1 plan: that call answered **409.1 TRIMP** — the
  very number the intent search returned before 0.50.1 — where the ceilings give **589.0**. The
  week's intent was an honest 56.6 km and the remainder was held to 35.6, so the card showed
  **47.6 km against the previous week's 58.7** and read as an unexplained 11 km drop in a week that
  is not a down week. Nothing had decided the week should be small. It now lays 56.0 km.

  **The week ceiling is charged against what the week has already cost.** `week_eq_cap` bounds a
  whole week; the remainder is only the days that are left, so passing the full ceiling would have
  been slack by construction — it would arm `_bio_on`, and so stand the peak brake down, while
  constraining nothing. The remainder gets the ceiling minus the eq_km the elapsed days really cost.
  Floored a hair above zero rather than at zero: both ceilings are tested for *truthiness*, so a
  0.0 would silently disarm the very ceiling it expresses. One of the ten golden scenarios reaches
  that floor, so the guard is not hypothetical.

- **A ceiling enforced on a week nobody runs is not a ceiling (§STRAD3).** The remainder lay passes
  `free_from` (place only today-onward days) and `long_km_aim`; the search did not — so it bounded a
  **four-session** week while the lay produced a **three-session** one. Latent for as long as the eq
  ceilings were absent from this call. The moment §STRAD2 made them the binder it became visible: a
  searched 22.70 eq-km passed a 22.88 budget that the laid 23.10 then breached. `_max_week_trimp`
  now forwards both to its own distribution (default None ⇒ byte-identical for every other caller),
  restoring §PRO17's rule that the search evaluates the week that will actually be laid.

- **`det/straddle-remainder`** — the laid remainder must fit what is left of the week's damage
  budget, swept at 30%, 60% and 85% of it already spent, and no bout may pass the session ceiling.
  Before §STRAD2 the 85% case laid 17.70 eq-km into an 8.19 budget. Reverting the week ceiling, the
  spent-budget charge, or §STRAD3 each fails it on its own.

- **`det/cap-truth-anchor` now asserts the §PRO9 cap instead of a run that happens to sit under it.**
  It read the window off the laid long run, a number three governors compete for — and it broke
  twice in one day as §STRAD and §STRAD2 each handed a correctly-applied ceiling the binding seat.
  It reads `long_km_cap` itself now: 9.24 with the straddling week's real 8.4 km anchoring it, 5.56
  without.

### Known gaps

- The deload road still re-phases with the regeneration day. 0.50.1 and this release fix the
  straddling week's **volume** (day-invariant now); its projected end-of-week ACWR still decays as
  the week empties (1.240 → 1.093 across one week, measured with the plan followed), and §PRO6
  compares that partial reading against a threshold written for full weeks. Judging the straddling
  week on a full-week-equivalent ratio is the remaining piece and moves a safety governor's
  decision variable, so it is deliberately not bundled here.
- `det/straddle-remainder` does not cover `session_eq_cap` on the remainder path: on a remainder the
  biggest bout is the long run and `long_km_cap` already bounds it, so the session ceiling is
  currently masked. It is passed for parity; no tooth is watching it.

## [0.50.1] - 2026-09-01

### Fixed

- **The week a mid-week regeneration lays was searched with a different set of governors from
  every other week (§STRAD).** §PRO13 reconstructs "the SAME target the full-week path would
  choose" for the week that straddles today. That reconstruction went out without either §3.1
  biomechanical ceiling (`session_eq_cap`, `week_eq_cap`) and without §PRO9's long-run cap, so the
  one week a Tuesday regeneration actually prescribes was bounded by neither the damage-axis
  ceilings nor the long-run ladder. All three are now hoisted above the call and passed, exactly as
  the full-week path already did — its own comment states the rule ("hoisted above the governor
  call because they are now INPUTS to it rather than a post-hoc reshape test").

  **Two symptoms, opposite in sign, from the one omission.**

  *Safety.* With no ceiling passed, nothing bounded the straddling week on the damage axis. **Five
  of the ten frozen golden scenarios were breaching the §3.1 week ceiling** on that week —
  69.10 eq-km against a 64.31 ceiling in four of them, and the taper scenario at **79.80 against
  64.31 while also laying a 26.00 eq-km session against a 20.54 ceiling**. Every one now sits
  under both. A fixture whose athlete logged one 8.4 km run in a 5 km week had the straddling week
  prescribing 33.8 km where the full-week path allowed 10.6.

  *Phasing.* `_bio_on` is `bool(session_eq_cap or week_eq_cap)` and `_peak_governs` is
  `not (shape_neutral and _bio_on)` — so passing no ceilings also **re-armed the per-day peak ACWR
  brake that §PRO17 deliberately stood down** for assertive weeks. Measured on one athlete's plan:
  the same search answered **409.1 without the ceilings and 647.2 with them**, against the **633.0**
  the full-week path had answered the previous day. The week's intent fell **56.3 → 36.4 km**, its
  projected end-of-week ACWR read **0.953** where the Monday read **1.294** — under
  `NEAR_CEILING_ACWR` — so §PRO6's near-ceiling streak reset mid-ride, the forced deload never
  tripped, and the block's down weeks went from **three spaced four apart to two spaced seven**.
  Purely from regenerating on the Tuesday. That is the exact invariant the straddle fold's own
  comment claims: *"Which DAY of the week the plan is regenerated on must not re-phase the road."*

  Walked day by day across a real week with the plan followed, the deload road is now identical on
  every regeneration day; before the fix, Tuesday and Wednesday re-phased it.

  ⚠ **Not fully closed.** A straddling week's projection still reads slightly lower as the week
  empties (~0.02 of ACWR per elapsed day, from the shrinking remainder window). Where a week sits
  within a few hundredths of `NEAR_CEILING_ACWR`, that can still tip the streak: measured on real
  data, the road holds at full compliance and down to ~70% of prescription, and re-phases at 60%.
  Judging the straddling week on a full-week-equivalent ratio is the completion; it moves a safety
  governor's decision variable and is deliberately left for its own release.

- **`det/straddle-caps`** — the straddling week may breach neither §3.1 ceiling and its intent must
  equal the full-week path's for that week, swept over every day of the week under **two** trailing
  corpora. Two, because whichever ceiling binds first hides the other: with one corpus a revert of
  `session_eq_cap` alone still passed. All three caps are now individually seen to fail.

- **`det/cap-truth-anchor`'s fixture logged one run per week** against a shape asking for 29.5 km —
  so the §3.1 week ceiling sat at ~6.5 eq-km, binding on everything, invisible only because the
  straddle path was skipping it. Given production-shaped supporting volume (all short runs, so the
  window's max still comes from the one 8.4 km long run) the long cap is back in the binding seat,
  which is where that det does its measuring — and its straddle limb is seen to fail again.

## [0.50.0] - 2026-08-31

### Changed

- **Down weeks were too deep — but not for the reason the source first suggested (§DELOAD).**
  `BUILD_DOWN_FRAC` **0.75 → 0.80**.

  The complaint was "three down weeks in the build phase". Checked against Davis, **three was right
  and the cadence was right**: down weeks belong every three to four weeks, and the Breeze plan (peak
  65–80 km/wk, goal times 3:15–4:30 — this athlete's tier) deloads at weeks **4, 8 and 12**, three in
  an eighteen-week plan. This engine already laid three, exactly four weeks apart.

  The depth was the problem. Davis's prose says "a 20–30% drop in mileage" and 0.75 honoured that
  sentence, but the published plans cut far less — Breeze 65 −9/−8/−11%, Breeze 80 −15/−15/−15%,
  Wind 90 −16/−17/−20%. That argued for 0.87, a 13% cut.

  ⛔ **0.87 is wrong, and `det/meso-rephase` is what caught it.** A percentage drop in a Davis plan
  and a percentage drop here are **not the same quantity**. Those plans are mileage prescriptions;
  this engine governs each week by ACWR against a chronic load that is still climbing, so cutting 13%
  off the mileage barely moves the ratio. The down week's `proj_acwr` by value:

  | `BUILD_DOWN_FRAC` | 0.75 | 0.78 | 0.80 | 0.82 | 0.84 | 0.87 |
  |---|---|---|---|---|---|---|
  | down-week `proj_acwr` | 1.139 | 1.166 | **1.184** | 1.202 | 1.217 | 1.238 |

  `NEAR_CEILING_ACWR` is 1.20, so from 0.82 up **the down week stops being a trough at all**. At 0.87
  it sits at 1.238 — a *higher* ratio than two of the building weeks around it — and §PRO6's
  guarantee degrades from 3 consecutive near-ceiling weeks to 6. A deload that does not deload is
  worse than no deload, because the safety net believes it happened.

  **0.80 is the deepest cut that still produces a real trough** (1.184 < 1.20), and it is the bottom
  of Davis's own stated 20–30% band. On the live 19-week block the two forward down weeks move from
  −24%/−22% to **−19%/−17%**, block volume 997 → **1012 km**, projected race CTL 109 → **111**,
  finish 4:05:49 → **4:05:17**.

  ⚠ **The cadence is deliberately untouched, and §PRO6 is what keeps it right.** Disabling
  `MESO_MAX_HARD` gives two down weeks unevenly placed; with it, three exactly four weeks apart. The
  forced deload is holding the plan *on* the book, not off it.

  ⛔ `BASE_DOWN_FRAC` is deliberately NOT moved — it shapes the base phase's pre-governor skeleton,
  while every down week's governed target, scheduled or forced and in any phase, comes from
  `BUILD_DOWN_FRAC` in `generate_block`. Moving both would change the same number twice.

## [0.49.0] - 2026-08-31

### Added

- **The training week is the athlete's shape now, not the table's (§DAYPREF).** Two new Settings
  fields: **Long run day** (a weekday) and **Rest days, most wanted first** (a ranked list). Empty
  keeps today's behaviour exactly.

  Rest days are a **ranking** rather than a fixed set because a plan's run count moves inside a
  single block — the reference 19-week block lays 4-, 5- **and** 6-run weeks, and §REST2 can re-lay a
  week up mid-plan to buy back volume. "Rest Monday and Friday" is therefore *undefined* at six runs,
  where only one rest day exists, and underspecified at four, where a third has to go somewhere. One
  ranking answers every frequency: take rest days off the top until the week has its quota, and when
  the ranking runs out, fall back to the house layout's own rest days for that frequency — so a
  partial ranking degrades toward the default shape rather than an arbitrary one.

### Changed

- **The long run stopped being "whatever ends the week".** `long_idx = n - 1` was never a decision;
  it was the layout table restated. Every entry in `RUN_DAY_LAYOUTS` ended on Sunday, so *last slot*,
  *the long run* and *Sunday* were one fact wearing one hat, and the comment on the table said so:
  "every layout ENDS on Sunday … because a week never ends on a rest, two consecutive weeks can't
  strand a double rest at the boundary". Letting the athlete name the day pulls those apart, and the
  seam rule that was riding along on the coincidence is now stated in its own right —
  `_max_rest_streak` measures rest clumping **around the week boundary** (a layout resting Sunday
  whose next week rests Monday is one two-day gap the athlete lives, not two one-day gaps), held
  under `REST_CLUMP_MAX`.

- **Three defects the decoupling exposed, none of which could fire while the long run was on Sunday.**
  Each is now a det tooth that was seen to fail on a revert:
  1. **The hard-gap distance was signed.** `days[n-1] - days[s]` was safe only while the long run
     ended the week. With a Wednesday long run, *every later day* reads negative, "< 2", and is
     refused — the week walks off the end of its slots and carries **no quality session at all**.
     Distance has no sign; the gap rule never cared which side of the long run a day sat on.
  2. **The guard was not running on the plain path.** It was gated on `av_blocked is not None`,
     because a template day set was trusted and an §AV-relaid one was not. A *derived* set is no more
     vetted than a relaid one — and before this, a Wednesday long run took its interval on **Tuesday,
     the day before it**, because slot 1 is Tuesday and Sunday used to be four days away.
  3. **`AV_MAX_STREAK` was judging a layout the athlete had vetted** — §REST2's pathology, one
     governor over. Someone who asks for Saturday and Sunday off at five runs has *already* chosen a
     five-day streak; measuring their spread day or their §AV relocation against 3 refuses every
     candidate, so the week sheds the volume instead of laying it. The ceiling is now what their own
     layout already spends (`_streak_ceiling`), and the §REST2 re-lay gate skips the within-week
     streak test for a derived layout while keeping the cross-week seam bound, which is about the
     week boundary rather than the shape they asked for.

  Where a preference and the engine's spacing genuinely cannot both hold, **the athlete's ranking
  wins and the plan says what it did** — the same ruling the run-day streak already follows. The
  engine only declines to make a week *denser* than the athlete's own choice already makes it.

- **§AV: a blocked long-run day goes to the nearest surviving day**, ties breaking to the later one
  (the week banks its easy days in front of its long run), instead of to whatever ends the week.

- `det/day-preference` and `det/long-run-day` are new. Between them they lock byte-identity with no
  preference set, the ranking answering a frequency a fixed pair cannot, the long-run day never being
  spent as a rest day, seam-aware clumping under its ceiling across **13,440 derived layouts** — with
  the ranking-wins escape hatch proven *reachable*, at two runs a week where clumping is arithmetic —
  and all three defects above. Eight reverts were each seen to fail on their own tooth; the first
  draft of the weekend-off case passed two of them and was rebuilt on a fixture that separates the
  re-lay from the append.

## [0.48.0] - 2026-08-31

### Changed

- **A kilometre now costs what it costs (§TP).** The engine solves in TRIMP and pays out in
  kilometres, and `est_trimp`'s five inline literals were the exchange rate between the two. They were
  wrong at the bottom. Runalyze's `activities.trimp` is Banister TRIMP — fitted over 1,037 of the
  athlete's own runs with HR, `TRIMP/min = HRr·k·e^(b·HRr)` reproduces it with RMSE **0.068/min** —
  so every rung is a checkable claim about heart rate. The old ladder claimed easy running costs
  134 bpm; measured two independent ways (914 min of §RD segment decodes since 08-01, and an
  HR-vs-pace regression read at the plan's own zone paces) it costs **143–144**.

  `easy 1.3 → 1.65 · long 1.4 → 1.72 · marathon 1.8 → 2.10`, with `threshold 2.6 → 2.55` and
  `interval 3.2 → 3.10` barely moving — they were already inside 3%. **The ladder was right at the
  top and wrong at the bottom**, which is why a uniform rescale (the obvious fix) was rejected: it
  would have put threshold at 3.24 and interval at 3.99 and broken the two accurate rungs.

  What it cost, measured on the live plan: the week of 2026-08-24 prescribed 54.0 km / 367 min
  against a **494**-TRIMP budget for work that actually costs **629**. The athlete ran it (+8.7% km,
  +32 min) for 647 TRIMP. Of the +153 overshoot, **+135 (88%) was this table** and +18 (12%) was the
  overrun — a realised peak ACWR of 1.377 against a plan projecting 1.205. One table, both known
  symptoms: the §SYM-A ledger's 36–57%-low CTL forecast, and realised ACWR landing above the ceiling
  the week was sized to respect. Across the remaining block the old price under-charged by **+24.4%**;
  the new one by +1.7%.

  Also new: `trimp_per_min()`, because four call sites read the per-minute RATE through
  `est_trimp(1, zone)` — which rounds its result to 0.1. Invisible while every rung was a 1-decimal
  literal, and it silently shaves a 2-decimal one (1.65 → 1.6). `det/trimp-price` asserts that on the
  syntax tree, so a call site that is merely right to within 3% still fails.

- **The weekly-load search had a hard ceiling nobody chose (§TP2).** `_max_week_trimp` binary-searched
  `lo, hi = 0.0, 700.0`. A search cannot report an allowance above its own `hi`, so any week whose
  real governed allowance exceeded 700 was clipped to 700 in silence — and it had become **the live
  plan's volume governor**: every build and peak week pinned at 700–701 TRIMP, and the 73.4 km/wk
  plateau was that literal divided by the price of a kilometre. Not ACWR (whose per-day test §PRO17
  already stands down on assertive weeks), not `CTL_RAMP_MAX`, not §3.1. Removing the ACWR and ramp
  ceilings entirely left the plateau exactly where it was; that is what identified it.

  Now `WEEK_TRIMP_SEARCH_MAX = 1000.0`, named, documented and set clear of the hand-over, so a
  calibrated constant decides the ceiling instead of a search range. **No safety constant changed:**
  `ACWR_SOFT`/`ACWR_HARD` stay 1.25/1.30 and `CTL_RAMP_MAX` stays 5.0, and it is `CTL_RAMP_MAX` that
  now binds — at `hi` 850 and at 900 the block is identical, with projected CTL climbing exactly
  +5.0/wk through the peak phase. `det/week-trimp-bound` holds it there.

  Together, on the live plan: **810.2 km over the remaining block (was 917.1), peak week 70.8 km
  (was 73.4), longest run 30.9 km, projected race CTL 111 against the §SYM-A ledger's independent
  estimate of 114** — the first time the engine's two opinions about race-day fitness agree (it
  projected 100 against that 114 before). Predicted finish 4:05:10.

### Fixed

- **A small week was told its long run was crushed by fatigue.** `_mark_load_integrity` tested the
  absolute floor (`LONG_RUN_MIN_KM`) before the flat test and returned, so a week that is simply
  *small* — a re-base week of three 3 km runs — was labelled "shakeout — long run held back by recent
  fatigue (ACWR ceiling)", a cause it had no evidence for, and never got the `long_flat` flag that
  says what actually happened. The flat reading now wins wherever it applies: "nothing here is
  meaningfully longer than anything else" describes the week whether or not the long run is also under
  the floor, while blaming fatigue is a claim about cause. Latent until §TP's honest price reached it
  (re-base wk6 fell 14.4 → 8.8 km); `det/long-run-identity` cases (d)/(e) were already written for it.

### Known defects

- **§TP-RATCHET — `session_eq_cap` bootstraps off the plan's own lay (filed, not fixed).** §3.1's
  per-session biomechanical cap is `SESSION_EQ_STEP × ` the largest recent session's eq_km, fed by
  `seed_seq + blk_seq`. The seed does not reach the phase: `_recent_session_eq` reads a correct
  `[14.0, 14.0]` off `det/regime-plan`'s fixture while the cap in force measures **7.21** (= 1.30 ×
  5.55, the eq of an interval session the block itself just laid). With no external anchor the cap
  follows the plan down and locks, and that fixture's block freezes at 17.9 km for eleven weeks while
  projecting the athlete detraining from CTL 55 to 15. Neutralising `SESSION_EQ_STEP` restores a
  healthy ramp (build peak 68.8 km, spanning 22.1 km).

  Same fixed-point shape §PRO23 documented for `long_km_cap` and fixed there by bounding on the
  exogenous cap; the session cap never got that treatment. **Not caused by this release** — at the OLD
  price with a 4 km seed run the same block freezes at 23.1 km — and **not present in the live plan**,
  whose real activities anchor the seed. It is a safety governor and gets its own release, its own
  dets and its own seen-to-fail proof rather than riding on a repricing. `det/regime-plan` carries a
  TRIPWIRE that goes RED the day it is fixed, with the two real assertions quoted verbatim beside it.

## [0.47.0] - 2026-08-31

### Added

- **Race day is a session on the calendar, and it reads like one (§RACE).** The engine knew a race
  as a phase label, a week role and a row in `objectives` — never as a *session*. `taper_shape`'s
  last week was laid like any other taper week, so race day drew whatever the distributor put in
  that slot: a 9.0 km "long easy run" on marathon morning. The `race` session kind landed in
  0.45.0's tree without a changelog entry, a log section or a test; this release documents it, locks
  it, and finishes the half that never shipped.

  A race is laid on its own day at its standard distance and its own pace (a marathon at marathon
  pace, everything shorter at threshold), **replacing** whatever the week had put there — one race
  day, one session. It is laid **last**, after every governing and re-governing path has finished
  with the week, because race day is a fixed day: charging ~340 TRIMP into the budget search would
  take peak ACWR past the §H1 rescue threshold, and the rescue *re-lays the week* — discarding the
  race and re-riding the assertive ceiling. A constraint on a day the search cannot change is not a
  constraint, it is a veto, and this is the third release to learn it (§REST2, §H1b, now §RACE).

  The surface half was missing entirely. The card called it a **"race run"**, and — worse — a race
  carries no `reps`, so it took the plain-run branch and printed the week's *easy* pace, labelled
  "easy", for a marathon. It now reads the race's own pace ("Race pace 4:28 /km marathon"), and the
  week listing marks the row with the race's name instead of a bare distance indistinguishable from
  a long run.

### Fixed

- **The race was laid on the calendar and its load dropped on the floor (§RACE).** The projection
  was re-rolled with the race in it and then thrown away: the reading (ACWR) was kept, the fitness
  (CTL/ATL) was not. Race week published a `proj_ctl` **7 points under** the week it had just laid
  on the single-A fixture and **14 under** on the multi-A one,
  and `end_ctl`/`end_atl` — the seed the *next* block starts from — described a taper with no race
  in it. On a single-A road that is a lie on a chart. On a §6q chain it is a **governor input**: the
  bridge block after an intermediate A-race was being sized off a fatigue number that omitted a
  marathon (measured on the multi-A fixture: `end_atl` 42.3 where the race-inclusive value is 60.2).
  Laying a session and not carrying its load is the shape §PRO20b and §101 each cost a release.

  It also fed the instrument 0.46.0 had just shipped: every week's `proj_ctl` is scored into
  `track_record`, so race week was posting a guaranteed under-prediction into the very ledger
  §SYM-A reads back as "the model under-predicts".

  **The race still does not predict itself.** §PRO7b reads race-day fitness as the peak carried
  *into* the taper, not the taper's own end, so charging the race leaves the projection for that
  race untouched — verified across every fixture: nine single-A scenarios unchanged, and only the
  chain moves (95 → 97), where the first race's load legitimately feeds the twelve weeks before the
  second.

- **The governor's decision variable was a number from neither world (§PRO23/§RACE).**
  `proj_acwr_soft` exists so the value a week *publishes* cannot drift from the value the search
  *decided on*. On race week it was being computed from the post-race numerator over the pre-race
  denominator — a hybrid that is neither. It now keeps the governor's own pre-race reading, so a
  reader checking "was this week held under the cap?" gets the number the cap was applied to, on the
  one week where the published reading and the governed one legitimately diverge.

- **A race that predates a field is still the same race (§GM).** 0.45.0 replaced the founding-road
  rule with `_same_race`, which asks for the same type — and `plan["objective"]` only gained its
  `type` partway through a block. On the live record that is **24 of 126 banked plans**, and they are
  the *oldest* ones: read as a distance mismatch, the plans that founded the road stopped founding
  it and the §6b anchor moved six days later (2026-06-19 → 06-25). An **absent** field is not a
  **disagreeing** one, which is how the label branch beside it already worked. Tightening the
  question a road is measured by can cost the road; this one now checks both directions.

- **The ACWR ceiling claim, in the two places it is actually read.** The 2026-08-30 docs pass
  corrected the manual and the README — every planned week is *sized* against 1.25 on a shape-neutral reading, and the
  raw sample shown on a card reads about a sixth high — and left the old promise ("under the 1.30
  ceiling, and volume is trimmed when a week would breach it") in the week-card tooltip and the ACWR
  tile. §RACE then made it plainly false: race week publishes a raw sample well past 1.30 and
  nothing trims it, because a fixed race is not a day the governor may negotiate. Both surfaces now
  match the manual, and the copy gate fails if the old wording returns.

## [0.46.2] - 2026-08-31

### Fixed

- **API errors now keep the API's JSON contract.** A missing `/api/*` route or a supported route
  called with the wrong method used to fall through to Flask's HTML 404/405 page, even though
  unhandled API failures already returned `{ok:false,error}`. HTTP errors now preserve their status
  and headers while using the same JSON envelope; ordinary page errors remain HTML.

- **The nightly scheduler has one freshness boundary and one whole-job lock.** The readiness card
  and boot catch-up called data stale after 26 hours while public `/healthz` waited 36, so the app
  and its uptime signal could disagree for ten hours. One server constant now feeds all three
  surfaces, including the browser bootstrap. A boot catch-up landing with the scheduled wake also
  used to serialize only its sync and then run the re-plan, scoring, watch push and backup twice;
  the second complete pass now exits while the first is in flight.

## [0.46.1] - 2026-08-30

### Fixed

- **A week frozen out of a pre-§P1 plan never healed, so its role stayed encoded in display copy
  (§P1).** §P1 promoted the periodization role from a sentence to a field: every shaper stamps
  `role` and `phase`, seven governors read the field, and rewording a human `intent` line can no
  longer move a decision. One path was missed. §6f Step E carries a fully-elapsed week **verbatim**
  out of the last saved plan, and a week banked before the field existed carried neither — so it came
  back unstamped on the next regeneration, and the one after, indefinitely: each regenerate re-freezes
  from the one before, and the gap propagates for the life of the block.

  On the live plan that was **4 of 19 weeks** — the whole base block — and one of them was a **down
  week** whose down-ness survived only because `_week_role` still parses `"Down week — absorb the
  block"`. `_is_down` is what the banking gates and the earned lift share, so the exact class of
  dependency §P1 was written to remove was still live, on a plan generated today.

  The freeze now stamps the week as it carries it, **only where there is a sentence to read**.
  Neutral by construction twice over: the value stamped is precisely what `_week_role` already returns
  for that week, so every reader gets the answer it got before — from a field instead of a parse; and a
  week carrying no `intent` at all is left alone, because `_week_role` answers `"build"` for an empty
  week and stamping that default would invent a decision rather than record one. `phase` is stamped
  only where the fallback actually knows one (it answers `None` for a legacy base week); inventing
  `"base"` there would move `_long_share_cap` off its block-wide default, and this is a
  no-behaviour-change step.

  `det/week-role` grows a limb over the freeze path itself, which nothing had covered: a plan is
  saved with the fields stripped, regenerated four weeks on, and every frozen week must come back
  stamped, agreeing with its own sentence, with the rest of the week untouched — `km`, `runs` and the
  `_ahead` counters excepted, which an elapsed week recomputes against what was actually run. Reverting
  the healing in source turns the limb red on all four weeks.

## [0.46.0] - 2026-08-30

### Added

- **The engine publishes a number about itself: how its own fitness forecast has scored (§SYM-A).**
  `track_record` has been scoring the weekly CTL projection since 0.37.0 — writing down what each
  plan projected, what actually happened, and never rewriting it. Nothing read it back. The drift
  view compared *this plan* with reality while the question one level up — *how well does the
  projection driving it actually predict?* — sat unasked in a table.

  `_tr_ctl_bias` rolls those rows into one published reading: signed **median** error in CTL points
  **and** percent, the horizon it was measured at, and the **n** it rests on. On the live record it
  reads **under-predicted by a median 40.5 % (16.84 CTL) at 28 days, over 4 scored weeks** — and the
  drift view's fitness chart now says so in a sentence, under the projection it scores.

  Median rather than the mean the §TR panel already published: at n = 4 one badly-covered week moves
  a mean by more than the signal, and on the live record the two differ (18.14 vs 16.84). Percent as
  well as points because a 16-point miss at CTL 40 and at CTL 100 are not the same statement. The
  `n` is printed *inside* the sentence rather than under it, because at these counts the count is
  half the claim — a median over three weeks is a hint, not a finding.

  **Read-only, and tested as such.** Nothing in the engine consumes it: `det/ctl-forecast-bias`
  generates a plan against an empty track record and against one screaming a 60 % under-prediction
  and requires the two plans to be byte-identical. This is the ordering ENGINE_SYMMETRY_PROPOSAL §5
  asks for — ship the instrument before anything that acts on it — and the day someone wires this
  into the periodizer that det goes red and says which promise broke. The same det holds the median
  against a fixture where mean and median disagree 5×, both signs, the ±`TR_CTL_CLOSE` "level" band,
  a published lead **span** when horizons are mixed, a shape (not `None`) at n = 0, agreement between
  the drift view and the §TR panel, and classification in the public allowlist so §82's silent-drop
  cannot repeat. No new calibration constant: the "level" band reuses `TR_CTL_CLOSE`.

## [0.45.0] - 2026-08-30

### Fixed

- **Correcting a race date by one day emptied two months of the prediction ledger (§GM).** The
  goal marathon moved from 2026-12-07 to the 6th. There is no edit-date endpoint, so that meant
  adding the 6th and removing the 7th; the next regeneration banked a plan under the new date; and
  the §FT4 prediction ledger — **58 days of forecasts, 5:23:33 down to 3:59:02, watched accumulate
  since June** — collapsed to a single point.

  Nothing had been deleted. All **126** banked plans still held every number they ever had. They had
  stopped matching the *question*, which was `objective.date == the current goal` — a string
  equality, and a one-day correction changes the string. The §6b founding-road anchor asked it too,
  so the whole drift scorecard reset to "just sealed, no drift yet" at the same moment. Worse and
  quieter: `_ft_prediction_score` and `_tr_race_plans` ask the same question with `type` added, so
  come December the engine would have settled the race against **no** prediction at all and written
  no §TR row — a forecast made, a race run, and no score, with nothing on any chart to say so.

  The rule was right about the case it was written for. A runner who *swaps* races must not measure
  the new one against the old one's road, and that is why the baseline resets on a goal change. But
  it encoded goal identity as the calendar cell the race sits on, and a race that moves is not a
  different race. `_same_race` (`sh_engine.py`) now owns the matching rule in one place — same type,
  same label, within `GOAL_MOVE_DAYS` (45 d, far inside the ~180 d that would separate a spring and
  autumn edition of one name, and nowhere near an annual's 365) — and the anchor, the ledger, the
  settlement and the §TR candidate scan all read through it. With no label on both sides there is
  nothing but the date left to hold identity, so the old exact rule stands there.

  On the live DB the ledger comes back at **58 points**, unbroken from 2026-06-30. `det/goal-moved`
  holds it from both sides: reverting to the exact-date rule reproduces the collapse to one point,
  and a rule that matches everything trips the anti-vacuity limb where a genuinely different race
  must still reset the baseline.

### Added

- **`POST /api/objectives/<id>/date` — move a race, and the objectives row keeps its identity
  (§GM).** The fix above makes the *plan history* survive an add-then-remove, but the objective
  itself does not: add-then-remove retires the row every plan was built against and stands a new one
  in its place, so the race loses its id, its `created_at` and — once run — the outcome and §FT4
  prediction score written onto it. There was no edit path, which is the only reason anyone was
  doing that. Now the date on an objective row is an inline editor: change it and the plan
  re-periodizes onto the new day with the row intact. Only an `upcoming` race can move — a resolved
  one is a matter of record, pinned to the day it was actually run, and re-dating it would point its
  result at a day nobody raced (409). Junk dates are rejected at the door like the add path's, and
  the write and its re-plan land together or not at all (`replan`).

## [0.44.11] - 2026-08-27

### Fixed

- **A day you had already run could veto the rest of your week (§H1b).** The evening of 2026-08-27
  the athlete ran 12.3 km — roughly the 11.4 km the plan had asked for that morning — on the back of
  a six-day streak. It cost 135 TRIMP against a 102 TRIMP prescription, and it took the day's ACWR to
  **1.338**, past the 1.30 hard cap. The plan regenerated one minute later and deleted the rest of
  the week: Saturday's 11.4 km easy and Sunday's 13.5 km long run, gone. The week fell from 59.1 km
  to 35.1 km — exactly what had already been run, with nothing ahead of it — and because forward
  volume is CTL-responsive, the dip propagated: **−72 km across the block**, race-day CTL 99.5 → 96.8.

  The governor binary-searches the largest weekly load whose in-week **peak** ACWR stays under the
  hard cap, and §PRO20b charges today's *actual* load into that same projection — it has to, or the
  rest of the week is bounded against a week the athlete has already partly outrun. But a day already
  run is not a candidate. With the spike inside the window the peak read the same 1.339 at **every**
  budget the search tried — 300, 150, 80, 40, 20, 10, 5, 1 and 0 — so every one was rejected, the
  search returned 0.0, and the remainder was laid empty. The engine was not judging Saturday too
  hard; it was rejecting the whole window on the strength of a day it could not change. §REST2's
  shape one release earlier: a gate that cannot pass, shedding volume in silence. It reads as caution
  from the outside, which is why it took a surprised athlete to find it.

  The measure of how wrong it was: **the prescription it deleted passes the engine's own governor.**
  Saturday 11.4 + Sunday 13.5 peaks at ACWR 1.240 — under the *soft* cap, never mind the hard one.

  The peak now bounds only the days the search can still place. A day is dropped from that reading
  only where the actual **binds** (the athlete ran at least what the plan wanted); where the plan
  still wants more of a day than was run, the surplus is a real decision variable and stays under the
  cap. Nothing else moves — every plannable day is still bounded by §H1, today's real load still
  reaches them through the curve, and the week card still publishes the honest 1.339. On the live
  plan the same evening now lays 21.0 km ahead (Sat 7.5 + Sun 13.5) rather than 24.9 or nothing: the
  overshoot is charged, the week tightens, it does not collapse. `det/plannable-peak` holds the
  boundary from both sides — a bigger spike must buy a *smaller* remainder, never a larger one.

## [0.44.10] - 2026-08-27

### Fixed

- **The spacing gate could never pass, so the engine paid for rest days in volume it did not have
  to spend (§REST2).** 0.44.9 stopped the free-day spread from chaining run days, and it worked —
  the forward plan's worst streak went from eight days to four. But the gate it used asks whether
  ONE loose day, dropped into a layout that was designed as a whole, keeps the spacing; and no
  loose day does. Every candidate for `RUN_DAY_LAYOUTS[5] = [0,1,3,5,6]` chains four running days
  (Wednesday makes Mon–Thu, Friday makes Thu–Sun), and every shape the engine lays uses five runs.
  So the spread was not gated but **dead**, and the volume it exists to hold was shed instead:
  −6.3 km on a 54 km week at an 11 km long-run cap, **−18.4 km at 7 km**. The live plan barely
  noticed (its cap hardly binds today), which is exactly why it needed measuring rather than
  assuming — a runner coming back from a layoff has a small trailing long run and a tight cap.

  The spread now **re-lays** the week to the next vetted layout instead of appending to the current
  one: `RUN_DAY_LAYOUTS[6]` is Mon–Wed, **Thursday rest**, Fri–Sun — three consecutive days at
  most, the same number §AV enforces, and a structure the methodology already endorses (Davis
  reports athletes over 160 km/wk running best on six days with a rest day, and — on precisely the
  question this lever decides — that the same weekly mileage spread over MORE days is less
  stressful on the body). Where no legal layout exists the week still sheds, and now says **how
  much**: the `rest_shed_km` that 0.44.9's own comment promised but never wrote. Bounded by
  `RELAY_MAX_RUNS` (never the seven-day layout — the 2026-07-16 no-rest week) and
  `RELAY_MAX_SEAM_STREAK` across the week boundary.

  **What this costs, stated plainly:** six runs in seven days means one rest day, so two 6-run
  weeks in a row run Friday→Wednesday and rest Thursday. The forward plan's worst streak is
  therefore **6, not 4** — on one seam, in one place (2026-09-04 → 09-09), where 0.44.9 held 4 and
  the pre-0.44.9 engine chained 8 with no rest day at all. Every week keeps a real rest day. The
  number is a constant, not a consequence: `RELAY_MAX_SEAM_STREAK = 5` permits the sixth run only
  after a lighter week, `4` refuses it outright and returns to 0.44.9's behaviour.

- **The published weekly intent stopped tracking the week the athlete actually gets.** §REST's
  HELD bound was meant to end the governor's runaway (charging a week that can no longer absorb
  the charge). It catches a week that stops responding entirely, but not one that sheds a *fraction*
  of every charge — so on the live plan `intent_km` read **78.6 km against a 58.8 km lay**, a
  19.8 km fiction where the pre-0.44.9 engine was within 1.3. `intent_km` is the only "what was
  asked of this week" number in the payload; it is published on the public box and it is what the
  plan explainer narrates as the block's volume range. A week that re-lays absorbs its charge
  again, and the two weeks affected are back to 60.0 and 66.0 against lays of 58.7 and 64.7.

- **A "held to rest days" chip on a week that held everything.** The marker fired whenever the day
  picker ran out of legal days, which is not the same as the week losing anything — three weeks of
  the live plan carried it while shedding 0.0 km. It now rides only a week that actually gave
  volume up, and carries the amount.

### Changed

- `det/rest-streaks` locks the re-lay contract: the laid day set must BE a vetted layout (an
  append and a re-lay are indistinguishable by day *count* — `[0,1,2,3,5,6]` also has six), the
  volume must land where a legal layout exists, the seam is checked on both sides of its bound, and
  every frequency the re-lay can reach must keep a rest day. Its founding-case tooth ran a 14.8 km
  cap against a 13 km long run, so the cap never bound and the tooth passed identically with the
  fix reverted; it now binds, and says so if it ever stops.

## [0.44.9] - 2026-08-27

### Fixed

- **The engine no longer trades rest days for volume: the §PRO9 free-day spread is streak-gated
  (§REST).** When the +10% long-run cap clipped a week, the spread that redistributes the freed
  volume onto extra easy days picked those days in raw calendar order — no spacing rule, no streak
  limit, no awareness of the week boundary. On the live plan it padded every 5-run base week to 6
  (Mon–Thu, one rest), filled the current week's remainder Thu–Sun, and chained an **8-day run
  streak across the seam — 14 run days out of 15**, with the only rest days invisible in the UI.
  The 2026-07-16 7-run no-rest week was the same defect at its pathological end; the fix then
  anchored the caps on truth but left the day-picker untouched.

  The spread now answers to the same spacing doctrine as every other day-laying rule in the engine
  (`AV_MAX_STREAK = 3`, the §AV relocation cap): an added easy day may never manufacture a run
  streak longer than 3 — measured within the week, against the week's already-fixed days, and
  across the seam into last week's run tail (threaded through the generator like the other carry
  state). What a gated week can't legally hold is **not** crammed back onto the days by inflating
  every easy run to the ceiling — that flattened the week past `LONG_RUN_MIN_RATIO` (the long run
  relabelled out of existence, measured on det/straddle-long's fixture) and sat every short AT the
  +10% ceiling, where the minute-rounding published 8.2 km against an 8.1 cap. The volume the
  spacing rule refuses to hold sheds instead — lighter, never denser — and the week says so with a
  `rest_gated` marker and a "held to rest days — spacing kept" chip. The governor itself also
  learned the HELD bound: a charge above what a gated week can lay no longer runs the search away
  (a 49-km-capable week was governed to `allowed` = 700, `intent_km` ≈ 90 — a fiction), so the
  published intent is again the week the athlete actually gets.

  The streak cap is the engine's own coaching guardrail, **not** a Davis-derived rule: the Davis
  corpus has no consecutive-day concept at all (checked against the nine saved Running Writings
  articles and *Marathon Excellence for Everyone* — the book's higher-mileage plans run 6–7-day
  weeks the engine deliberately never prescribes; its 5-run templates mirror the book's entry-tier
  structure, two rest days included). The methodology's aligned levers — the damage-equivalent
  ceilings, the +10% long-run ladder, and the redistribute-rather-than-shed trade the spread
  exists for — are untouched. The forward plan now holds a rest day at least every 3 days within
  every week.

- **Rest days are visible.** The plan JSON lists run sessions only, so every week card read as an
  unbroken wall of runs — the streak above was invisible, and so were the healthy weeks. Week
  cards now fill each uncovered weekday with a muted *Rest* line (display-only; the engine
  artifact is unchanged).

## [0.44.8] - 2026-08-27

### Fixed

- **A session the athlete completed as prescribed was filed as bonus volume and counted for
  nothing.** `block_log` decides "was this day scheduled?" by asking the CURRENT plan — but the
  current plan is the road ahead, and past prescriptions come from plan history. An *overflow* day
  (§PRO15's free-day spread borrows a non-template weekday only while the week's budget will not
  fit across the template's days) stops being needed the moment its own run lands, because that run
  is what stops it fitting. Run it, and the day vanishes from the next regeneration — leaving a run
  on a day with no session, tagged `unplanned`, explicitly excluded from adherence.

  Measured on the live block: Wednesday was laid at 8.6 km that morning, run at 8.62, and by the
  evening regeneration block adherence read **17/24 whether or not the run existed**. It now reads
  18/25.

  `_prescribed_at_start` restores such a session from plan history, and the normal enrichment owns
  it from there — done/missed, the actual overlay and both counters follow. A day the plan told the
  athlete to REST stays a genuine bonus run, as does a day no saved plan ever reached.

  The lookup is keyed on the run's **start**, not its date: a day resolution cannot separate the
  plan the athlete left the house under from the one written after they got back. Stamps are
  compared as instants — plans are written in UTC and activities arrive in the athlete's local
  offset, so a text compare reads one clock against the other and is wrong by exactly the offset.

## [0.44.7] - 2026-08-26

### Fixed

- **The public plan card rendered `nullk` for an unplanned run.** An unplanned run — a logged run on
  a day the plan places no session — carries `km: None`, because there is no prescription to state.
  The card knows to read the `unplanned` flag instead. `_PV_SESSION` published the `None` and
  withheld the flag, so the public line fell through both of its branches at once: it printed
  `nullk → 8.6k @ 6:49` under a ✓, as though the athlete had been told to run an unknown distance
  and had done it. The flag describes the absence of a prescription, not the athlete, and is now
  published with the field it governs.

  The `seed` DB lays no unplanned runs, so this field never appeared in a public-surface diff —
  the same gap in the same allowlist that the last one went through.

- `sessSummary` now returns `—` for a session with no prescribed distance instead of interpolating
  the absence into its label. The allowlist fix removes today's cause; this removes the class.

## [0.44.6] - 2026-08-26

### Fixed

- **The cold-start intake seeded its load state from a day that had not finished.** `_ft_cold_start`
  and `plan_seed` fill the **same** `ctl0`/`atl0` argument — `generate_plan` takes one or the other —
  so they owe the same contract, and §PRO20 defines that slot as the state at the **end of
  yesterday**, because `generate_block` rolls the projection from today *inclusive*. A seed that has
  already run through today gets today applied twice. The snapshot path was fixed for that in
  §PRO20; the no-snapshot path was not, leaving two seeds filling one parameter on two different day
  boundaries.

  It carried §PRO5b's phantom rest day with it. `reconstruct_history` pads to its `end`, so a
  cold-start plan generated in the *morning* charged today a full day of decay before it had
  happened. Measured on a constructed month of steady running: **ATL₀ 52.5 before the day's run was
  logged against 70.0 after** — the same athlete, the same day, a 33% swing in the acute-load seed
  decided by what time the plan was generated.

  Nothing is lost by stopping at yesterday: `today_trimp` is computed for **both** seed paths and
  floors today's projection under §PRO20b, so today's own run is applied exactly once — by the roll —
  instead of once by the seed and again by the roll.

  Tooth (g) on `det/plan-seed`, beside the §PRO20 teeth it completes: two databases identical but
  for a run logged today must seed identically, and the seed must equal the settled end-of-yesterday
  reconstruction. Reverted and seen to fail on both.

### Changed

- `test/golden/cold-start.json` is rewritten; the other nine are byte-identical, since it is the only
  scenario that takes the no-snapshot path. **Measured before accepting it**, because the headline
  number moved the alarming way: week 1's reported `peak_acwr` goes 2.635 → 3.334 while its
  prescribed distance does not move at all (10.1 km either way), and the whole 21-week road changes
  by **+1.2 km — 0.15%, rounding**. The ratio rose because the seed now states the acute load
  honestly against a near-zero cold-start chronic base, not because the governor loosened; on a cold
  start the ACWR ceiling cannot bind at CTL ≈ 4 and the §PER1 floors own the verdict, which is why
  the *distance* is unchanged. Every later week reads a marginally **lower** ACWR than before.

## [0.44.5] - 2026-08-26

### Fixed

- **A read the decoder had superseded was still being used to grade a session.** After 0.44.3 an
  interval session showed the two halves of one page disagreeing: the read-back was correct —
  4× 2min — while the Effort Discipline panel a few centimetres away still called it *too hard*.
  Which raises the right question: is that classification a stored verdict from the previous
  read-back, or is it recomputed?

  It is always recomputed — `/api/effort-discipline` has no response cache and recomputes every grade
  on every load. What was stale was its **input**. The monitor reads structures cached-only
  (`fetch=False`) by design: it grades a dozen runs at once, and letting a panel load fan out into a
  dozen stream fetches is exactly the cost that rule exists to prevent. But on a `STRUCT_VERSION`
  mismatch the cached-only path handed the old row back **verbatim** — there is no fetch to heal it —
  so the panel went on grading the session off the three reps v8 had found, at a work pace biased
  fast by the rep v8 had lost. Opening the run is what re-classified and stored v9; the panel had
  been rendered before that write.

  A version stamp is the decoder saying *its semantics moved*. So a caller that turns a structure
  into a **judgement** may no longer read one written under the old ones: the two cached-only reads
  in the effort monitor now pass `stale_ok=False` and fall back to the whole-run read — a verdict it
  can justify, at **low** confidence, with no per-rep claim attached. It stops asserting a reading it
  no longer has.

  **Display deliberately keeps the old behaviour.** A slightly-old decode is descriptive rather than
  a judgement; it self-heals the moment the private box views or re-syncs the run; and the public
  container shares this same database while never being able to fetch, so blanking the read-back
  there would strand it with nothing that could ever restore it. `stale_ok` therefore defaults to
  true, and only the grading path opts out.

  Four teeth added to `det/effort-discipline`, on the same fixture as the per-rep read they qualify:
  the identical cached row one version stamp apart must grade sharply at the current version and not
  at all at the superseded one, the display path must still receive it, and a current-version row
  must survive the same flag — otherwise a fix that simply stopped trusting the cache would pass
  while silently killing the per-rep read for every run. Both halves of the wiring reverted and seen
  to fail.

## [0.44.4] - 2026-08-26

### Fixed

- **The assertive ceiling followed the time of day the plan was regenerated.** `shape_response` — the
  §PRO5 read that compares measured fitness to the plan's own projection and eases the ride when the athlete
  is behind — ended the CTL curve on **today**. `reconstruct_history` pads to its `end`, so a day
  whose training had not been logged yet arrived as a day with *no* training, and the EWMA charged it
  a full day of decay before it had happened.

  On a live database the same curve read **66.6** at the end of the 25th and **63.5** on the
  morning of the 26th — a drop of 3.1, exactly `66.6 × (1 − 2/43)`, pure phantom rest. With a
  2% on-track dead band, that put every *morning* regeneration ~4.5% below projection (ease the ride)
  and every *evening* one on track (full ceiling). Same athlete, same day, same data.

  Realised fitness is now measured at the settled **end of yesterday**. Yesterday is a complete day;
  today never is. This is §PRO20's boundary — the plan's seed was moved there for the same reason,
  and `_ft_state_at` already reads a race day at `day_before` — with the measurement side the one
  place still reading a partial day. `projected` is a week-END value, so both sides now sit on day
  boundaries, and the existing "Sunday strictly before today" filter already places that Sunday on or
  before the day being measured. Today's own load is not lost: §PRO20b still floors the projection
  with it.

  `det/shape-response` gains two teeth for the property that actually matters — the same athlete on
  the same day, seen before and after the day's run is logged, must produce the same `realized` and
  the same ride factor. They are built so the pre-fix code straddles the on-track boundary, and on
  revert they report the defect in its own terms: **0.954 in the morning against 1.0 in the
  evening.** ⚠ The det's existing assertions were all *relative* to whatever `realized` came back, so
  it passed identically before and after; it could never have caught this.

### Changed

- The ten golden plans are rewritten, and the diff is the point: **10 lines, one `realized` field per
  scenario, nothing else.** Every golden scenario carries `projected: null`, so its ride factor was
  1.0 either way and no prescribed number moved. Which also means the goldens never exercised the
  ratio path at all.

## [0.44.3] - 2026-08-26

### Fixed

- **A 4×2min VO₂ session read back as "3× reps + 4× strides", and the effort monitor called it too
  hard.** Two independent defects in the §RD decoder produced one symptom, and the second one turned
  a correctly-executed session into a red mark on the card.

  **A rep is a contiguous stretch of work running, not a single block.** Segmentation cuts on pace
  CONTRAST, so a rep not held perfectly flat splits in two — and each fragment was then
  measured against the ~2min rep floor *alone*. In the reported session rep 1 went out hard and
  settled (45s @4:51 +
  90s @5:21, a 10% shift, one frame over the sustain bar); both halves fell under the floor, both
  were re-labelled *warm-up*, and a rep that had actually been run vanished from the read. Adjacent fast
  blocks carry no recovery between them, so they are one effort by construction: they are now fused
  first and measured after, at the honest time-over-distance pace.

  **The peak inside a rep is not a stride.** The stride pass ran *before* the block grammar and
  counted prominent short peaks anywhere in the run. A rep's own summit is short, rides far over the
  local floor and lifts cadence — every stride gate passes — so the same two minutes were counted on
  both axes at once. It now runs after work detection and skips the frames the reps already own.
  A genuine strides session has no work reps, so nothing is excluded and its count is untouched.

  **Why it mattered beyond the read-back.** The lost rep was the *slowest* — the first one, off the
  warm-up — so dropping it biased the effort monitor's work pace fast: **4:53/km instead of 4:57**,
  against a 5:05 interval target with a ±4% tolerance. The session was graded `too_hard` by
  **0.0001 in log space, about 0.04 s/km**. With all four reps counted it reads `on` — which is what
  the session actually was.

  `STRUCT_VERSION` 8 → 9, so cached reads re-classify lazily on first view.

  **Impact measured, not assumed.** Real streams were re-fetched and v8 A/B'd against v9 on
  identical inputs across 14 activities — every quality session, every strides session and the
  easy-runs-with-strides in the recent corpus. **Exactly one read changes: the broken one.** ⚠ A
  first pass at that sweep reported six changes and every one was the harness — today's pace zones
  applied to June runs, and the wrong floor for §SJ parts. A comparison of this kind needs
  `_zones_asof` and the part-relative `min_s`, or it invents regressions.

  `det/rd-double-count` builds 1Hz streams from a plan of legs with deterministic jitter (a flat
  synthetic pace chart has no peaks at all, so the stride tooth could never have bitten without
  texture) and reproduces that session verbatim: pre-fix it returns 3 reps / 4 strides. Six teeth,
  each half of the fix reverted and seen to fail independently — including the work pace coming back
  as 293 against the session's real 297. Three of the six exist to stop the fix over-reaching: a
  genuine strides session still counts its strides, fusion never crosses a recovery (2×5min with a
  float between stays two reps), and a lone surge still isn't a workout.

  ⚠ The §RD decoder had **no det at all** before this — which is why a change of this size passed
  the suite silently on the first run.

## [0.44.2] - 2026-08-26

### Fixed

- **A day already run kept changing what it had been asked to do.** Reported from a live plan card:
  a Monday prescribed **8.2 km** on Monday read **11.1 km** by Tuesday and **11.5 km** by Wednesday.
  Plan history holds the whole trail — 7.4 → 8.1 → 8.2 → 11.1 → 11.5 — and none of it was a
  re-anchor or a regime change: the week's intent is legitimately recomputed every day against fresh
  CTL/ATL, and the days *already run* were being recomputed along with it.

  The straddling week has to be re-laid on every regeneration — the remainder must be re-governed —
  and the elapsed slice of that re-lay was published as the day's prescription. `generate_block`'s
  own docstring had promised the opposite since §6o ("the elapsed days are kept verbatim"), but the
  slice was cut from a fresh lay of the whole week, so it tracked today's arithmetic.

  Lived days are now read back from **plan history**: for each one, the newest saved plan generated
  *on or before* that date whose road covers it. Such a plan necessarily laid the day as a
  today-or-future session — the elapsed slice is strictly `< today`, so even a plan generated on the
  day itself carries it in the governed remainder — which means a re-laid value can never become the
  source. And once the day passes, no new plan can qualify: the answer is frozen by construction
  rather than by a flag. A day the road covered but prescribed **nothing** for stays empty, so a
  prescribed rest day cannot gain a session after the fact. A day no saved plan reaches
  (fresh or rebuilt DB) falls back to the re-lay, so this degrades to the old behaviour rather than
  to a hole.

  This is the §PRO12 posture applied to the current week. `_laid_sessions` deliberately lets the
  *newest* carrier win, which is correct for fully-elapsed weeks (frozen verbatim by §6f Step E) and
  exactly wrong here — the newest carrier **is** the re-lay.

  Two knock-on corrections came with it. The week-level §PRO9 cap note is now read only off sessions
  *this* generation laid: a pinned day carries the flag from the cap in force when it was written,
  and the card names today's trailing-4wk number — pairing an old decision with today's figure is
  the §6e2 defect. And a pinned session that predates the `km` field is refused rather than
  published, because `km` is summed unguarded into the week header and a partial pin would have
  crashed plan generation outright.

  `det/lived-days-pinned` pins seven teeth: the pin itself, stability across two regenerations on
  different seeds, rest days staying empty, the newest-on-or-before ordering, the graceful fallback,
  and the governed remainder plus ACWR projection being byte-identical with and without pinning.
  ⚠ The seventh exists because the first six were **measured** to be vacuous at the seam — with the
  `pinned_past=` argument deleted from its one call site they all still passed. It plants a
  distinctive distance in a saved plan and drives `generate_plan` → `_split_freeze` →
  `generate_block` end to end, with a control run on wiped history proving the value could only have
  come from history. Both reverts now fail: deleting the wiring, and neutering the pin.

## [0.44.1] - 2026-08-25

### Fixed

- **The map and the read-back went blank, because Runalyze stopped sending streams unasked.**
  `get_activity_details` grew an `include_streams` flag that defaults to **false** — raw per-sample
  series are too large to return by default — and we were still calling it with an activity id
  alone. The reply came back perfectly valid and perfectly empty: `streams_included: false`,
  `returned_points: 0`, every array `[]`, while `streams_meta.available` listed ten recorded
  channels over 2090 samples. Nothing raised, nothing logged. The route map, the pace/HR profile and
  the §RD read-back all quietly stopped existing for any run synced after the change. The read now
  asks for the streams it needs by name (distance, time, HR, cadence, elevation, lat/long); the one
  caller that only wants the summary — the HR-zone distribution — still doesn't pay for them.

- **And the emptiness was being written down as an answer.** Both caches key on a version: a profile
  stamped with the current version is a hit, and a stored structure that already refused is a hit.
  So the blank profile and the "no usable pace/distance streams" verdict would have been served
  *forever*, on rows nothing ever revisits — the run would have stayed mapless and unread long after
  the request was fixed, and the same would happen to any run whose upload is still landing when the
  nightly sync reaches it. An empty read is now only an answer when the source itself says there is
  nothing to read (`streams_meta.available` empty); otherwise the next look tries again. A run that
  genuinely records no streams still settles on the first read and is never re-fetched.

  `det/stream-optin` pins the request, both retry rules and both settle rules. Six mutations, six
  reds — including the two that restore the old cache behaviour, which is the half that would have
  survived the outage.

## [0.44.0] - 2026-08-25

### Fixed

- **The guides were not guiding.** First live guided interval session: the watch offered the guide,
  the summary was right, the transitions fired on time — a beep at ten minutes, a beep at every two
  after — and it never once said what to do. Everything a step carries (the countdown, the pace and
  heart-rate targets, the detail line) lives on the SuuntoPlus display, which is a *screen of its
  own*; the watch does not switch to it for you. So the whole prescription sat on a page nobody was
  looking at, and each step change arrived as an unexplained noise. The Guide format has the answer
  and we simply never emitted it: **`notification`**, a popup shown for about twenty seconds when a
  step starts, on whatever screen is actually in front of you. Every step now fires one — the step's
  name, what the block is, and its targets: `Work 1/4` · `2min at interval - 5:00/km - HR 169-184`.
  It is capped at 13 and 54 characters, so segments are added whole and dropped whole: a target left
  out beats `HR 169-1`. A rep whose detail already opens with its duration does not announce itself
  twice.

- **`@` was being deleted from every instruction sent to the watch.** The watch charset genuinely
  has no `@`, and the sanitiser dropped unsupported characters rather than translating them — so
  `2min @ interval` arrived as `2min  interval`, an instruction with its preposition removed, and
  `short VO₂ touch` as `short VO touch`. Characters that carry meaning are now transliterated:
  `@`→`at`, `₂`→`2`, `≥`→`>=`, `×`→`x`. The map is the complete out-of-charset inventory of the last
  sixty saved plans rather than a guess; `✓`, a decoration whose sentence reads the same without it,
  still goes.

  `det/guide-notify` locks both, over three fixtures — the third exists only to overflow the
  54-character budget, because nothing else in the battery reached it and a drop-whole rule asserted
  by cases that always fit is not asserted at all. Six mutations, six reds.

## [0.43.0] - 2026-08-25

### Added

- **"Rebuild on watch" — for guides you deleted from the wrist.** The nightly push is idempotent: a
  session already on your Suunto account is *updated in place*, keeping its guide id, so a re-plan
  quietly keeps the watch current without ever stacking duplicates. That is the right behaviour and
  it has one blind spot, which only shows up when you clear the guides off the watch yourself.
  Suunto hands the watch the guides it does not already hold, keyed on that same id — and an update
  does not change it. So the push honestly reports every session sent, and exactly one of them
  appears on the wrist: the day that has just entered the seven-day horizon, whose id is new to the
  account and therefore created rather than updated. Six sent, one arrives, and nothing anywhere is
  in error. The Settings window now carries a second button beside the push. **Rebuild** deletes
  each guide in the window and sends it again under a fresh id, so the whole week syncs as new. It
  is deliberately not the default — churning every id nightly would be a delete-and-recreate cycle
  on a watch that is already correct — and a delete that fails falls back to the ordinary update,
  because a stale guide beats no guide. `det/guide-recreate` runs both modes over one fixture and
  pins the difference in both directions, including that neither mode ever touches a guide that
  isn't ours.

## [0.42.0] - 2026-08-25

### Fixed

- **The readiness card stopped reporting things you never told it.** Legs and sleep defaulted to
  "ok" whenever they were absent, on both sides of the app. So a day with no check-in was scored as
  though you had declared fresh legs and decent sleep, and the card printed **"All signals normal"**
  over it. With the HRV feed also dark since mid-August, that was the live state: a green light and
  a confident sentence resting on nothing at all — no check-in, no heart-rate variability, no
  evidence of any kind. Saving a check-in that was only a note wrote the same two invented
  declarations into the database, where they read back as reported signals for the rest of the day.
  Absence is now absence. The card names which signals actually informed it, says plainly when none
  did, and the check-in dropdowns start on "—" instead of pre-selecting an answer you did not give.
  **The verdict itself is unchanged, deliberately.** Green here means "run the plan", and easing
  training because nobody filled in a form is the same mistake pointing the other way — it would
  have fired every day for seven weeks. What changed is the claim, not the call: the light can still
  be green, but it no longer pretends to know why.

### Changed

- **A check-in can now say less than everything.** Leaving legs or sleep unset records nothing for
  them rather than storing "ok", so a note-only check-in is exactly that. Any value you do pick is
  still validated against the same vocabulary.

## [0.41.0] - 2026-08-25

### Added

- **A chart for the thing that actually moves first: speed per heartbeat.** Running the same pace at
  a lower heart rate *is* aerobic fitness arriving, and unlike a training-load model it needs nothing
  but the watch. The readiness section now carries a full-width chart of it — one point per aerobic
  run, with the fitted trend and a hover carrying that run's pace, distance and heart rate. It sits
  above the durability tile and is deliberately more prominent: economy decay over a long run only
  says something once the long runs are long, while efficiency reads on every easy run there is.
  **Temperature is drawn underneath, in its own panel, on its own scale.** Not overlaid on a second
  axis — that is the standard way to make two series look like they explain each other, and this is
  exactly the case where you must not: heat depresses efficiency, and the cool runs are also the
  recent, fitter ones. The two panels share only their time axis, and **nothing subtracts a
  temperature correction**, because how large that correction should be is not settled on one
  runner's data. The card says so in as many words, and carries the correlation as a number.
  Aerobic runs only — average heart rate at or below the top of Z2, using the app's own
  LTHR-anchored zones. Mixing an all-out 5k with an easy hour makes the trend read the training mix
  rather than the athlete: on the calibration corpus that alone moved the fit from r 0.40 to r 0.78.
  Private-only, like durability — it is heart-rate data. Measure-first: shown and trended, governing
  nothing.

### Fixed

- **Chart marks stopped turning into ellipses on a phone.** The trend charts here are drawn stretched
  to whatever width they are given, which is fine for lines and bars and quietly wrong for a circle:
  the same markup that drew round points on a laptop drew tall ovals at 390px. The new chart's points
  are drawn so their size is fixed in real pixels and the stretch cannot deform them. This is the
  same defect as the squeezed axis labels fixed in 0.29.0, one layer down — the stretch scales marks
  as well as lettering.

## [0.40.1] - 2026-08-25

### Fixed

- **The readiness card stopped telling you to take a hard session easy.** The daily readiness
  narrator was given your HRV, your legs, your sleep and your note — and nothing at all about the
  session it was writing above. So on an interval day it wrote what it assumed: *"run as planned at
  an easy, conversational effort"*, printed directly over a card reading INTERVAL SESSION. The
  wording blended two different verdicts, because the instructions it works from used "easy" as the
  word for *hold back* while telling it green means *run as planned*. It now knows what today's
  session is and what pace it was prescribed at, and its instructions say plainly that softening a
  hard session is a change to the plan it is not allowed to make.
  This mattered more than a clumsy sentence: the rule for the whole AI layer is that it may narrate
  and may raise caution, never prescribe — and telling a runner to take a prescribed interval
  session conversationally is prescribing, in prose, past a green light that was itself perfectly
  correct. The check that guards the verdict never read the sentence. So the fix is not only better
  instructions: on a green light with a hard session prescribed, a "take it easy" narration is now
  dropped for the engine's own wording, deterministically. It stays out of the way on easy days,
  where the word is right, and on amber and red days, where the narrator being cautious is the
  entire point.

## [0.40.0] - 2026-08-25

### Added

- **The long run can finally become a marathon long run.** The plan's ceiling on how much of a week
  may sit in one run is no longer a single number for the whole road. Base keeps the published
  doctrine (a long run is a quarter to a third of the week) — that is the right ceiling while
  weekly volume is still being built. The marathon-specific block is allowed more, so the road now
  arrives at the start line by way of a 30 km and a 32 km run instead of topping out at 24 km. The
  block's total distance does not change; the same training is redistributed. **The two big runs are
  delivered by the existing +10 %-per-week ladder, not by the new ceiling** — a share ceiling only
  ever permits, and the check that holds this says so: with the ladder in force, the lifted ceiling
  may not add a single metre. Every other brake is untouched.
  ⚠ The cost is time on feet, and it is deliberate: 32 km at an easy pace is about 3h36, past the
  2:30–3:00 duration cross-check the doctrine also carries, and the injury evidence behind the
  ladder is specifically about the longest run. Recorded as a deliberate operator decision, with
  the simulated ladder alongside it, in the calibration inventory.
- **The scorecard says which half was wrong.** The engine has been grading its own four-week fitness
  forecasts since 0.37.0, and every settled forecast so far came in low. That single number fused
  two very different failures: a model whose physics is wrong, and a plan the runner did not run.
  Each scored week now also recovers what its plan asked for that week and what was actually run, so
  the two can be told apart — and on the first four weeks they separate cleanly, which is a finding
  about the prescription, not about the projector. Derived when the scorecard is read, so the ledger
  of scored forecasts is still written once and never revised. The public showcase gets the ratio;
  the raw weekly distances stay on the private box.

### Changed

- **A week's role is a field, not a sentence.** Whether a week is a down week, a taper week or a
  race week used to be decided by reading the first word of the sentence written for the athlete —
  seven separate governors re-parsed that copy, so rewording a caption could silently move the
  recovery anchor, the taper's starting point and the progression gates all at once. Each week now
  carries its role and its phase as real fields, published in the plan, with the sentence rendered
  for humans alongside. Nothing about any plan changes; a check now fails if a pure copy edit ever
  moves a training decision again.
- **A week's stated target is the target it was actually set.** The number the plan published as
  "what this week asked for" was the fixed template the block started from, not the volume the week
  was really governed to. While the engine is riding its ceiling — which it does whenever the
  evidence allows — those two diverged badly: through the base phase the stated bar read between a
  half and a third of the sheet the athlete was actually handed, and then snapped back into
  agreement at the next phase boundary. It now publishes the bar the week was governed to, so
  adherence is measured against what was really asked. The cautious path is unchanged.

## [0.39.0] - 2026-08-23

### Changed

- **The plan engine is its own file.** Everything a training plan is computed from — the engine, the
  feasibility model, the fitness/fatigue projector, the pace maths — has left the application file
  and lives in `sh_engine.py`. It imports nothing from the app: the arrow points one way, so the
  engine can be read and tested on its own, and a change to the web layer cannot alter a plan. The
  application file drops from 12,815 lines to 8,244 and is what its name says — routes, syncing,
  storage, the scheduler. Nothing about the app behaves differently, and the proof that it doesn't
  was part of the move: the ten golden plans regenerate byte-identical.
- **The self-test knows which file it is looking at.** The battery's clock pin, its constant
  inventory and its source scans all walk a list of the app's modules rather than assuming one file,
  so the next time something moves out, the checks move with it instead of quietly covering less.
  Two new checks hold the split itself: one fails if the engine ever imports the app back, if a
  re-exported name drifts, or if a module is missing from those lists; the other fails if a module
  the app imports is missing from the container image — a mistake that shows up nowhere except in
  the container.

## [0.38.0] - 2026-08-23

### Added

- **The footer says which version you are looking at.** Between the sync line and the Runalyze
  attribution, the page now names the release that served it — the same string the stylesheet and
  script are cache-busted with, so a screenshot can never claim a version the browser did not
  actually load. It is served by the page, not typed by hand: it cannot be forgotten at release time.

### Fixed

- **The stop-the-run control no longer out-sizes the row it sits in.** "I had to stop / chest
  symptom" was set two-and-a-bit points larger than the ENERGY and SLEEP labels beside it, in the
  same uppercase mono — big enough to read as a different typeface rather than as emphasis. It takes
  the sibling labels' type now. The emphasis that control needs is the one it already had: it turns
  red when it is ticked.
- **The run browser's footer had never learned the sync time.** The explorer and the dashboard are
  one document and have always shared a footer, but only the dashboard's loaders ever painted it, so
  `/runs` sat on the shell's placeholder — "not synced yet" — however recently you had synced. Both
  pages now paint that line through one writer, and the two footers are compared, word for word, by
  the browser suite.

## [0.37.1] - 2026-08-23

### Fixed

- **A week stopped being "this week" a day early — in the southern hemisphere, once a year.** The
  code that works out which Sunday a training week ends on mixed a UTC timestamp with local-time
  date arithmetic. In New Zealand and eastern Australia, where daylight saving begins on a Sunday
  at 2 a.m., that lands the end one day early for the week containing the switch — so on that
  week's final Sunday the plan no longer recognised it as the current week. It has never affected
  anyone in Europe or the Americas, which is exactly why it survived: the arithmetic is only wrong
  in timezones nobody has run this app from. Now computed entirely in UTC, and checked by a browser
  test that loads the page in Auckland.

## [0.37.0] - 2026-08-23

### Added

- **A track record: the engine now keeps score on itself.** Every night the app forecasts a weekly
  fitness trajectory and a finish time with a band — and every night the new plan replaced the old
  one, so those forecasts were overwritten before anyone could check them. A model that
  continuously re-forecasts and never scores itself cannot be wrong, which is another way of saying
  it cannot be trusted. Each prediction is now scored the first time its outcome is knowable,
  written down, and **never rewritten**: weekly fitness checkpoints (with the *bias* reported apart
  from the scatter, because a persistent sign is a model error while noise is just weather) and
  race bands, scored both at the final call and at eight weeks out. Only forecasts made at least
  four weeks before the outcome are scored at all — grading the plan regenerated the night before
  would be grading hindsight. A new *Track record* panel shows it, and the public box carries it
  too, with one deliberate difference: it publishes whether a band contained the race, never the
  finish time. Publishing a prediction beside its error would hand the result back, and results
  stay private.

## [0.36.1] - 2026-08-23

### Documentation

- **Every number in the engine now says where it came from.** The manual gains a short note on what
  a self-hoster who is not the author inherits: most of the engine's numbers are the sport's
  published consensus and travel to any runner, but about twenty were fitted to one athlete's own
  history. The governors among those fail safe — they are ceilings that only ever reduce load, so
  being wrong for you makes a plan too cautious rather than too aggressive — while the finish-time
  projection does not, which is why it always ships with an uncertainty band. Treat the band, not
  the time, as the output. The full constant-by-constant inventory lives in the engine-science notes
  and is now checked by the test suite: a new constant that nobody has classified fails the build.

## [0.36.0] - 2026-08-23

### Changed

- **The plan explanation is computed once per plan, not once per click.** Asking "why this plan?"
  twice about the same plan used to mean two AI calls, two waits and two bills for an answer that
  could not have changed. It is now remembered against the plan it describes, so the second look is
  instant, and it is forgotten the moment the plan is regenerated, the change being explained is
  different, or the athlete context that shapes the wording is edited. A failed call is never
  remembered — one provider hiccup must not become the plan's permanent explanation.

### Added

- **The AI boundary is now written down.** ENGINE_SCIENCE gains an architecture decision record
  stating, and then evidencing, that the language model parses, narrates and listens but never
  prescribes and never clamps: a table of all five places it is used and what each is allowed to
  return, the deterministic tests that hold each safety limit, and the plainest evidence of all —
  the engine plans, governs and projects identically with no API key at all. It also records what
  would have to be true before a model-generated number could ever become part of a plan.

## [0.35.0] - 2026-08-23

### Added

- **Keys now show a fingerprint.** A key box that never shows you the key cannot tell you *which*
  key it is holding — "configured" reads exactly the same before and after you rotate a token, so a
  save that silently failed and a save that worked look identical. Each stored credential now
  displays eight hex characters of its SHA-256 beside the status badge. It is one-way, so nothing
  about the key is revealed, and you can compute the same digest yourself (the MANUAL gives the
  one-liner) to confirm the box holds what you think it does.

### Fixed

- **Saving one key no longer wipes the ones you were typing.** The keys block re-renders itself
  after a save, and the re-render used to blank every field — so pasting the three Suunto
  credentials and pressing the first Save lost the other two, with nothing to say why. Unsaved text
  in the other fields is now preserved; only the field you just saved is cleared.

## [0.34.0] - 2026-08-23

### Added

- **The health page now tells you when a feed has died.** Sleep, HRV and overnight heart rate arrive
  from the watch every night — and when that stopped on 15 August, nothing on the page said so. The
  charts did not look broken; they simply ended, with the last point sitting there reading like
  today's, and the gap was found by eye more than a week later. A banner above the markers now names
  the feeds that have gone quiet and the date they stopped, and the card that owns a dead feed
  carries its age. It stays silent for anything that is not actually a broken pipe: a blood marker
  from last winter is not stale, and a weight you typed in yourself last month is you telling us
  something, not a sync to worry about. A cue that fires on ordinary data is a cue you learn to
  ignore, which is how the real outage stayed invisible.

## [0.33.0] - 2026-08-23

### Changed

- **Two numbers the app collected but never used are no longer collected.** Every sync wrote
  Runalyze's *monotony* and *training strain* into the daily shape snapshot, and nothing in the
  engine — no governor, no view, no page — had ever read either one. Both are computed from TRIMP,
  the training-load axis this project's own science notes identify as the wrong one for predicting
  injury (running injuries are biomechanical; the evidence for the acute:chronic family is thin),
  so the honest answer was to stop carrying them rather than to find them a job: a number already
  sitting in the database is the one a future change reaches for precisely because it is there.
  The columns stay in the database so the readings already banked are not deleted, and the verbatim
  upstream response is still kept, as it always was, privately. A new check fails the build if any
  code writes, reads or names either field again — the point is not the removal, it is that the
  re-introduction now has to be a decision.
- **The signals that were built but not wired now have written answers.** ENGINE_SCIENCE gains a
  reckoning section covering all three: long-run decoupling stays *display-only* (the durability
  tracker measures and shows, and the bar it would have to clear before it governs anything is
  written down in advance), monotony/strain are dropped as above, and the psychological axis —
  feel, readiness notes, free-text status — is confirmed as a one-way gate. It can ease a week or
  halt it; it can never add load. That asymmetry is deliberate, and the reasons are on the record.

## [0.32.1] - 2026-08-23

### Fixed

- **The readiness row breathes again.** Three cards abreast made the durability tracker's debut row
  cramped. The verdict card now takes the full width of the section — it is the one thing that
  matters today — and the acute:chronic and durability trackers share the row below, half each.
  On the public box, where durability stays private, the acute:chronic card takes the whole second
  row instead of sitting beside an absence.

## [0.32.0] - 2026-08-23

### Added

- **The durability tracker.** Long-run aerobic decoupling — how much your pace:HR drifts from the
  first half of a long run to the second, the proxy for how well your running economy holds up over
  distance — has been measured and banked since July, but only reachable as an API payload. It now
  has a home: a third card in *Today's readiness*, beside the verdict card and the acute:chronic
  tracker. A gauge shows your median decoupling over the last six long runs (durable under 5%, high
  fade past 10%), a verdict line names the state and the prior median it moved from, and a tracker
  chart draws one bar per long run, coloured by its fade band, with each bar's distance in its
  tooltip — because decoupling drifts with distance, a chart that hid duration would lie. The trend
  word (improving / steady / declining) voids itself when your recent long-run distances change mix,
  exactly as the engine has always read it. This is **measure-first**: the card tracks and trends
  the signal and governs nothing — it earns a governing role only if the corpus shows it predicts
  race fade. It is also **private-only** (decoupling is HR-adjacent): the card and its new
  `GET /api/durability` endpoint are absent from the public box.

## [0.31.2] - 2026-08-22

### Fixed

- **The public plan gets its detail back.** Tightening the public view in 0.31.0 also removed seven
  small fields it should have kept — the "long-run held" chip, the marker that turns a completed
  week's rest day into "optional", the flags explaining why a week has no distinct long run, and
  the number behind the "measured fitness is X% of projection" line, which without it read 0%.
  None of them say anything about the athlete; they describe the plan. They are back, and there is
  now a check that every field the plan engine produces has been deliberately classified as either
  published or withheld — the list built from test data alone had missed the ones that only appear
  when the training-load governors act.

## [0.31.1] - 2026-08-22

### Fixed

- **The watch stops collecting guides it should have thrown away.** A planned session's guide is
  identified by its date and its kind, but a re-plan can change the kind on a day already sent — an
  easy-only check-in turns a tempo into an easy run, and the load ceiling turns a clipped long run
  into a shakeout. The new guide was uploaded and the superseded one simply stayed on the watch,
  because nothing in the app could delete a guide at all. The nightly push now removes its own
  guides once they are superseded, once their day has passed, or once the day no longer carries a
  session, so the watch shows one guide per planned day and nothing behind today. It never touches
  a guide it didn't write, and a day whose upload failed keeps the guide it already had.
- **A duplicated internal helper.** A time-formatting function was defined twice; the first copy had
  been unreachable since it was written. Removed, with a check added so a shadowed definition can't
  slip in again unnoticed — in a single-file application it produces no error of any kind.

## [0.31.0] - 2026-08-22

### Changed

- **The public site now names what it publishes.** Every public endpoint used to serve the private
  payload and then remove the fields someone remembered were personal. That is only ever as good as
  the last person to think about it, and two things had slipped through: the shape endpoint was
  serving the entire upstream snapshot record — including the HRV baseline and normal range — and
  the same endpoint published the time of the household's nightly sync, which the health endpoint
  deliberately withholds and which the page footer was printing. Both are gone. The rule is
  inverted: a field reaches the public view because it is listed, not because nobody removed it, so
  anything added in future is private until someone publishes it deliberately.
- **Settings and keys are applied as one unit.** The values a self-hoster can set in the Settings
  window were applied one at a time, which left a brief window where a page could render with half
  the old configuration and half the new. They are now resolved first and published together.

### Fixed

- **Changing the Runalyze token in the Settings window now takes effect immediately.** The HTTP
  session kept the key it was built with, so data pulls carried on authenticating with the replaced
  token until the app was restarted — while the other half of the client picked the new one up at
  once. Both now follow the change.
- **Two simultaneous reconnections to Runalyze no longer trip over each other.** A page load and
  the nightly sync could each start a new session at the same moment and the wrong one could win,
  leaving the app holding a connection the server had already dropped.

## [0.30.1] - 2026-08-22

### Fixed

- **The readiness card no longer depends on a payload it doesn't need.** The HRV signal read the
  latest shape snapshot's raw payload without checking there was one. Every row the sync writes
  carries it, so the hole was latent — but any other writer (a restored backup, an interrupted
  write, a test fixture) leaves it empty, and the whole readiness card would then fail with it,
  over a nice-to-have. A missing or unreadable payload now reads as "no HRV signal", the card
  still renders, and the stop-symptom safety floor still halts.
- **Two self-test checks that could only pass on one kind of database.** The taper's race-pace
  check and the week-card truth check both read whatever plan the running instance happened to
  hold. On an instance with no upcoming race — a demo instance after its race day, or any runner
  between goals — there is no taper and no race week to inspect, so both checks reported a failure
  that was about the data, not the code. They now build the road they need (a marathon anchor and
  a non-marathon one) and assert on that, keeping the live instance as an extra check when it has
  one to give. Net effect: the suite is green on every seeded variant, and both checks now cover
  ground they never reached before — the non-marathon taper pace had no live road at all, and the
  race-week card rule is now checked on every instance rather than only on one mid-block.

## [0.30.0] - 2026-08-22

### Changed

- **The category colours are back.** The square-polychrome palette — the shape tiles tinted by
  metric, the plan phases in their colour families, the green volume bars, the blue fatigue trace —
  came out in 0.29.0 as an undocumented trial, and one day of the flat single-hue dashboard settled
  the question: too dull. It is back, and this time it is written into the design document instead
  of diverging from it. One improvement over the trial: the shape tiles take their hue from what
  they measure (a metric identity) rather than where they sit, so adding or reordering a tile can
  no longer repaint the row.

### Fixed

- **The two acute:chronic numbers on the dashboard agree.** The readiness card divided the synced
  snapshot's fatigue by its fitness, while the Acute:chronic gauge painted the ratio field the sync
  provides — which is computed on a different basis and can sit visibly apart on the same row
  (0.87 against 1.09). The gauge now divides the same row the same way: one number, as the gauge's
  own caption always claimed.

## [0.29.0] - 2026-08-22

### Changed

- **The front end is code now.** The dashboard's markup, style and script — some 270 KB — used to
  live inside the Python file as one giant string, where a syntax error in 2,600 lines of
  JavaScript could only be found by opening the page and noticing nothing worked. It is three real
  files (`static/index.html`, `app.css`, `app.js`), served versioned and checked in CI like
  everything else. Nothing about the page should look different — and the proof that it doesn't
  (byte-identical screenshots in all three themes) was part of the move.
- **The app speaks in its own voice.** Every remaining browser `alert()`/`confirm()` is gone;
  destructive questions and errors arrive in the app's own dialog, which explains the consequences
  before you confirm.
- **Colours belong to the theme.** The heart-rate zones were the last hard-coded hex values and are
  per-theme tokens now. An activity's pace trace no longer ramps red→green — the one pair a
  colourblind reader can't separate — but deepens a single hue, fastest of the run strongest, as
  before. The square-polychrome tile overlay (a trial that never made it into the design document)
  is removed; the code agrees with the document again.
- **The keyboard reaches everything the mouse can.** The phase bar, week strip, completed-session
  lines, per-run links, calendar days and metric chips take focus and press with Enter/Space, with
  a visible focus ring, and "reduce motion" is honoured by the animations and the scrolling alike.
  Segmented controls that announced themselves to screen readers as tab lists with no tabs now
  describe themselves truthfully.
- **Chart axes survive a phone.** The trend charts stretch to fill their width, and the axis labels
  used to stretch with them — about 3px wide on a 390px screen: drawn, present, unreadable. The
  labels are HTML now, they thin themselves rather than collide, and a resize, rotation or tab
  switch re-fits them without a re-render.
- **Small controls grew invisible padding.** The theme swatches, the "?" help bubbles, the A|B|C
  priority segments and the health-range buttons all sat under the 24px touch floor; their hit
  areas clear it now, with no change to how anything looks.
- **The browser chrome follows the theme.** The tab and PWA title-bar colour, and the installed
  window's splash, match Daylight, Charcoal or Aurora — whichever is active.

### Added

- **A freshness chip on the readiness card** (private view): "synced 3 h ago", amber past 26 h —
  the same staleness threshold the nightly sync's own catch-up uses, so the screen and the
  scheduler can't disagree. The public card stays silent on timing: a visitor has no business
  learning the household's sync routine.
- **An honest offline shell.** Open the installed app with no connection and each tile says it is
  offline and dates its data from the last sync the app remembers — never a parked "Loading…".

### Fixed

- **The public box no longer points visitors at buttons it doesn't have.** The empty plan tile, the
  "this plan is stale" banners, the readiness card's no-plan line and the drift view's empty state
  all used to suggest hitting Generate plan — a control that exists only on the private console.
- **Small repairs.** The gauge "you:" pill is clamped inside the gauge at the scale's ends; the
  health form's date is prefilled to today; the activity profile hint keeps its text on a phone
  rather than reserving legend space it no longer needs; the route map's tiles dim to match the
  dark themes; and the stretched charts draw their strokes at constant width, as the design
  document specifies.

## [0.28.1] - 2026-08-22

### Fixed

- **The browser self-check's readiness probe was looking in the wrong place.** It asked the server for
  today's readiness and then read the verdict from a field the answer has never contained, so it
  reported a failure with an empty result — and had done since the day it was written in June. Nothing
  was ever wrong with the readiness verdict itself; only the check was broken. It reads the right
  field now, and a new test pins both halves — that the server puts the verdict where it says it does
  (on the private view and the public one), and that the page's probe reads it from there. A check
  that only runs when someone opens a page can rot for months without a sound.

## [0.28.0] - 2026-08-21

### Added

- **The engine's answers are frozen as golden snapshots.** Nine synthetic training scenarios — a cold
  start, a mid-block, a maintenance stretch, the week after a race, both regime postures, a short
  rebuild, a taper, a week away — are rebuilt at a fixed clock and compared byte-for-byte against
  files committed to the repository. A refactor now has to prove it changed nothing; a deliberate
  change to what the engine prescribes has to update those files in the same commit, where the diff
  is readable line by line. The fixtures are hermetic: they were checked under wall clocks years apart
  and produce the same plan, so they cannot quietly rot as time passes.
- **Continuous integration.** Every push runs the full battery on a freshly seeded synthetic database,
  checks the goldens still match, and scans for personal data — plus the browser flows in six modes.
  The gate used to exist only on one laptop.

### Fixed

- **A plan is computed for the day it is for, not the day the server happens to be running.** The
  engine takes the date it should plan for, and three readings ignored it and asked the clock
  instead: how fit you have actually become, and — on a first-ever plan — which past race still
  describes you and what starting fitness to assume from it. In everyday use the two dates are the
  same, so nothing about your plan changes here. It mattered for testing: a plan built for a fixed
  date quietly answered differently tomorrow, which is exactly the kind of drift the golden snapshots
  exist to catch, and it was hiding one layer below them. A new check rebuilds every scenario under
  clocks eight months apart and requires the same plan.

### Changed

- **The self-test battery no longer runs inside the app.** It lives in its own file and runs as its
  own process against a snapshot of the database. Two things follow. A mistake in a test can no longer
  take the web app and the nightly sync down with it — it breaks the self-test and nothing else. And
  the app no longer goes offline while testing itself: it used to answer "a self-test is running,
  retry in a minute" to every request for about forty seconds, which was the only downtime it ever
  had, and it inflicted it on itself. Starting a battery now answers immediately and the page follows
  its progress; the site stays fully usable throughout.
- **The scenarios that call the language model are opt-in.** The default battery is free and
  deterministic — no key needed, no dependence on a model's mood. Set `SH_SELFTEST_LLM=1` on the
  machine where the key lives to exercise those paths for real.

## [0.27.2] - 2026-08-21

### Fixed

- **The readiness card is readable in every theme and state.** White text on the amber and red
  gradients measured as low as 1.8:1 at the big verdict — washed out somewhere in all three themes.
  Daylight is back on the design spec's deeper greens and maps amber/red to the theme's own
  warn/danger colours; Charcoal darkens its neon stops; Aurora keeps the neon and switches to dark
  ink. A new self-test computes the WCAG contrast ratios straight from the stylesheet, so a future
  recolour can't silently regress (verdict ≥3:1, card footer ≥4.5:1, every theme × every state).
- **The stop-symptom halt has an explicit control.** The check-in accepted a stop-the-run symptom
  flag all along, but the only way to raise it was a matching phrase in the free-text note. A quiet
  "I had to stop / chest symptom" checkbox now posts the flag directly — the card flips to red +
  halted and the plan rests — while the note's phrase catch stays on as a backstop. It is quiet on
  purpose: it sits in the same grey as the energy and sleep labels beside it, and only turns the
  warning colour once it is actually ticked, so it isn't raising an alarm on every ordinary day.
- **Tiles fail honestly instead of spinning forever.** A dashboard tile whose fetch failed used to
  sit on "Loading…" for the life of the page. Every loader now ends somewhere: data, an honest empty
  state, or a failure notice that names a dropped connection ("offline — data as of your last sync")
  versus a server error ("unavailable") and offers a working retry. That now includes the zones and
  effort-discipline panels: the effort section used to hide itself outright when its read failed, so a
  server problem looked exactly like a feature you didn't have. The check-in button shows busy and
  couldn't-save states instead of silently doing nothing. The plan-drift view is also no longer loaded
  twice on every page open — the heaviest read on the page was being fetched two times over.
- **A missed nightly sync can't hide.** A container restart across the nightly minute used to skip
  the night without a trace. The scheduler now records each run's outcome, runs a catch-up pass at
  boot when the last success is more than 26 hours old, and reports through `/healthz` — timestamps
  on the private console, booleans only on the public box so a probe can't learn the household's
  routine. Each successful nightly also leaves a dated database snapshot beside the data volume,
  keeping a week.
- **API errors answer as JSON, without internals.** An unhandled route error now returns
  `{ok:false,error}` with status 500 for API paths (and a quiet HTML page for the app itself) —
  never a traceback; the details go to the server log. The run-metrics endpoint's numeric filters
  are bounded and validated like every other route (junk values used to be silently ignored).
- **The drift view's accent no longer depends on the trial palette.** Several drift styles used the
  second accent colour bare, which only the removable polychrome trial block defines in two of the
  three themes — removing that block would have stripped those tints and strokes. They now fall
  back to the main accent, as the design doc promises.

## [0.27.1] - 2026-08-21

### Fixed

- **Away days no longer reach the public view — on any phase, through any payload.** The public plan
  strip covered the four classic phases only, so a re-base week or a multi-race chain segment kept its
  away dates; and the public training log (`/api/log`) spread the same week records whole and was never
  stripped at all — a real away date was served there for the week it mattered. The
  strip now walks every record at any depth and runs on the plan *and* the log; the self-test drives
  both endpoints for real instead of checking a fixture. Found by an external review (Gemini 3.1 Pro),
  widened in verification.
- **"Explain this plan" works again.** Since 2026-07-04 the explainer's grounding summary referenced a
  variable that was no longer bound, so every explanation answered a 502 — invisible to the key-gated
  check on a keyless box. A new key-free self-test builds that summary on chain and caution plans. The
  per-phase summary handed to the explainer now covers multi-race chain segments, not just
  Base/Build/Peak/Taper.
- **A dead Runalyze MCP session is no longer sticky.** The client re-initialised with the stale session
  id still attached, and a non-JSON "not found" answer crashed before the re-initialise could run — so
  one expired session broke every MCP read (hover profiles, LTHR derivation, run read-back, the
  health/sleep sync) until a restart. It now re-initialises cleanly, once, and retries.
- **Several tabs opening at once run one sync, not several.** The page-load sync throttle was
  check-then-act; simultaneous tabs each pulled from Runalyze (harmless to the data, wasteful to the
  API). A lock lets the first one run and tells the others it is in flight; "Sync now" and the nightly
  job queue behind it rather than overlap.
- **The public view's hover profile never fetches or writes.** On a cache miss the tokenless public box
  made a doomed call and — had a token been present — tried to write into its query-only database (a
  server error instead of the intended "not cached" answer). It now serves what is cached or says so.
- **The self-test battery holds other requests while it runs.** The battery swaps module globals for
  its ~40 s (read-only mode, the planner, the key store); any other request served meanwhile could read
  those values. During a battery the private server now answers other requests 503 + `Retry-After`
  (health, `/selftest` and its API stay reachable), refuses a second battery (409), and the nightly
  job waits for it to finish.

## [0.27.0] - 2026-08-21

### Changed

- **The dashboard says what the load ratio is, not what it predicts.** The acute:chronic tile used to
  call its band a "sweet spot" with "injury risk" above it and "detraining" below it. It now reads
  descriptively: ATL ÷ CTL, the band where a steady training week usually sits, taper and down weeks
  below it *by design*, the 1.30 ceiling and 1.25 planning target the engine actually holds — and
  the plain statement that a load ratio is not an injury predictor (the long-run-jump and fast-load
  brakes are the governors aimed at that). The "you:" marker no longer turns red below the band,
  only above it. The week badges' hint says the same thing.
- **The regime badge names the ceiling it quotes.** "riding the full safe ceiling (ACWR ≤ 1.25)"
  is now "riding the full load ceiling (ACWR planning cap 1.25, hard cap 1.30)": 1.25 is the
  settled-week planning target, 1.30 the in-week hard cap; neither is called "safe". On a phone the
  badge wraps instead of overflowing. The manual's regime paragraph matches.
- **The product narrates nobody's history.** The three halt messages ("the exertional symptom that
  preceded 2025…") now describe the symptom, not a person's year; the AI reply is addressed to "the
  runner". Same red light, same halt, same doctor referral.
- **README caught up with 0.26.0.** The front page still described the removed gate — "earned
  progression", "2 well-absorbed weeks banked", the opt-in earned levers, the CTL floor. It now
  describes the regime rule the engine actually runs: body evidence only.
- **Hygiene** (from an external review; none reachable as an attack, all belt-and-braces): the
  private self-test page escapes every scenario field before rendering; the adjustment dialog
  escapes its error text; the secrets store is created owner-only (`0600`) on disk.

## [0.26.3] - 2026-08-20

### Fixed

- **A bad objective can no longer jam the planner.** Adding a race with a malformed date used to be
  saved first and re-planned second — the save stuck, the re-plan crashed, and from then on *every*
  regeneration (the nightly one included) failed silently behind the last good plan until the row
  was removed by hand. Dates and priorities are now checked at the door (a clear error, nothing
  saved), and every change that triggers a re-plan — objectives, availability, adjustments — is
  applied and re-planned as one unit: if the re-plan fails, the change is rolled back and you get
  a readable error instead of a half-applied state.
- **The daily check-in only accepts its own vocabulary.** An unexpected energy or sleep value used
  to be stored as-is and then read as "all signals normal"; it is now rejected.
- **Numeric query parameters answer junk with a clear error** (`days`, `weeks`, `months` on the
  effort, projector, weekly-volume and VO₂max endpoints) instead of a server error page.
- **Self-test honesty.** The card-truth check's frozen-week coverage no longer depends on what
  happens to be in *your* database (it built its fixture on top of the live road anchor, so a fresh
  or synthetic install reported a spurious failure); the browser test-drive's cold-start check was
  updated to the 0.26.0 regime rule; and the release script now refuses to publish unless the whole
  suite is green on the synthetic demo database — the one a new install actually runs.

## [0.26.2] - 2026-08-19

### Fixed

- **Strides no longer stack onto the heavy end of the week.** They used to ride the first easy run
  — Monday, right between Sunday's long run and Tuesday's intervals, three harder days in a row.
  Strides now land on the easy day furthest from every hard session (this week's long, any quality
  day, and last Sunday's long), which on a typical week means Thursday. Same sessions, same weekly
  volume — just spread so your legs get to be fresh for the part that needs them.

## [0.26.1] - 2026-08-19

### Fixed

- **No more 0.0 km runs.** When you front-load a week hard enough that the safety brake rests the
  remaining days entirely, the plan used to keep an empty placeholder — a "0.0 km · long easy run"
  that even counted as a run in the week's header. A rested remainder now simply lays nothing: the
  card shows what you ran and nothing fake ahead. If the brake ever rests a whole *future* week, the
  card now says the build was capped by recent fatigue instead of showing an unexplained blank.
- **The absorption week no longer doubles up.** If the plan was regenerated *during* a down week,
  the engine forgot that the recovery it was asking for was already happening — so it pulled the
  next scheduled down week forward and you got two absorption weeks back to back, while the
  recovery that belonged at the end of the block vanished (and the miscount rippled extra troughs
  into the build). The week you are living now counts exactly as it will once it's over: one
  recovery per cycle, and the road ahead no longer depends on which day of the week you hit
  regenerate.

## [0.26.0] - 2026-08-18

### Changed

- **The plan follows your measured form — the obedience gate is gone.** Until now the engine
  inferred your health from how closely each week matched its prescription: a "banked streak" of
  plan-adherent weeks earned the normal (assertive) build, and any single miss — even a travel week
  where you ran *more* kilometres than planned, in fewer runs, and absorbed them cleanly — zeroed
  that streak and collapsed the whole road into the post-illness rebuild. That machinery is
  removed: the streak, the regime's "well-absorbed weeks banked" requirement, the re-base's earned
  early exit, the opt-in earned faster build, and the opt-in earned 6th run. The conservative
  posture now engages on **body evidence only** — a medical hold, or a stop-symptom check-in within
  the last 56 days — and lifts by itself once that window is clean. How you lived last week is
  never a reason to shrink the next one; your measured fitness, and the safety ceilings, already
  carry that. (The two opt-in toggles disappear from Settings; nothing you need to do.)
- **Coming back after a long healthy break starts from a small dose, not from zero.** With the
  gate gone, a runner whose recent weeks are empty (travel, a lapsed season) is planned from a
  conservative restart dose — the same first week the post-illness block prescribes — and the plan
  ramps from there by measurement. Previously this case could only produce the gated rebuild;
  internally the ungated path would have degenerated to near-zero weeks, which is also fixed.

## [0.25.3] - 2026-08-15

### Fixed

- **Completed weeks now say what you actually ran.** A week already lived kept showing the total
  that had been *prescribed* for it, carried forward unchanged from the plan it was first laid in —
  so a card could keep claiming "48.6 km · 5 runs" over a week you really ran as 42.4 km, for the
  life of the block. Every completed week's header is now recomputed from your training log on each
  replan; the sessions listed under it remain what the plan asked of you that week, so the per-day
  plan-vs-reality lines are unchanged. Weeks completed before this release correct themselves on
  the first replan after deploying.
- **Adherence is judged against the plan again, not against yourself.** The header fix in 0.25.2
  had a quiet side effect: the run count the engine checks your completed weeks against — the
  evidence behind the assertive posture, the earned volume lift and the sixth-run advance — was
  read from the same field the header rewrite had just set to your *actual* count, so the "did you
  run within one of the planned runs" test always passed. The prescribed count is now stored
  separately, survives every header rewrite, and the earn-back evidence reads that bar. Your
  current posture is unchanged by this — it only bites, honestly, on weeks lived from now on.

## [0.25.2] - 2026-08-07

### Fixed

- **Out-running the plan no longer shrinks the current week's headline.** The week in progress
  summed its *prescribed* elapsed days plus the remaining prescription — so every kilometre you ran
  beyond a day's prescription made the week's total *smaller*, and a well-run week could read below
  a recovery week (the reported case: the card said 35.8 km while the week was really 29.1 already
  run + 11.3 still ahead = 40.4, above the 38.3 km down week it appeared to sit under). The current
  week's header now counts days you have already run at their real logged distance and days ahead at
  their prescription, shows the split ("29.1 run + 11.3 ahead"), and a same-evening replan never
  counts today twice — the day's prescription is superseded by what you actually ran. Future weeks,
  down weeks and completed weeks are untouched, and the reduced *remaining* prescription after an
  over-run is deliberate and unchanged: volume already run is never re-prescribed.

## [0.25.1] - 2026-08-07

### Fixed

- **The taper's "race-pace touch" now actually runs at race pace.** Every taper carried a short
  sharpening session labelled a race-pace touch — hardcoded to the threshold zone. For a 10k that's
  about right; for a marathon it prescribed the plan's *only* threshold-zone repetitions, at a pace
  no session in the previous seventeen weeks had visited, two weeks before the race — at nearly
  twice the per-kilometre tissue load of the pace the label promised. A marathon taper now sharpens
  at marathon pace, the pace every marathon-pace long run has rehearsed since the build began; other
  race distances keep the threshold touch they always had, and in a multi-race plan each taper
  sharpens at its own race's pace. New `det/taper-touch` checks the published plan, not just the
  template, and fails if a taper ever debuts a pace the build never rehearsed.

- **A week's card now states what its own listing shows.** The header above each week — "35.8 km ·
  5 runs" — printed the *template's* run count, while the sessions listed under it came from the
  governed week, which can shed a day (a crushed budget), add one (spreading a capped long run), or
  drop the dead days after a race. Three weeks of the current plan were wrong, in both directions:
  a four-run week titled "5 runs", a six-run week titled "5 runs", and the race week claiming five
  over the single remaining shakeout. The count, the distance and the load in every week's header
  are now recomputed from exactly the sessions displayed beneath them, on every path that publishes
  a week — and a rest note (an eased-away day, an optional-rest card) is never counted as a run.
  Verified on the rendered page, not just the data. New `det/card-truth` sweeps every published
  week in both regimes and fails if any header disagrees with its own listing.

- **The plan now runs on your clock, not the server's.** Only one part of the app knew what time zone
  you live in: the nightly sync, which fires at your wall-clock hour. Everything else simply asked the
  machine what day it was — and the containers run on UTC. So from midnight until the server's
  midnight (two hours in summer, one in winter), the whole engine believed it was still yesterday,
  while your runs arrived carrying their real local date. Two clocks in one database. In practice it
  meant a plan generated late at night was built for the wrong day, and the daily fitness snapshot was
  filed under the previous one — 22 of 53 snapshots and 3 of 79 plans in this database were stamped
  that way. The timezone setting now governs the whole engine's idea of "today", takes effect the
  moment you save it, and — because leaving it unset means "use whatever clock this machine is on" —
  changes nothing for anyone who never set it. Its label and help text were misleading about all of
  this and have been rewritten.
- **A timezone that cannot be loaded is now refused out loud instead of quietly becoming UTC.** The
  obvious version of the fix above — setting the container's timezone in the compose file — would have
  done *nothing at all* on this deployment, and would have done nothing silently: the slim Python image
  ships without the system timezone database, and when the C library is given a zone name it cannot
  load it does not fail, it reads the name as an abbreviation with a zero offset and carries on in UTC.
  The app now supplies the database itself and then checks that the clock actually moved, saying so in
  the log if it didn't. New `det/one-clock`.

## [0.25.0] - 2026-08-07

### Changed

- **Your easy days are no longer all the same distance.** Every easy run in every planned week came
  out at exactly the same number — four 7.6 km days in a row, then the long run. No training plan in
  any book looks like that, and it isn't what you have ever actually run. Your own weeks were the
  evidence: across 161 of them, easy days grade steeply from longest to shortest (the longest is
  about 1.4× the week's average easy day, the shortest about 0.7×), and that grading explains more
  than half of all the variation between your easy runs. What it does *not* depend on is the day of
  the week — there is no Monday effect, no Friday effect, nothing. So the sizes come from your
  history, and where each one lands comes from how recovery works: the longest easy run goes on the
  day furthest from any long run, and the days on either side of a long run — the recovery day after
  the last one, the freshness day before the next — get the short ones. A week that used to read
  7.6 / quality / 7.6 / 7.6 / 7.6 / 12.0 long now reads 7.5 / quality / 9.6 / 8.5 / 6.4 / 12.3 long.
  Nothing about the safety limits was loosened to do this; in fact the highest fatigue ratio anywhere
  in the plan comes down slightly, because putting the bigger easy day further from your heaviest day
  costs less under the same brake.

### Fixed

- **Your week can no longer outgrow your longest run.** The plan was tracking two things that should
  move together and didn't: how much you run in a week, which grew whenever recent fatigue allowed
  it, and how far your longest run may go, which grows by at most 10% over the previous few weeks.
  Nothing connected them. So the week rode its fatigue ceiling and the long run, held to its own
  slower ladder, shrank to whatever share was left — 21–24% of the week, under the 25–30% that
  distance plans actually prescribe and that this plan's own skeleton was already asking for. That
  isn't a plan with a long run in it, it's a pile of distance with a longest day. Worse, because the
  ceiling is a ratio against your *recent* load, the week became a readout of the last fortnight
  rather than a prescription — two rest days could triple it. The week is now capped at whatever
  distance the long run can properly anchor, and the long run is raised to its own ceiling instead of
  being left below it, so the two advance together. Your long run now holds 25–30% of the week all
  the way through the block.
- **A weekly safety check was being verified against a slightly wrong number.** The value the volume
  governor decides on was calculated privately, so everything that reported on it afterwards had to
  reconstruct it — and the reconstruction used your fitness at the *end* of a week where the engine
  uses the week's *average*. On a steeply-rising week those differ by about 5%, enough to report a
  ceiling breach that had not happened. The governor now publishes the number it actually used.

## [0.24.1] - 2026-08-04

### Fixed

- **A good run no longer shrinks the week that follows it.** The plan tracks a second kind of load
  besides effort — the mechanical pounding a run puts through your legs, which depends on how fast you
  were going. Two things about how that was measured could make it move on its own. First, pace was
  sorted into bands with hard edges, so two runs a single second per kilometre apart could be scored
  40% apart. Second, past runs were re-scored against your *current* fitness estimate, and those band
  edges shift as that estimate changes — so a run from a fortnight ago could quietly change what it
  had cost you. Together they bit: a good evening run nudged the fitness reading up, an old run
  slipped across a band edge, and the following week was cut by 7 km. Pace is now weighted on a smooth
  curve with no edges to fall off, and every past run is scored against the fitness you had on the day
  you ran it, so finished training keeps the value it earned. The scale itself is unchanged — the same
  paces mean the same thing they always did. One side effect worth knowing: a briskly-run easy day
  used to be recorded as costing no more than a gentle jog, and now it counts for what it was.

## [0.24.0] - 2026-08-03

### Fixed

- **A long run is now actually longer than your easy days.** Your week is built from a total distance
  and a ceiling on how far any single run may jump from recent weeks. When those two meet — the week
  is big enough that dividing it across your running days reaches the ceiling — every day came out the
  same length, and the plan called one of them a long run anyway. Measured on a real week: four easy
  days of 11.3 km and a "long run" of 11.4 km. The long run's benefit comes from its duration, from
  the things that only happen once a run goes on long enough, and five identical runs cannot supply
  it. The plan now makes the long run bigger where it is free to, holds the easy days below it where
  the jump ceiling has the long run pinned (spreading the freed distance over an extra easy day, so
  the weekly total is unchanged), and where the week is too small for either — a three-run week, for
  instance — it stops calling any of them a long run and says so. Weekly distance is unchanged and
  the hardest week of the block now peaks lower than before, because the same distance spread over
  more days is a smaller single-day spike.

### Added

- **Your plan now tells you when it was built from an older reading of your shape.** A plan is seeded
  from where your fitness and fatigue stood at the end of the previous day, so the plan waiting for
  you in the morning was built for yesterday. When that starting point has since moved, the plan says
  so and shows both readings, so you can decide whether to regenerate. It reports only that the
  starting point changed — never that your sessions would change, which is a different and much more
  expensive question. If the new reading is the same as the old one, nothing is shown.

## [0.23.3] - 2026-07-31

### Fixed

- **The app's log messages now actually appear in `docker logs`.** Container output is a pipe rather
  than a terminal, so Python held everything the app printed in an 8 KB buffer that a long-running
  server never fills — while the web server's own logging, which goes to a different stream, kept
  scrolling past. The result was a log that looked healthy with the application's voice missing from
  it entirely, including the warning that explains why an unreadable nightly-sync time fell back to
  the default. The container now runs unbuffered. No training or plan behaviour changes.

## [0.23.2] - 2026-07-31

### Fixed

- **A plan generated on a day you had already trained no longer believes you were rested.** The
  projection that decides how much a week may carry rolls forward from today, so the state it starts
  from has to be the state at the end of *yesterday*. It was instead taking the fitness/fatigue
  reading dated today — and that reading has already moved through today with whatever training had
  been recorded when it was taken. Taken before an evening run it reads the day as a rest day, so the
  day was counted twice: once as rest, once as the session. Fatigue decays about a quarter in a day,
  so the plan was consistently handed a figure well below the truth, saw headroom that was not there,
  and offered a week roughly twice the size it should have. Taken after the run it failed the other
  way round, the day's load being counted once by the reading and once again by the projection.
  Thirteen of the forty-one plans generated in July were built this way, every one of them in the
  direction that inflates the week — and because there is one reading per day, a later sync
  overwrites the row, so nothing afterwards showed what a plan had actually been built from. The
  state is now taken from the last settled day and rolled forward over the training actually
  recorded, which makes it independent of when any reading happened to be taken. A day whose reading
  never arrived is bridged by the same arithmetic rather than by treating an older figure as current,
  and a saved plan now records which day it was seeded from and how many days it had to bridge.
- **A day you have already trained now reaches the projection as what you ran, not as what was
  prescribed.** With the seed correctly stopping at the end of yesterday, today's load arrived only
  through today's prescription — and once you have run, that prescription is beside the point. On the
  evening this was found the plan had prescribed rest and the run had been an hour of work, so the
  projected in-week peak read 1.13 against a measured 1.47, and the rest of the week was bounded
  against load already spent. Today's recorded load is now applied as a floor: it can only ever raise
  the projected figure, so it can only ever make the plan more cautious, and it goes into the
  projection alone — what the plan asks of you is unchanged. Across the four days checked, the
  projected peak now lands within 0.03 of the independently measured value, where before it sat up to
  0.37 below it.
- **The note that says a week's volume is already run now quotes the figure the decision was actually
  made on.** It was printing the skeleton weekly target while the engine had decided against a larger
  one — "32km of 22km planned", where the number that closed the week was 25.6. Both readings were
  honest arithmetic; only one of them was the one being used.
- The week currently underway is the part that changes: on the day this was found, that week's
  allowance went from 51 km to 25 km, closer to the 32 km already run. Season totals barely move
  (1138 → 1070 km, projected finish 4:17:06 → 4:18:09), which is the expected shape — the error was
  concentrated in the week being laid, and later blocks re-derive from their own carried state. Plans
  built from a reading that was already settled are unaffected.

## [0.23.1] - 2026-07-29

### Fixed

- **The note offering the earned sixth weekly run no longer describes itself as dormant when it
  isn't.** It ended in a fixed sentence promising the plan would stay at five runs until weekly
  volume grew enough for a sixth run to be real training rather than a token one. That was true when
  the sentence was written, at low volume — and it was never revisited, so as volume grew the offer
  went on calling itself quiet while accepting it would in fact have added a day to most building
  weeks. It could also sit directly above a week already showing six runs, because when the long-run
  ceiling holds a long run back the surplus distance is spread onto an extra easy day — a different
  mechanism entirely, with nothing to do with the opt-in. The offer is now measured rather than
  asserted: the engine counts the weeks whose run count would genuinely change and says how many,
  and it excludes weeks that already carry a sixth day for that other reason, since accepting would
  not move those. A plan saved before this release carries no such count, so the sentence is left out
  rather than guessed. No plan numbers change.

## [0.23.0] - 2026-07-29

The limit on how much a plan may build moves off the heart-rate axis and onto the biomechanical one.
Plans now grow to a genuine marathon volume instead of sitting flat for months.

### Changed

- **The ceiling that decides how much a week may carry is no longer an acute-to-chronic heart-rate
  ratio.** That ratio was read on the last day of the week — which is the long-run day, the biggest
  session there is. At an identical, perfectly flat weekly load the reading moves from 0.97 to 1.51
  depending only on *where in the week the long run sits*. Judged against a fixed ceiling, most of the
  room for progression was being spent on the calendar rather than on training, and the long run —
  being the spike that inflated the reading — was effectively taxing itself. Two limits replace it,
  both measuring tissue load rather than cardiac load: no single session may jump beyond a set step
  over the largest session of the previous four weeks, and the week's total tissue load may not jump
  beyond its own ceiling. This follows the evidence rather than convention: in a study of over five
  thousand runners, sharp increases in the **longest single run** predicted injury, while increases in
  weekly mileage did not. The heart-rate reading is still computed, still shown, and is still one of
  the limits — it is simply no longer the one deciding the volume, and it is now read in a way that
  does not depend on which weekday you look at.
- **The long run is now capped at 30% of weekly volume, in line with mainstream marathon coaching.**
  It was previously allowed up to half the week. Counter-intuitively this makes plans *larger*, not
  smaller: the long run is the single biggest session, so capping its share frees tissue-load headroom
  that flows into more easy running. It also brings the long run inside the usual duration guidance of
  two-and-a-half to three hours for most runners. It still grows every week, on a ladder that never
  adds more than ten percent over the longest run of the past four weeks.
- **The mid-week safety check that could strip a week of its quality session now triggers only at the
  problem it was written for.** It was set at a threshold ordinary training weeks routinely reach, so
  it had quietly become a volume limiter rather than the rare rescue it was designed to be.

### Fixed

- **A plan regenerated mid-week no longer shrinks the long run along with everything else.** The
  remaining days of a partly-run week are governed as a share of what is left, and the long run was
  taking that reduction like any other session. Because the long-run ladder measures itself against
  recent long runs, one shortened long run then held the ceiling down for weeks afterwards. The long
  run is now sized against the week as a whole; when the remainder cannot carry everything, an easy
  day is dropped instead.
- **The long-run progression ceiling now covers every run in the week, not only the one labelled
  "long".** With the long run taking a smaller share of a larger week, an ordinary easy day could be
  laid longer than the long run itself — and therefore above the ceiling — while the session named
  "long" sat safely under it. The ceiling applies to the longest run of the week whatever it is
  called, which is what it always meant.

## [0.22.1] - 2026-07-28

The notice added in 0.22.0 could not appear immediately after regenerating — the one moment it most
needed to be trustworthy.

### Fixed

- **The "built by an earlier version" notice was suppressed for the whole render that follows a
  regeneration.** The check compares the version stamped into the plan against the version of the
  engine serving it. That second value was attached only to the request the page makes when it
  loads — but after you regenerate, the page renders the response of the regeneration itself and
  never makes that request. With nothing to compare against, the comparison quietly resolved to
  "current", so the notice could not be shown no matter what was true. It looked correct, because
  regenerating a plan does normally make it current; it would have stayed just as silent on a plan
  that had failed to save. A warning that cannot be wrong cannot be relied on to be right. Both
  responses now carry the serving version, from a single shared place rather than one path each, so
  the notice is driven by real data whichever way the plan reached the screen. As before, the
  serving version is attached only on the way out and is never written into the saved plan.

## [0.22.0] - 2026-07-28

A saved plan now tells you when it was built by an older version of the app.

### Added

- **Plans carry the version of the engine that generated them, and say so when that is no longer
  the version running.** A plan is a versioned artifact: updating the application deliberately does
  not rebuild the plans already saved, because the weeks you have lived are frozen on purpose. The
  consequence is that immediately after an upgrade the app serves a plan produced by code that is no
  longer installed — same numbers as yesterday, computed by an engine that has since been corrected.
  There was a warning for this, but it worked by recognising the *shape* of one specific older
  payload, so it could only ever catch the breakage it had been written for; a plan that was merely
  one release behind slipped past it silently, showing stale figures with nothing to indicate it.
  Every generated plan is now stamped with its engine's version, the served plan is compared against
  the version actually running, and any difference raises a banner above the plan explaining that
  the weeks below — and everything derived from them — are exactly as they were generated, with a
  prompt to regenerate. The comparison is a plain match rather than an ordering, so a rollback is
  reported just as clearly as an upgrade, and a plan predating the stamp entirely is correctly
  treated as out of date. An internal check ties the stamp to this changelog, so a release cannot be
  published with the version marker left behind.

## [0.21.1] - 2026-07-28

Regenerating a plan part-way through the week no longer quietly shrinks the rest of the season.

### Fixed

- **A plan regenerated mid-week no longer collapses the week in progress — or every week after it.**
  The engine runs one of two regimes: a cautious one, which treats the planned week's volume as
  written, and an assertive one — unlocked by consistently well-absorbed training — which
  deliberately rides the safe ceiling above that figure. The logic handling the week you are
  currently in predates the assertive regime and only ever knew the cautious rule, so it re-laid the
  remainder of that week at the cautious volume no matter which regime the plan was actually
  running. On an assertive plan that is roughly a third less training than intended. The damage did
  not stop at the one week: the plan projects fitness forward from wherever it currently stands, and
  later volume tracks that projection, so the shortfall propagated down the entire road to race day,
  fading only slowly and never fully recovering. Because a plan regenerates daily, most
  regenerations land mid-week — this was the ordinary case, not an edge case. The week in progress
  now follows the intent its own regime holds. **Safety is unchanged:** the acute-load ceiling still
  bounds the remaining days exactly as it did before, and on a constrained week it still binds; only
  the target being bounded was wrong, and it is the target that changed.
- **The days already run this week are shown at the volume that was actually prescribed for them.**
  The same mistaken figure drove the display of the elapsed part of the week, so a day you had
  already completed could be listed at a shorter distance than the plan had asked for. The two
  related checks that decide whether a week's training is "already covered" — and therefore whether
  the remaining days become optional — read the corrected intent too, so an assertive week is no
  longer declared complete a third of the way short.

## [0.21.0] - 2026-07-28

The prediction gets stricter about what counts as evidence — and the plan stops forgetting what you
have already banked.

### Changed

- **A short recording can no longer move your speed estimate.** The model's speed axis is built from
  per-run aerobic estimates, which are inferred from pace against heart rate and therefore need a
  steady effort to mean anything. A stride set recorded separately from the run it belongs to has no
  steady effort in it at all — bursts threaded through jogged floats, with heart rate lagging the
  bursts it is being divided by — and such fragments were scattering estimates in both directions.
  Two changes: the unit is now the **session**, not the recording, so a deliberately split training
  day counts once instead of twice and cannot leave your current speed sitting on whichever piece
  you saved last; and a recording below a minimum distance cannot inform the axis at all. That floor
  is calibrated, not chosen — measured against the smoothed history, short recordings scatter three
  to four times as widely as full ones — and it is locked below the shortest race distance the model
  predicts, so a 5 km race always still counts as evidence for the axis it is evidence for.
- **The prediction band's width is measured rather than assumed.** The band widened by a fixed
  amount per remaining week, which is not how forecast error actually behaves: fitness is
  mean-reverting, so uncertainty rises quickly at first and then slowly. Measured against your own
  history — how far the projection has really strayed over two, four, eight, nineteen weeks — the
  old rule was more than twice too narrow for a race a month out and half again too wide for one far
  off. It is now one measured term in place of the two estimated ones it replaces (which were also
  double-counting each other), with the shape shared across runners and the size learned from each
  runner's own record, falling back to the population value until there is enough history to earn
  its own.
- **A speed estimate from before a layoff no longer presents itself as today's shape.** The
  projection compares where you are now against where the plan takes you, but "now" was simply the
  most recent estimate on file, however old. If your most recent measured running is older than
  about eight weeks — the same window past which the long-run readiness measure already goes neutral
  — the comparison is withheld and labelled, rather than quoting a pre-layoff number as current. It
  is not decayed by some assumed rate of detraining: there is no honest way to calibrate that yet,
  and silence beats a guess. Running re-anchors it automatically. The race-day projection itself is
  unaffected.

### Fixed

- **Evidence of your completed weeks survives the plan moving on.** A plan describes the road ahead,
  so when a training block ends the road re-anchors to the new block and weeks already lived are no
  longer part of it. That is deliberate — but the gates that reward consistent training were reading
  their evidence from that document, so on the day a block ended they found no completed weeks at
  all. A build that had earned its assertive posture dropped back to caution, projected race-day
  fitness fell sharply with it, and because the next plan saved was equally short, the reset
  sustained itself. Those gates now read the whole plan history instead. Where the plan still covers
  its own past — the ordinary case — the result is unchanged.
- **The effort monitor no longer loses prescriptions when the plan re-anchors.** The same fault, in
  the place a runner would actually notice it: prescribed quality sessions are matched to your runs
  and excluded from the easy-day score, and with those prescriptions invisible your hardest sessions
  were re-graded against the easy bar and marked too hard. It also reads the full history now.
- **A run on a rest day is graded like a run, not like a rest day.** Taking a run on a day the plan
  left free had it matched to the rest day itself and marked "too easy" — a verdict that cannot be
  true of a run nobody asked for — and it consumed the match, distorting the nearby
  moved-session matching. A rest day is no longer treated as a session to execute; such a run is
  simply held to the easy bar, the standard it would have been given had it been prescribed.

### Documentation

- The manual and README described the finish projection as it looked before it became a range, and
  never mentioned the prediction band at all. Both are rewritten for what the engine actually does:
  the range as the headline, what the block buys you, how the band's width is composed, when the
  engine declines to answer and why, and the ledger that scores every prediction against the result.
  The manual also states what a plan document is — the road ahead, not a record of the past — and
  where the past is kept instead.

## [0.20.1] - 2026-07-27

A prediction saved by an older engine now says so, instead of quietly presenting itself as current.

### Fixed

- **A plan saved by an earlier version of the engine is labelled as such.** Plans are versioned
  artifacts: the app serves the most recently *generated* plan, and updating the application does
  not regenerate it (past weeks are frozen on purpose). That meant a plan generated before an engine
  upgrade rendered through the new interface with its old numbers and no indication that a newer
  model was available — the finish projection in particular could show a pre-upgrade estimate as
  though it were current. Such a plan now carries an explicit *"saved by an earlier engine —
  regenerate to re-read"* marker, and the same note in the projection's hover. Regenerating the plan
  re-reads it on the current model.
- **The projection never claims a trend its own numbers do not show.** The finish strip's hover
  offered a "with more runway" comparison unconditionally. On a plan whose projection was flat, that
  printed a sequence of identical times directly after stating that more runway means a faster race —
  asserting a trend and then contradicting it in the same tooltip. The comparison is now suppressed
  whenever the projected times do not actually differ, whatever the reason.

## [0.20.0] - 2026-07-27

The finish projection stops answering a question nobody asked, and starts answering the one every
runner actually has: **what does this block buy me?**

### Changed

- **The finish strip now shows what the training buys.** It used to read `now → +4w → +8w`, which
  looks like a timeline but is not one: those points price a **later race date** at the same
  training, a hypothetical nobody asked about. Worse, before the projection was un-frozen in 0.19.0
  the three values were often identical, so the strip read as *"eight more weeks of training changes
  nothing."* It now runs the same model at **today's measured state** — current speed axis, current
  fitness, and the long-run ladder actually behind you — against the race-day projection, and names
  the difference: *off today's shape 5:32 → by race day 4:45, the build buys 47 min.* The runway
  points move into the hover, labelled as the what-if they always were.

  The framing stays honest when the answer is not flattering: a runway that is mostly taper reads
  *"this runway costs 6 min"*, and one that projects nowhere reads *"holds today's shape"* rather
  than implying movement. The 80% range remains the headline; this pair is the trend detail.

### Fixed

- **A correction learned at one race distance no longer leaks into another.** The engine learns a
  personal correction from your own race results. Because its speed axis and the classic reference
  construction diverge by an amount that **grows with race duration** — roughly 1% at 5k rising to
  4.4% at the marathon — a correction learned on marathons carried marathon-specific scale error
  into a 10k prediction, and vice versa. That deviation was previously assumed to be a constant,
  which would have cancelled harmlessly; it is not. The correction is now carried across distances
  explicitly, stripping the tilt of the distances it was learned on and applying the target
  distance's own. This is **exactly neutral** when your races and your objective are the same
  distance, and when you have no raced history at all, so the established path and the cold-start
  prior are both unchanged — only genuine cross-distance transfer moves.
- **Race-day noise is no longer overstated for a mixed-distance history.** The band's race-noise
  term pooled results across distances without accounting for the same tilt, booking the gap between
  distances as day-to-day variability the runner never produced. It is now measured on a common
  scale. A single-distance history is unaffected.

## [0.19.0] - 2026-07-27

The plan and the prediction become one object. The engine now says what it thinks you will run —
as a **range**, projected along the build it is actually laying for you — and then keeps score of
its own bets when the race settles.

### Added

- **The predicted finish is a model, not a frozen number (§FT1).** The old estimate stopped
  responding to fitness above a certain threshold: three different projected fitness levels could
  return the same finish time to the second, which made the whole "more training → faster race"
  story unfalsifiable. The prediction now reads three state axes — projected race-day fitness, the
  projected long-run ladder, and the speed axis — and is **strictly monotone in every one of them**,
  which is asserted by a permanent invariant test. A more demanding (but still safely governed)
  plan can therefore never predict a slower race, by construction. Below the healthy-finish floor
  the old conservative fade is preserved exactly, so the "too soon" / "earn it" verdicts are
  unchanged.
- **A per-runner correction, learned from your own races (§FT1).** Every race in your history is
  re-predicted from the state you actually carried into it, and the ratio between prediction and
  clock becomes a shrunk personal correction — one noisy race nudges it, a consistent history moves
  it. With no raced history the population model stands unchanged.
- **The speed axis projects along the build (§FT2).** Your aerobic capacity is no longer frozen at
  today's value for a race months away. It is projected week by week through the load the plan
  actually prescribes, saturating toward your own demonstrated ceiling, with the response rate
  shrunk from the population prior toward your measured one. Every regeneration re-bases the
  projection on the **measured** value, so a fast or slow responder can never drift away from
  reality.
- **The band IS the prediction (§FT3).** The headline is now an 80% range; the median is reported
  as the trend detail. The width is composed from real sources — race-day noise, how well your
  corpus has calibrated the model, how much of the projected gain is still unrealized, and how much
  runway remains — so it narrows honestly as runs land, races bank, and the race approaches. A cold
  start gets a wide band by design.
- **The prediction ledger — the product watches itself (§FT4).** Every saved plan already carried a
  prediction, so the plan table was a retroactive ledger of every bet the engine has made. A new
  chart draws it: predicted finish per regeneration, with the band envelope. When a race resolves,
  the last pre-race prediction is scored against the clock — inside the band or not, plus a **proper**
  log score, so an over-tight band is punished exactly as hard as an over-wide one and the band
  cannot cheat its way to looking calibrated. Races that settled before scoring existed are
  backfilled.
- **Cold start: an age, one race effort, and an objective (§FT5).** A brand-new database with no
  fitness snapshot can now generate a real plan. A recent race-distance effort seeds the speed axis
  by inversion, whatever training history exists reconstructs the fitness seeds (truth beats a magic
  constant), and an optional age in Settings anchors a heart-rate prior until real data lands. The
  plan starts in its conservative mode by construction and every seed is replaced by measurement as
  runs arrive.

### Fixed

- **Today's high can no longer freeze tomorrow's projection.** A runner sitting at their own
  all-time best had their ceiling pinned to that value, which made the projected gain exactly zero —
  the same frozen-curve failure 0.17.0 fixed on the fitness axis, re-grown on the speed axis. The
  ceiling now always keeps headroom above the current value, at every history size.
- **The predicted finish moved in 42-second steps.** The model read race pace off the *displayed*
  pace grid, which is rounded to a whole second per kilometre — over a marathon that quantized every
  prediction into 42-second treads. Fitness could improve measurably with the predicted time frozen,
  and then jump a whole tread at once, so the ledger chart drew a staircase and the band's
  state-uncertainty term wobbled by ±15% depending on where the rounding fell. The model now
  evaluates the same pace fraction continuously. Displayed training zones are untouched.
- **Duplicate and manually-ignored activities reached the speed model.** Every other consumer of
  your history drops them; the new speed series did not, so a duplicated row could drag the current
  value, the ceiling and the response fit.
- **The cold-start seed could come from a personal best years ago.** It now requires a race effort
  from the last twelve months, so the seeded pace and the reconstructed fitness describe the same
  runner rather than contradicting each other.
- **Race scoring used moving time while calibration used elapsed time.** A race result is
  gun-to-mat. The two had drifted apart, so every stopped second read back as prediction error.
  There is now one definition, shared by the corpus that calibrates the model and the ledger that
  scores it — including races recorded in several chunks.
- **Projected load double-counted the current week.** The remaining weekly load is now summed from
  the sessions still ahead, so days already run this week are not counted twice on top of the
  measured value they already produced, and race day itself is no longer counted as training for
  the race. The response calibration is also restricted to runs, matching the run-only load the
  plan prescribes.
- **The public read-only view could serve cold-start seeds** (an age and a heart-rate prior). It
  no longer does, matching the existing privacy posture for personal signals.
- **The prediction ledger's band tooltip** showed a dangling value with no label on hover.

## [0.18.0] - 2026-07-24

The recovery cadence learns coordination — the tissue limiter re-phases the plan's own down weeks
instead of stacking extra ones, and the projected build finally climbs to the race.

### Fixed
- **Re-phase, don't stack (§PRO11).** The progression floor (0.17.0) made every building week ride
  near the ceiling *by design* — which meant the tissue limiter's consecutive-week counter now
  tripped on schedule, forcing a deload every fourth week forever and stripping that week's quality.
  Those forced troughs landed one or two weeks away from the plan shape's own 3:1 down weeks, so a
  meso spent two recoveries where it designed one, and projected fitness plateaued at roughly the
  runner's current shape months before the race. Now, when the counter trips and the shape already
  schedules a down week later in the block, that down week is **pulled forward** (the two weeks
  swap): one trough per cycle at the tissue-safe cadence, and the displaced building week keeps its
  quality sessions. The limiter's guarantee is unchanged — never more than three consecutive
  near-ceiling weeks — and a shape with no down week ahead still gets the original forced deload.
  The early-arriving down week is labelled in the plan (`deload_pulled`). On the reference data this
  moves projected race-day fitness from a flat line at current shape to a monotone build peaking
  just before the taper.

## [0.17.0] - 2026-07-23

The projection learns progressive overload — a plan that used to draw a flat fitness line to race
day now compounds, honestly labelled. And the run read-back learns that workouts end.

### Added
- **Progressive-overload floor on the assertive ceiling (§PRO10).** The weekly-load ceiling is a
  ratio of chronic load, so riding it has a fixed point: the projection allowed ~maintenance, the
  down weeks handed the small surplus back, and a months-long build could project **zero fitness
  gain by race day**. Now a building week's allowance can't be soft-clipped below a modest
  progression (+6%/wk) over the last realised non-down week. The acute guardrails are untouched
  and always win: the in-week peak ACWR hard cap, the chronic-ramp cap, forced deloads, the
  long-run step cap and the biomechanical ceiling all still bound every week — and the floor
  suspends itself entirely whenever the shape-response brake measures that absorption is lagging.
  Weeks the floor lifted are labelled in the plan (`prog_ridden`): the drawn trajectory assumes
  continued clean absorption and re-anchors on your actuals at every regeneration.
- **Build phase intents grow again.** The Build weekly intent ramp rises 2%→4.5% (matching Base):
  "lightly growing" plus 3:1 down weeks nearly cancelled, so even the conservative regime asked
  for a flat Build.

### Fixed
- **Read-back (§RD v8): the workout ends.** A trailing work-zone block whose rest gap dwarfs the
  session's own observed rest scale (≥8 min AND ≥3× the longest real inter-rep rest) is cooldown
  drift — terrain letting go on the way home — not another rep. A 2-rep VO₂ session no longer
  gains a phantom third rep from a cool-down that quickens when the uphill flattens; everything
  after the last real rep reads as cooldown, so per-rep effort grading stays honest. Long-recovery
  formats (uniform 5-min jogs) keep their genuine final reps; a plain 2-rep session can never
  lose its second rep. Cached reads re-heal lazily on first view.

## [0.16.0] - 2026-07-20

Record a session in deliberate parts — easy body saved, fresh recording for the strides — and the
engine reads it back as ONE session. And it now counts your strides even when you barely rest
between them.

### Added
- **Split sessions — "1+1" (§SJ).** Some runners deliberately record a mixed session as separate
  parts so no platform averages the strides into the easy run's numbers. The engine now agrees
  with them: same-day recordings a save-and-restart apart (≤30 min, never overlapping) are read as
  **one logical session**, derived at view time — activity rows are never merged or edited.
  Everything downstream understands the group: the **effort monitor** judges your easy discipline
  on the session's aerobic body only (the strides part's HR stays out of your easy score — the
  whole point of splitting) and matches ONE prescription per session, so a second same-day
  recording can no longer "reschedule" itself onto a neighbouring day's quality session; the
  **read-back line** joins the parts ("47min @6:42/km · then 6min · 10× strides @4:24/km") with a
  `1+1` chip on the tile and a merged day entry in `/runs`; the **long-run progression cap**
  anchors on the whole outing's distance (a 3-minute save-and-restart doesn't reset a continuous
  run); a race recorded in chunks (watch save mid-race) can still resolve; and a short part that
  was unreadable alone (under 10 min / 2 km) is read on the strength of the session it belongs
  to. Public mirror posture unchanged: pace-only, every HR field withheld server-side.
- **Strides execution read (§SQ).** When a session carries strides, the read-back gains an
  execution line: **count vs what was prescribed** ("10× strides · count on target (8–12)"),
  strides-only pace, and — private box only — the HR *response*: per-rep peaks and whether your
  HR floor recovers between reps (a creeping floor reads as "rest ran short"). HR is reported as
  response, never as an effort verdict — a 15–20 s stride's cardiac peak lands in the recovery,
  so pace remains the honest effort signal.

### Fixed
- **Short stride-dense recordings read as Strides, not "tempo" (§RD).** A 6-minute strides
  recording with jog recovery used to blend into a "sustained effort, no easy bracket" verdict:
  the wall-to-wall-hard branch answered before the stride count was consulted, and the old rule
  required a walking-pace base calibrated on full-length sessions. Now ≥4 counted strides inside
  ~12 minutes is a strides session regardless of recovery pace, and the strides check runs first.
- **Strides counted through frame aliasing (cadence-burst pass, §RD).** With rests shorter than
  the 15 s analysis grid, every other stride can vanish into a pace blend (a real 10-stride set
  counted 6). The raw ~1 Hz **cadence** stream keeps one distinct high-cadence run per stride —
  legs stop turning over instantly even when frame-blended pace doesn't — so cadence graduates
  from countersign to counting channel: bursts ≥10% over a recovery-quartile cadence floor,
  5–60 s wide, corroborated against pace (half the stride bar, so a flat-pace cadence flutter
  never counts), deduped against pace-counted strides. Pace stays primary. Each stride rep's own
  pace now also quotes the fastest frame it touches, never a rest-blend frame.

## [0.15.0] - 2026-07-19

The plan learns to lay itself around your life: declare the days you can't run and the week
re-shapes — honestly.

### Added
- **Away days (§AV).** Settings gains an **"Away / can't run"** row: declare a date or range
  (travel, life — no explanation needed, a note is optional) and the plan **re-lays the week
  around it** immediately. The displaced run slides to the nearest sensible day (a blocked
  Tuesday moves its session to Wednesday, not to a far corner of the week), hard sessions keep
  their spacing, and the long run takes the last available day — a blocked weekend moves it to
  Friday. A heavily blocked week gets honestly **lighter**: runs with no legal day left are shed
  *with their load*, never crammed into the surviving days, and a relocation is refused rather
  than allowed to create an unbroken multi-day run streak. Affected weeks carry an `✈ away` tag.
  Away days are **structural, not easing** — different from a check-in ("legs flat"), which
  reduces load; this moves it. Deterministic input, no LLM in the path, works on a keyless
  install. **Private by design**: away dates never appear on the public read-only mirror in any
  form — not as rows, tags, or markers (an away date on a public page is an empty-house
  broadcast; redacted at the data layer, locked by `det/av-public-strip`). MANUAL §9 documents
  it. New `det/availability`.

### Fixed
- **Progression caps anchor on what you actually ran, not on old prescriptions (§PRO9/§3.1).**
  The +10% long-run cap and the biomechanical (eq-km) ceiling window over *elapsed* weeks used
  to read the plan's own frozen prescriptions — so an athlete who consistently out-ran a
  conservative stretch could see the cap collapse below their real trailing long run and the
  honest day-padding degenerate into many tiny no-rest days. Elapsed and straddling weeks now
  seed those windows from **logged actuals** (weeks with no runs contribute nothing, so the cap
  follows reality down too). New `det/cap-truth-anchor`.
- **A still-ahead quality session survives the mid-week re-plan (§6o-QF).** The week that
  straddles today regenerates its remaining days as easy — correct for a quality session whose
  day already passed (a missed session is never crammed into the back of the week), but wrong
  for one whose laid day is still ahead (now normal, since §AV can relocate a session later
  into the week). A future quality day now keeps its session, pinned in place, with an
  easy-only fallback when the week's remaining budget can't honestly carry it. New
  `det/quality-forward`.
- **Interval read-back no longer counts a hot run-home as an extra rep (§RD v5).** The
  classifier's work/easy contrast baseline could anchor on a recovery *float's own pace* level,
  letting a faster-than-prescribed cooldown clear the work-contrast bar by a hair and read as a
  work rep. The baseline is now the slowest qualifying level's time/distance pace. Cached
  read-backs reclassify lazily on next view. New `det/float-baseline` fixture.
- **Readiness tile refreshes on a morning sync even when no new run arrived.** The page-load
  auto-sync only re-rendered tiles when activities were added, so an HRV-only overnight pull
  could leave yesterday's amber readiness on screen until a manual reload. Any real sync now
  re-reads readiness.
- **Settings window scrolls as one surface.** With the Suunto key rows present, the keys section
  alone could fill the dialog's height — collapsing the settings area below it to zero and
  making everything under the keys unreachable on shorter windows. The dialog body is now a
  single scroll region.

## [0.14.0] - 2026-07-12

The watch learns the plan, a finished race settles its own score, and your data finally has a
backup story.

### Fixed
- **Health-marker range toggle windows from the last reading, not today.** A series that stopped
  (e.g. resting HR after a watch change) always produced an empty 6m/1y window and silently fell
  back to the full span with the button still lit; the window is now the last 6m/1y *of data*.
  The toggle also renders as a proper segmented control — one continuous outline instead of
  per-button borders that broke around the selected segment.

### Added
- **Suunto watch push (§SG).** Planned sessions can now land on a Suunto watch as **SuuntoPlus
  Guides**: connect once via OAuth in Settings (bring your own Suunto partner-app keys — client
  id/secret + subscription key from `apizone.suunto.com`; there is no central relay), and the
  nightly re-plan pushes the upcoming days' structured sessions — steps with duration/distance,
  pace windows and HR bands — replacing yesterday's push idempotently. Without keys or a
  connection the push is simply off; nothing else changes. *(First on-hardware verification is
  still pending — a Suunto-side login issue is blocking the initial OAuth connect; details that
  their docs leave unspecified may need small follow-up fixes.)*
- **Backup & export (§BX).** The app finally has a data-safety story. Settings gains a
  **Backup & export** section with two private-only downloads: a **database snapshot** (a
  consistent, compacted copy of the whole DB, safe to take while the app runs — restore by
  dropping it into `./data`) and a portable **JSON export** of everything that cannot be
  rebuilt from Runalyze (objectives + race outcomes, readiness check-ins, reflections,
  adjustments, lab markers, ignore-list, plan history, settings). A new
  `python SparingHorse.py import <file>` restores the JSON into a fresh instance — and
  refuses to import over existing data. API keys live in the separate secrets store and are
  never in either artifact (locked by the new `det/backup-export`). MANUAL §12 documents the
  whole story.
- **Race lifecycle (§RL).** A race that has passed now resolves instead of staying "upcoming"
  forever: the nightly re-plan (and the objectives list itself) matches the race-day run and
  settles the objective — **done** with a recorded result (finish time, DNF distance, and
  whether the goal time was beaten) or **lapsed** when no race-day run appears after a short
  sync-grace window (a `custom` race with no standard distance settles done/unverified rather
  than being accused of a DNS). Resolved races show in a private-only **Past races** strip on
  the objectives card, and the drift scorecard's post-race reckoning now survives resolution.
  Plans are untouched — periodization already ignored passed races. Race results are redacted
  at the data layer on the public read-only mirror (`outcome`/`resolved_at` never leave the
  private console). New `det/race-lifecycle`.

## [0.13.0] - 2026-07-05

Sleep lands on the health view, the run browser learns your whole history, and an over-run week
finally stops prescribing.

### Fixed
- **An over-run week no longer keeps prescribing.** The in-flight week's remaining days are now
  charged for the km you have already run (§6o-B): once the week's planned volume is done — even
  when the run *count* is short, as with doubles — the remaining days become optional rest
  (marked *"✓ volume run — today optional"*) instead of laying more sessions just to hit a count.
  A partial over-run shrinks the remaining prescription to exactly the km still owed. The charge
  only ever reduces: an under-run week still gets its day-prorated share, so a missed day is never
  crammed into the back of the week, and a week tracking its plan is byte-identical.

### Added
- **The run browser's stats rail now spans three windows.** Next to the browsed month's totals sit
  two more columns: the trailing 12 calendar months ending with that month (so browsing back to a
  peak year compares its rolling volume, not today's), and all time — dated from your first
  recorded run. Same rows throughout: runs, distance, time on feet, average pace, duration-weighted
  average HR, training load, longest run.
- **Sleep, on the health view.** The nightly sleep summary now syncs from Runalyze alongside HRV,
  weight and resting HR — total duration, deep and REM minutes, and the 0–10 quality score, one
  honest point per night (a night is attributed to the morning you woke, and the main sleep wins
  over any nap). It also brings **Overnight low HR**, a de-facto resting-heart-rate trend that —
  unlike the existing Resting HR series, which dead-ends where the device changed — carries
  through to today. These are **displayed signals only**: a study of four years of nights against
  next-day run quality found no predictive link, so sleep never steers the plan or a readiness
  gate. The honest training brakes stay the mechanism; sleep is there to look at.

## [0.12.0] - 2026-07-05

The app now reads your runs back to you — and a safety-brake week can no longer decay into junk
prescriptions.

### Added
- **"Read back" — automatic workout-structure detection.** When a run lands, the app decodes its
  recorded pace profile into the plan's own vocabulary and narrates it on the activity tile:
  `Intervals — 12min wu @5:50 · 3× 3–4min @5:05–5:15 w/ 60–90s floats · 15min cd @6:30`, or simply
  `Easy run — 45min @6:25/km · 2× strides`. Structure is found by *contrast* (sustained pace
  shifts on grade-adjusted streams at any sample rate — hills can't fake intervals), then each
  block is named against your zones **as of that day**, so the read tracks your fitness, not a
  constant. Honest by design: a noisy signal yields no label rather than a guess, and the
  structure line describes what you did — whether it was too hot stays the effort monitor's
  verdict. New runs classify at sync; older ones the first time you open them (the run browser
  included). The label is pace-based and public-safe; per-segment heart rate stays private.
- **Strides, counted the way a human reads the chart.** Short, prominent speed peaks over the
  run's local valley floor — each countersigned by a cadence rise, because a GPS speed spike
  leaves cadence flat and pace alone cannot tell them apart — with width judged on the time axis
  (seconds is a stride, minutes is a rep). A dedicated strides workout gets its own read:
  `Strides — 18min @7:53/km · 11× strides (5+6) @4:40/km` — count, set grouping detected from the
  gap pattern, and the strides-only pace alongside the whole-session pace.
- **Per-rep effort verdicts.** A quality session with a detected structure is now graded on its
  work reps only — warm-up, floats and cool-down excluded — against the prescribed zone's HR
  band, tagged *·reps read* (the whole-run *·rough read* remains only where no structure exists).
  Reps shorter than ~3 minutes are judged on pace instead: a short rep starts rested and the HR
  peak lands in the recovery, so a within-rep average would under-read every short rep.
- **Junk-run floor.** When the safety governor crushes a week's budget (say, after an acute-load
  spike), the week now sheds run-days instead of prescribing 300-metre stubs: no planned run
  under 2.5 km, a gutted week collapses to fewer real runs (a single one at the extreme), taper
  race-week leg-looseners stay exempt, and normal weeks are untouched.

### Fixed
- The earned-volume and 6th-run gate self-tests run their end-to-end checks on a constructed
  fixture instead of the ambient database — their verdicts no longer flip with whatever data is
  loaded (both failed on a live copy after the training-regime flip).
- The quality-drop reshape's documentation now states its honest guarantee: weekly load is
  *bounded* at the pre-drop level, not "preserved exactly" — the pure-easy layout can carry
  slightly less.
- The last plan readers that only understood single-race plans now read the whole multi-race
  road (chain segments count toward banked evidence and effort matching), with a legacy fallback
  so an old saved plan can't silently zero the banked streak right after an upgrade.

## [0.11.0] - 2026-07-04

Quality sessions now hand you their execution plan, and a new run-browser page turns your whole
training history into a clickable, zone-coloured calendar.

### Added
- **Run browser — a new `/runs` page.** The first "explorer" page beside the status dashboard: a
  month calendar of your whole training history where every day with runs carries dots coloured by
  the run's average-HR zone — the same unified zone grid the activity chart band uses — so a month
  reads as an intensity map at a glance (a healthy polarized block is mostly Z1/Z2 dots). Click a
  day and the run opens below in the full activity tile you already know: metrics, hoverable
  pace/HR/cadence/climb traces, route map. Days with several runs open a picker; non-run activity
  shows as a faint tick (its load still counts — the viewer is run-centric). Beside the calendar,
  a month-totals rail sums the month at a glance: runs, distance, time on feet, average pace,
  duration-weighted average heart rate, training load, longest run. Navigation is bounded
  to where your data actually exists. Desktop gets a header link, the phone's bottom bar gains a
  Runs tab. Private-only: the calendar grades intensity by heart rate and the tile carries route
  maps, so the public container serves neither the page nor its API.
- **Workout instruction cards.** A structured quality session (tempo, VO₂ intervals, the long run's
  marathon-pace finish) now presents a clear execution plan instead of a cramped note string: a
  one-line "how to run it" cue plus a per-segment table — duration, approximate distance, the pace
  window and the heart-rate band for every segment, with totals. Pace and HR are cut from the
  *current* unified training zones (the same grid the effort monitor, the activity-chart band and
  the zones card share), so the card always prescribes today's fitness, never the plan's birthday.
  The card sits open on the Today readiness tile when the day's session is structured, and expands
  from any quality-session row in the plan's week panels. The public read-only view shows paces
  only — heart-rate data stays private.
- **Phase-aware Today tile.** The readiness tile's session kicker now names the week's actual phase
  ("Base week 3", "Build week 5") instead of the hardcoded "re-base week" caption left over from
  the caution-era plan shape, and structured sessions get honest titles ("Interval session",
  "Long run · MP finish").

### Fixed
- **Strides pinned to the run that carries them.** The plan's weekly "strides×2" note floated
  under the week's session list, disconnected from any run and silent about when to do them. The
  marker now sits on the session line of the run that actually carries the strides (the week's
  first short easy run — where the engine has always scheduled them), with a hover explaining
  what a stride is and that it adds no training load.
- **Effort-table "low conf" tag was ambiguous.** It read like a compliance judgment; it actually
  flags the verdict's own confidence (a structured session's whole-run average HR blends work
  reps with recovery, so its "did you hit it" read is approximate). Now rendered as "·rough read"
  with a hover spelling that out, and the verdict column's help covers it too.
- **Regime-comparison overlay copy was written one-way.** With the plan now assertive, the
  "Conservative vs Assertive" drift view still framed the other road as "what earning conservative
  unlocks — upper envelope", which is nonsense in that direction. The copy is now direction-aware:
  from a conservative plan the assertive line remains "what earning it unlocks"; from an assertive
  plan the conservative line is presented as what it is — the floor the engine would hold you to
  if the readiness gate re-closed.

## [0.10.0] - 2026-07-04

Every session now says what it's *for* — and the assertive plan sequences fitness by component,
with the quality mix calibrated against real training history.

### Added
- **Component-aware training plan (the four-component model).** Marathon fitness decomposes into
  VO₂max × running economy × SSmax/LT2 plus physiological resilience — how little the first three decay
  over the race distance. Every quality session now carries a chip naming the component it chiefly
  builds (hover for the science), and each phase header sums what the phase is *for*, derived from the
  sessions themselves so the label can never drift from the prescription. An eased or fatigue-capped
  session loses its chip — a session that won't happen builds nothing.
- **Component periodization on the assertive regime ("VO₂max early, maintain late").** The earned
  assertive build now sequences quality by component: a short VO₂ touch appears already in the Base
  phase (developed early, it's cheap to hold — and deliberately small, sized to fit the biomechanical
  damage budget of a steep volume rebuild), the mid-week interval session then shifts to a
  *maintenance role* through Build and Peak while the marathon-pace long-run segment grows week over
  week **at constant speed** (longer, not faster), and the Peak pivots to resilience — the long-fast
  run becomes the workout. Calibration note (verified by replay on real data): the maintenance
  session keeps its full *size* — a smaller mid-week session concentrates the week's load into fewer
  hard days, and under the per-day safety cap that lowered the whole plan's safe volume with no
  offsetting benefit. All existing safety governors (ACWR ceiling, polarized easy floor, phase
  hard-caps, long-run jump cap, the biomechanical brake) bound the new mix unchanged; the
  conservative regime's plan is untouched. Locked by a new `det/components` self-test.

### Fixed
- **An assertive plan looked half-missing: no training-log overlay, and a "No active plan"
  readiness tile.** Three readers of the plan looked only at the re-base phase's weeks — and the
  assertive regime *skips* the re-base, so once a plan graduated: elapsed weeks lost their
  done/missed marks, actual km + pace, unplanned-run entries and journal reflections; the Today's
  Readiness tile claimed there was no active plan at all; and the AI plan explainer was handed an
  empty week list. All three now read the whole road through one shared reader (every phase's
  weeks, each tagged to its phase — multi-race chain segments included), so past/current weeks in
  any phase carry the full overlay and today's session always resolves. Locked by a new
  `det/log-phases` self-test.

## [0.9.0] - 2026-07-03

Zones you can see, a threshold you can set, damage weights tuned to real history — and a fix that
stops a cheerful check-in from slowing your graduation.

### Added
- **A "Current zones" table on the Fitness tab (private).** Your training-intent zones — easy, marathon,
  threshold, interval — each with a pace window and an HR band, both tracking *current* fitness: paces are
  fractions of vVO₂max off your live VO₂max (easy bar = LT1 ≈ 80% of 5k pace), and HR bands are cut from
  the same unified zone grid the effort monitor and chart band read, so the table can't disagree with the
  verdicts (the easy row's top *is* the monitor's easy ceiling). The two columns are deliberately
  independent estimators (VDOT vs LTHR) and their divergence is the decoupling diagnostic, not an error.
  Every cell carries a "?" explaining exactly how it was computed. Locked by `det/zones`.
- **Manual LTHR override + a readiness-gated field-test suggestion.** You can now enter a field-tested
  lactate-threshold HR (Settings → Manual LTHR); a fresh entry outranks the data-derived estimate
  everywhere the app anchors on LTHR (effort ceilings, the HR-zone band, the LT1 cross-check), then
  **ages out over weeks** — LTHR moves with fitness, so the derived estimate takes back over rather than
  trusting a stale number, and re-saving the same value deliberately doesn't re-freshen it. The manual
  documents the 30-minute test protocol, and the app only *suggests* the test when every clearance
  holds — assertive regime, green readiness, no medical hold, and an anchor that's actually improvable —
  never during a conservative rebuild (it's a near-maximal effort; the safety gate is the point). Locked
  by a new `det/lthr-manual` self-test.

### Fixed
- **A positive check-in could silently delay your graduation to the assertive build.** Telling the app
  a run *felt good* records a "no change" note — but the week-banking rule counted *any* recorded
  note-with-dates as "the engine eased this week" and threw the week's evidence out. So a week you
  fully absorbed (plan met, recovery clean) could fail to bank purely because you engaged positively —
  the opposite of the design's intent, and it kept a real training block on the conservative regime
  longer than earned. Banking now distinguishes a genuine ease (reduced volume, easy-only, a clamp, a
  medical hold — any of these still voids the week, even if later superseded) from a no-op check-in,
  which no longer costs anything. Locked into the regime-gate self-test with both cases.

### Changed
- **Biomechanical damage weights calibrated against a real training history.** The eq_km damage-per-km
  multipliers shipped in 0.8.0 as a literature starting point, with the promise they'd be tuned to real
  data before hardening. A full-history replay (4.7 years, ~1,100 runs, zones tracking fitness at the
  time) kept its promise pointing the other way: the one week in the whole record where a fast-load spike
  coincided with an escalating overuse symptom is caught by this axis (and only this axis — volume
  brakes pass it), but the literature weights would also have falsely eased seven quality weeks the
  corpus demonstrably absorbed at peak fitness. The weights are now softened (marathon ×1.4, threshold
  ×2.5, interval ×3.5) — the true catch is kept, false brakes on proven-absorbable training drop by
  more than half, and the jump threshold is unchanged. Harsher weights were strictly worse on the same
  data. With a single true positive on record this remains a false-positive-rate calibration, not a
  validated injury model; it will be re-calibrated as the corpus grows.

## [0.8.0] - 2026-07-02

New injury brakes on load axes the acute:chronic ratio structurally can't see — a pace-weighted
biomechanical lens and a long-run progression cap — plus a fitness-tracking easy-pace ceiling (LT1),
a read-only durability signal, and a side-by-side view of the assertive build your data could unlock.

### Added
- **Conservative-vs-assertive overlay on the plan-drift charts.** A toggle turns the drift view into a
  side-by-side of the road you're on versus the assertive build your data could unlock — distance, load,
  fitness and projected finish, on the same axes. It's a lazy, read-only projection (never persisted, so it
  can't pollute your plan history) and honestly labelled an upper envelope. Locked by `det/regime-compare`.
- **Biomechanical load axis (eq_km).** A second load lens — the tissue-damage axis John Davis's
  biomechanical-load writing highlights — one that heart-rate load (TRIMP/ACWR) structurally can't see. `eq_km` is a *damage-equivalent* distance
  (km weighted by pace, since fast running does several times more tissue damage per km than easy). On the
  assertive build it adds a **soft biomechanical brake**: if a week's pace-weighted load would jump too far
  above your recent weeks *because of fast running* (not just more volume), the engine eases that week's fast
  work back to easy — keeping the aerobic volume, only ever reducing load, and never hard-refusing (injury is
  probabilistic, so it shapes the odds). The conservative rebuild is byte-for-byte identical, and the damage
  weighting is deliberately uncalibrated-to-you for now, so it's contained and will be tuned to your own fast
  sessions and injury history before it tightens. Each plan week now shows its biomechanical `eq-km` (on
  quality weeks, where it exceeds raw km), and a week the brake eased is flagged *"fast load eased"*; locked
  by `det/eq-km`.
- **LT1 — your fitness-tracking easy-pace ceiling.** Making pace the intensity anchor (informed by John
  Davis's pace-first intensity model), the app now derives **LT1 (your aerobic threshold)** from your current
  fitness — operationalized as ≈80% of 5k pace — so the easy-day ceiling *moves as you get fitter or detrain*
  instead of sitting at a fixed heart-rate percentage. The effort monitor now reads against this moving LT1 (with a pace cross-check on
  every run) and no longer falls back to a fixed heart-rate percentage when it lacks a threshold estimate —
  though where a trustworthy, moving heart-rate threshold exists it stays the primary easiness read, because
  your own data shows heart rate is the honest signal of how easy a run really was (and pace alone would
  over-flag a rebuild). When you're rebuilding and your easy pace runs a little ahead of the heart rate it
  costs (normal cardiac decoupling), it says so and explicitly *doesn't* police your easy pace. Surfaced on
  the private effort panel and `/api/lt1`. Locked by `det/lt1`.
- **Durability signal (read-only).** A resilience read from your long-run *aerobic decoupling* — how
  much your pace-to-heart-rate efficiency drifts over the back half of a long run. Low = your economy
  holds over the distance (durable); high or rising = it's decaying — the durability/resilience limit
  John Davis's marathon framework emphasizes. It's computed with a legible verdict and a distance-aware trend
  and accumulating cases on your private run-metrics data, but deliberately *governs nothing yet* — it
  earns its way into race projection only once your corpus shows it predicts your actual race fade
  (the same evidence discipline that walked back the heat coefficient). Locked by `det/durability`.
- **Long-run progression cap (the strongest single injury lever).** Informed by John Davis's writing on
  the Aarhus/Nielsen injury cohort (n≈5000), the plan now never *prescribes* a long run that jumps more
  than +10% beyond your longest run of the previous four weeks — in that cohort a sharp long-run jump
  predicted injury where weekly-mileage jumps did not, and it lives on a biomechanical axis that
  load/ACWR can't see (the +10% figure is our own conservative operationalization). The freed volume is redistributed to your easy runs, so
  the week's total load and its ACWR are unchanged — only the single long-run spike is held back. It
  applies in the assertive build (where the plan grows fast enough to risk such a jump); the
  conservative rebuild is byte-for-byte identical. A week the cap held is flagged *"long-run held (+10%)"*.
  Locked by a new `det/long-run-step` self-test.

### Fixed (pre-release, from an adversarial multi-angle review of the above)
- **Long-run cap could be defeated by its own redistribution.** When the cap fell below a week's natural
  even-run length (a small recent-longest run + a growing week — the returner profile), the freed long-run
  volume made the *easy* runs longer than the capped long, so the true longest run quietly exceeded +10%.
  Now no single run may exceed the cap: the volume spreads over more (shorter) easy days instead, holding
  the promise the cap exists to make.
- **Effort monitor could call a redlined run "easy" when it lacked a reliable heart-rate threshold.** After
  the LT1 change, a run at easy *pace* but with the heart rate sitting at hard-effort level could read "on
  target". It now still won't over-police a merely-elevated heart rate on an easy run (that's normal when
  you're rebuilding), but a genuine redline can no longer read as easy.

### Safety
- Every brake is preserved and was adversarially reviewed: the ACWR ceiling is never breached, a
  recovery trough and periodic deloads bound the connective-tissue load the ratio can't see, readiness
  and symptom halts still dominate, and a recent medical hold keeps the conservative regime in force.

## [0.7.0] - 2026-06-30

The plan now *uses the safe headroom your own metrics reveal* instead of always playing it timid —
an accelerator to match the brakes — while keeping every safety guardrail intact.

### Added
- **Adaptive, goal-driven progressive overload (the "assertive" build).** Once your data shows the
  restart caution isn't needed — a clean symptom/medical window, settled readiness, and a couple of
  well-absorbed weeks banked — the engine graduates from the conservative re-base into an assertive
  build that *rides the safe ACWR ceiling* rather than a timid fixed ramp, so volume and load grow as
  fast as the science safely allows and your fitness is retained through the build instead of bleeding
  away. It's earned automatically from your training data (never from raw fitness alone), and the
  conservative regime stays byte-for-byte identical for anyone still rebuilding.
- **A self-calibrating sense of your response.** The build compares your measured fitness to what it
  projected and eases off when you're falling behind, rides the full ceiling when you're on track —
  your own rate of adaptation tunes how hard it pushes.
- **Finish-time honesty valve.** A short runway isn't a refusal — it's a slower projected finish, and
  now the plan says so: a projected marathon time plus how it improves with more weeks of runway.
- **Two new at-a-glance surfaces on the plan** — a regime badge (with the reason it chose conservative
  vs assertive) and the finish-time curve.

### Safety
- Every brake is preserved and was adversarially reviewed: the ACWR ceiling is never breached, a
  recovery trough and periodic deloads bound the connective-tissue load the ratio can't see, readiness
  and symptom halts still dominate, and a recent medical hold keeps the conservative regime in force.

## [0.6.0] - 2026-06-29

A more accurate read on your fitness, and a safety fix that keeps easy days easy when you're
rebuilding from a low base.

### Fixed
- **More precise fitness & fatigue reconstruction** — the reconstructed fitness/fatigue curve was
  systematically undershooting the values your training service reports, and the gap grew as
  training load rose. The smoothing was corrected so the reconstruction now matches the source
  closely across both rest days and hard days. This sharpens every place the plan reasons about
  fatigue.

### Changed
- **Easy days stay easy at low fitness** — when fitness is low and the safety governor trims a
  week's volume hard, the small fixed amount of quality work could become an outsized share of the
  shrunken week, leaving you doing intensity exactly when you're most fragile. The plan now drops
  that week's quality to easy — never adding volume — so the week stays genuinely easy-dominant,
  and restores the quality automatically as fitness returns. Your marathon-pace long-run finish is
  preserved; only true high-intensity work is held back.

## [0.5.0] - 2026-06-28

The plan starts learning from how a run actually went — not just that it happened. Every run
becomes a controlled data point, compared against your own past runs on the same route, so
patterns in heat, terrain, fatigue and how you felt can surface over time.

### Added
- **Per-run metrics table** — one queryable row per run with every signal we capture (weather,
  terrain, heart rate, an efficiency measure, and your fitness/fatigue state on the day),
  plus an automatic same-route analysis that only compares like-for-like (terrain and fitness
  held) so a finding has to be real, not a season artifact. Private; not exposed on the public box.
- **Worked examples** — each run is auto-compared to your recent runs on the same route, with
  the directional changes laid out and a flag when how you *felt* pointed the opposite way to the
  objective markers (fatigue, HRV). It records the case; it doesn't pass a verdict from a single
  run. The casebook grows as you log more same-route runs. Private.
- **Full-history fitness & fatigue on every run** — the reconstructed fitness/fatigue curve now
  backfills every past run, so the analysis spans your whole history instead of the few days the
  fitness service reports directly.

### Changed
- **Frequency-met days** — once you've already run the week's prescribed number of runs *and*
  its distance, today's remaining run becomes optional rest rather than a forced extra. A short
  junk run on an already-met week does nothing for aerobic shape; the plan stops asking for it.
- **Honest building weeks** — when recent fatigue forces the safety governor to cut a long run
  below a real long-run distance, the plan now relabels it a shakeout and flags the week, instead
  of quietly calling a fitness-trivial run a "long run".

### Fixed
- **Plan ACWR-ceiling self-test** scoped to the weeks the governor actually controls, clearing a
  spurious failure caused by a settled past week's load.

## [0.4.0] - 2026-06-28

Heart-rate zones get a real physiological anchor, and the health view starts tracking the
metrics behind the engine — HRV, weight and resting heart rate over the long horizon.

### Added
- **LTHR-anchored HR zones** — heart-rate zones now anchor on a data-derived lactate-threshold
  HR (Friel's %LTHR grid) when there's enough data, falling back to a %HRmax grid otherwise.
  One definition drives the chart, the new zone band, and the effort monitor, so they can't
  disagree.
- **HR-zone band on the activity chart** — a thin strip along the top colours each section of
  a run by the zone you were in, with an always-on legend showing the basis (LTHR vs %HRmax).
- **Pace ↔ HR coherence check** — surfaces when your prescribed easy *pace* and your easy *HR*
  ceiling disagree (classic when detrained), shown in the effort card. Diagnostic only — it
  never changes the plan.
- **Watch metrics in the health charts** — each sync pulls daily HRV (sleeping RMSSD), body
  weight and resting heart rate from Runalyze, charted against your own long-horizon baseline
  (the view a watch's short rolling baseline can't give you). Private; stripped on the public box.

### Changed
- **Effort monitor reads heart rate against LTHR** when it's confidently known — sharper at the
  easy↔threshold turnpoint than %HRmax — and labels the basis it used. The easy ceiling never
  loosens relative to the previous %HRmax read.

### Fixed
- **Sync/backfill errors are legible** — a backfill that exceeds the gateway timeout now shows a
  clear message instead of a cryptic JSON-parse error, and the sync endpoint always returns JSON.

## [0.3.0] - 2026-06-27

A mobile app experience. On a phone the app now behaves like an installed app rather than
a long web page, and it opens on what matters today.

### Added
- **Mobile app shell** — on phones a fixed bottom tab bar (Today / Plan / Fitness / Body)
  replaces the endless scroll; each area is its own tab with deep-linkable sections.
- **Home-screen icons** — crisp app icons for installing Sparing Horse to your home screen.

### Changed
- **Readiness-first home** — the app opens on Today's Readiness, with your current main
  objective pinned at the top and the latest activity right beneath it.
- **Current-shape numbers** moved into their own section, grouped with the rest of your
  fitness readouts under the Fitness tab.

### Fixed
- **Effort table on small screens** — it now fits a phone in portrait (the key columns,
  with the verdict always visible) and shows every column again in landscape.

## [0.2.0] - 2026-06-26

Multi-race periodization, a more honest feasibility verdict, in-app key setup, and
the app becomes installable. Each engine change ships with a regression lock test.

### Added
- **Installable app (PWA)** — install Sparing Horse to your home screen or desktop for a
  standalone window with an offline app shell. The service worker caches only the UI shell,
  never your data or the API.
- **Set your API keys in the app** — the Settings window now configures your Runalyze token
  and (optional) Claude key directly, with a live "valid / rejected" check, so a fresh
  self-host needs no `.env` editing. Keys live in a private-only store, never the shared DB.
- **Per-race fitness on combined A-race builds** — each race in a chained build shows its own
  projected race-day fitness and feasibility verdict, not just the final peak.
- **Multi-peak plan-drift** — the drift scorecard breaks out each A-race's projected-fitness
  drift against the founding plan and names the next peak still ahead.
- **Public effort-discipline** — the read-only showcase now shows a sanitized, pace-based
  easy-discipline score (no heart-rate data or personal critique).
- **Self-hoster manual** — a full how-to (`MANUAL.md`): setup, the first-run checklist,
  daily/weekly workflow, and a panel-by-panel reading guide.

### Changed
- **The taper now lands on race week** — periodization is anchored so the final taper week
  spans race day, instead of ending up to ~2 weeks short for a race that isn't a whole number
  of weeks out.
- **Honest "earn it" feasibility verdict** — a third reading between "too soon" and "finish":
  when the plan's own projection is below the fitness a healthy finish needs but the runway is
  long, the verdict says the race is reachable *only if you build into it*, rather than
  promising a flat "finish".
- **Anticipated / postponed sessions read correctly** — a run is matched to its nearest
  prescribed session within ±2 days, so doing tomorrow's quality session today (or shifting an
  easy day) is no longer misread as a missed session plus a stray extra.
- **Trail and treadmill runs count** — the running family now reaches the plan-side views
  (effort, mileage, HR), not just activities typed exactly "Running".
- **Stable re-base anchor** — the re-base start is derived from your run history, so it's
  consistent across machines and database rebuilds.
- **Readiness card colours** — the light theme's readiness status card adopts the richer
  green/amber/red signal colours from the dark theme.
- **Deletes explain themselves** — removing an activity now spells out the consequence before
  you confirm.

### Fixed
- **Phase-bar week count** — the "periodization" label now distinguishes the full re-base→race
  span from the weeks still ahead, instead of overstating time-to-race.
- **Medical-hold residuals** — closed remaining gaps so a logged stop symptom holds the plan at
  rest until explicitly cleared; the dominant medical track is locked by test.

## [0.1.1] - 2026-06-26

A full-engine safety and correctness review. Seven load/safety defects and a batch
of correctness fixes — each paired with a regression lock test that reproduces the
bug before the fix.

### Fixed
- **Peak-load ceiling enforcement** — a quality session's minimum impulse could push a
  week's mid-week peak acute:chronic load ratio past the hard ceiling at low fitness,
  even though the week-level governor stayed in band. The full-week builder now re-checks
  the peak and drops to an all-easy week when a quality day would breach the ceiling.
- **Readiness medical-stop gate** — a free-text stop symptom (e.g. "chest pain") no longer
  depends on an optional language model to be caught; a deterministic phrase net halts the
  day on its own, and the halt now persists and reduces the prescription instead of
  reverting the next day.
- **Persistent medical hold** — a logged stop symptom now writes a forced-rest directive,
  regenerates the plan, and keeps the day red until cleared, rather than clearing on the
  next calendar day.
- **Post-race recovery block over-prescription** — the fitness-tracking volume floor no
  longer lifts the post-race bridge, so a recovery re-build keeps its conservative shape
  instead of being inflated by a fresh taper's slack governor headroom.
- **Re-plan transparency** — the plan diff now reports per-phase volume and run-frequency
  changes (and any adjustment directive), so a regeneration that raises load can no longer
  be summarised as "no change".
- **Cross-phase week continuity** — an already-lived week that drifts across a phase
  boundary as the race nears is now frozen verbatim from history instead of being
  redrawn under a different phase's shaper.
- **Feasibility honesty bound** — the feasibility check warns "too soon" only when a short
  runway *and* a low projected race-day fitness coincide; a short runway off high fitness,
  or a long runway off a low fitness base, still reads "finish".
- **Re-base runway clamp** — a near-term race can no longer push the taper past race day;
  the introductory re-base block is clamped to the available runway.
- **Output escaping** — remaining user- and engine-supplied strings in the single-page app
  are HTML-escaped (outcomes, diff banner and change list, adjudication and adjustment
  summaries, plan explanation headline).

### Changed
- **Activity sync** — sync now refreshes an activity whose source data was edited after
  import (reported separately from new activities) and no longer double-counts training
  load when a near-duplicate is re-imported after a local delete.

## [0.1.0]

First documented baseline — the deployed state at the start of the changelog.

### Added
- Deterministic running-coach engine: chronic/acute training-load model (fitness, fatigue,
  acute:chronic ratio) with a safety governor that keeps prescribed load in band.
- Dynamic, objective-driven training plans across re-base → base → build → peak → taper
  phases, regenerated and version-diffable on every change.
- Multi-race build chaining: sequential priority-A races planned as one build with
  bridge / peak / taper segments and a priority selector.
- Daily readiness assessment and an effort-discipline monitor that grades each run against
  its prescription.
- Plan-drift view: original plan versus the plan as it stands, with a settle-the-score
  verdict.
- Post-race reckoning: an honest end-of-race endgame.
- Two-instance deployment off one codebase: a public read-only instance (no private data,
  no mutations) and a private full-console instance behind an access gate.
- Security hardening: content-security-policy, security headers, CSRF tokens, a health
  endpoint, and a public/private data-leak and mutation gate.
- In-app self-test harness guarding the engine invariants.

[Unreleased]: https://github.com/dros74/sparinghorse
[0.1.1]: https://github.com/dros74/sparinghorse
[0.1.0]: https://github.com/dros74/sparinghorse
