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
  race, with a hard ACWR safety ceiling on every week.
- **Two-direction replanning** — the road moves as results come in: eases toward an honest goal after a
  rough patch, expands to exploit fitness you've earned. Diff-able week to week.
- **Combined multi-A periodization** — chain several A-races into one continuous build (intermediate
  peaks/tapers + re-build bridges), each race's role set by how far apart they are.
- **CTL-responsive volume + earned levers** — volume tracks measured fitness; opt-in "earned" faster
  build / 6th weekly run / faster Phase-0 exit, all ACWR-capped, recovery weeks protected.
- **Plan drift** — distance / effort / CTL / race-outcome charts comparing your founding road to where
  it stands now, plus a settle-the-score verdict.
- **Effort discipline** — grades whether your easy days are actually easy (HR-led, Runalyze-native).
- **Readiness gate** — a daily green/amber/red verdict that flags stop-the-run / cardiac-type symptoms.
- **Latest running activity** — stats + per-point trace (pace/HR/cadence/elevation), an HR-zone band, and a route map.

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

## Manual
A full how-to — setup, the first-run checklist, daily/weekly workflow, and how to read every panel — lives
in [`MANUAL.md`](MANUAL.md). The sections below are the quick-start.

## Requirements
- **Runalyze Premium** + a **Personal API token** (generate at `runalyze.com/settings/personal-api`). The
  app reads your activities and Runalyze's computed shape/effort metrics — it does not replace them.
- **Anthropic API key** *(optional)* — enables the LLM layer (natural-language objectives, clamped
  qualitative adjustments, readiness judgment, plan narration). Blank keeps the AI features dormant; the
  deterministic engine is unaffected.
- **Suunto** *(optional, planned)* — structured-workout push to the watch, pending Suunto partner-API access.

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
`docker compose` runs the **same image twice off one shared `./data` DB**:

    mkdir -p data && cp .env.example .env   # fill RUNALYZE_TOKEN (+ optional keys)
    docker compose up -d --build

- **`sparinghorse`** (`:8770`) — full read/write, holds the tokens, runs the nightly sync. Keep it private
  (e.g. behind a reverse proxy / Cloudflare Access / VPN).
- **`sparinghorse-public`** (`:8771`) — `SH_READONLY=1`, no tokens, an always-open **read-only** showcase.
  Read-only is enforced server-side (403 on every mutation, query-only connection) and the **medical sections
  — blood markers, readiness, and the per-run effort detail — are withheld** from the public view. Decision:
  training shape + plan can be public; medical/HR detail stays private.

## Configuration (env)
| Var | Purpose |
|---|---|
| `RUNALYZE_TOKEN` | Runalyze Personal API token (required on the writable instance) |
| `ANTHROPIC_API_KEY` | enable the LLM layer (optional) |
| `SH_TZ` / `SH_SYNC_AT` / `SH_SCHEDULE` | nightly-sync timezone / time / on-off |
| `SH_SEED_OBJECTIVE` | seed a first race on a fresh DB (`label\|date\|type\|target\|priority`) |
| `SH_ATHLETE_CONTEXT` | one-line context injected into the LLM prompts (e.g. returning from injury) |
| `SH_WEATHER_CITIES` | header weather widget (`Name,lat,lon;…`); blank = hidden |
| `SH_HOUSE_URL` / `SH_HOUSE_NAME` | optional back-link in the header |
| `SH_READONLY` | public container only (set in docker-compose) |

## Calibration
The engine **self-calibrates** most things from your synced data (pace zones from VO₂max, CTL/ATL from TRIMP,
HR zones from a derived lactate-threshold HR). A few constants near the top of the engine —
`EASY_TRIMP_PER_MIN`, `K_CTL_VOLUME`, and the `REBASE_SHAPE` starter block — are sensible defaults derived
from one masters-runner dataset; they're conservative on purpose and tunable. The CTL floor and earned levers
adapt the plan upward as your fitness proves itself.

### Two intensity models (and the check that keeps them honest)
Effort lives in two places, on two different physiological anchors:

- **Prescription — pace, from VO₂max.** What the plan *tells you to run*. Daniels VDOT zones (fractions of
  velocity-at-VO₂max), validated to reproduce Runalyze's 5 k prognosis. Session load is TRIMP from the zone.
- **Judgment — heart rate, from LTHR.** How a completed run is *graded* (the effort-discipline monitor) and
  how the activity chart colours HR. Run zones anchor on a **data-derived lactate-threshold HR** (Friel's
  %LTHR grid: Z1<0.85, Z2 0.85–0.89, Z3 0.90–0.94, Z4 0.95–0.99, Z5 ≥1.00·LTHR), because %HRmax is loosest
  exactly at the easy↔threshold turnpoint the app cares about. LTHR is estimated streamlessly from the
  whole-run average HR of your sustained hard efforts (20–70 min at ≥85 % robust HRmax), with a confidence
  flag; below confidence it falls back to a %HRmax grid, flagged provisional. The monitor's easy/hard
  ceilings *are* the chart's zone boundaries — one definition, so they can never disagree.

These are **independent fitness estimates that should agree**: running at the easy *pace* ceiling should keep
HR under the easy *HR* ceiling. They diverge most under cardiac decoupling (when detrained, a given easy pace
drives a higher HR than VDOT predicts). A **pace↔HR coherence check** surfaces that divergence as a
diagnostic — it does **not** alter the prescription. Caveat worth knowing: the streamless LTHR understates the
true value for *structured* tempos (warm-up/cool-down dilute the whole-run average), so the easy HR ceiling is
deliberately set at the conservative (lower) Friel boundary.

## Self-test
`python SparingHorse.py selftest` runs the deterministic engine battery (and the key-gated LLM checks where a
key is present). Also at `/selftest` (private only).

## Changelog
Notable features and fixes are tracked in [`CHANGELOG.md`](CHANGELOG.md)
([Keep a Changelog](https://keepachangelog.com) + Semantic Versioning).

## Layout
    SparingHorse.py     the app (Flask + waitress backend + embedded SPA)
    CHANGELOG.md        versioned record of features and fixes
    Dockerfile          container build
    docker-compose.yml  optional two-service public/private deployment
    requirements.txt    pinned dependencies

## License & authorship
AGPL-3.0-or-later — see `LICENSE`. Self-host freely; if you run a modified version as a network service, share
your changes. Built on, and requires, Runalyze. Copyright © 2026 Duarte Rosado. The code was written with
substantial AI assistance under the author's direction — see `AUTHORS.md` for the full, honest provenance.
