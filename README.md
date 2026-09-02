# Sparing Horse

A self-hosted, data-owning **running companion built on [Runalyze](https://runalyze.com)**. It shows your
**current running shape** (reusing Runalyze's computed sports-science metrics) and grows a **dynamic,
objective-driven training-plan engine** around it.

Unlike the usual *type-an-objective-get-a-template* apps — which leave you either set up to fail by an
impossible goal or held back by a generic, over-cautious one — Sparing Horse **starts from your own data and
builds a real periodized road from where you actually are to a concrete objective, then moves that road,
visibly, as your results come in — in both directions.** Run poorly or miss sessions and it eases toward an
honest objective; rebuild faster than projected and it expands to exploit your earned potential — always
safety-governed, always diff-able week to week. The North Star: *never set you up to fail, and never hold
you below what your own data proves you can do.*

Named for Pheidippides, who *spares the horse by being the horse*.

> **Engine status — under active development.** The training-plan engine is evolving quickly: its
> heuristics, safety levers and outputs can change between versions as the underlying model matures.
> Treat its plans as an informed, safety-governed starting point to reason about — not a fixed
> prescription — and read them alongside the *Not medical advice* note below.

> **Not medical advice.** Sparing Horse is a training tool, not a medical device. It tracks and visualizes
> your own data and applies sports-science heuristics; it does not diagnose. Its readiness gate flags
> stop-the-run / cardiac-type symptoms and tells you to **see a doctor** — always do. Train at your own risk.

## Features

Everything below runs from one small Flask app. The optional public/private split (see *Run with Docker*)
decides what a visitor sees versus what only you — behind your own auth — can see and do. The public
showcase deliberately shows a slice; the real console has the full set.

**The engine (deterministic, no AI required)**
- **Current shape** — VO₂max, CTL/ATL (fitness/fatigue) and ACWR, read from your Runalyze data.
- **Objective-driven plan** — reverse-periodized Re-base → Base → Build → Peak → Taper toward a goal
  race, safety-governed every week: a hard ACWR ceiling, a long-run-jump cap and a biomechanical-load
  brake (see *How the plan is governed*).
- **Two-direction replanning** — the road moves as results come in: eases toward an honest goal after a
  rough patch, expands to exploit fitness you've earned. Diff-able week to week.
- **Combined multi-A periodization** — chain several A-races into one continuous build (intermediate
  peaks/tapers + re-build bridges), each race's role set by how far apart they are.
- **Volume follows measured fitness** — the build rides the load ceiling and self-calibrates the ride to
  how your measured fitness tracks the projection; down weeks and the taper are protected. No opt-in
  levers — nothing to flip.
- **Two regimes, decided by body evidence** — the normal posture is the ceiling-riding **assertive**
  build; the **conservative** post-illness rebuild engages only on a medical hold or a stop-symptom
  check-in within the last 56 days, and lifts by itself once that window is clean (see *How the plan
  is governed*). No "train harder" button — the safety is in the governors.
- **Injury brakes beyond ACWR** — long-run-jump cap, a biomechanical damage-equivalent lens, a tissue
  limiter and a chronic-growth cap, so the ACWR ceiling isn't the only guardrail.
- **Plan drift** — distance / effort / CTL / race-outcome charts comparing your founding road to where
  it stands now, plus a settle-the-score verdict.
- **Effort discipline** — grades whether your easy days are actually easy against a *moving*,
  fitness-tracking threshold: HR-led on a derived lactate-threshold HR where that's trustworthy, otherwise
  your grade-adjusted pace vs an aerobic-threshold (LT1) bar — with an HR-redline backstop either way.
- **Readiness gate** — a daily green/amber/red verdict that flags stop-the-run / cardiac-type symptoms.
- **Today** — a one-screen daily surface (`/today`): the readiness verdict, the session, the check-in and why this session; the installed app starts there.
- **Latest running activity** — stats + per-point trace (pace/HR/cadence/elevation), an HR-zone band, and a route map.
- **Race lifecycle** — a passed race resolves on its own: the race-day run is matched and the objective
  settles as done (result + goal comparison recorded) or lapsed, with a private *Past races* history.
- **Backup & export** — one-click consistent database snapshot + a portable JSON export of everything
  that can't be rebuilt from Runalyze, with a fresh-instance restore command. Keys are never included.
- **Suunto watch push** *(optional)* — upcoming planned sessions land on a Suunto watch as SuuntoPlus
  Guides (steps with pace + HR bands), via your own Suunto partner-app keys. The nightly push updates
  guides in place; if you delete them on the watch itself, use **Rebuild on watch** in Settings — the
  watch only fetches guide ids it has never seen, so an update alone will not bring them back.

**AI layer** *(optional — set `ANTHROPIC_API_KEY`; blank = dormant, the engine is unaffected)*
- Natural-language objectives ("sub-45 10k in October"), multi-A adjudication advice, plain-language
  plan narration, and qualitative check-ins ("knee's a bit sore" → the engine eases, never pushes).

**Public showcase vs. private console**

| Capability | Public (read-only) | Private (owner) |
|---|:---:|:---:|
| Shape, plan & phases, weekly volume, fitness/fatigue, plan drift | ✅ | ✅ |
| Latest **running** activity (stats + trace) | ✅ | ✅ |
| Readiness | verdict only (inputs redacted) | full check-in |
| Route map (GPS) | — *(location privacy)* | ✅ |
| Health / blood markers | — | ✅ |
| Effort discipline (per-run HR + critique) | — | ✅ |
| Sync · Backfill · Settings | — | ✅ |
| Add / remove / re-prioritize objectives | list only | ✅ |
| AI features (parse · adjudicate · explain · check-in) | — | ✅ *(with key)* |

The public container runs `SH_READONLY=1` with **no tokens** and a query-only DB connection — it
physically cannot sync, write, or call the AI, and the medical/location endpoints are withheld
server-side (not just hidden in the UI).

**The symmetry (0.59.0).** The engine has always braked on evidence and deloaded on a schedule. Now
a scheduled deload can be cancelled by evidence: when the block before it was not run, the load
ratio has headroom, fatigue sits under fitness and a fresh check-in says legs good, the week is
offered as a level week instead — owner-confirmed until the engine's own ledger earns it the right
to decide alone. Both denominators of a week (the ramp's bar and the session sheet) are published,
and readiness is a bounded permission token: it can cancel a brake, never add load.

**The limits (0.58.0).** Every laid week publishes the body's limits as one object: per governor axis
(load ratio, long-run step, damage-equivalent km per week and per session, near-ceiling streak, fitness
gain per week) the ceiling, what the week was laid at, the headroom, whether it bound the week, and the
evidence basis of the ceiling (literature, fitted to this athlete, or structural). The week card renders
it as a strip ("This week is held by the long-run step") and Today carries the binding limit. The
long-run step carries an injury-risk read against the Aarhus cohort — a read, not a percentage: one
athlete cannot calibrate one. The ledger scores more too: fitness checkpoints at 7 and 14 days, the
session sheet against what was run, readiness calls against the day, and the weeks run far past their
intent. Distances and paces can be shown in miles (Settings → Distance units; the engine stays metric).

**The day (0.57.0).** `/today` is the daily surface — the readiness verdict, the session, the check-in and one
line saying why this session — and the page the installed app opens on. The dashboard is plan-first, with
the analytics collapsed until opened (remembered per device); the public and demo boxes open on the track
record. Two themes, following the system unless a choice is saved.

**Access (0.56.0).** The private console locks itself: on first boot it serves only a *set a passphrase*
page (or takes `SH_PASSPHRASE` from the environment and never shows one), then a login page and a 30-day
session cookie per device. With `SH_TRUST_PROXY_AUTH=1` a request carrying a **verified** proxy identity —
a Cloudflare Access JWT checked against the team's keys, or `X-Forwarded-User` from inside
`SH_PROXY_CIDR` — skips the login. The secrets store is encrypted at rest (`SH_SECRET_KEY`, or a key file
beside it). The AI layer has three switches in Settings that name what each one sends; check-in judgment is
off until switched on. Details in [`DEPLOY.md`](DEPLOY.md) and MANUAL §12.

## Manual
A full how-to — setup, the first-run checklist, daily/weekly workflow, and how to read every panel — lives
in [`MANUAL.md`](MANUAL.md). The sections below are the quick-start.

## Requirements
- **Runalyze Premium** + a **Personal API token** (generate at `runalyze.com/settings/personal-api`). The
  app reads your activities and Runalyze's computed shape/effort metrics — it does not replace them.
- **Anthropic API key** *(optional)* — enables the LLM layer (natural-language objectives, clamped
  qualitative adjustments, readiness judgment, plan narration). Blank keeps the AI features dormant; the
  deterministic engine is unaffected.
- **Suunto partner-app keys** *(optional)* — enable the watch push by registering your own app at
  `apizone.suunto.com` (partner program) and pasting its client id/secret + subscription key in Settings.

## Run locally
    pip install -r requirements.txt
    cp .env.example .env          # fill RUNALYZE_TOKEN (and the optional keys/personalization)
    RUNALYZE_TOKEN=... python3 SparingHorse.py        # http://127.0.0.1:8770

Hit **Sync now** in the UI (or `POST /api/sync`) to pull your activities + today's shape snapshot into a
locally-owned `sparinghorse.db`, then **Backfill all** once for your full history. Add your goal race in the
**Objectives** panel (or seed one with `SH_SEED_OBJECTIVE`); with no objective the engine runs in maintenance
mode. A nightly auto-sync (default `22:00` in `SH_TZ`, override `SH_SYNC_AT`, disable `SH_SCHEDULE=0`) keeps
the data current.

**Install it as an app.** Sparing Horse is a PWA — open it in a browser and use *Install / Add to Home
Screen* for a standalone window with an offline app shell. No store, no build step; the service worker
caches only the shell and never the API.

## Run with Docker — optional public + private split
`docker compose` runs the **same image twice off one shared `./data` DB**. The operator's page —
the trust model, what must never be reachable, the proxy examples, upgrade, backup and rollback — is
[`DEPLOY.md`](DEPLOY.md); the quick version follows. Since 0.55.2 the image runs unprivileged on a
read-only root with a healthcheck, and installs a hash-pinned `requirements.lock`.

    mkdir -p data && cp .env.example .env   # fill RUNALYZE_TOKEN (+ optional keys)
    docker compose up -d --build

- **`sparinghorse`** (`:8770`) — full read/write, holds the tokens, runs the nightly sync. Keep it private
  (e.g. behind a reverse proxy / Cloudflare Access / VPN).
- **`sparinghorse-public`** (`:8771`) — `SH_READONLY=1`, no tokens, an always-open **read-only** showcase.
  Read-only is enforced server-side (403 on every mutation, query-only connection) and the **medical sections
  — blood markers, readiness, and the per-run effort detail — are withheld** from the public view. Decision:
  training shape + plan can be public; medical/HR detail stays private. Individual runs are served by number
  only when the page points at them (the latest run, a run the plan or log references, or one from the last
  14 days), and with the date only — no time of day, no title.
- **`sparinghorse-demo`** — `SH_DEMO=1`, the **full private console over a synthetic athlete**. Not the same
  thing as the read-only view: this one lets a stranger regenerate the plan, post a check-in, move the race
  and watch the engine actually respond. It seeds itself into its **own** database (a named volume, never the real one) on first boot and restores it hourly, so whatever a visitor leaves behind is temporary.

### Running a public demo
The demo exists because the honest problem with showing this project to anyone is that a screenshot doesn't
demonstrate an engine — you have to be able to push it and see it push back.

    docker compose up -d --build sparinghorse-demo

It uses a **named volume**, so there is nothing to create first — the demo's database is synthetic,
seeded on first boot and restored hourly, so no host directory needs to exist, be backed up, or be
inspected.

(On a host where Docker needs root — a Synology NAS, for instance — prefix the compose command with
`sudo`. Note also that Compose reads `.env` for the *whole* file whatever service you name: if `.env`
is root-owned, a non-root `docker compose` fails with a permission error before it looks at any
service at all.)

It carries **no credentials**: no Runalyze token, no Claude key, and no secrets-store mount. On top of that,
`_demo_guard` refuses these families of request, each for its own reason:

| Refused | Why |
|---|---|
| `POST /api/secrets` | a public box must never accept an API key from a stranger, nor hold one to leak |
| `POST /api/sync`, `/api/suunto/*` | outbound calls against someone's real account |
| `POST /api/selftest/run` | an anonymous POST that burns ~3 minutes of CPU is a denial-of-service primitive |
| `GET /api/backup/db`, `GET /api/export/json` | a full-database snapshot per call and a JSON dump of every table — the data is synthetic, the CPU and bandwidth are not |
| `POST /api/health` | a lab value and its note are shown to the *next* visitor's Body tab (the read stays open so the tab renders) |
| `private_url`, `house_url`, `house_name` | the only settings displayed to the *next* visitor — the defacement and open-redirect surface |
| `tz`, `athlete_context` | the process clock, and prose that reaches the next visitor and every prompt |

Everything else really runs: regenerate, check in, objectives, availability, adjustments, rest-day and
long-run-day preferences, manual LTHR and age. Status reads (`GET /api/secrets`, `GET /api/suunto/status`,
`GET /api/health`) stay open because the panels render them. On top of the refusals, every box carries the
same dampers: a 64 KB request-body cap, a per-address rate limit on writes and on the backup/export
downloads, and one demo reseed per ten seconds across all visitors (see *Configuration*).

**The plan explainer** is the one feature a demo can't run live — it needs a Claude key, and a public host
should not be holding one. Bake the narration once, wherever your key already lives, and the demo serves that
text flagged as a sample:

    SH_DB=demo/sparinghorse.db python SparingHorse.py demo-bake

Without it the panel says plainly that the AI layer isn't wired up on the demo box and that everything else
on screen is computed by the deterministic engine — which is true, and a better thing for a visitor to read
than an invented explanation.

## Configuration (env)
| Var | Purpose |
|---|---|
| `RUNALYZE_TOKEN` | Runalyze Personal API token (required on the writable instance) |
| `ANTHROPIC_API_KEY` | enable the LLM layer (optional) |
| `SH_TZ` / `SH_SYNC_AT` / `SH_SCHEDULE` | nightly-sync timezone / time / on-off |
| `SH_SEED_OBJECTIVE` | seed a first race on a fresh DB (`label\|date\|type\|target\|priority`) |
| `SH_ATHLETE_CONTEXT` | one-line context injected into the LLM prompts (e.g. returning from injury) |
| `SH_HOUSE_URL` / `SH_HOUSE_NAME` | optional back-link in the header |
| `SH_GUIDE_URL` | "more info" link on pushed Suunto guides (default: this repo) |
| `SH_READONLY` | public container only (set in docker-compose) |
| `SH_DEMO` | demo container only — full private console over a self-resetting synthetic athlete |
| `SH_DEMO_RESET_EVERY_S` | how often the demo restores its synthetic athlete (default 3600) |
| `SH_PASSPHRASE` | private box: set the console passphrase non-interactively on first boot (else `/setup` asks) |
| `SH_SECRET_KEY` | private box: encrypts the secrets store; unset = a random `secrets.key` beside the store |
| `SH_TRUST_PROXY_AUTH` / `SH_CF_ACCESS_TEAM` / `SH_CF_ACCESS_AUD` / `SH_PROXY_CIDR` | skip the login for a verified proxy identity (Cloudflare Access JWT, or a forwarded user from inside the CIDR) |
| `SH_COOKIE_SECURE` | `0` only for a plain-http LAN box; the session cookie is Secure by default |
| `SH_AI_NARRATION` / `SH_AI_PARSING` / `SH_AI_JUDGMENT` | env fallbacks for the three AI switches (1/0; Settings overrides; judgment defaults to 0) |
| `SH_UNITS` | `km` (default) or `mi` — how distances and paces are SHOWN, on every page and box; the engine and the API stay metric (0.58.0; Settings overrides) |
| `SH_BACKUP_DIR` / `SH_BACKUP_KEEP` / `SH_BACKUP_PUSH` | private box: where nightly snapshots go (compose: `./backups`), how many to keep (7), a command to run after each (inside the container, file in `$SH_BACKUP_FILE`) |
| `SH_MAX_BODY_BYTES` | request-body cap, every box (default 65536; a larger body is a JSON 413) |
| `SH_RATE_POST` / `SH_RATE_EXPENSIVE` / `SH_RATE_RESET` | per-address requests per minute for writes (120), backup/export downloads (10) and the demo reset (6); `SH_RATE_LIMIT=0` disables the limiter |
| `SH_DEMO_ROUTE_CENTER` | `lat,lon` the demo's synthetic routes are centred on (default: Central Park) |
| `SH_DEMO_ROUTE_BEARING` | degrees the demo's loop is rotated to (default 60.565, Central Park's own axis) |

## Calibration
The engine **self-calibrates** most things from your synced data (pace zones from VO₂max, CTL/ATL from TRIMP,
HR zones from a derived lactate-threshold HR). A few constants near the top of the engine —
`EASY_TRIMP_PER_MIN`, `K_CTL_VOLUME`, and the `REBASE_SHAPE` starter block — are sensible defaults derived
from one masters-runner dataset; they're conservative on purpose and tunable. The assertive ride adapts the
plan upward as your measured fitness proves itself.

### Two intensity models (and the check that keeps them honest)
Effort lives in two places, on two different physiological anchors:

- **Prescription — pace, from VO₂max.** What the plan *tells you to run*. Daniels VDOT zones (fractions of
  velocity-at-VO₂max), validated to reproduce Runalyze's 5 k prognosis. Session load is TRIMP from the zone.
- **Judgment — a *moving*, fitness-tracking easy bar.** How a completed run is *graded* (the
  effort-discipline monitor). The primary anchor depends on what your data supports (the run's `anchor` field
  says which):
  - **`lthr` — heart rate, where a trustworthy lactate-threshold HR exists.** HR is the honest read of how
    easy a run really was, so where a confident *moving* LTHR is available it leads (Friel %LTHR grid:
    Z1<0.85, Z2 0.85–0.89, Z3 0.90–0.94, Z4 0.95–0.99, Z5 ≥1.00·LTHR).
  - **`lt1_pace` — grade-adjusted pace vs a moving LT1 bar, when LTHR isn't trustworthy.** The easy ceiling
    is LT1 (aerobic threshold), which we operationalize as ≈80 % of 5 k pace — a pace-first anchor informed
    by John Davis's intensity model — and it *moves* as you get fitter or detrain, with an **HR-redline
    backstop** so an easy-*paced* run whose HR sat at threshold+ can never read easy.
  - **`hrmax` — a %HRmax grid** only as a last resort (no confident LTHR *and* no pace).

  LTHR is estimated streamlessly from the whole-run average HR of your sustained hard efforts (20–70 min at
  ≥85 % robust HRmax), with a confidence flag. The activity chart's HR-zone band uses the same LTHR when
  confident, %HRmax otherwise. A **field-tested LTHR can be entered manually** (Settings → Manual LTHR):
  a fresh entry outranks the derived estimate, then ages out over weeks — LTHR moves with fitness, so the
  data takes back over rather than trusting a stale number. The 30-min test protocol is in the manual, and
  the app only *suggests* it when every readiness clearance holds (never during a conservative rebuild).

These are **independent fitness estimates that should agree**: running at the easy *pace* ceiling should keep
HR under the easy *HR* ceiling. They diverge most under cardiac decoupling (when detrained, a given easy pace
drives a higher HR than VDOT predicts) — which the engine *deliberately doesn't police* on the pace bar (a
merely-elevated easy-run HR is normal in a rebuild), reserving the HR-redline backstop for a genuine
threshold+ effort. A **pace↔HR coherence check** surfaces that divergence as a diagnostic — it does **not**
alter the prescription. Caveat worth knowing: the streamless LTHR understates the true value for *structured*
tempos (warm-up/cool-down dilute the whole-run average), so the easy HR ceiling is deliberately set at the
conservative (lower) Friel boundary.

## How the plan is governed — safety & progression
The plan isn't a fixed template with a ceiling bolted on; it's the output of a few interacting governors.
This is what decides how much you run and how fast that grows.

### Two regimes, decided by body evidence
The normal posture is the **assertive** build — one that *rides* the load ceiling instead of a timid fixed
ramp, under the full set of brakes below. The **conservative** regime — a Re-base plus a gentle fixed ramp,
the right posture after illness — engages only on **body evidence**:

- a **medical hold** in force, or one cleared within the last **56 days**; or
- a **stop-the-run symptom** check-in within the last **56 days**.

It lifts by itself once that window is clean, and the plan says *which* evidence holds it ("a medical hold is
in force"). How last week compared to its prescription is deliberately *not* evidence — a travel week, a
skipped run, a week lived differently never demotes the plan; your measured fitness and the ceilings already
carry that. After a long healthy *break* the plan restarts from a small conservative dose and ramps from
there by measurement — no gate, just a floor. There is deliberately **no manual override**: the safety comes
from the governors, not a setting you can flip. In assertive the engine further self-calibrates the ride to
how you're actually absorbing load — riding the full ceiling while your measured fitness tracks the
projection, easing automatically when you fall behind.

### The ACWR ceiling — useful, *not trusted alone*, and read two ways
Every week is sized so its projected **ACWR** (acute:chronic workload ratio — this week's load vs. your
rolling chronic load) stays under **1.25**. That bound holds absolutely, but it is applied to a
*shape-neutral* reading — the week's mean acute over mean chronic, chronic floored at low CTL — rather
than to the last-day sample, which falls on the long run and is inflated by placement rather than by
stress. The per-week number the UI prints is that last-day sample, so it reads higher and can exceed
1.30 on a building week with nothing having been breached; the governed value is published alongside
it. The in-week peak is held to **1.30**, except where the biomechanical governors are in force on the
assertive build — there the damage-axis bounds take over as the acute brake by design (see below).
1.30 sits at the top of Gabbett's widely-cited training-load "sweet spot." It's also a **contested** metric —
it can be unstable when your chronic load is low — so the engine treats it as one guardrail among several,
never the whole safety story.

### Load lenses beyond ACWR (the injury brakes)
Informed by John Davis's writing on multi-lens injury risk, the assertive engine also guards the axes a
single mileage ratio can't see. Each brake only ever *reduces* a week; the conservative regime is
byte-identical with them dormant.

- **Long-run-jump cap** — the single longest run never grows more than **+10 %** over your longest of the
  previous four weeks. (In the Aarhus/Nielsen cohort, n≈5000, sharp *long-run* jumps predicted injury where
  weekly-mileage jumps did not; the +10 % figure is our own conservative operationalization.)
- **Biomechanical load (`eq_km`)** — a pace-weighted "damage-equivalent" distance (fast kilometres count for
  several easy ones), following the tissue-damage axis Davis's biomechanical-load work highlights; a soft
  brake trims a week's fast work when that damage proxy spikes, even if mileage looks fine. The pace→damage
  weighting is our own, deliberately uncalibrated to you until your fast-session and injury history tune it.
- **Tissue limiter** — forces a recovery week after too many consecutive near-ceiling weeks.
- **Chronic-growth cap** — bounds how fast chronic load itself may climb, week over week.

### What each session is *for* (the four fitness components)
Marathon fitness decomposes into four components: **VO₂max** × **running economy** × **SSmax/LT2** (the
classic three-factor model) plus **physiological resilience** — how little the first three decay over
42 km, the marathon's modern fourth component. Every quality session in the plan carries a chip naming
the component it chiefly builds, and each phase header sums what that phase is *for* — derived from the
sessions themselves, so the label can never drift from the prescription.

On the assertive regime the plan also *periodizes by component*: VO₂max is developed **early** (a short
interval touch already in the base phase, sized to fit the biomechanical budget of a volume rebuild) and
then held in a maintenance *role* — the session never grows, but it keeps its size, because verification
showed a smaller mid-week session concentrates the week and lowers its total safe load. Marathon-pace
work **grows** through the build (SSmax plus near-race-pace economy), and the peak pivots to
**resilience** — the long run's marathon-pace segment extends week over week at *constant speed*: longer,
not faster. The conservative regime keeps its gentler classic mix unchanged; only the tags show there.

> **Scientific basis.** The safety model is *informed by* John Davis (*Marathon Excellence for Everyone*;
> runningwritings.com) and builds on primary work by Gabbett (ACWR), Nielsen et al. (the Aarhus load-change
> injury cohort), Friel (%LTHR zones), Daniels (VDOT pacing) and Jones (physiological resilience). The
> specific thresholds — the `eq_km` damage weighting, the ≈80 %-of-5k LT1, the +10 % long-run cap, and the
> maintenance/progression fractions of the component periodization — are our own operationalization, tuned
> to the athlete's data, not prescriptions issued by any of these authors.

### Honest about the *goal*, not just the week
For a marathon the engine predicts a **finish time**, and the prediction is a **range, not a number** —
an 80 % band, with the median shown as the trend signal beneath it. A single bold time would claim a
precision nobody has.

The prediction and the plan are one object: the plan is the most load the safety model will let you
carry, and the prediction is what that load is worth on the day. So the headline answers the question a
runner actually has — *what does this block buy me?*

```
Projected marathon finish 3:35:10–4:33:05
  off today's shape: 4:28:10  →  by race day: 4:02:45   · the build buys 25 min
```

Both axes move: projected fitness (chronic load and the long-run ladder you'll have built) and
projected speed, each re-anchored on what you have actually run every time the plan regenerates, so the
projection can never drift away from reality. The band narrows as races calibrate it and as the horizon
shortens — its width at a given horizon is measured from your own history, not assumed.

It is honest when it cannot answer. A prediction saved by an older version of the engine says so and
asks to be regenerated. If your last measured run is too old to describe your shape today, the
"off today's shape" line is withheld rather than quietly quoting a number from before a layoff. And if
the timeline makes the objective a survival-shuffle rather than a race, it tells you — and shows what a
bigger, still safety-governed build would buy — instead of quietly prescribing a plan that leaves you
under-built. It never refuses the goal; it makes the trade-off visible, so the choice is yours.

Every prediction is also **scored against the result** once the race is run — in the band or not, and by
a proper log score — and the record is kept where you can see it.

## Self-test
`python SparingHorse.py selftest` runs the deterministic engine battery. It lives in `sh_selftest.py` and can
also be run directly — `python sh_selftest.py --db <path> [--json <out>]` — which is what CI does. Also at
`/selftest` (private only), where the server runs it as a SEPARATE PROCESS against a snapshot of the database:
the site stays fully usable while a battery runs, the page polls `/api/selftest/status` for progress, and a
second battery is refused (409). The scenarios that call the language model are opt-in — set
`SH_SELFTEST_LLM=1` (with a key) to include them; the default battery is free and deterministic.

The engine's answers are pinned by golden snapshots in `test/golden/`: ten synthetic scenarios rebuilt at a
fixed clock and compared byte-for-byte. A refactor must show no diff; a deliberate change re-writes them with
`python SparingHorse.py golden` in the same commit.

## Changelog
Notable features and fixes are tracked in [`CHANGELOG.md`](CHANGELOG.md)
([Keep a Changelog](https://keepachangelog.com) + Semantic Versioning).

## Layout
    SparingHorse.py     the app — Flask routes, sync, storage, scheduler, the LLM layer
    sh_engine.py        the plan engine: the deterministic core, importable on its own
    sh_selftest.py      the deterministic self-test battery
    static/             the front end (shell + stylesheet + script)
    CHANGELOG.md        versioned record of features and fixes
    Dockerfile          container build
    docker-compose.yml  optional two-service public/private deployment
    requirements.txt    pinned dependencies

## License & authorship
AGPL-3.0-or-later — see `LICENSE`. Self-host freely; if you run a modified version as a network service, share
your changes. Built on, and requires, Runalyze. Copyright © 2026 Duarte Rosado. The code was written with
substantial AI assistance under the author's direction — see `AUTHORS.md` for the full, honest provenance.
