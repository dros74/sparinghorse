# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(pre-1.0: the minor version moves for features, the patch version for fixes).

## [Unreleased]

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
