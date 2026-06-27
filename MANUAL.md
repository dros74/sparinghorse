# Sparing Horse — Self-Hoster's Manual

A hands-on guide to **running** Sparing Horse (the app) and to **reading what it tells you**. For the
one-paragraph pitch, the feature list and the public-vs-private capability matrix, see the
[README](README.md); this document is the longer how-to that sits behind it.

> **Not medical advice.** Sparing Horse is a training tool, not a medical device. Its readiness gate flags
> stop-the-run / cardiac-type symptoms and tells you to see a doctor — always do. Train at your own risk.

---

## Contents

1. [The mental model](#1-the-mental-model)
2. [Setup](#2-setup)
3. [First run — the five-minute checklist](#3-first-run--the-five-minute-checklist)
4. [Getting your data in](#4-getting-your-data-in)
5. [Setting objectives](#5-setting-objectives)
6. [Reading the dashboard, panel by panel](#6-reading-the-dashboard-panel-by-panel)
7. [The AI layer](#7-the-ai-layer)
8. [Day-to-day and week-to-week](#8-day-to-day-and-week-to-week)
9. [Easing, medical holds and adjustments](#9-easing-medical-holds-and-adjustments)
10. [The privacy model](#10-the-privacy-model)
11. [Settings and secrets](#11-settings-and-secrets)
12. [Troubleshooting](#12-troubleshooting)
13. [Glossary](#13-glossary)

---

## 1. The mental model

Sparing Horse does **not** generate a static plan from a template. It is a *function of your data*: every
night (and on demand) it re-reads your current shape and your objectives and re-derives the **entire road
ahead** — past weeks frozen exactly as you lived them, future weeks re-periodized. Three ideas to hold:

- **It starts from where you actually are.** Pace zones come from your VO₂max, load from your TRIMP history,
  the re-base block from your recent training. Nothing is assumed.
- **The road moves in both directions.** Run well and earn fitness → the plan expands to use it. Run poorly
  or miss sessions → it eases toward an honest goal. Every regeneration is diff-able against the last.
- **Safety is a hard ceiling, not a suggestion.** Every planned week is bounded by an ACWR cap (acute:chronic
  workload ratio ≤ 1.25). Earned "faster build" levers raise *volume targets*, never the ceiling.

You operate it by keeping your data synced and your objectives current. The engine does the rest; the AI
layer (optional) only narrates and parses — it never overrides the deterministic safety logic.

---

## 2. Setup

### Prerequisites

- **Runalyze Premium** + a **Personal API token** — generate at `runalyze.com/settings/personal-api`.
  Sparing Horse reads your activities and Runalyze's computed shape/effort metrics; it does not replace
  Runalyze, it builds on it.
- **Anthropic API key** *(optional)* — enables the natural-language layer. Leave it blank and every AI
  feature stays dormant; the deterministic engine is unaffected.

You can supply both **in the app's Settings window** (recommended — see [§11](#11-settings-and-secrets)); no
file editing is required for day-to-day use.

### Run locally

    pip install -r requirements.txt
    cp .env.example .env          # optional; you can also set the token in Settings later
    python3 SparingHorse.py       # serves http://127.0.0.1:8770

### Run with Docker (the public/private split)

`docker compose` runs the **same image twice off one shared `./data` DB**:

    mkdir -p data && cp .env.example .env
    docker compose up -d --build

| Service | Port | Role |
|---|---|---|
| `sparinghorse` | 8770 | Full read/write, holds the tokens, runs the nightly sync. **Keep it private** — behind a reverse proxy, Cloudflare Access, or a VPN. |
| `sparinghorse-public` | 8771 | `SH_READONLY=1`, **no tokens**, an always-open read-only showcase. |

**Why two containers and not one with a toggle?** The public container literally has no token and a
query-only DB connection, so it *cannot* sync, write, or call the AI even if it wanted to. The split is the
security boundary; see [§10](#10-the-privacy-model).

### Install it as an app (PWA)

Sparing Horse is a Progressive Web App. In any modern browser, open your instance and choose **Install**
(desktop Chrome/Edge: the install icon in the address bar) or **Add to Home Screen** (mobile) — you get a
standalone window/icon with no browser chrome, and an offline app shell so the UI still loads with no
connection (it'll show empty tiles until you're back online; the service worker caches only the shell, never
your data or the API). Nothing to build or sideload; it works on both the private and public instances. On
iOS the home-screen icon is lower-fidelity (Safari doesn't render the SVG app icon) — cosmetic only.

---

## 3. First run — the five-minute checklist

On a fresh database the private dashboard shows a three-step guided card. In order:

1. **Connect Runalyze.** Open **Settings → Connections & keys** and paste your Personal API token. (It is
   stored in a private-only secrets store, never the shared DB — see [§11](#11-settings-and-secrets).)
2. **Pull your history.** Hit **Sync now**, then **Backfill all** once to load your full activity history.
   The first backfill can take a minute or two depending on how many years you have.
3. **Add your first race.** Open the **Objectives** panel and add a goal race (label, date, type, target,
   priority A). With no objective the engine runs in *maintenance* mode — it holds fitness with an easy
   aerobic base and no taper.

The card removes itself once all three are done.

---

## 4. Getting your data in

- **Sync now** pulls recent activities plus today's shape snapshot. **Backfill all** walks your whole
  history (run it once at setup; after that, nightly sync keeps you current).
- **Nightly auto-sync** runs at `SH_SYNC_AT` (default `22:00` in `SH_TZ`). Disable with `SH_SCHEDULE=0`.
- **Duplicates.** If the same activity lands twice (e.g. a re-upload), a banner appears with a link to the
  duplicate. Duplicates are excluded from the de-duplicated model that drives CTL/ATL, so they never quietly
  inflate your fitness.
- **Delete / ignore an activity.** Each activity row has a 🗑 action. Deleting removes the *local* copy only
  (it does not touch Runalyze). A re-sync will **not** silently re-import a row you deleted unless it is
  re-fetched in a backfill window — the app tells you the consequence before you confirm any destructive
  action.

> **Reconstruction vs. snapshot.** CTL/ATL are reconstructed locally from your *running* TRIMP, and also
> arrive as a daily *snapshot* from Runalyze (which includes all sports). The two can differ by a point or
> two at the seam — that is expected (different scopes, different t0), not a bug. Non-running load (a tennis
> match, a bike ride) reaches the plan via the Runalyze snapshot, not via the local running reconstruction.

---

## 5. Setting objectives

Objectives have a **priority**:

- **A** — a goal race. The engine periodizes a full Re-base → Base → Build → Peak → Taper toward it.
- **B / C** — tune-up races. They appear as *tune-ups* before the peak; they do not get their own build.

### One A-race

The standard case: one continuous build whose **final taper week lands on race day** (the calendar is exact
— a race that is not a whole number of weeks out still tapers onto the correct day, not a few days short).

### Several A-races (a chain)

Set two or more A-races and the engine **chains** them into one continuous build: an intermediate peak and
taper for the earlier race, a re-build *bridge* back up, then the peak and taper for the next. Each race's
**role** is decided by how far apart they are:

- **Goal** — the final peak; gets the full peak + taper.
- **Co-equal** — far enough from the next race to hold a real (short) peak of its own.
- **Tune-up (subordinate)** — too close to recover from a full peak, so it gets a one-week sharpen instead.

The **A | B | C** selector and the chain strip in the plan tile let you see and steer this. If you set two
A-races impossibly close, the engine clamps the phases so they can't overrun a race date.

### Target times

Enter a target like `3:30` (marathon/half, `H:MM`) or `21:00` (5k/10k, `MM:SS`), or just `finish`. The
feasibility verdict reads your target honestly: it separates **finish healthy** (realistic off a rebuild)
from a **time target** that the runway's chronic load won't support, and it re-reads this every block as
real fitness returns.

---

## 6. Reading the dashboard, panel by panel

### Current shape
VO₂max, **CTL** (chronic load ≈ your fitness, a slow ~42-day average), **ATL** (acute load ≈ recent fatigue,
a fast ~7-day average), and **ACWR** (ATL ÷ CTL — how hard recent load is relative to your base). ACWR near
1.0 is balanced; the plan keeps every week ≤ 1.25.

### The plan
A phase bar (Re-base → Base → Build → Peak → Taper, plus any chain bridges) over a "weeks to race day" count.
Tap a phase to open its weeks; tap a week to open its sessions. Each week shows planned km, run count, the
projected end-of-week ACWR badge, and — once lived — what you actually ran. Watch for:

- **`clipped to fit ACWR`** — the safety ceiling trimmed that week's volume. Expected on aggressive weeks.
- **Re-base graduation** — bank enough solid weeks and Phase 0 exits a week early (the reward is *time*;
  volumes and the ceiling are unchanged).
- **Earned levers** (opt-in, private): *faster build* (a small ACWR-capped volume bump on hard weeks),
  *earned 6th run* (same volume spread over one more day), and the *CTL volume floor* (volume tracks
  measured fitness once it outruns the default ramp). All protect recovery weeks and hold the ceiling.

### Race chain strip (multi-A only)
When you chain ≥ 2 A-races, a strip shows each race with its **role**, date, **projected race-day CTL**, and
its own **feasibility verdict** — so you see where *each* peak lands, not just the final one. (A single
A-race omits the strip; the headline verdict already covers it.)

### Plan drift / the scorecard
*The road vs. the road as it stands.* Four charts (distance, effort/TRIMP, CTL, race-outcome) compare your
**founding road** (the first plan saved for this goal) to where it stands now, plus a one-line verdict on
three axes: **volume**, **fitness**, and the **race-day projection**.

- For a **multi-A** build the scorecard breaks out **each peak's** founding→now projection and trend, and
  the headline names the **next peak** still ahead.
- Once a race **passes**, the scorecard stops projecting and **reckons**: the fitness you actually arrived
  with vs. what the founding road promised, and your finish vs. your goal (DNF detected). This reckoning is
  **private-only** (your finish time is more than the public "shape + plan" line shows).

### Effort discipline
*Are your easy days actually easy?* HR-led (Runalyze's HR model already internalizes terrain and heat), with
training-effect as corroboration only. A 0–100 easy-discipline score plus per-run verdicts (on / hot / too
hard). Prescribed quality sessions are matched to your runs within ±2 days and excluded from the easy score,
so an anticipated or postponed session isn't misread. The **public** view is sanitized to a pace-based score
with no HR, no critique.

**How "effort" is actually computed.** The app keeps the *prescription* and the *judgment* on different
anchors, on purpose:

- The **plan prescribes pace** (Daniels VDOT zones from your VO₂max). That is what feeds the engine — volume,
  TRIMP load, taper, the ACWR ceiling. None of the HR machinery below touches the plan numbers.
- The **monitor judges heart rate**, anchored on a **derived lactate-threshold HR (LTHR)**, not %HRmax —
  because two runners with the same HRmax can have thresholds 15+ bpm apart, and %HRmax is loosest exactly at
  the easy↔threshold line this score is about. Run zones use Friel's %LTHR grid (Z1 < 0.85 · Z2 0.85–0.89 ·
  Z3 0.90–0.94 · Z4 0.95–0.99 · Z5 ≥ 1.00 · LTHR). An easy run averaging above the Z1/Z2 boundary reads
  *hot*; at/above Z4 (threshold) it reads *too hard*.

**Where the LTHR comes from.** It is estimated from runs you already did — no field test. For a continuous
hard effort (a race, or a tempo with little warm-up/cool-down) the whole-run average HR ≈ LTHR; the app pools
your sustained hard efforts (20–70 min at ≥ 85 % robust HRmax) and takes a spike-resistant high percentile,
with a **confidence** flag that decays as the data ages (LTHR drifts up as fitness returns). With too little
data it falls back to a %HRmax estimate, flagged *provisional*. Known limitation: for *structured* tempos the
warm-up/cool-down dilute the whole-run average, so this method **understates** LTHR — which is why the easy
ceiling is pinned to the conservative (lower) Friel boundary, never a looser one. (A manual LTHR override and
the classic 30-min time-trial protocol are on the roadmap, gated behind readiness so the app never prompts a
maximal test during a restart.)

**Pace vs HR coherence.** Because prescription and judgment are independent estimates, the app cross-checks
them: if your runs done *at* the prescribed easy pace keep landing *above* the easy HR ceiling, your easy pace
is ahead of your current aerobic fitness (classic cardiac decoupling in a rebuild) — the check says so and
tells you to trust HR on easy days. It is a **diagnostic only**; it never silently rewrites the plan.

### Readiness
A daily **green / amber / red** verdict. It flags stop-the-run / cardiac-type symptoms deterministically (no
AI needed to catch them) and, on red, halts the plan and tells you to see a doctor. The public view shows the
*verdict only* — the inputs are redacted.

### Latest running activity + route map
The most recent **running** activity (trail and treadmill count), with a per-point trace (pace / HR / cadence
/ elevation) and a route map. The **map is private-only** (location privacy). If the most recent activity is
a non-run, a private note tells you so.

A thin **HR-zone band** runs along the top of the chart: each section of the run is coloured by the HR zone
you were in (the same Z1–Z5 model used everywhere else — LTHR-anchored when confident, %HRmax otherwise; hover
the HR metric to see the legend and the anchor). Because the zones are Friel-LTHR, a *properly* easy run reads
mostly Z1 (wide by design) with any creep into Z2+ clearly visible — that band *is* your easy-discipline read,
section by section. The per-point HR trace and the band are **private**: on the public box both the HR stream
and the zone model are stripped server-side, so the band simply doesn't render there.

---

## 7. The AI layer

Set `ANTHROPIC_API_KEY` (or add a Claude key in Settings) and four capabilities wake up:

- **Natural-language objectives** — "sub-45 10k in October" → a structured race.
- **Multi-A adjudication advice** — guidance on how to treat closely-spaced A-races.
- **Plain-language plan narration** — "Explain this plan" narrates the engine's numbers (it explains, it
  does not invent — it is fed the computed plan, not asked to design one).
- **Qualitative check-ins** — "knee's a bit sore" → the engine *eases*; it never pushes.

**The guardrail that never bends:** the readiness gate's stop-symptom and medical-hold logic is
deterministic. The AI can soften a routine check-in toward "take it easy", but it **cannot** talk the engine
out of a red/halt. Blank key = every one of these is dormant and the deterministic engine is identical.

---

## 8. Day-to-day and week-to-week

- **Daily:** glance at readiness before a hard session. Log nothing manually — your runs flow in from
  Runalyze on the next sync and attach themselves to the matching prescribed session.
- **Anticipating / postponing:** the engine matches a run to the nearest prescription within ±2 days, so
  running tomorrow's tempo today (or skipping today's easy and doing it tomorrow) is read correctly, not
  flagged as a missed session + a rogue extra one.
- **Weekly:** check the plan drift scorecard. "Ahead on fitness, behind on volume" tells you which lever to
  pull. If you've banked solid weeks, the earned levers offer themselves — opt in if you want them.
- **When you change a goal:** add/remove/re-prioritize in the Objectives panel and regenerate. The drift
  baseline re-anchors to the new goal and self-heals as plans for it accrue.

---

## 9. Easing, medical holds and adjustments

- A **qualitative check-in** ("legs flat", "easy week, travelling") applies a *clamped* load adjustment for a
  bounded window — the engine eases volume, never raises it from a complaint.
- A **medical hold** rests the plan **open-ended** (not on the routine clamp's timer). It stays red + halt
  through its window and past it, until you **explicitly clear it** or a fresh hold replaces it. A later
  routine "feeling better" does *not* lift a medical hold — only an explicit clear does.
- Every easing is shown in the plan (an `eased` tag on affected weeks) and is diff-able against the prior
  version, so nothing changes silently.

---

## 10. The privacy model

The two containers **share one `./data` DB**, so the hard rule is: **anything written to the shared DB is
readable by the public container.** Sparing Horse is built around that constraint:

- **Secrets never touch the shared DB.** Tokens and the Claude key live in a **private-only** secrets store
  (`SH_SECRETS_DB`, default `./secrets`) mounted *only* to the private container. The public box has no
  tokens, full stop.
- **Sensitive endpoints are withheld server-side**, not merely hidden in the UI. On the read-only container
  the route map (GPS), blood/health markers, the per-run HR effort detail, the readiness inputs, and the
  post-race reckoning all return 403 / are sanitized — the public mirror physically cannot serve them.
- **Read-only is enforced at the connection.** The public container uses a query-only DB connection and 403s
  every mutation; it cannot sync, write, delete, or call the AI.

The decision line: *training shape + plan* can be public; *medical / location / HR detail* stays private.

---

## 11. Settings and secrets

The **Settings** window (private container only) is where you configure the app without editing files:

- **Connections & keys** — set your **Runalyze token** and (optional) **Claude API key** here. They are
  written to the private-only secrets store, applied live (no restart), and **write-only**: the UI shows
  whether a key is configured and whether it currently **validates** ("✓ in use · valid" / "✗ key rejected"),
  but never echoes the secret back. The Claude key check uses a zero-token metadata call.
- **Personalization** — athlete context (one line injected into AI prompts), weather cities for the header
  widget, an optional house back-link, and the timezone. These are non-secret and stored in the DB.

> **Deploy note for the Docker split:** the secrets store adds a `./secrets` volume on the **private**
> service. If you adopt it on an existing deployment, recreate the container (`docker compose up -d`, not just
> `restart`) so the new volume mounts.

Anything you'd rather set via environment still works — see the env table in the [README](README.md).

---

## 12. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| "No shape snapshot — Sync first" | You haven't synced yet. Hit **Sync now**, then **Backfill all**. |
| Plan is in *maintenance* mode | No objective set. Add an A-race in the Objectives panel. |
| Fitness looks inflated, a banner mentions a duplicate | A duplicate upload. Open the banner link and resolve it; the de-duplicated model already excludes it from CTL. |
| CTL from the chart ≠ the shape snapshot by a point or two | Expected seam — local running reconstruction vs. Runalyze's all-sport snapshot. Not a bug. |
| AI buttons are disabled / "add a Claude API key" | No Anthropic key. Add one in **Settings → Connections & keys** (optional — the engine runs without it). |
| A trail run didn't reach the plan | Running-family activities (trail/treadmill) are included; check the activity's sport actually matches. Pure non-runs are excluded by design. |
| Public site won't sync | By design — the public container has no token and a query-only connection. Sync from the private instance. |
| Want to verify the engine | Run `python SparingHorse.py selftest` (or `/selftest`, private only) — the deterministic battery, plus the key-gated LLM checks when a key is present. |

---

## 13. Glossary

- **CTL** — Chronic Training Load. A slow (~42-day) average of training load; the app's proxy for *fitness*.
- **ATL** — Acute Training Load. A fast (~7-day) average; the app's proxy for *fatigue*.
- **ACWR** — Acute:Chronic Workload Ratio (ATL ÷ CTL). The injury-risk lever; the plan caps every week ≤ 1.25.
- **TRIMP** — TRaining IMPulse. A single number for a session's load (intensity × duration), the input to CTL/ATL.
- **VO₂max** — Aerobic ceiling, read from Runalyze; drives the prescribed pace zones (Daniels VDOT).
- **LTHR** — Lactate-Threshold Heart Rate. The HR you can hold at the aerobic/anaerobic turnpoint; anchors the
  HR zones and the effort monitor (Friel's run zones are all %LTHR). Derived from your sustained hard efforts,
  with a confidence flag; a %HRmax estimate stands in (provisional) until there's enough data.
- **HR zones (Z1–Z5)** — Friel's %LTHR run grid (Z1 < 0.85 … Z5 ≥ 1.00 · LTHR), shown as the activity-chart
  band and used by the effort monitor. Falls back to a %HRmax grid when LTHR isn't yet confident.
- **Pace↔HR coherence** — a diagnostic that checks whether your prescribed easy *pace* and your easy *HR*
  ceiling agree; flags when easy-paced runs run hot on HR (decoupling). Never alters the plan.
- **Re-base (Phase 0)** — the gentle restart block that re-establishes the easy-aerobic habit before the real build.
- **Founding road** — the first plan saved for your current goal; what the drift scorecard measures "now" against.
- **Reckoning** — the post-race settle-up: fitness you arrived with vs. projected, finish vs. goal (private only).
- **Earned levers** — opt-in, ACWR-capped progressions (faster build / 6th run / faster Phase-0 exit) unlocked by banked solid weeks.

---

*For the change history see [CHANGELOG.md](CHANGELOG.md). For licensing and the honest AI-assisted provenance
see [AUTHORS.md](AUTHORS.md) and the AGPL-3.0 `LICENSE`.*
