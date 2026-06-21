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

> **Not medical advice.** Sparing Horse is a training tool, not a medical device. It tracks and visualizes
> your own data and applies sports-science heuristics; it does not diagnose. Its readiness gate flags
> stop-the-run / cardiac-type symptoms and tells you to **see a doctor** — always do. Train at your own risk.

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
mode. A nightly auto-sync (default `22:30` in `SH_TZ`, override `SH_SYNC_AT`, disable `SH_SCHEDULE=0`) keeps
the data current.

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
HR zones from your `hr_max`). A few constants near the top of the engine — `EASY_TRIMP_PER_MIN`,
`K_CTL_VOLUME`, and the `REBASE_SHAPE` starter block — are sensible defaults derived from one masters-runner
dataset; they're conservative on purpose and tunable. The CTL floor and earned levers adapt the plan upward
as your fitness proves itself.

## Self-test
`python SparingHorse.py selftest` runs the deterministic engine battery (and the key-gated LLM checks where a
key is present). Also at `/selftest` (private only).

## Layout
    SparingHorse.py     the app (Flask + waitress backend + embedded SPA)
    Dockerfile          container build
    docker-compose.yml  optional two-service public/private deployment
    requirements.txt    pinned dependencies

## License & authorship
AGPL-3.0-or-later — see `LICENSE`. Self-host freely; if you run a modified version as a network service, share
your changes. Built on, and requires, Runalyze. Copyright © 2026 Duarte Rosado. The code was written with
substantial AI assistance under the author's direction — see `AUTHORS.md` for the full, honest provenance.
