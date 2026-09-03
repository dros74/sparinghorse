# Sparing Horse

A self-hosted running planner built on [Runalyze](https://runalyze.com). It reads your current shape
from Runalyze's computed metrics and lays a periodized road from where you are to a goal race. The road
moves as results come in, in both directions: a rough patch eases it toward a goal you can still reach,
a faster rebuild expands it. Every week is sized under safety limits, and every regeneration can be
compared with the one before.

Named for Pheidippides, who *spares the horse by being the horse*.

**Try it without an account:** [demo-sparinghorse.rosado.lu](https://demo-sparinghorse.rosado.lu) is the
full console over a synthetic athlete. It resets itself every hour.

> **The engine is under active development.** Its heuristics, limits and outputs can change between
> versions. Treat a plan as a governed starting point to reason about, not a fixed prescription, and read
> it alongside the note below.

> **Not medical advice.** Sparing Horse is a training tool, not a medical device. It tracks and visualizes
> your own data and applies sports-science heuristics; it does not diagnose. The readiness gate flags
> stop-the-run and cardiac-type symptoms and tells you to see a doctor. Do that. Train at your own risk.

## Features

One small Flask app. The optional public/private split (see *Run with Docker*) decides what a visitor
sees and what only you see behind your own login. The public showcase shows a slice; the console has
everything.

**The engine (deterministic, no AI required)**
- **Current shape.** VO₂max, CTL/ATL (fitness/fatigue) and ACWR, read from your Runalyze data.
- **Objective-driven plan.** Re-base → Base → Build → Peak → Taper toward a goal race. Every week is
  governed: a hard ACWR ceiling, a long-run-jump cap and a biomechanical-load brake (see *How the plan
  is governed*).
- **Replanning in both directions.** Run poorly or miss sessions and the road eases toward a goal you
  can still reach. Rebuild faster than projected and it expands to use the fitness you have. Each
  regeneration can be compared with the last.
- **Several A-races in one build.** Chain them into one continuous road with intermediate peaks, tapers
  and re-build bridges. Each race's role follows from how far apart they are.
- **Volume follows measured fitness.** The build rides the load ceiling and calibrates the ride to how
  your measured fitness tracks the projection. Down weeks and the taper are protected. There is nothing
  to switch on.
- **Two regimes, decided by body evidence.** The normal posture is the ceiling-riding *assertive*
  build. The *conservative* post-illness rebuild engages only on a medical hold or a stop-symptom
  check-in within the last 56 days, and lifts by itself once that window is clean. There is no "train
  harder" button; the safety is in the governors.
- **Injury brakes beyond ACWR.** A long-run-jump cap, a biomechanical damage-equivalent lens, a tissue
  limiter and a chronic-growth cap, so the ACWR ceiling is not the only guardrail.
- **Which limit holds the week.** Every planned week publishes its limits: per governor axis (load ratio,
  long-run step, damage-equivalent km per week and per session, near-ceiling streak, fitness gain per
  week) the ceiling, what the week was laid at, the headroom, whether it bound the week, and the
  evidence behind the ceiling. The week card shows it as a strip ("This week is held by the long-run
  step") and Today carries the binding limit.
- **A down week that is not owed.** A scheduled deload can be cancelled by evidence: when the block
  before it was not run, the load ratio has headroom, fatigue sits under fitness and a fresh check-in
  says legs good, the week is offered as a level week instead. You confirm it until the engine's own
  ledger earns it the right to decide alone. Readiness is a bounded permission: it can cancel a brake,
  never add load.
- **The shape of your week.** A long-run day, a ranked list of rest days, and away days (a flight, a
  family day) that re-lay the week around the gap instead of cramming it.
- **Plan drift.** Distance, effort, CTL and race-outcome charts comparing your founding road to where it
  stands now, with a verdict.
- **Track record.** The engine keeps score on itself: every 28-day fitness forecast is scored against
  what Runalyze then measured, with 7- and 14-day checkpoints, the session sheet against what was run,
  readiness calls against the day, and the weeks run far past their intent.
- **Effort discipline.** Grades whether your easy days are easy against a moving, fitness-tracking
  threshold: HR-led on a derived lactate-threshold HR where that is trustworthy, otherwise grade-adjusted
  pace against an aerobic-threshold (LT1) bar, with an HR-redline backstop either way.
- **Readiness.** A daily green/amber/red verdict that flags stop-the-run and cardiac-type symptoms.
- **Today.** A one-screen daily surface (`/today`): the readiness verdict, the session, the check-in and
  why this session. The installed app starts there. The dashboard is plan-first, with the analytics
  collapsed until opened (remembered per device).
- **Reads of your running.** The latest run with stats, a per-point trace (pace, HR, cadence,
  elevation), an HR-zone band and a route map. A read-back line names what the recorded run was: reps,
  strides, a progression. A run browser (`/runs`) opens any past run the same way. A durability tracker
  follows long-run decoupling, an efficiency chart plots speed per heartbeat, and a zones table shows
  your current pace windows and HR bands.
- **Body.** HRV, body weight and resting HR pulled from Runalyze and charted against your own long
  baseline, plus lab values you enter by hand. Private only.
- **Race lifecycle.** A passed race resolves on its own: the race-day run is matched and the objective
  settles as done (result and goal comparison recorded) or lapsed, with a private *Past races* history.
- **Two themes, two unit systems.** Daylight and Charcoal, following the system unless a choice is
  saved. Distances and paces in kilometres or miles; a display choice, the engine and the API stay
  metric.
- **Suunto watch push** *(optional)*. Upcoming planned sessions land on a Suunto watch as SuuntoPlus
  Guides (steps with pace and HR bands), through your own Suunto partner-app keys. The nightly push
  updates guides in place. If you delete them on the watch itself, use **Rebuild on watch** in Settings:
  the watch only fetches guide ids it has never seen, so an update alone will not bring them back.

**AI layer** *(optional: set `ANTHROPIC_API_KEY`; blank means dormant, the engine is unaffected)*
- Natural-language objectives ("sub-45 10k in October"), advice on closely spaced A-races, plain-language
  plan narration, and qualitative check-ins ("knee's a bit sore" eases the engine, never pushes it).

**Self-hosting**
- **Owner login.** The private console locks itself. On first boot it serves only a *set a passphrase*
  page (or takes `SH_PASSPHRASE` from the environment), then a login page and a 30-day session per
  device. A verified proxy identity skips the login (`SH_TRUST_PROXY_AUTH=1`): a Cloudflare Access JWT
  checked against the team's keys, or `X-Forwarded-User` from inside `SH_PROXY_CIDR`.
- **Keys stay encrypted.** The secrets store is encrypted at rest (`SH_SECRET_KEY`, or a key file beside
  it). The AI layer has three switches in Settings that name what each one sends; check-in judgment is
  off until switched on.
- **Backup and export.** A one-click consistent database snapshot and a portable JSON export of
  everything that cannot be rebuilt from Runalyze, with a restore command for a fresh instance. Keys are
  never included.
- **Hardened image.** Unprivileged, read-only root, a healthcheck, hash-pinned dependencies. The
  operator's page is [`DEPLOY.md`](DEPLOY.md); console access is MANUAL §12.

**Public showcase vs. private console**

| Capability | Public (read-only) | Private (owner) |
|---|:---:|:---:|
| Shape, plan and phases, weekly volume, fitness/fatigue, plan drift, track record | ✅ | ✅ |
| Latest **running** activity (stats + trace) | ✅ | ✅ |
| Readiness | verdict only (inputs redacted) | full check-in |
| Effort discipline | pace-based score, no HR, no critique | full (HR + per-run critique) |
| Route map (GPS) | — *(location privacy)* | ✅ |
| Body: HRV, weight, resting HR, lab markers | — | ✅ |
| Durability, efficiency, zones (HR-derived) | — | ✅ |
| Sync · Backfill · Settings · Backup | — | ✅ |
| Add / remove / re-prioritize objectives | list only | ✅ |
| AI features (parse · adjudicate · explain · check-in) | — | ✅ *(with key)* |

The public container runs `SH_READONLY=1` with **no tokens** and a query-only DB connection. It cannot
sync, write, or call the AI, and the medical and location endpoints are withheld server-side, not just
hidden in the UI.

## Manual
Setup, the first-run checklist, the daily and weekly workflow, and how to read every panel are in
[`MANUAL.md`](MANUAL.md). The sections below are the quick start.

## Requirements
- **Runalyze Premium** and a **Personal API token** (generate at `runalyze.com/settings/personal-api`).
  The app reads your activities and Runalyze's computed shape and effort metrics; it does not replace
  them.
- **Anthropic API key** *(optional)*, for the AI layer: natural-language objectives, clamped qualitative
  adjustments, readiness judgment, plan narration. Blank keeps the AI features dormant.
- **Suunto partner-app keys** *(optional)*, for the watch push: register your own app at
  `apizone.suunto.com` (partner program) and paste its client id, secret and subscription key in
  Settings.

## Run locally
    pip install -r requirements.txt
    cp .env.example .env          # fill RUNALYZE_TOKEN (and the optional keys/personalization)
    RUNALYZE_TOKEN=... python3 SparingHorse.py        # http://127.0.0.1:8770

Hit **Sync now** in the UI (or `POST /api/sync`) to pull your activities and today's shape snapshot into
a local `sparinghorse.db`, then **Backfill all** once for your full history. Add your goal race in the
**Objectives** panel (or seed one with `SH_SEED_OBJECTIVE`); with no objective the engine runs in
maintenance mode. A nightly auto-sync (default `22:00` in `SH_TZ`, override `SH_SYNC_AT`, disable with
`SH_SCHEDULE=0`) keeps the data current.

**Install it as an app.** Sparing Horse is a PWA: open it in a browser and use *Install / Add to Home
Screen* for a standalone window with an offline app shell. No store, no build step. The service worker
caches only the shell, never the API.

## Run with Docker: the public / private / demo split
`docker compose` runs the **same image three times**: a private console and a public showcase off one
shared `./data` database, and a demo on its own volume. The operator's page, with the trust model, what
must never be reachable, proxy examples, upgrade, backup and rollback, is [`DEPLOY.md`](DEPLOY.md). The
quick version follows. The image runs unprivileged on a read-only root with a healthcheck and installs a
hash-pinned `requirements.lock`.

    mkdir -p data secrets backups && cp .env.example .env   # fill RUNALYZE_TOKEN (+ optional keys)
    docker compose up -d --build

- **`sparinghorse`** (`:8770`): full read/write, holds the tokens, runs the nightly sync. Keep it private
  (behind a reverse proxy, Cloudflare Access or a VPN).
- **`sparinghorse-public`** (`:8771`): `SH_READONLY=1`, no tokens, an always-open **read-only** showcase.
  Read-only is enforced server-side (403 on every mutation, query-only connection) and the medical
  sections (blood markers, readiness inputs, per-run effort detail) are withheld. The rule: training
  shape and plan can be public; medical and HR detail stays private. Individual runs are served by number
  only when the page points at them (the latest run, a run the plan or log references, or one from the
  last 14 days), and with the date only: no time of day, no title.
- **`sparinghorse-demo`**: `SH_DEMO=1`, the **full private console over a synthetic athlete**. Unlike the
  read-only view, this one lets a stranger regenerate the plan, post a check-in, move the race and watch
  the engine respond. It seeds itself into its **own** database (a named volume, never the real one) on
  first boot and restores it hourly, so whatever a visitor leaves behind is temporary.

### Running a public demo
A screenshot does not demonstrate an engine. The demo exists so that anyone can push it and see it push
back.

    docker compose up -d --build sparinghorse-demo

It uses a **named volume**, so there is nothing to create first: the demo's database is synthetic, seeded
on first boot and restored hourly. No host directory needs to exist, be backed up or be inspected.

On a host where Docker needs root (a Synology NAS, for instance), prefix the compose command with `sudo`.
Compose reads `.env` for the whole file whatever service you name: if `.env` is root-owned, a non-root
`docker compose` fails with a permission error before it looks at any service.

The demo carries **no credentials**: no Runalyze token, no Claude key, no secrets-store mount. On top of
that, `_demo_guard` refuses these requests, each for its own reason:

| Refused | Why |
|---|---|
| `POST /api/secrets` | a public box must never accept an API key from a stranger, nor hold one to leak |
| `POST /api/sync`, `/api/suunto/*` | outbound calls against someone's real account |
| `POST /api/selftest/run` | an anonymous POST that burns about three minutes of CPU is a denial-of-service primitive |
| `GET /api/backup/db`, `GET /api/export/json` | a full-database snapshot per call and a JSON dump of every table; the data is synthetic, the CPU and bandwidth are not |
| `POST /api/health` | a lab value and its note would be shown to the *next* visitor's Body tab (the read stays open so the tab renders) |
| `private_url`, `house_url`, `house_name` | the only settings displayed to the *next* visitor: the defacement and open-redirect surface |
| `tz`, `athlete_context` | the process clock, and prose that reaches the next visitor and every prompt |

Everything else runs: regenerate, check in, objectives, availability, adjustments, rest-day and
long-run-day preferences, manual LTHR and age. Status reads (`GET /api/secrets`, `GET /api/suunto/status`,
`GET /api/health`) stay open because the panels render them. Every box also carries the same dampers: a
64 KB request-body cap, a per-address rate limit on writes and on the backup/export downloads, and one
demo reseed per ten seconds across all visitors (see *Configuration*).

**The plan explainer** is the one feature a demo cannot run live. It needs a Claude key, and a public host
should not hold one. Bake the narration once, wherever your key already lives, and the demo serves that
text flagged as a sample:

    SH_DB=demo/sparinghorse.db python SparingHorse.py demo-bake

Without it the panel says that the AI layer is not wired up on the demo box and that everything else on
screen is computed by the deterministic engine.

## Configuration (env)
| Var | Purpose |
|---|---|
| `RUNALYZE_TOKEN` | Runalyze Personal API token (required on the writable instance) |
| `ANTHROPIC_API_KEY` | enable the LLM layer (optional) |
| `SH_PORT` | listen port (default 8770) |
| `SH_TZ` / `SH_SYNC_AT` / `SH_SCHEDULE` | nightly-sync timezone / time / on-off |
| `SH_SEED_OBJECTIVE` | seed a first race on a fresh DB (`label\|date\|type\|target\|priority`) |
| `SH_ATHLETE_CONTEXT` | one-line context injected into the LLM prompts (e.g. returning from injury) |
| `SH_ATHLETE_AGE` / `SH_MANUAL_LTHR` | age in years; a field-tested lactate-threshold HR (Settings overrides both) |
| `SH_LONG_RUN_DAY` / `SH_REST_DAY_RANK` | the shape of the week: one weekday, and a ranked list of rest days (`fri,mon,wed`); Settings overrides |
| `SH_HOUSE_URL` / `SH_HOUSE_NAME` | optional back-link in the header |
| `SH_PRIVATE_URL` | public box: an optional "Log in" link to your private console |
| `SH_GUIDE_URL` | "more info" link on pushed Suunto guides (default: this repo) |
| `SH_SUUNTO_PUSH_DAYS` | how many days of the plan the nightly push sends to the watch (default 7) |
| `SH_READONLY` | public container only (set in docker-compose) |
| `SH_DEMO` | demo container only: the full private console over a self-resetting synthetic athlete |
| `SH_DEMO_RESET_EVERY_S` | how often the demo restores its synthetic athlete (default 3600) |
| `SH_PASSPHRASE` | private box: set the console passphrase non-interactively on first boot (else `/setup` asks) |
| `SH_SECRET_KEY` | private box: encrypts the secrets store; unset means a random `secrets.key` beside the store |
| `SH_SECRETS_DB` / `SH_SECRETS_KEY_FILE` | where the secrets store and its key file live (compose: `/secrets`) |
| `SH_TRUST_PROXY_AUTH` / `SH_CF_ACCESS_TEAM` / `SH_CF_ACCESS_AUD` / `SH_PROXY_CIDR` | skip the login for a verified proxy identity (Cloudflare Access JWT, or a forwarded user from inside the CIDR) |
| `SH_COOKIE_SECURE` | `0` only for a plain-http LAN box; the session cookie is Secure by default |
| `SH_AI_NARRATION` / `SH_AI_PARSING` / `SH_AI_JUDGMENT` | env fallbacks for the three AI switches (1/0; Settings overrides; judgment defaults to 0) |
| `SH_UNITS` | `km` (default) or `mi`: how distances and paces are shown, on every page and box; the engine and the API stay metric (Settings overrides) |
| `SH_BACKUP_DIR` / `SH_BACKUP_KEEP` / `SH_BACKUP_PUSH` | private box: where nightly snapshots go (compose: `./backups`), how many to keep (7), a command to run after each (inside the container, file in `$SH_BACKUP_FILE`) |
| `SH_UID` / `SH_GID` | the user the container drops to (default 10001); see DEPLOY.md |
| `SH_MAX_BODY_BYTES` | request-body cap, every box (default 65536; a larger body is a JSON 413) |
| `SH_RATE_POST` / `SH_RATE_EXPENSIVE` / `SH_RATE_RESET` | per-address requests per minute for writes (120), backup/export downloads (10) and the demo reset (6); `SH_RATE_LIMIT=0` disables the limiter |
| `SH_DEMO_ROUTE_CENTER` | `lat,lon` the demo's synthetic routes are centred on (default: Central Park) |
| `SH_DEMO_ROUTE_BEARING` | degrees the demo's loop is rotated to (default 60.565, Central Park's own axis) |

## Calibration
The engine calibrates most things from your synced data: pace zones from VO₂max, CTL/ATL from TRIMP,
HR zones from a derived lactate-threshold HR. About twenty constants do not travel: they were fitted to
one athlete's corpus, mine, and the manual's first section lists them in order of how much they matter
to you and says which ones fail safe. The most visible are `EASY_TRIMP_PER_MIN` (the km-to-TRIMP
exchange rate) and the `REBASE_SHAPE` starter block near the top of `sh_engine.py`. They are
conservative and tunable, and the assertive ride adapts the plan upward as your measured fitness proves
itself.

### Two intensity models, and the check between them
Effort lives in two places, on two physiological anchors:

- **Prescription: pace, from VO₂max.** What the plan tells you to run. Daniels VDOT zones (fractions of
  velocity at VO₂max), validated to reproduce Runalyze's 5 k prognosis. Session load is TRIMP from the
  zone.
- **Judgment: a moving, fitness-tracking easy bar.** How a completed run is graded (the effort-discipline
  monitor). The anchor depends on what your data supports, and the run's `anchor` field says which:
  - **`lthr`: heart rate, where a trustworthy lactate-threshold HR exists.** HR is the direct read of how
    easy a run was, so where a confident moving LTHR is available it leads (Friel %LTHR grid: Z1 < 0.85,
    Z2 0.85–0.89, Z3 0.90–0.94, Z4 0.95–0.99, Z5 ≥ 1.00 · LTHR).
  - **`lt1_pace`: grade-adjusted pace against a moving LT1 bar, when LTHR is not trustworthy.** The easy
    ceiling is LT1 (aerobic threshold), which we set at about 80 % of 5 k pace, a pace-first anchor
    informed by John Davis's intensity model. It moves as you get fitter or detrain, with an HR-redline
    backstop so an easy-paced run whose HR sat at threshold or above can never read easy.
  - **`hrmax`: a %HRmax grid**, only as a last resort (no confident LTHR and no pace).

  LTHR is estimated without a field test from the whole-run average HR of your sustained hard efforts
  (20–70 min at 85 % or more of a robust HRmax), with a confidence flag. The activity chart's HR-zone band
  uses the same LTHR when confident, %HRmax otherwise. A field-tested LTHR can be entered by hand
  (Settings → Manual LTHR): a fresh entry outranks the derived estimate, then ages out over weeks, because
  LTHR moves with fitness and the data should take back over rather than trust a stale number. The
  30-minute test protocol is in the manual, and the app only suggests it when every readiness clearance
  holds, never during a conservative rebuild.

These are independent fitness estimates that should agree: running at the easy pace ceiling should keep
HR under the easy HR ceiling. They diverge most under cardiac decoupling (when detrained, a given easy
pace drives a higher HR than VDOT predicts). The engine does not police that on the pace bar, because an
elevated easy-run HR is normal in a rebuild; the HR-redline backstop is reserved for a threshold-level
effort. A **pace-HR coherence check** shows the divergence as a diagnostic and does not alter the
prescription. One caveat: the streamless LTHR understates the true value for structured tempos (warm-up
and cool-down dilute the whole-run average), so the easy HR ceiling sits at the conservative, lower
Friel boundary.

## How the plan is governed: safety and progression
The plan is not a fixed template with a ceiling bolted on. It is the output of a few interacting
governors, and they decide how much you run and how fast that grows.

### Two regimes, decided by body evidence
The normal posture is the **assertive** build, which rides the load ceiling instead of a timid fixed
ramp, under the full set of brakes below. The **conservative** regime, a Re-base plus a gentle fixed
ramp, is the right posture after illness, and it engages only on body evidence:

- a **medical hold** in force, or one cleared within the last **56 days**; or
- a **stop-the-run symptom** check-in within the last **56 days**.

It lifts by itself once that window is clean, and the plan says which evidence holds it ("a medical hold
is in force"). How last week compared to its prescription is not evidence: a travel week, a skipped run,
a week lived differently never demotes the plan. Your measured fitness and the ceilings already carry
that. After a long healthy break the plan restarts from a small conservative dose and ramps from there
by measurement; no gate, just a floor. There is no manual override: the safety comes from the governors,
not from a setting you can flip. In assertive the engine also calibrates the ride to how you absorb
load, riding the full ceiling while your measured fitness tracks the projection and easing when you fall
behind.

### The ACWR ceiling: useful, not trusted alone, and read two ways
Every week is sized so its projected **ACWR** (acute:chronic workload ratio, this week's load against
your rolling chronic load) stays under **1.25**. That bound holds absolutely, but it is applied to a
shape-neutral reading (the week's mean acute over mean chronic, chronic floored at low CTL) rather than
to the last-day sample, which falls on the long run and is inflated by placement rather than by stress.
The per-week number the UI prints is that last-day sample, so it reads higher and can exceed 1.30 on a
building week with nothing breached; the governed value is published beside it. The in-week peak is held
to **1.30**, except where the biomechanical governors are in force on the assertive build, where the
damage-axis bounds take over as the acute brake (see below). 1.30 sits at the top of Gabbett's
widely-cited training-load "sweet spot". ACWR is also a contested metric, unstable when your chronic load
is low, so the engine treats it as one guardrail among several.

### Load lenses beyond ACWR (the injury brakes)
Informed by John Davis's writing on multi-lens injury risk, the assertive engine also guards the axes a
single mileage ratio cannot see. Each brake only ever reduces a week; the conservative regime is
byte-identical with them dormant.

- **Long-run-jump cap.** The single longest run never grows more than **+10 %** over your longest of the
  previous four weeks. In the Aarhus/Nielsen cohort (n≈5000), sharp long-run jumps predicted injury where
  weekly-mileage jumps did not; the +10 % figure is our own conservative choice.
- **Biomechanical load (`eq_km`).** A pace-weighted "damage-equivalent" distance (fast kilometres count
  for several easy ones), following the tissue-damage axis Davis's biomechanical-load work highlights. A
  soft brake trims a week's fast work when that damage proxy spikes, even if mileage looks fine. The
  pace-to-damage weighting is our own and is not calibrated to you until your fast-session and injury
  history tune it.
- **Tissue limiter.** Forces a recovery week after too many consecutive near-ceiling weeks.
- **Chronic-growth cap.** Bounds how fast chronic load itself may climb, week over week.

### What each session is for (the four fitness components)
Marathon fitness decomposes into four components: **VO₂max** × **running economy** × **SSmax/LT2** (the
classic three-factor model) plus **physiological resilience**, how little the first three decay over
42 km, the marathon's fourth component. Every quality session carries a chip naming the component it
chiefly builds, and each phase header sums what that phase is for, derived from the sessions themselves,
so the label cannot drift from the prescription.

On the assertive regime the plan also periodizes by component. VO₂max is developed early (a short
interval touch already in the base phase, sized to fit the biomechanical budget of a volume rebuild) and
then held in a maintenance role: the session never grows, but it keeps its size, because a smaller
mid-week session concentrates the week and lowers its total safe load. Marathon-pace work grows through
the build (SSmax plus near-race-pace economy), and the peak pivots to resilience: the long run's
marathon-pace segment extends week over week at constant speed, longer, not faster. The conservative
regime keeps its gentler classic mix; only the tags show there.

> **Scientific basis.** The safety model is informed by John Davis (*Marathon Excellence for Everyone*;
> runningwritings.com) and builds on primary work by Gabbett (ACWR), Nielsen et al. (the Aarhus
> load-change injury cohort), Friel (%LTHR zones), Daniels (VDOT pacing) and Jones (physiological
> resilience). The specific thresholds (the `eq_km` damage weighting, the 80 %-of-5k LT1, the +10 %
> long-run cap, and the maintenance/progression fractions of the component periodization) are our own
> operationalization, tuned to one athlete's data, not prescriptions issued by any of these authors.

### The goal, not just the week
For a marathon the engine predicts a **finish time**, and the prediction is a **range, not a number**: an
80 % band, with the median shown as the trend signal beneath it. A single bold time would claim a
precision nobody has.

The prediction and the plan are one object: the plan is the most load the safety model will let you
carry, and the prediction is what that load is worth on the day. So the headline answers the question a
runner has: *what does this block buy me?*

```
Projected marathon finish 3:35:10–4:33:05
  off today's shape: 4:28:10  →  by race day: 4:02:45   · the build buys 25 min
```

Both axes move: projected fitness (chronic load and the long-run ladder you will have built) and
projected speed, each re-anchored on what you have run every time the plan regenerates, so the projection
cannot drift away from reality. The band narrows as races calibrate it and as the horizon shortens. Its
width at a given horizon is measured from your own history, not assumed.

When it cannot answer, it says so. A prediction saved by an older version of the engine asks to be
regenerated. If your last measured run is too old to describe your shape today, the "off today's shape"
line is withheld rather than quoting a number from before a layoff. And if the timeline makes the
objective a survival shuffle rather than a race, it tells you, and shows what a bigger, still governed
build would buy, instead of prescribing a plan that leaves you under-built. It never refuses the goal; it
shows the trade-off, and the choice is yours.

Every prediction is also scored against the result once the race is run, in the band or not and by a
proper log score, and the record is kept where you can see it.

## Self-test
`python SparingHorse.py selftest` runs the deterministic engine battery. It lives in `sh_selftest.py` and
can also be run directly, `python sh_selftest.py --db <path> [--json <out>]`, which is what CI does. It is
also at `/selftest` (private only), where the server runs it as a separate process against a snapshot of
the database: the site stays usable while a battery runs, the page polls `/api/selftest/status` for
progress, and a second battery is refused (409). The scenarios that call the language model are opt-in
(`SH_SELFTEST_LLM=1`, with a key); the default battery is free and deterministic.

The engine's answers are pinned by golden snapshots in `test/golden/`: ten synthetic scenarios rebuilt at
a fixed clock and compared byte for byte. A refactor must show no diff; a deliberate change rewrites them
with `python SparingHorse.py golden` in the same commit.

## Changelog
Features and fixes are recorded in [`CHANGELOG.md`](CHANGELOG.md)
([Keep a Changelog](https://keepachangelog.com) + Semantic Versioning).

## Layout
    SparingHorse.py     the app: Flask routes, sync, storage, scheduler, the LLM layer
    sh_engine.py        the plan engine: the deterministic core, importable on its own
    sh_selftest.py      the deterministic self-test battery
    static/             the front end (shell + stylesheet + script)
    test/               golden snapshots and the browser-driven local test
    MANUAL.md           how to run it and how to read it
    DEPLOY.md           the operator's page: trust model, proxy, upgrade, backup, rollback
    CHANGELOG.md        versioned record of features and fixes
    Dockerfile          container build (entrypoint.sh drops privileges)
    docker-compose.yml  the three-service private / public / demo deployment
    prepare_env.sh      prepares .env and the volume directories for a deploy
    requirements.txt    dependencies; requirements.lock pins them by hash

## License and authorship
AGPL-3.0-or-later; see `LICENSE`. Self-host freely; if you run a modified version as a network service,
share your changes. Built on, and requires, Runalyze. Copyright © 2026 Duarte Rosado. The code was
written with substantial AI assistance under my direction; `AUTHORS.md` records how.
