# Sparing Horse — Self-Hoster's Manual

A hands-on guide to **running** Sparing Horse (the app) and to **reading what it tells you**. For the
one-paragraph pitch, the feature list and the public-vs-private capability matrix, see the
[README](README.md); this document is the longer how-to that sits behind it.

> **Not medical advice.** Sparing Horse is a training tool, not a medical device. Its readiness gate flags
> stop-the-run / cardiac-type symptoms and tells you to see a doctor — always do. Train at your own risk.

> **Engine under active development.** The plan engine's heuristics, levers and outputs are still evolving
> and can change between versions. Use its plans as an informed starting point, not a fixed prescription.

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
12. [Backing up your data](#12-backing-up-your-data)
13. [Troubleshooting](#13-troubleshooting)
14. [Glossary](#14-glossary)

---

## 1. The mental model

Sparing Horse does **not** generate a static plan from a template. It is a *function of your data*: every
night (and on demand) it re-reads your current shape and your objectives and re-derives the **entire road
ahead** — past weeks frozen exactly as you lived them, future weeks re-periodized. Three ideas to hold:

- **It starts from where you actually are.** Pace zones come from your VO₂max, load from your TRIMP history,
  the re-base block from your recent training. Nothing is assumed.
- **The road moves in both directions.** Run well and earn fitness → the plan expands to use it. Run poorly
  or miss sessions → it eases toward an honest goal. Every regeneration is diff-able against the last.
- **A plan is the road *ahead*.** Weeks you have already lived are carried verbatim and shown locked
  (🔒) for as long as they are part of the current block — but when a block ends and the road
  re-anchors, the plan starts from the new block and those older weeks are no longer in it. Nothing
  is lost: every plan ever generated is kept, and the parts of the app that judge you against what
  was prescribed (the effort monitor) read from that whole history, not
  from the road currently on screen. The plan document answers "where am I going", not "where have
  I been" — the drift scorecard and the prediction ledger answer that.
- **Safety is a hard ceiling, not a suggestion.** Every planned week is bounded by a hard ACWR cap
  (acute:chronic workload ratio) of **1.30**, with **1.25** the conservative planning target the
  post-illness rebuild sticks to. On the normal (assertive) build the week rides between the two, and
  further injury brakes beyond ACWR engage (long-run-jump cap, a biomechanical load lens, a tissue
  limiter). The plan follows your *measured* form — nothing is unlocked by adherence bookkeeping.

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
- **Watch-recorded health metrics.** Each sync also pulls your daily **HRV (sleeping RMSSD)**, **body weight**
  and **resting HR** from Runalyze into the health-markers charts (a routine sync grabs the last ~60 days; a
  **Backfill all** pulls the full history). These are private (stripped on the public box) and chart against
  your own long-horizon baseline — useful precisely when a watch's short rolling baseline has re-anchored to
  a depressed period. Lab values (triglycerides, cholesterol, etc.) are entered manually and kept local.
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

### After the race — the lifecycle

Once a race date passes, the engine settles it on the next re-plan (or the next look at the
objectives card): it finds the race-day run, marks the objective **done**, and records the result —
finish time and whether your target fell, or the distance you reached on a DNF. If no race-day run
syncs within a few days, the objective **lapses** instead. Settled races move to a **Past races**
strip under the objectives card (private console only — results are personal), and the drift
scorecard keeps reckoning the race for ~12 weeks. The plan itself never trains toward a past date.

### Target times

Enter a target like `3:30` (marathon/half, `H:MM`) or `21:00` (5k/10k, `MM:SS`), or just `finish`. The
feasibility verdict reads your target honestly: it separates **finish healthy** (realistic off a rebuild)
from a **time target** that the runway's chronic load won't support, and it re-reads this every block as
real fitness returns.

### The finish projection

For a marathon the engine also predicts a finish time. **The prediction is a range, not a number** — an
80 % band, headlined as such, with the median underneath as the trend signal. A bare bold time would
claim a precision that does not exist, so the interface does not print one.

The strip answers *what the block buys you*:

```
Projected marathon finish 3:35:10–4:33:05
  off today's shape: 4:28:10  →  by race day: 4:02:45   · the build buys 25 min
```

- **off today's shape** — the same model run at your *current* measured state: today's speed, today's
  chronic load, the longest long actually behind you.
- **by race day** — the same model at the state the laid plan projects you into.
- **the build buys** — the difference, named honestly in both directions. A taper-only runway reads
  *"this runway costs 6 min"*; a flat one reads *"holds today's shape"*.
- Hovering shows the **what-if** the strip used to lead with: if the race were later, on the same
  training, +4 and +8 weeks. That prices a *later race date*, not your timeline — which is why it is a
  footnote now and not the headline.

Both axes move. Projected fitness comes from the plan's own chronic load and the long-run ladder it
builds; projected speed is your measured aerobic profile carried forward through the laid weeks. Both
are re-anchored on what you have actually run at every regeneration, so the projection cannot drift
away from reality — a fast or slow responder bends the curve, but neither can wander off it.

**How wide the band is, and why.** Three things widen it: how variably you have raced before, how few
races the engine has to calibrate on, and how far away the race is. The horizon part is *measured* from
your own history — how far the projection has actually strayed over 2, 4, 8, 19 weeks — not assumed, and
it grows slowly, because fitness is mean-reverting: week 20 adds far less uncertainty than week 2 did.
The band narrows as races land and as race day approaches.

**When it will not answer.** A prediction saved by an older version of the engine is labelled
*"saved by an earlier engine — regenerate to re-read"* rather than presented as current — plans are
versioned artifacts, and updating the app deliberately does not rewrite them. And if your most recent
measured running is older than about eight weeks, the *"off today's shape"* line is withheld entirely,
because a value from before a layoff is not a description of today. Run, and it re-anchors on its own.

**It keeps score.** Every prediction is recorded, and once you race, settled against the result: whether
the outcome fell inside the band, and a proper log score for how well-placed it was. The drift page
plots the whole ledger, so the projection's own track record is visible rather than quietly forgotten.

---

## 6. Reading the dashboard, panel by panel

### Current shape
VO₂max, **CTL** (chronic load ≈ your fitness, a slow ~42-day average), **ATL** (acute load ≈ recent fatigue,
a fast ~7-day average), and **ACWR** (ATL ÷ CTL — how hard recent load is relative to your base). ACWR near
1.0 is balanced; the plan holds every week under a hard **1.30** ceiling — targeting **1.25** on the
conservative rebuild, riding toward 1.30 on the assertive build.

### The plan
A phase bar (Re-base → Base → Build → Peak → Taper, plus any chain bridges) over a "weeks to race day" count.
Tap a phase to open its weeks; tap a week to open its sessions. Each week shows planned km, run count, the
projected end-of-week ACWR badge, and — once lived — what you actually ran. Watch for:

- **Regime badge (conservative / assertive)** — the plan tile shows which posture it's in, with the reason.
  *Assertive* is the normal posture: the plan follows your measured form toward the objective, riding
  the safe ACWR ceiling under the full set of injury brakes. *Conservative* is the **post-illness**
  posture (a Re-base plus a gentle fixed ramp), and it engages only on **body evidence** — a medical
  hold, or a stop-symptom check-in within the last 56 days; it lifts by itself once that window is
  clean. How last week compared to its prescription is *not* a body signal: a travel week, a skipped
  run, a week lived differently never demotes the plan. In the assertive regime a
  week can be flagged **`long-run held (+10%)`** (the long-run-jump cap) or **`fast load eased`** (the
  biomechanical brake trimming a fast-load spike to easy). After a long healthy *break*, the plan
  restarts from a small conservative dose and ramps from there by measurement — no gate, just a floor.
- **`clipped to fit ACWR`** — the safety ceiling trimmed that week's volume. Expected on aggressive weeks.
- **Component chips + the "builds" line** — every quality session carries a small chip naming which of the
  four fitness components it chiefly builds (**VO₂max**, **SSmax/LT2**, **economy**, **resilience** — hover
  a chip for the science), and each phase header sums what the phase is *for*, derived from the sessions
  themselves. On the **assertive** regime the quality mix is periodized by component: a short VO₂ touch
  appears already in Base (develop it early — it's cheap to hold later; the touch is deliberately small so
  it fits the biomechanical budget of a volume rebuild), the mid-week interval session then keeps its full
  size in a *maintenance role* through Build and Peak (it never grows — but shrinking it turned out to
  lower the whole week's safe load, so it stays), while the marathon-pace long-run segment **grows week
  over week at constant speed** (longer, not faster) and Peak pivots to resilience — the long-fast run is
  the workout. The conservative regime keeps its gentler classic mix; you'll just see the tags. An eased
  or fatigue-capped session loses its chip — a session that no longer happens builds nothing, and the app
  won't claim otherwise.

### Race chain strip (multi-A only)
When you chain ≥ 2 A-races, a strip shows each race with its **role**, date, **projected race-day CTL**, and
its own **feasibility verdict** — so you see where *each* peak lands, not just the final one. (A single
A-race omits the strip; the headline verdict already covers it.)

### Plan drift / the scorecard
*The road vs. the road as it stands.* Five charts (distance, effort/TRIMP, CTL, race-outcome, and the
**prediction ledger**) compare your **founding road** (the first plan saved for this goal) to where it
stands now, plus a one-line verdict on three axes: **volume**, **fitness**, and the **race-day
projection**.

- The **prediction ledger** plots every finish prediction the engine has ever made for this goal —
  median plus the 80 % envelope — against the day it was made. Steps in the line are model upgrades or
  your shape moving; both are honest, and both are on the record. Once the race is run, the outcome is
  scored against the prediction standing before it (in the band or not, plus a proper log score) and
  the reckoning names the engine's call.
- For a **multi-A** build the scorecard breaks out **each peak's** founding→now projection and trend, and
  the headline names the **next peak** still ahead.
- Once a race **passes**, the scorecard stops projecting and **reckons**: the fitness you actually arrived
  with vs. what the founding road promised, and your finish vs. your goal (DNF detected). This reckoning is
  **private-only** (your finish time is more than the public "shape + plan" line shows).

### Effort discipline
*Are your easy days actually easy?* Graded against a *moving*, fitness-tracking easy bar — HR-led on a
derived lactate-threshold HR where that's trustworthy, otherwise your grade-adjusted pace vs an
aerobic-threshold (LT1) bar (with an HR-redline backstop either way). A 0–100 easy-discipline score plus per-run verdicts (on / hot / too
hard); each run's `anchor` names the basis it used. Prescribed quality sessions are matched to your runs
within ±2 days and excluded from the easy score, so an anticipated or postponed session isn't misread. A
**rest day is not a session**: a run you take on one isn't matched to it, it's simply held to the easy
bar — the standard it would have been given had it been prescribed. The **public** view is sanitized to a
pace-based score with no HR, no critique.

**Quality sessions get a per-rep read** where the run's detected structure (the "Read back" line) is
available: the verdict is graded on the **work reps only** — warm-up, floats and cool-down excluded —
against the prescribed zone's HR band, tagged **·reps read**. Reps shorter than ~3 minutes are judged
on **pace** vs the zone target instead: a short rep starts rested and HR peaks *into* the recovery
(the pace and HR peaks are out of phase), so a within-rep HR average would under-read every short rep
as sandbagged. Pace also stands in whenever the reps carry no HR. Without a detected structure the whole-run average has to stand in, tagged **·rough read**
(reps blend with recovery, so that verdict is low-confidence by construction).

**How "effort" is actually computed.** The app keeps the *prescription* and the *judgment* on different
anchors, on purpose:

- The **plan prescribes pace** (Daniels VDOT zones from your VO₂max). That is what feeds the engine — volume,
  TRIMP load, taper, the ACWR ceiling. None of the HR machinery below touches the plan numbers.
- The **monitor judges the completed run against a moving easy bar**, on whichever basis your data supports
  (the run's `anchor` field names it):
  - **Heart rate vs a derived LTHR** (`lthr`) — the primary read where a confident, *moving* lactate-threshold
    HR exists, because HR is the honest signal of how easy a run really was. Two runners with the same HRmax
    can have thresholds 15+ bpm apart, and %HRmax is loosest exactly at the easy↔threshold line this score is
    about. Run zones use Friel's %LTHR grid (Z1 < 0.85 · Z2 0.85–0.89 · Z3 0.90–0.94 · Z4 0.95–0.99 · Z5 ≥
    1.00 · LTHR): an easy run averaging above the Z1/Z2 boundary reads *hot*, at/above Z4 (threshold) *too hard*.
  - **Grade-adjusted pace vs a moving LT1 bar** (`lt1_pace`) — when there's no trustworthy LTHR. LT1 (≈80 %
    of 5 k pace) is your aerobic-threshold easy ceiling and *moves* as fitness changes. A merely-elevated HR
    on an easy-*paced* run is deliberately **not** policed (normal decoupling in a rebuild), but an
    **HR-redline backstop** keeps an easy-paced run whose HR sat at threshold+ from ever reading easy.
  - **%HRmax grid** (`hrmax`) — a last-resort read only when there's neither a confident LTHR nor a pace.

  None of this touches the plan numbers — it only grades what you already ran.

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

### Current zones
One table of your training-intent zones — easy / marathon / threshold / interval — with a **pace** window
and an **HR** band per row, both tracking your *current* fitness (private view only; it carries HR). The
two columns are deliberately independent estimators:

- **Pace** comes from your effective VO₂max via the Daniels–Gilbert oxygen-cost curve: zone paces are fixed
  fractions of vVO₂max (marathon 0.81 · threshold 0.88 · interval 0.97), and the easy bar is **LT1** ≈ 80%
  of 5k pace. As your VO₂max moves, every pace moves — the zones are never stale.
- **HR** bands are cut from the same unified zone grid the effort monitor and the activity chart band read
  (Friel %LTHR when a trustworthy LTHR exists — derived or your manual entry — else a %HRmax fallback), so
  the table can never disagree with the effort verdicts: the easy row's top *is* the monitor's easy ceiling,
  and LTHR sits at the top of the threshold band.

Because pace derives from VDOT and HR from LTHR, the columns can visibly disagree while you rebuild
(cardiac decoupling) — the Pace↔HR coherence line above tracks exactly that; on easy days HR is the honest
read. On short intervals the opposite caveat applies: HR lags the effort, so pace leads.

### Readiness
A daily **green / amber / red** verdict. It flags stop-the-run / cardiac-type symptoms deterministically (no
AI needed to catch them) and, on red, halts the plan and tells you to see a doctor. The public view shows the
*verdict only* — the inputs are redacted.

### Latest running activity + route map
The most recent **running** activity (trail and treadmill count), with a per-point trace (pace / HR / cadence
/ elevation) and a route map. The **map is private-only** (location privacy). If the most recent activity is
a non-run, a private note tells you so.

**Read back** — under the metrics, the app narrates what the recorded pace profile *says you did*, in the
plan's own vocabulary: `Intervals — 12min wu @5:50 · 3× 3–4min @5:05–5:15 w/ 60–90s floats · 15min cd @6:30`,
or simply `Easy run — 45min @6:25/km · 2× strides`. Structure is detected from **contrast** (sustained pace
shifts, grade-adjusted so hills don't fake intervals), then each block is named against **your zones as of
that day**. It's a description of what happened, never a judgment — and if the signal is too noisy it says
nothing rather than guess. New runs are read at sync; older runs the first time you open them (in the
`/runs` browser too). The label is pace-based and public-safe; per-segment HR stays private.

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
  pull.
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
- **Away days** (Settings → *Away / can't run*) are the third, structural lever — for when a day
  doesn't exist for training (a flight, a family day), not for when you're struggling. Declare the
  date or range and the plan **re-lays the week around it**: the displaced run slides to the nearest
  sensible day, hard sessions keep their spacing, and the long run takes the last available day. A
  heavily blocked week gets honestly **lighter** — runs that have no legal day are shed *with their
  load*, never crammed into the days around them. Affected weeks carry an `✈ away` tag. Away days
  are **private**: they never appear on the public page in any form (an away date on a public site
  tells the world your house is empty).

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
- **Manual LTHR (bpm)** — your lactate-threshold heart rate from a field test. When set, it overrides the
  data-derived estimate everywhere the app anchors on LTHR (the effort monitor's ceilings, the HR-zone
  band, the LT1 cross-check). It **ages out**: a fresh entry (≤6 weeks) outranks the derived read, then
  decays — LTHR moves with fitness, so an old number from a fitter or less-fit you would mis-anchor your
  easy ceiling. Re-test rather than re-typing the same value (re-saving the same number deliberately does
  not re-freshen it). Empty = derive from your runs (the default).

### The 30-minute field test (LTHR)

The canonical no-lab way to measure LTHR (Friel):

1. Warm up ~10–15 min easy.
2. Run **30 minutes all-out, alone, on flat ground** — a solo time trial, paced like a race (don't start
   too fast; a group or race situation reads ~5 bpm high).
3. Your **average HR over the final 20 minutes** is your LTHR. Enter it under **Settings → Manual LTHR**.

⚠️ **This is a near-maximal effort.** The app only *suggests* the test (a line on the effort panel) when
every clearance holds: your plan is on the assertive regime (never during a post-illness
conservative rebuild), your latest check-in is green (no stop-symptom, not heavy-legged), there is no
medical hold, and the current threshold estimate is actually improvable. If you have any cardiovascular
history, clear max-effort testing with your doctor first — the derived estimate needs no test at all and
self-corrects as you train. Re-test roughly every 6–8 weeks in a building block if you want to keep the
manual anchor fresh; otherwise clear the field and let the data lead.

> **Deploy note for the Docker split:** the secrets store adds a `./secrets` volume on the **private**
> service. If you adopt it on an existing deployment, recreate the container (`docker compose up -d`, not just
> `restart`) so the new volume mounts.

Anything you'd rather set via environment still works — see the env table in the [README](README.md).

---

## 12. Backing up your data

Everything the app knows lives in one SQLite file — but only some of it can be rebuilt. Runalyze can
re-backfill your activities and fitness history any time; what **cannot** be rebuilt is what you put
in yourself: objectives and their race outcomes, readiness check-ins, session reflections,
adjustments, manual lab markers, and the versioned plan history (which carries every prescription
ever laid — the effort monitor reads from it). Back those up.

Two downloads, both in **Settings → Backup & export** (private console only):

- **Database snapshot (.db)** — a complete, consistent copy of the database (safe to take while the
  app runs). Restore: stop the container, drop the file into `./data` as `sparinghorse.db`, start it.
  This is the recommended backup.
- **Data export (.json)** — a portable export of just the non-rebuildable tables. Restore into a
  *fresh* instance with `python SparingHorse.py import <file>` (it refuses to import over existing
  data), then **Sync** + **Backfill all** to rebuild activities from Runalyze.

API keys and tokens are **never** included in either file — they live in a separate secrets store.

---

## 13. Troubleshooting

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

## 14. Glossary

- **CTL** — Chronic Training Load. A slow (~42-day) average of training load; the app's proxy for *fitness*.
- **ATL** — Acute Training Load. A fast (~7-day) average; the app's proxy for *fatigue*.
- **ACWR** — Acute:Chronic Workload Ratio (ATL ÷ CTL). A widely-used (and contested) training-load lever;
  the plan holds every week under a hard **1.30** ceiling, targeting **1.25** on the conservative rebuild.
- **TRIMP** — TRaining IMPulse. A single number for a session's load (intensity × duration), the input to CTL/ATL.
- **VO₂max** — Aerobic ceiling, read from Runalyze; drives the prescribed pace zones (Daniels VDOT).
- **LT1** — Aerobic-threshold *pace* (≈ 80 % of 5 k pace), derived from your current fitness so it *moves* as
  you get fitter or detrain. The effort monitor's easy-pace ceiling when a trustworthy LTHR isn't available.
- **Regime (conservative / assertive)** — the plan's safety posture: *assertive* is the normal,
  form-following build; *conservative* is the post-illness rebuild, entered on body evidence only
  (a medical hold or a recent stop-symptom) and lifted once the 56-day window is clean. No manual
  override, and no adherence bookkeeping.
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
- **Prediction band** — the 80 % range the finish projection is quoted as. Widened by how variably you've
  raced, how few races there are to calibrate on, and how far off race day is; the horizon part is measured
  from your own history rather than assumed. The median under it is the trend signal, not a promise.
- **Prediction ledger** — the running record of every finish prediction made for a goal, and how each one
  scored once the race was run.
---

*For the change history see [CHANGELOG.md](CHANGELOG.md). For licensing and the honest AI-assisted provenance
see [AUTHORS.md](AUTHORS.md) and the AGPL-3.0 `LICENSE`.*
