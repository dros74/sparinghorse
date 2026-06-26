# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(pre-1.0: the minor version moves for features, the patch version for fixes).

## [Unreleased]

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
