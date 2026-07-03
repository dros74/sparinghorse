# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(pre-1.0: the minor version moves for features, the patch version for fixes).

> **Status — the training-plan engine is under heavy, active development.** Its behaviour, levers and
> outputs may change between releases as the model matures. Versions are checkpoints on a moving
> target, not a stable API.

## [Unreleased]

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
- **Biomechanical damage weights calibrated to the owner's own history.** The eq_km damage-per-km
  multipliers shipped in 0.8.0 as a literature starting point, with the promise they'd be tuned to real
  data before hardening. A full-history replay (4.7 years, ~1,100 runs, zones tracking fitness at the
  time) kept its promise pointing the other way: the one week in the whole record where a fast-load spike
  coincided with an escalating overuse symptom is caught by this axis (and only this axis — volume
  brakes pass it), but the literature weights would also have falsely eased seven quality weeks the
  owner demonstrably absorbed at his fittest. The weights are now softened (marathon ×1.4, threshold
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
