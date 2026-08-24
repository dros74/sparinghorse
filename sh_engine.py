"""The plan engine — the deterministic core of Sparing Horse (TECH-12).

Everything here computes; nothing here serves. There is no Flask name in this file, no request, no
route, and — deliberately — no import of the app module: `SparingHorse.py` imports THIS, never the
other way round. That one-way arrow is the point of the split. It means the engine can be read,
reasoned about and exercised on its own, and that a change to the web layer cannot alter a plan.

What lives here is the dependency closure of plan computation, which is a little wider than "the
engine" reads at first glance: the plan is computed FROM THE DATABASE, so the small readers it needs
come with it — `get_meta`/`set_meta` (the rebase anchor is engine state), the settings spec and its
resolver (the engine reads the manual LTHR and the date of birth), `RUN_FAMILY_SQL` (which sports
count as running), the CTL/ATL projector, the §SJ session grouper and the pace/VDOT maths. The app
imports all of them back, so every existing caller — and every `S.<name>` in the battery — still
resolves exactly where it did before. A later slice can lift a store/settings module out of here now
that the boundary is written down; doing it in the same move would have made this one unverifiable.

What deliberately did NOT come: `regenerate`, `save_plan` and `resolve_passed_races` (orchestration
and lifecycle writes — they read `READONLY`, which the battery rebinds on the app module), the LLM
layer, the readiness gate, §TR's scoring, and everything under a route.

⏱ THE CLOCK: functions here take `today=` and must answer to it. `datetime` is this module's own
name, so anything that pins the wall clock has to pin it HERE as well as on the app module —
det/golden-plans and det/clock-purity are what prove the pin reached this file.
"""

import functools
import json
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone


# The engine counts the whole RUNNING FAMILY — Running, Trail Running, Treadmill Running, … — as runs.
# This SQL predicate is the SINGLE source of truth so trail/treadmill runs reach the plan-side run views
# (effort discipline, banking adherence, plan-vs-actual, the block log, weekly mileage, HR) the way they
# already reach the latest-activity tile. The CTL/ATL reconstruction (daily_trimp_series) is all-sport
# already, so broadening here never touches the digit-for-digit-validated fitness model.
RUN_FAMILY_SQL = "LOWER(sport) LIKE '%run%'"


# §PRO14 — the engine's own identity, stamped onto every plan it generates so a SAVED plan can say
# whether the engine that made it is the one now running. Plans are versioned artifacts and a deploy
# never regenerates them (§6f Step E), so after an upgrade the app serves a plan built by code that
# no longer exists — with no way for the view to know. §FT7 half-covered this by sniffing for a
# PRE-§FT payload shape (no band and no `today`), which by construction cannot see a plan that is
# merely one release old: on 2026-07-28 a 0.21.0 plan rendered under 0.21.1 with no marker at all,
# and the owner reasonably read the unchanged numbers as a failed deploy. A shape sniff can only ever
# recognise the breakages it was written for; an identity comparison catches every future one.
# WHY A CONSTANT, not a CHANGELOG parse: the container ships the modules and nothing else (see the
# Dockerfile), so there is no changelog to read at runtime. WHY NOT A SOURCE HASH: it would fire on comment-only
# releases and train the owner to ignore the marker, which is the failure it exists to prevent.
# Drift is prevented instead by `det/engine-version`, which fails the suite whenever this constant
# and the newest CHANGELOG heading disagree — so cutting a release without bumping it cannot pass.
ENGINE_VERSION = "0.40.0"


def _zones_asof(db, date_iso=None):
    """Pace zones AS OF a date — the snapshot VO2max on/just before it, so an old run is read
    against the fitness the runner HAD, not today's. Falls back forward (earliest snapshot) for
    runs predating the history, then to the latest snapshot.
    §PRO22 — returns {} rather than raising when the snapshot table is absent or unreadable. This is a
    read-only lookup used to SCORE history; every caller has a fallback (`_zones_asof(...) or zones`),
    and since §PRO22 put it on the biomechanical hot path it is reached from in-memory fixtures that
    build only the tables they care about. A history read must never be able to take the plan down."""
    import sqlite3 as _sq
    try:
        row = None
        if date_iso:
            row = db.execute("SELECT effective_vo2max FROM shape_snapshots WHERE snapshot_date<=? "
                             "AND effective_vo2max IS NOT NULL ORDER BY snapshot_date DESC LIMIT 1",
                             (date_iso[:10],)).fetchone()
            if not row:
                row = db.execute("SELECT effective_vo2max FROM shape_snapshots WHERE effective_vo2max "
                                 "IS NOT NULL ORDER BY snapshot_date ASC LIMIT 1").fetchone()
        if not row:
            snap = latest_snapshot(db)
            return pace_zones(snap["effective_vo2max"]) if snap else {}
        return pace_zones(row["effective_vo2max"])
    except _sq.OperationalError:      # no such table/column — degrade to the caller's fallback zones
        return {}


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def set_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))


def get_meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


# ── Runtime settings (§ Settings panel) — the spec, and the engine's read of it ───────────────
# Here rather than in the app because the ENGINE reads settings (the manual LTHR, the date of
# birth) and a plan must be computable without importing a web layer. The panel that EDITS them
# is the app's: `current_settings`, `validate_setting`, `save_settings` import this spec back.
# Non-secret personalization a self-hoster can edit live in the private console instead of
# redeploying with new env vars. Stored in `meta` under a `set:` prefix; resolution is
# meta → SH_* env → built-in default. None-vs-"" matters: an ABSENT meta row falls back to env,
# but a row stored as "" is a deliberate clear (NOT a fallback). The effective value lives in the
# same module global each read-site already uses (seeded from env at import, overlaid from meta at
# startup, re-applied on save), so the read-sites stay simple. SECRETS (RUNALYZE_TOKEN,
# ANTHROPIC_API_KEY) are deliberately NOT here — they stay env-only and are never written to the DB.
# Writes are private-only: the public container's _readonly_guard rejects the mutating POST, so the
# panel physically can't be used there.
SETTINGS_SPEC = [
    {"key": "athlete_context", "env": "SH_ATHLETE_CONTEXT", "label": "Athlete context", "kind": "text",
     "help": "Injected into the LLM prompts (e.g. 'masters runner returning from injury'). "
             "The cardiac/exertional-symptom safety net is always on regardless of this text."},
    {"key": "house_url", "env": "SH_HOUSE_URL", "label": "House link — URL", "kind": "url",
     "help": "Optional back-link in the header to your own site (must be http/https). "
             "Empty = no link. Reload to see header changes."},
    {"key": "house_name", "env": "SH_HOUSE_NAME", "label": "House link — label", "kind": "line",
     "help": "Text shown for the header back-link (defaults to the URL)."},
    {"key": "weather_cities", "env": "SH_WEATHER_CITIES", "label": "Weather widget cities", "kind": "line",
     # The Settings panel renders a search-and-pick city widget for this; the stored value is the
     # `Name,lat,lon,CODE;…` string this help describes (still the env/API contract).
     "help": "Header weather-widget cities, stored as Name,lat,lon[,CODE];… No cities = widget hidden."},
    {"key": "tz", "env": "SH_TZ", "label": "Your timezone", "kind": "line", "default": "UTC",
     "help": "IANA zone (e.g. Europe/Luxembourg). This is the clock the whole plan runs on — which "
             "calendar day counts as 'today', and the wall-clock hour the nightly sync fires at. Set "
             "it to where you actually train: a container running UTC while you are two hours ahead "
             "spends every night thinking it is still yesterday. Applies immediately (the nightly "
             "job picks it up on its next cycle)."},
    {"key": "private_url", "env": "SH_PRIVATE_URL", "label": "Private console URL", "kind": "url",
     "help": "The 'Log in' link shown on the PUBLIC page, pointing back to this private console. "
             "Stored here (read from the shared DB) so it survives redeploys; the public container "
             "picks up a change on its next restart."},
    {"key": "manual_lthr", "env": "SH_MANUAL_LTHR", "label": "Manual LTHR (bpm)", "kind": "line",
     "help": "Your lactate-threshold heart rate from a field test (30-min TT: average HR of the final "
             "20 minutes — see the manual). Overrides the data-derived estimate while fresh; it ages "
             "out over weeks because LTHR moves with fitness. Empty = derive from your runs."},
    {"key": "athlete_age", "env": "SH_ATHLETE_AGE", "label": "Age (years)", "kind": "line",
     "help": "Used only as a cold-start PRIOR before real data exists (HRmax ≈ 208 − 0.7×age, "
             "Tanaka); measured heart-rate data takes over as it lands. Empty = no prior."},
]
SETTINGS_BY_KEY = {s["key"]: s for s in SETTINGS_SPEC}


def _resolve_setting(db, spec):
    """Effective (value, source) for one setting in ONE read, so value and provenance can never
    disagree. Precedence: stored meta (`set:<key>`) wins, else the SH_* env var, else the built-in
    default. An ABSENT meta row falls back to env; a stored '' does NOT (it's a deliberate clear).
    An env var that is set-but-empty counts as 'env' (value ''), not 'default'."""
    v = get_meta(db, "set:" + spec["key"])
    if v is not None:
        return v, "saved"
    env = os.environ.get(spec["env"])
    if env is not None:
        return env, "env"
    return spec.get("default", ""), "default"


# ── Fitness/fatigue projector (CTL/ATL impulse-response) ─────────────────────
# The engine's core. Runalyze's `fitness`(CTL)/`fatigue`(ATL) are exponentially-weighted
# moving averages of daily TRIMP over ~42 / ~7 days. We reproduce that math so we can both
# reconstruct history AND roll fitness/fatigue *forward* under a planned training load —
# which is what makes "keep projected ACWR in band" plannable.
#
# Confidence (2026-06-14):
#  - STRUCTURE confirmed against the owner's account: ACWR = ATL/CTL (Runalyze's 0.952 = 20/21
#    exactly), fitness/fatigue are TRIMP EWMAs, and they are whole-body (all sports' TRIMP feed
#    them — cross-training counts).
#  - SPANS are Runalyze's *documented defaults*: ATL=7d, CTL=42d, as a standard N-day EWMA
#    CTL_t = CTL_{t-1}·(1-α) + TRIMP_t·α with smoothing factor α = 2/(N+1) — see _ewma_step below.
#    (blog.runalyze.com/tutorial/runalyze-understanding-the-calculations)
#  - Reconstruction match at today (CTL 20.83/ATL 20.20 vs Runalyze 21/20) is consistent but
#    WEAK proof of τ on its own: he's at a plateau (CTL≈ATL) where ACWR≈1 regardless of τ.
#    The τ values rest on Runalyze's docs, not this single point. RE-VALIDATE as daily snapshots
#    accrue — especially rebuild weeks where CTL and ATL diverge (the discriminating data).
#  - 2026-06-29 — FORMULA CORRECTED. The earlier code used α = 1−e^(-1/N), which is ~half the
#    real weight (CTL 0.0235 vs 0.0465, ATL 0.133 vs 0.25) → reconstruction undershot Runalyze and
#    the gap GREW with load (−1.6→−7.4 CTL over 06-15→06-28 on live data). Refitting against 14 days
#    of settled snapshots spanning rest+impulse days pinned the convention: α = 2/(N+1) with the SAME
#    N=42/7 reproduces BOTH curves to rmse 0.27/0.33 (every day within ±0.5 = the integer-rounding
#    floor). The 0.75/day rest-day ATL decay (64→48, 60→45) is TRIMP-independent and unambiguous.
#  - 2026-06-21 — VALIDATED on live production data at a divergent point: NAS reconstruction
#    CTL 23.98 / ATL 31.5 vs Runalyze 26 / 33 on a day with ATL≫CTL — real proof of the spans, not a
#    plateau coincidence (errors quoted were under the old factor; the spans 42/7 were right). Caveat on the self-test, not the model:
#    the latest snapshot is dated today while the last run was a day or two earlier, so it LEADS
#    the activity frontier. On a rest lead-day that's harmless (pure decay); but if you run and
#    haven't synced, the snapshot reflects a session the reconstruction lacks → a malformed
#    comparison that false-fails (this is exactly what a stale local copy showed: ATL err −14.33,
#    a phantom run reconciled by a single impulse fitting CTL and ATL at once — a data-coverage
#    artifact, never a model error). `_stc_projector` (§6k) therefore validates only LIKE-FOR-LIKE
#    (settled rest-day snapshots behind the frontier). Same day-ahead seam the §6j scorecard de-seams.
#  - Caveat: "default" — if the owner changed his Runalyze calc settings, confirm and adjust.
TAU_CTL = 42  # days, "fitness" (CTL) EWMA span — Runalyze default
TAU_ATL = 7   # days, "fatigue" (ATL) EWMA span — Runalyze default


def _ewma_step(prev, value, span):
    """One day of Runalyze's CTL/ATL exponential moving average. `span` is the N-day window
    (42 fitness / 7 fatigue); the smoothing factor is the standard EWMA α = 2/(N+1) — NOT
    1−e^(-1/N). Validated against live snapshots 2026-06-29 to rmse 0.27/0.33 (see the block above)."""
    return prev + (value - prev) * (2.0 / (span + 1.0))


def find_duplicates(db):
    """Likely-duplicate activities: same timestamp + distance + sport but different ids
    (e.g. a watch/Strava double-upload). Returns the list of duplicate ids to drop (keeps
    the lowest id of each group). Such dups inflate Runalyze's own fitness/fatigue too."""
    rows = db.execute(
        "SELECT id, date_time, distance, sport FROM activities WHERE date_time IS NOT ''"
    ).fetchall()
    groups = {}
    for r in rows:
        key = (r["date_time"], round(r["distance"] or 0, 2), r["sport"])
        groups.setdefault(key, []).append(r["id"])
    drop = []
    for key, ids in groups.items():
        if len(ids) > 1:
            drop += sorted(ids)[1:]  # keep the first, drop the rest
    return drop


def manual_ignores(db):
    """Activity ids the owner has manually flagged (near-dups / mis-tags the exact-match
    heuristic misses). Persisted in `ignored_activities`."""
    return {r["id"] for r in db.execute("SELECT id FROM ignored_activities").fetchall()}


def dropped_ids(db):
    """Every activity id excluded from the owned reconstruction: auto-detected exact
    duplicates ∪ the owner's manual ignore-list. The single source of truth for de-dup —
    every projector/actuals consumer drops this set."""
    return set(find_duplicates(db)) | manual_ignores(db)


def daily_trimp_series(db):
    """{YYYY-MM-DD: summed TRIMP} across ALL sports (Runalyze's CTL/ATL are whole-body).
    Skips likely-duplicate activities so our reconstruction isn't double-counted."""
    drop = dropped_ids(db)
    out = {}
    for r in db.execute(
        "SELECT id, date, trimp FROM activities WHERE date IS NOT '' AND trimp IS NOT NULL"
    ).fetchall():
        if r["id"] in drop:
            continue
        out[r["date"]] = out.get(r["date"], 0.0) + (r["trimp"] or 0.0)
    return out


def _date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def roll(daily, start, end, ctl0=0.0, atl0=0.0):
    """Walk start..end inclusive day by day, applying each day's TRIMP (0 on rest days).
    Returns a list of {date, trimp, ctl, atl, tsb, acwr} — the daily impulse-response curve."""
    from datetime import timedelta
    ctl, atl = ctl0, atl0
    series, cur = [], start
    while cur <= end:
        t = daily.get(cur.isoformat(), 0.0)
        ctl = _ewma_step(ctl, t, TAU_CTL)
        atl = _ewma_step(atl, t, TAU_ATL)
        series.append({
            "date": cur.isoformat(), "trimp": round(t, 1),
            "ctl": round(ctl, 2), "atl": round(atl, 2),
            "tsb": round(ctl - atl, 2), "acwr": round(atl / ctl, 3) if ctl else None,
        })
        cur += timedelta(days=1)
    return series


def reconstruct_history(db, end=None):
    """Reconstruct the fitness/fatigue curve from the first activity to `end` (today)."""
    daily = daily_trimp_series(db)
    if not daily:
        return []
    end = _date(end) if end else datetime.now().date()
    return roll(daily, min(_date(d) for d in daily), end)


def latest_snapshot(db):
    """The most recent Runalyze shape snapshot row (all columns), or None."""
    return db.execute(
        "SELECT * FROM shape_snapshots ORDER BY snapshot_date DESC LIMIT 1"
    ).fetchone()


# ── §PRO20 — the plan's seed is END-OF-YESTERDAY, not "today's snapshot" ──────────────────────
# `generate_block` rolls the projection from `today` INCLUSIVE — its own docstring states the
# contract: "only TODAY-ONWARD days are governed and projected from today (model A — no
# double-count)". That needs a seed that STOPS at the end of yesterday. Runalyze's snapshot for
# `today` does not: it has already advanced the EWMA through today with whatever TRIMP had landed
# when it was captured. So today gets applied TWICE — once by Runalyze, once by the roll.
#
# MEASURED on his live DB (2026-07-30), exact to the decimal — this is arithmetic, not inference:
#   07-28 settled at ATL 80. The 07-29 snapshot was captured 20:00Z, BEFORE that evening's run, and
#   read ATL 60 == _ewma_step(80, 0, 7) — i.e. today already applied as a REST day. Plan #70 was
#   seeded from it and the week's allowance came out 50.9 km (laid 47.3, with a 9.4 km Sunday long);
#   seeded from the settled state the same code allows 25.4 km. A 22-point ATL under-read doubled
#   the week, and he was then told he had "over-run" it.
#   The settled 07-29 snapshot later read ATL 82 == _ewma_step(80, 88, 7) with his real 88-TRIMP run
#   — and OVERWROTE the row (one row per day), erasing the evidence. `plans.inputs` is the only
#   surviving record of what a plan was actually built from.
# 13 of the 41 plans generated in July 2026 were built from an ATL the same-day snapshot later
# contradicted by 17–30 points — EVERY ONE in the same direction (seed low ⇒ ACWR low ⇒ week
# inflated). Never once the other way. A systematic bias, not sync noise.
# Post-run it fails the other way rather than not at all: a snapshot captured after the day's run
# already holds that load, and the roll then re-applies today, so the projection reads the week
# freer than it is (plan #71: wk1 proj_acwr 0.561, decaying a 93-TRIMP day it had already counted).
#
# FIX: seed from the newest snapshot dated STRICTLY BEFORE today, rolled forward over the locally
# known daily TRIMP to end-of-yesterday. Independent of WHEN any snapshot was captured, so the whole
# class dies rather than the one case — no clock races, no "is the day settled yet" heuristic.
# ⚠ Direction of the correction is NOT uniformly safe-ward, so it is measured, never assumed: pre-run
# it RAISES the seed ATL (tightens the governor), post-run it lowers it by one day's decay while also
# stopping the roll from double-decaying that day. See PROJECT_LOG §55 for the measured before/after.
# eVO₂max deliberately still comes from the NEWEST snapshot: it is a fitness read, not a load EWMA
# the roll re-applies, so the freshest is the right one — and pace zones (a pure function of it)
# stay put, keeping this change confined to the load axis.
def plan_seed(db, today):
    """§PRO20 — the (vo2, ctl0, atl0, meta) a plan generated on `today` must be seeded from: the load
    state at the END OF YESTERDAY, which is what generate_block's roll-from-today-inclusive needs.

    A missing snapshot day (a failed sync) is BRIDGED by measurement — rolled forward over the local
    daily TRIMP with the same `_ewma_step` the projector is validated on — never by reading an older
    row as if it were "now". `meta` records the day seeded from and how many days were bridged, so the
    plan can say so instead of asserting freshness it can't know.

    Returns None when there is no snapshot at all (the caller's §FT5 cold-start path owns that case).
    When there is no snapshot dated BEFORE today (a first day / fresh install), falls back to the
    newest row verbatim — the pre-§PRO20 behaviour — and says so in meta["fallback"]."""
    newest = latest_snapshot(db)
    if not newest:
        return None
    vo2 = newest["effective_vo2max"]
    prior = db.execute(
        "SELECT snapshot_date, fitness, fatigue FROM shape_snapshots WHERE snapshot_date < ? "
        "ORDER BY snapshot_date DESC LIMIT 1", (today.isoformat(),)).fetchone()
    if not prior:
        return (vo2, newest["fitness"] or 0.0, newest["fatigue"] or 0.0,
                {"from": newest["snapshot_date"], "bridged_days": 0,
                 "fallback": "no snapshot before today — seeded from the newest row"})
    ctl, atl = prior["fitness"] or 0.0, prior["fatigue"] or 0.0
    seeded_from, yday = _date(prior["snapshot_date"]), today - timedelta(days=1)
    bridged = 0
    if seeded_from < yday:
        daily = daily_trimp_series(db)
        cur = seeded_from + timedelta(days=1)
        while cur <= yday:
            t = daily.get(cur.isoformat(), 0.0)
            ctl, atl = _ewma_step(ctl, t, TAU_CTL), _ewma_step(atl, t, TAU_ATL)
            cur, bridged = cur + timedelta(days=1), bridged + 1
    return (vo2, ctl, atl,
            {"from": prior["snapshot_date"], "bridged_days": bridged, "fallback": None})


def project_forward(planned, ctl0, atl0, start_date):
    """Engine-facing: roll fitness/fatigue FORWARD under a planned load.
    `planned`: {YYYY-MM-DD: TRIMP} for future days (missing days = rest = 0). Seeds from
    today's observed CTL/ATL (`ctl0`/`atl0` — use Runalyze's authoritative values). Returns
    the projected daily curve so the engine can keep projected ACWR inside the 0.8–1.3 band."""
    if not planned:
        return []
    start = _date(start_date)
    end = max(_date(d) for d in planned)
    return roll(planned, start, end, ctl0=ctl0, atl0=atl0)


# ── §SJ split sessions ("1+1") — several recordings, one session (PROJECT_LOG §30) ─────
# The owner deliberately records a mixed session as separate parts (easy body saved, fresh recording
# for the strides) so neither part pollutes the other's numbers — the right instinct watch-side; the
# engine must read the parts back as ONE session. Groups are DERIVED at read time (a pure function
# over one day's owned rows) — activity rows are Runalyze's raw truth and are never merged at rest.
SJ_MAX_GAP_MIN = 30   # a save-and-restart between parts is minutes; a real morning/evening double is
#                       hours apart. Overlapping recordings NEVER join (same-instant duplicate-source
#                       pairs exist in his history under different TZ spellings — a restart starts
#                       after the previous part ended).
SJ_PART_MIN_S = 180   # a grouped part's own §RD floor: the GROUP supplies the context RD_MIN_RUN_S


def _sj_col(r, key):
    """Tolerant column read for sqlite3.Row/dict mixes (minimal det fixtures omit columns)."""
    try:
        return r[key]
    except (KeyError, IndexError):
        return None


def _session_groups(rows):
    """§SJ — split-session ("1+1") grouping: owned run rows → time-ordered groups, each group one
    LOGICAL session. Pure + deterministic (testable without a DB). Rules:
      • only same-DATE rows group (cross-midnight is a non-goal);
      • within a date, rows sort by (date_time, id) and consecutive rows JOIN when the recording gap
        0 ≤ start(next) − end(prev) ≤ SJ_MAX_GAP_MIN — end from elapsed_time (wall clock), falling
        back to duration; chains (wu + reps + cd as three recordings) join transitively;
      • a NEGATIVE gap (overlap) never joins: a restart starts after the previous part ended —
        overlap means duplicate-source rows (his 2023 history has same-instant pairs under
        different TZ spellings);
      • a blank/unparseable date_time never joins (the dedup blank-timestamp posture), nor does a
        naive/aware timestamp mix (the gap is not computable — honesty over guessing).
    Callers drop-filter (ignored/deleted) BEFORE grouping so an ignored part leaves cleanly.
    Returns a list of groups (each a time-ordered list of the input rows), dates ascending."""
    from datetime import datetime as _dtt

    def ts(r):
        v = _sj_col(r, "date_time")
        try:
            return _dtt.fromisoformat(v) if v else None
        except ValueError:
            return None

    by_date = {}
    for r in rows:
        if _sj_col(r, "date"):
            by_date.setdefault(r["date"], []).append(r)
    out = []
    for d in sorted(by_date):
        day = sorted(by_date[d], key=lambda r: (_sj_col(r, "date_time") or "", _sj_col(r, "id") or 0))
        cur = [day[0]]
        for nxt in day[1:]:
            prev = cur[-1]
            pt, nt = ts(prev), ts(nxt)
            gap = None
            if pt and nt:
                try:
                    gap = (nt - pt).total_seconds() - \
                          (_sj_col(prev, "elapsed_time") or _sj_col(prev, "duration") or 0)
                except TypeError:                      # naive vs tz-aware mix — not computable
                    gap = None
            if gap is not None and 0 <= gap <= SJ_MAX_GAP_MIN * 60:
                cur.append(nxt)
            else:
                out.append(cur)
                cur = [nxt]
        out.append(cur)
    return out


# ── Plan engine v1 (deterministic; §6) ──────────────────────────────────────
# Owns the numbers. Pace zones from effective VO2max (Daniels VDOT — validated to
# reproduce Runalyze's 5k prognosis exactly), session load estimated as TRIMP, weekly
# progression bounded so projected ACWR stays under the soft cap. The LLM layer (later)
# only proposes adjustments the engine then clamps to these guardrails.
ACWR_SOFT = 1.25   # planning target ceiling (margin under the hard limit)
ACWR_HARD = 1.30   # never exceed (the model has error near the boundary, §6a-bis)
# §PRO8 — low-CTL soft-ceiling floor [[governor-lever-retune]]. ACWR is a RATIO (ATL/CTL): at low
# chronic load its denominator is tiny, so the settled end-of-week ratio becomes hypersensitive and the
# SOFT ceiling permits only ~maintenance load — riding it (assertive) plateaus CTL near its current value
# instead of building (verified on live.db: even full-ceiling assertive topped at CTL ~31 / 27 km, the
# plan projecting the athlete LESS fit on race day). The fix: when judging the SOFT (end-of-week, settled)
# ceiling only, floor the CTL denominator at this value — the chronic-load level below which the ACWR
# soft signal is unreliable. This lets the settled-week ceiling rise toward demonstrated tolerance.
# CRITICAL — it ONLY touches the soft eow decision: the in-week PEAK hard cap (ACWR_HARD) and
# CTL_RAMP_MAX stay on the RAW CTL, so they remain the true acute-spike + chronic-growth brakes (verified:
# under the floor, real ACWR rides UP TO 1.30 — the hard cap — and no further; it is the new binding
# ceiling). Displayed/historical ACWR stays RAW (honest). OPT-IN: only the live ASSERTIVE plan passes it
# (caution stays byte-identical, default off ⇒ every constructed test is unchanged). Owner-approved
# 2026-06-30 (the masters/post-illness acute safety = the raw peak + ramp, both preserved).
ACWR_SOFT_CTL_FLOOR = 45.0
# §PRO10 — progressive-overload floor on the ASSERTIVE ceiling (2026-07-23, owner-approved: "I can't
# see the logic of a plan that establishes a peak CTL this far from the race"). The ceiling is STATE-
# based (a ratio of the carried CTL), so riding it has a fixed point: at his chronic load the soft
# test allows ~maintenance+ε, down weeks hand the ε back, and the 19 projected weeks Aug→Nov drew CTL
# 44→45 — a plan honestly claiming he won't get fitter (verified on plan 58: every non-down week
# pinned at eow_soft≈1.25 against the floored denominator, i.e. ATL_end ≈ 1.25×ACWR_SOFT_CTL_FLOOR,
# CTL equilibrium ≈ the floor itself; §PRO8 raised the old ~31 fixed point to ~45, it didn't remove
# it). The fix: a BUILDING week's allowance may not be soft-clipped below (1+PROG_RAMP)× the last
# realised non-down week's load — progressive overload as a floor on the SOFT test only. The acute
# brakes are UNTOUCHED and always clip the floor: in-week PEAK ≤ ACWR_HARD on RAW CTL, CTL_RAMP_MAX,
# §PRO6 forced deloads (near-ceiling weeks still count and still force recovery every
# MESO_MAX_HARD+1), §PRO9 long +10%, §3.1 bio cap. So growth = min(6%/wk, what the hard caps afford)
# — compounding, not equilibrium, with every safety line intact. Assertive building weeks only
# (base/build/bridge; peak trims into specificity by design, taper is a deliberate drop, down/forced-
# deload weeks keep their troughs — which themselves rise as last_nondown rises). Caution passes no
# floor ⇒ byte-identical. Weeks the floor actually lifted carry `prog_ridden` (honest label: the
# drawn trajectory assumes continued clean absorption).
PROG_RAMP = 0.06           # ≥6%/wk over the last realised non-down load — the classic conservative
#                            progression band (his absorbed-but-unproductive June ramp ran ~26%/wk;
#                            eVO₂ stayed flat — that audit is why this is 6, not 10)
EASY_TRIMP_PER_MIN = 1.3   # calibrated from his easy runs (HR≤135 → ~1.1–1.5/min)
EASY_PACE_FRAC = 0.72      # fraction of vVO2max for easy running (top of the easy zone; sits just under LT1)

# §3.4 — LT1 (aerobic threshold) as the PACE-anchored easy bar (ENGINE_SCIENCE.md §3.4 + the §6.3 decision:
# pace is the intensity anchor, HR the cross-check). Davis: LT1 ≈ 80% of 5k PACE (velocity), and easy runs
# must sit BELOW LT1. We derive it from the CURRENT effective VO2max so the bar MOVES with fitness (a
# detrained rebuild gets a slower LT1, not a stale fast one — the fix for §3.4's "fitness-tracking" ask).
V5K_VVO2MAX_FRAC = 0.95    # 5k velocity ≈ this × vVO2max (Daniels; a masters 5k runs a touch under vVO2max)
LT1_5K_FRAC = 0.80         # Davis: LT1 velocity ≈ 80% of 5k velocity → LT1 ≈ 0.76·vVO2max (easy < LT1 < MP)
MARATHON_PACE_FRAC = 0.81  # fraction of vVO2max at marathon pace — ONE definition, shared by the
                           # displayed zone grid (rounded to sec/km) and §FT1's speed axis (unrounded,
                           # §33f-11): the two must never drift apart, they describe the same pace

# (§FORM1 2026-08-18 — the §6e banked-week machinery is GONE: graduation, the earned volume lift,
# the 6th-run unlock, and the regime's banked-streak clause all judged plan OBEDIENCE, not the body.
# A travel week — 30.1 km run and cleanly absorbed against a 5-run lay — zeroed a 3-week streak and
# collapsed the assertive road into a 13 km/wk detraining re-base (live, 2026-08-18). The owner's
# ruling: the plan follows MEASURED form toward the objective; conservative posture is entered on
# body evidence only (medical hold / stop-symptom — see training_regime). Volume responsiveness
# lives in §PRO5's measured-vs-projected ride; frequency spreads live in §PRO9; nothing is "earned"
# by adherence bookkeeping any more.)

RUN_MIN_KM = 2.5   # §JR — no prescribed run below this (owner call 2026-07-05, after the ACWR brake


def _vo2_at_v(v):  # Daniels: VO2 cost (ml/kg/min) at velocity v (m/min)
    return -4.60 + 0.182258 * v + 0.000104 * v * v


def _v_at_vo2max(vo2max):  # velocity (m/min) at VO2max
    lo, hi = 100.0, 500.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if _vo2_at_v(mid) > vo2max:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def pace_zones(vo2max):
    """Training pace zones (sec/km) from effective VO2max, as fractions of vVO2max."""
    if not vo2max:
        return {}
    vv = _v_at_vo2max(vo2max)
    # §3.4 — "lt1" = the aerobic-threshold pace (Davis: 80% of 5k pace); "p5k" = the 5k-pace equivalent.
    # Both are fractions of vVO2max, so they track fitness like the rest of the grid (easy < lt1 < marathon).
    frac = {"easy": 0.70, "easy_top": EASY_PACE_FRAC,
            "lt1": V5K_VVO2MAX_FRAC * LT1_5K_FRAC, "p5k": V5K_VVO2MAX_FRAC,
            "marathon": MARATHON_PACE_FRAC, "threshold": 0.88, "interval": 0.97}
    return {k: round(1000.0 / (vv * f) * 60) for k, f in frac.items()}  # sec/km


def fmt_pace(sec):
    return f"{int(sec // 60)}:{int(sec % 60):02d}" if sec else "—"


def est_trimp(minutes, zone="easy"):
    """Estimate a session's TRIMP from duration + intensity zone (calibrated from his data)."""
    per_min = {"easy": EASY_TRIMP_PER_MIN, "marathon": 1.8, "threshold": 2.6,
               "interval": 3.2, "long": 1.4}.get(zone, EASY_TRIMP_PER_MIN)
    return round(minutes * per_min, 1)


def weeks_until(d, today=None):
    today = today or datetime.now().date()
    return max(0, (_date(d) - today).days // 7)


def _plan_span(block_start, race_date):
    """Monday-anchored weeks from `block_start` THROUGH the week that contains `race_date` (1-based,
    inclusive) — the calendar length the plan must span so its final taper week lands ON race day.
    The plan is laid contiguously in whole Mon–Sun weeks from `block_start`, but `weeks_until(race,
    today)` counted from *today* and floored the remainder, so the plan ended ~1–2 weeks short and the
    race fell in an un-generated week. Anchoring to the same grid the weeks are laid on, and including
    the race's own week, closes that gap (the extra weeks land in the building phases; the taper keeps
    its length). `block_start` is a date; `race_date` a date or ISO string."""
    rd = _date(race_date) if isinstance(race_date, str) else race_date
    return max(0, (rd - block_start).days // 7 + 1)


def periodize(today, race_date, rebase_weeks=6):
    """Reverse periodization: split the runway into Phase-0 re-base → Base → Build →
    Peak → Taper. Phase lengths scale with the weeks available."""
    total = weeks_until(race_date, today)
    taper = 3 if total >= 16 else 2
    remaining = max(0, total - rebase_weeks - taper)
    base = round(remaining * 0.45)
    build = round(remaining * 0.40)
    peak = max(0, remaining - base - build)
    phases = [("Re-base (Phase 0)", rebase_weeks), ("Base — aerobic", base),
              ("Build — specific", build), ("Peak / sharpen", peak), ("Taper", taper)]
    return [{"phase": n, "weeks": w} for n, w in phases if w > 0], total


FULL_PEAK_ROLES = ("goal", "coequal")   # roles that earn a full peak + full taper (vs subordinate)


def _full_peak(role):
    return role in FULL_PEAK_ROLES


def _seg_taper(total, full):
    """Taper weeks for one segment: a full taper (3 wk on a long runway, else 2) for a goal/co-equal
    peak; a 1-week sharpen for a subordinate race (it doesn't get a full peak it can't recover from).
    Never longer than the segment itself (so a short gap can't overrun the race date)."""
    return min((3 if total >= 16 else 2) if full else 1, max(0, total))


def periodize_chain(today, chain, rebase_weeks=6, block_start=None):
    """§6q — reverse-periodize the whole A-race CHAIN into a flat phase list. Each phase carries a
    unique `key` (what generate_plan stores its block under + the UI selects), a `kind` (which shaper
    builds it), and the `race`/`role` it serves. Segment 0 is the full Re-base→Base→Build→Peak→Taper
    toward the first race; each later race adds a re-build BRIDGE→Peak→Taper off the prior race. A
    subordinate race gets peak=0 + a 1-week sharpen instead of a full peak. Returns (phases,
    total_weeks). For a single goal race this REDUCES to periodize() (same kinds + same week counts;
    the Peak/Taper names just gain the race label). `chain` is select_chain()'s first return.

    When `block_start` (a date) is given, each segment's week count is anchored to that Monday grid and
    is INCLUSIVE of the race's own week (`_plan_span` / cumulative deltas), so the plan laid contiguously
    from block_start lands every race week ON race day — instead of the old today-floored count that
    ended ~1–2 weeks short. With block_start None it keeps the legacy today-anchored counts (the
    single-race oracle path used by the reduction self-test). The returned total_weeks stays the
    intuitive `weeks_until(final_race, today)` for the 'weeks away' display either way."""
    # Cumulative Monday-week index (from block_start) of the week containing a race — segment counts are
    # deltas of these, so the contiguous layout hits each race's week exactly.
    cum = (lambda d: _plan_span(block_start, d)) if block_start is not None else None

    def seg0_split(total, full):
        taper = _seg_taper(total, full)
        rem = max(0, total - rebase_weeks - taper)
        base = round(rem * 0.45)
        if full:
            build = round(rem * 0.40)
            peak = max(0, rem - base - build)
        else:                                  # subordinate: no peak, the build runs longer
            build = max(0, rem - base)
            peak = 0
        return base, build, peak, taper

    r0 = chain[0]
    lbl0, role0 = r0.get("label", "race"), r0["role"]
    total0 = cum(r0["date"]) if cum else weeks_until(r0["date"], today)
    base, build, peak0, taper0 = seg0_split(total0, _full_peak(role0))
    phases = [
        {"phase": "Re-base (Phase 0)", "weeks": rebase_weeks, "kind": "rebase", "key": "rebase", "race": None, "role": None},
        {"phase": "Base — aerobic", "weeks": base, "kind": "base", "key": "base", "race": lbl0, "role": role0},
        {"phase": "Build — specific", "weeks": build, "kind": "build", "key": "build", "race": lbl0, "role": role0},
        {"phase": f"Peak → {lbl0}", "weeks": peak0, "kind": "peak", "key": "peak", "race": lbl0, "role": role0},
        {"phase": f"Taper → {lbl0}", "weeks": taper0, "kind": "taper", "key": "taper", "race": lbl0, "role": role0,
         "type": r0.get("type")},   # §TT — the taper needs to know WHICH race it freshens for
    ]
    for k in range(1, len(chain)):
        rk, prev = chain[k], chain[k - 1]
        lblk, rolek = rk.get("label", "race"), rk["role"]
        fullk = _full_peak(rolek)
        totalk = max(0, cum(rk["date"]) - cum(prev["date"])) if cum else weeks_until(rk["date"], _date(prev["date"]))
        taperk = _seg_taper(totalk, fullk)        # clamped ≤ totalk → segment never overruns the race
        remk = max(0, totalk - taperk)
        peakk = min(2, remk) if fullk else 0      # short inter-race sharpen; fitness is held, no new base
        bridgek = max(0, remk - peakk)            # taperk + peakk + bridgek == totalk (no calendar drift)
        phases += [
            {"phase": f"Bridge → {lblk}", "weeks": bridgek, "kind": "bridge", "key": f"bridge{k}", "race": lblk, "role": rolek},
            {"phase": f"Peak → {lblk}", "weeks": peakk, "kind": "peak", "key": f"peak{k}", "race": lblk, "role": rolek},
            {"phase": f"Taper → {lblk}", "weeks": taperk, "kind": "taper", "key": f"taper{k}", "race": lblk, "role": rolek,
             "type": rk.get("type")},   # §TT — per-segment: each taper sharpens at ITS race's pace
        ]
    return [p for p in phases if p["weeks"] > 0], weeks_until(chain[-1]["date"], today)


# Re-base block targets (§6d, a conservative masters/returning-runner default): a GENTLE
# build — the re-base maintains/lightly builds and re-establishes the easy-aerobic habit; real
# CTL-building is the Base phase. Volumes chosen so end-of-week ACWR sits ~1.0–1.18 and only the
# final/biggest week grazes the soft cap. Week 4 is a genuine down week.
#
# LONG_RUN_MAX_FRAC: the long run's max share of weekly volume. Raised 0.35→0.50 (2026-06-20) after
# reading the owner's OWN history — his real long runs ran at median 0.33 / p75 0.40 / p90 0.50 of the
# week, and 44% of his training weeks exceeded 0.35, so the old cap suppressed the cornerstone marathon
# session below what he actually (and safely) trained. This lifts the *ceiling* and fattens the early
# long runs; it does NOT front-load the timeline to his fitter-era comeback rate — the long run stays
# CTL-gated by the same EOW ACWR governor (his big 18–30km long runs rode CTL 80–100; off today's
# CTL ~24 the safe peak long run is ~12–13km, which his own data confirms — his 22–35km runs ran at
# ACWR 1.26, right at the 1.25 cap). Safety is unchanged: only the EOW ACWR governor bounds load, and
# it's untouched. NOTE (2026-06-20): "no explicit intraweek-peak-ACWR guard is needed *because the
# long run is CTL-limited to ~12km here*; if this is ever combined with the volume push the long run
# grows and that unguarded spike reopens — add a peak guard then."
# ✅ THAT CONDITION HAS SINCE ARRIVED, AND THE GUARD EXISTS (audited 2026-07-28). Off CTL 51→58 the
# long run now reaches ~17.5 km, well past the ~12 km this note assumed. `_max_week_trimp` bounds
# in-week PEAK ACWR ≤ ACWR_HARD (§H1) as well as EOW ≤ cap, and it BINDS: across the block the peak
# tops out at exactly 1.300 = ACWR_HARD. So the guard this note asks a future reader to add is
# already in place and holding — do not add a second one. What is worth watching instead is that
# §PRO10's progression floor legitimately rides weeks PAST the 1.25 soft cap up to that hard cap, so
# the hard cap is the cruising altitude on building weeks, not a rare backstop (3 up : 1 down).
# ✅ SETTLED 2026-07-28 — THE OWNER'S EXPLICIT CALL: "I want to cruise the hard cap." So this is the
# intended posture, not drift, and NO code change was made: §PRO10 already rides to ACWR_HARD and
# must keep doing so. Do not "restore" the soft cap as the assertive ceiling — that would silently
# revoke a decision he made with the numbers in front of him (building weeks measured at 1.296–1.300
# against soft 1.25 / hard 1.30, on a 3-up-1-down cadence with down weeks falling to ~1.12–1.18).
# THE CONSEQUENCE HE OWNS, stated plainly so nobody has to rediscover it: with the backstop adopted
# as the target, there is no longer any ACWR headroom ABOVE normal operation. ACWR_HARD is now both
# the ceiling and the aim, so it can no longer also serve as the margin that absorbs a bad week —
# any future emergency brake has to come from a different axis (readiness, §PRO5's ride-cap easing,
# or the biomechanical eq_km governor), never from room inside this one.
# The re-base (the pure-easy, post-illness restart) keeps its ORIGINAL conservative cap
# (REBASE_LONG_CAP) so it stays byte-identical — the recalibration is for the marathon-prep phases.
# §PRO18 — the long run's share of weekly volume now follows the Daniels / Hansons doctrine (long run
# ≤ 25–30% of the week, with Daniels' 2:30–3:00 duration ceiling as the cross-check) instead of the
# 0.42/0.48 fitted on his own history (median 0.33 · p75 0.40 · p90 0.50, calibrated 2026-06-20).
# ⚠ THIS DELIBERATELY OVERRIDES A HIS-DATA CALIBRATION WITH PUBLISHED DOCTRINE — his ruling
# 2026-07-29, on measurement, not preference. Under §PRO17 the long run is the biggest single session
# and so eats the per-session eq_km budget; capping its SHARE frees biomechanical headroom that flows
# into easy volume (the same redistribution §PRO9 already does when it clips a long run). Measured on
# his DB: block 933 → 1049 km, peak CTL 92.3 → 96.4, peak week 69.5 → 69.9 km, longest long 32.2 →
# 23.1 km (= 33% of its week, ≈2:40 at his easy pace — inside BOTH Daniels rules; 32.2 km was ~3:40
# and satisfied neither). Predicted finish moves 4:14:30 → 4:18:38, but that 4 min comes almost
# entirely from §FT1's ladder term whose SHAPE is an explicit prior with a single anchor (his whole
# race corpus sits at ratio 0.71) — the model cannot really rank these two, so the choice was made on
# the evidence axis instead: Aarhus is specifically about the LONGEST RUN, and 23 km is less
# single-bout damage than 32 km while the block carries 116 km MORE total training.
LONG_RUN_MAX_FRAC = 0.30
# §PRO26 — the share ceiling BY PHASE. §PRO18 set one number for the whole block on published
# doctrine (Daniels/Hansons: the long run ≤ 25–30% of the week, cross-checked by a 2:30–3:00 duration
# ceiling), and that is the right ceiling for BASE, where chronic volume is being built and the
# long-run share is competing with frequency for the same biomechanical budget. It is the wrong
# ceiling for the marathon-specific block: at 0.30 the whole 20-week road tops out at a 24.4 km
# longest run on a 72.5 km peak week, and the athlete arrives at a marathon never having been on his
# feet past 2h42. His OWN history (calibrated 2026-06-20) ran median 0.33 · p75 0.40 · p90 0.50, so
# the peak number below sits inside his p90 rather than past it.
# ⚠ THE COST IS DURATION, AND IT IS REAL AND OWNED: 32 km at his easy pace is ~3h36, past Daniels'
# 3:00 cross-check by 36 minutes, and Aarhus is specifically about single-bout damage from the
# LONGEST run. This is the owner's call of 2026-08-25, taken with the simulated ladder in front of
# him: he judged that arriving at a goal marathon never having run 30 km was the larger risk of the
# two. Measured on his live DB, the whole block total does not move — the same training, redistributed.
# ⚠ The two big runs are DELIVERED BY THE STEP CAP, not by this number: §PRO9's +10%-over-trailing-4wk
# ladder is what actually walks the long run up, so the peak value alone cannot produce them (peak is
# 2 weeks; from a 23.1 km build ceiling the ladder reaches only ~28 km by race week). The build entry
# exists to start the ramp early enough for the ladder to arrive, NOT to make build weeks long-heavy —
# it binds on the last build weeks only. Both caps still only ever REDUCE; every ACWR, eq_km and
# §PRO9 governor above is untouched and still binds first.
LONG_RUN_MAX_FRAC_BY_PHASE = {"build": 0.38, "peak": 0.45}
REBASE_LONG_CAP = 0.35     # pure-easy blocks (re-base) keep the original cap — leave the cautious restart untouched
LONG_RUN_MIN_KM = 4.0      # a "long run" the ACWR governor clips below this isn't functioning as a long run —
                           # relabel it a shakeout (never force load past the safety ceiling). See _mark_load_integrity.
# §P1 — every shape week carries its PERIODIZATION ROLE and its PHASE as real fields. Before this the
# only carrier was the human `intent` sentence, which seven governors then re-parsed (`_is_down`), so a
# week's role was encoded in display copy: rewording a sentence silently moved the §PRO2 trough anchor,
# the taper's non-down chain, the load-integrity pass and the banking gates at once. The house rule is
# that a governor publishes its decision variable rather than making its readers reconstruct it.
# `intent` stays the sentence it always was; `role`/`phase` are what the engine reads. They ride the
# `{**wk}` spread into the published week, so the plan payload now carries them too.
REBASE_SHAPE = [
    {"wk": 1, "km": 13, "runs": 3, "long": 5, "strides": 0, "phase": "rebase", "role": "build", "intent": "Re-establish frequency — pure easy feel, HR controlled, no urge to stop"},
    {"wk": 2, "km": 15, "runs": 4, "long": 6, "strides": 0, "phase": "rebase", "role": "build", "intent": "Add the 4th run if week 1 felt easy"},
    {"wk": 3, "km": 17, "runs": 4, "long": 6, "strides": 2, "phase": "rebase", "role": "build", "intent": "First gentle neuromuscular touch — strides ×2"},
    {"wk": 4, "km": 13, "runs": 3, "long": 5, "strides": 0, "phase": "rebase", "role": "down", "intent": "Down week — consolidate (masters + post-illness conservative)"},
    {"wk": 5, "km": 18, "runs": 4, "long": 7, "strides": 2, "phase": "rebase", "role": "build", "intent": "Extend easy aerobic volume"},
    {"wk": 6, "km": 19, "runs": 5, "long": 7, "strides": 2, "phase": "rebase", "role": "build", "intent": "End-of-block check → optional relaxed 5k probe, ready for base-build"},
]
# Run-day layouts per weekly frequency (0=Mon … 6=Sun). The block is Monday-anchored
# (_rebase_start), so these are REAL weekdays. Every layout ENDS on Sunday (offset 6): the long run
# lands on the calendar weekend (where _distribute_week assigns is_long), AND because a week never
# ends on a rest, two consecutive weeks can't strand a double rest at the boundary — fixes the
# 2026-06-22 cross-week seam (a 3-run week ending Sat + the next week resting Mon). Within a week no
# 3 run days fall consecutively; 6 runs/wk is the unavoidable exception (only one rest day).
RUN_DAY_LAYOUTS = {
    1: [6],                    # Sun
    2: [2, 6],                 # Wed, Sun
    3: [1, 3, 6],              # Tue, Thu, Sun
    4: [1, 3, 5, 6],           # Tue, Thu, Sat, Sun
    5: [0, 1, 3, 5, 6],        # Mon, Tue, Thu, Sat, Sun
    6: [0, 1, 2, 4, 5, 6],     # Mon–Wed, Fri–Sun (one rest, Thu)
    7: [0, 1, 2, 3, 4, 5, 6],
}


def _run_days(n):
    """Day-of-week slots for n weekly runs, spread to avoid 3 consecutive run days, with the long run
    on the last slot — always Sunday (offset 6), so a week never ends on a rest. Falls back to an
    even spread (which also spans 0..6, hence ends on Sunday)."""
    if n <= 0:
        return []
    if n in RUN_DAY_LAYOUTS:
        return RUN_DAY_LAYOUTS[n]
    if n == 1:
        return [6]
    return sorted({round(i * 6 / (n - 1)) for i in range(n)})


AV_MAX_STREAK = 3   # §AV — a relocation may never create a run-streak longer than this (the n=6
#                     layout's unavoidable 3 is the ceiling); if every candidate would, the run is
#                     SHED instead — blocked days make a week lighter, never denser.



def _max_streak(days):
    """Longest run of consecutive days in `days` (§AV day spacing). Lived in the self-test
    section until TECH-1 moved the battery out and revealed the engine had been calling it —
    it is engine code, and now it sits with the engine."""
    days = sorted(days); best = cur = 1
    for a, b in zip(days, days[1:]):
        cur = cur + 1 if b == a + 1 else 1
        best = max(best, cur)
    return best

def _av_run_days(n, blocked_offsets):
    """§AV — availability-aware run-day slots: the template layout re-laid around blocked week
    offsets (0=Mon…6=Sun). Minimal displacement: unblocked template slots stay put; each blocked
    slot relocates to the free day minimizing (resulting max run-streak, distance, day) — ties break
    to the earlier day, so the choice is deterministic and a ±1-day slide beats a far jump. A
    relocation that would force a streak > AV_MAX_STREAK (or has no free day at all) is SHED
    (reduce-only: §AV may move or remove load, never cram it). The sorted result's last slot is the
    long run — "long = last available day" generalizes the template's fixed Sunday. Returns
    (days, shed_count); blocked days that were rest anyway return the template untouched."""
    base = _run_days(n)
    blocked = set(blocked_offsets or ())
    if not blocked & set(base):
        return list(base), 0
    days = [d for d in base if d not in blocked]
    for b in [d for d in base if d in blocked]:
        cands = [c for c in range(7) if c not in blocked and c not in days]
        if not cands:
            continue                                   # nowhere to go — shed
        best = min(cands, key=lambda c: (_max_streak(sorted(days + [c])), abs(c - b), c))
        if _max_streak(sorted(days + [best])) > AV_MAX_STREAK:
            continue                                   # any placement over-densifies — shed
        days.append(best)
        days.sort()
    return days, n - len(days)


# Base phase (§6f Step B) — the aerobic-base block after the re-base. A gentle, mostly-under-cap
# volume ramp (the ACWR governor in generate_block is the hard ceiling regardless), with a 3:1
# load:recovery mesocycle. Conservative posture: hold the 5-run week (frequency-advance is the
# banking-gated §6e step, deferred); long run grows but stays ≤ LONG_RUN_MAX_FRAC of the week.
BASE_RUNS = 5
BASE_WEEKLY_RAMP = 0.045   # ~4.5%/wk *intent* — keeps Base mostly below the cap (re-base posture)
BASE_DOWN_EVERY = 4        # every 4th week is a down week (3 build : 1 recovery)

# (§6h CTL-responsive volume FLOOR removed 2026-06-30 — superseded by the §PRO assertive ride, which is
# the proper fitness-tracker; the floor was dormant in real plans because the re-base decay kept measured
# CTL below its activation band. Caution is now a clean conservative ramp.)

# §PRO1 (regime re-engineering) — CTL-ramp-rate ceiling. A second, science-based brake layered into
# the per-week governor alongside the ACWR ceiling: the week's load may not make CTL rise faster than
# CTL_RAMP_MAX points/week. ACWR caps the ACUTE spike; this caps the CHRONIC-load growth — the
# connective-tissue temper (tendon/bone adapt over weeks, ACWR is partly blind to them). Riding the
# ACWR soft cap continuously grows CTL ~8%/wk, so this absolute cap is the high backstop that bites
# only on a strong rebuild (~CTL 60+), with the §PRO6 duration limiter handling sustained near-ceiling
# riding. Owner-chosen MODERATE (~5 pts/wk, upper end of standard ramp-rate guidance). Threaded as an
# OPTIONAL governor arg (default None ⇒ today's exact behaviour) so caution stays byte-identical and
# only the assertive regime (§PRO2) passes it. NEVER raises the allowance — a pure additional ceiling.
CTL_RAMP_MAX = 5.0         # max CTL points/week the plan may add (the assertive-regime tissue backstop)
# §52 (2026-07-29) — CALIBRATED AT LAST, conditioned on CTL level, because §PRO17 made this the PRIMARY
# governor on late build weeks and it had never been checked against anything. 239 weekly samples over
# 4.7 years. The naive read is misleading: raw p90 gain is +8.94/wk, which would suggest 5.0 is timid.
# Splitting REBOUND weeks (CTL below its own trailing 26-wk peak — coming back, where CTL rises fast
# mechanically because the EWMA sits far under current load) from BUILD weeks (CTL ≥95% of that peak,
# i.e. genuinely new territory) shows where that tail comes from: rebound p90 +9.10 vs build p90 +6.72.
#   BUILD weeks, at the CTL levels this constant actually governs:
#     CTL 75–90  n=14   p50 −0.11  p75 +4.32  p90 +5.86  p95 +6.42   (p90 relative 6.7%)
#     CTL 90+    n=40   p50 −2.34  p75 +4.94  p90 +6.43  p95 +6.72   (p90 relative 6.1%)
# So 5.0 sits at his p75–p80 on build weeks where it binds — permitting three quarters of what he has
# demonstrated in new territory and refusing the top quarter. LEFT UNCHANGED on that evidence.
# ⚠ TWO LIMITS A FUTURE READER MUST NOT LOSE. (1) Below CTL 75 there are FEWER THAN 5 build weeks in
# the whole corpus — when he was under 75 he was almost always rebounding. So this is calibrated for
# CTL ≥75 and is UNEVIDENCED below it, which is exactly where a plan's first ~10 weeks live: at CTL 50,
# 5.0/wk is ~10% relative, faster than anything he has shown on a build week. (2) Over the range we CAN
# measure, the absolute form (~+6) and the relative form (~6%) are INDISTINGUISHABLE — the band is too
# narrow to separate them — and they diverge precisely in the unevidenced region. The relative variant
# `min(CTL_RAMP_MAX, 0.06 × ctl)` was built and measured on his DB: peak CTL 96.9 → 86.5, block 1070 →
# 928 km, longest long 23.1 → 21.5 km, finish 4:18:09 → 4:24:35. It is the lever if conservatism in the
# early block is ever wanted; it was not taken because it imposes a high-CTL rate on a region with no
# data, at a measurable cost. ⛔ As everywhere on this axis: DEMONSTRATED BEHAVIOUR, never injury —
# his corpus contains none (4 gaps ≥14d in 4.7 years). See PROJECT_LOG §52.

# §PRO6 — duration-aware tissue limiter. Riding the ACWR ceiling sits the athlete at ~1.25 EVERY
# building week, so the connective-tissue load is sustained far longer than under the timid caution
# ramp (and ACWR's 28-day window is blind to multi-week accumulation). The 3:1 down-week cadence
# normally provides the deload, but it can misalign (a long phase, a phase boundary). This is the hard
# SAFETY NET: no more than MESO_MAX_HARD consecutive near-ceiling building weeks without a recovery —
# the (MESO_MAX_HARD+1)th is FORCED to a deload regardless of the shape. Assertive-only; under a normal
# 3:1 shape it never fires (the down week resets the streak first), so it disturbs nothing — it only
# catches the pathological long-grind a thinner moderate-ramp margin (CTL_RAMP_MAX=5) can't otherwise see.
NEAR_CEILING_ACWR = ACWR_SOFT - 0.05   # 1.20 — "near the ceiling" for the consecutive-week count
MESO_MAX_HARD = 3                      # max consecutive near-ceiling building weeks before a forced deload

# §PRO9 — long-run progression cap (the Davis/Aarhus injury lever, ENGINE_SCIENCE.md §3.2). Aarhus
# (n≈5000): a sharp jump in the SINGLE longest run vs the longest of the trailing ~4 weeks predicts
# injury MORE strongly than weekly-mileage jumps — a biomechanical-axis signal ACWR/TRIMP structurally
# cannot see. HARD cap (owner-chosen): the plan never PRESCRIBES a long run beyond LONG_RUN_STEP_CAP ×
# the longest run of the trailing LONG_RUN_STEP_WINDOW weeks; the clip only ever REDUCES the long-run
# day, and the freed volume redistributes to the week's short easy runs, so weekly total TRIMP + the
# ACWR projection are UNCHANGED (only the single long-run spike shrinks — and a smaller spike can only
# lower peak transient, never raise it). Assertive-only, caution byte-identical — the §PRO8 template.
# Magnitude 🟡 LITERATURE (+10% = the 10% rule); earn a looser step from his corpus before widening.
LONG_RUN_STEP_CAP = 1.10    # max long-run jump vs the trailing-window longest (+10%)
LONG_RUN_STEP_WINDOW = 4    # trailing weeks whose longest run sets the progression baseline

# §PRO21 — the long run has to BE the long run. Every long-run constant above this line is a CEILING
# (LONG_RUN_MAX_FRAC/BASE_LONG_FRAC = the Daniels/Hansons share cap; LONG_RUN_STEP_CAP = the Aarhus
# jump cap). Nothing expressed what the long run is FOR, and the only floor was the absolute
# LONG_RUN_MIN_KM stub check — so a long run could stop functioning as one while sailing past it.
# §PRO9's clamp bounds every easy day at the SAME +10% ceiling as the long slot: correct on the
# biomechanical axis (no run may jump), silent on SHAPE. Once weekly budget ÷ running days reaches
# that ceiling, every day pins to it and the week has no long run left — five identical days, one of
# them merely labelled. MEASURED on his 2026-08-03 week: cap 11.4 (= 1.10 × his 10.38 km Sunday,
# itself a 62-min THRESHOLD effort — the ladder's baseline is "longest run", which does not ask what
# kind of run it was), budget 56.6 km over 5 days = 11.32 ⇒ long 11.4 vs easy 11.3, ratio 1.01. Four
# such weeks in one block. The long run's physiology is DURATION-dependent — glycogen depletion and
# fat oxidation, capillary + mitochondrial density, connective-tissue durability under accumulated
# fatigue — a stimulus repetition cannot supply, so five 80-min runs are not one 80-min long run plus
# four easy days; they are five easy days. ONE principle — the long run is the week's longest run —
# carried by TWO levers, chosen by whether §PRO9 has the long run pinned:
#   · Long run FREE (no cap, or the cap is not binding): RAISE the long share to the least that
#     clears the target, bounded by `long_cap` (the Daniels/Hansons share ceiling). Costs nothing —
#     no extra running day, weekly total untouched — so it is the preferred lever and runs in BOTH
#     regimes. This is what a flat week actually needs: a bigger long run, not smaller easy days.
#   · Long run PINNED by §PRO9 (his real weeks): the long run cannot rise, so LONG_RUN_EASY_FRAC
#     bounds each easy day at a fraction of the long run laid and the freed volume spreads onto MORE
#     easy days — the durability principle §PRO9 already uses in the same block. Weekly total still
#     untouched, but the week gains a running day. Lives under `long_km_cap` ⇒ assertive-only.
#   · LONG_RUN_MIN_RATIO (honesty) is the backstop for a week neither lever can fix — `long_cap`
#     forbids the share the raise needs AND there is no day left to spread onto. Reachable at ≤2 short
#     easy days: the raise needs R/(n_short+R) = 0.37 at n_short=2, above every long_cap we set, so a
#     3-run week is capped at ratio 1.077 by construction (seen: re-base wk1, 12.9 km over 3 runs).
#     Then the plan stops calling it a long run, as _mark_load_integrity already does for a sub-4 stub.
# ⚠ NOT caution-byte-identical — the raise lever is deliberately regime-independent, because a flat
# week is not a load question and the §PRO8 template does not apply. MEASURED on his DB: caution block
# 273.7 → 273.0 km (−0.26%), 66 sessions both, 5 runs/week both, no week gains a day; the whole delta
# is long runs rising into their share and easy days giving it back. The RE-BASE is load-byte-identical
# (every km/minute/TRIMP equal — its weeks already sit at REBASE_LONG_CAP), one label moved.
# The pair is deliberately ordered 1/0.85 = 1.176 > 1.15: BOTH levers aim at 1/LONG_RUN_EASY_FRAC and
# never at LONG_RUN_MIN_RATIO itself, so the honesty check fires only where the construction genuinely
# failed and never merely because the final round-to-0.1 km ate the last thousandth.
# Magnitude 🟡 REASONED, not literature: no source prescribes a long-vs-easy ratio (the textbooks
# assume the long run dominates and never state it). 0.85 is the LOOSEST fraction that still makes
# the long run unambiguously the week's longest run while forcing the fewest extra running days —
# tightening it toward the week's own natural shape (n_short × BASE_LONG_FRAC / (1 − BASE_LONG_FRAC)
# ⇒ 0.75 at 4 easy days) pushes his current weeks to SEVEN running days, which is the decoupled-clocks
# problem (CTL-driven weekly volume outrunning the +10% long-run ladder), not this one. Revisit from
# his own corpus once the ladder has caught up.
LONG_RUN_EASY_FRAC = 0.85   # max easy-day distance as a fraction of the long run laid that week
LONG_RUN_MIN_RATIO = 1.15   # below this multiple of the week's longest easy run it is not a long run

# §PRO24 — THE EASY DAYS ARE A LADDER, NOT N IDENTICAL RUNS. §PRO21 made the long run the week's
# longest and §PRO23 made the week grow with it, but the days underneath stayed uniform by
# construction: every short easy took (1 − long_w)/n_short, so a base week read
# 10.3 / 10.3 / 10.3 / 10.3 + a quality touch + the long run. That is not a plan a coach would
# recognise, and it is the residue of the owner's original complaint — the long run stopped being
# "just another day", and then every other day still was.
# ⭐ THE NUMBER IS HIS, NOT THE LITERATURE'S — ENGINE_SCIENCE §0: "Davis gives us theory, Duarte's own
# data decides the numbers." No source states a within-week easy-day distribution, so this was FITTED
# to his corpus: 150 weeks with ≥4 run-days and ≥30 km, days summed per date (§SJ) and ranked. His
# median shapes are a clean descending ladder and remarkably stable across week sizes —
#   5 runs: 33.8 / 19.7 / 17.3 / 15.1 / 12.8 %      6 runs: 27.6 / 18.7 / 16.2 / 13.5 / 11.8 / 9.5 %
#   7 runs: 25.9 / 16.9 / 14.7 / 12.7 / 11.6 / 10.3 / 8.8 %
# Least squares over the easy ranks (dropping rank 1 = the long run and the shortest day = the
# quality/shakeout slot the engine sizes on its own) gives STEP = 0.110, mean-square error 0.00385
# against 0.00914 for the FLAT split the engine lays today — his own weeks fit a ladder 2.4× better
# than they fit uniformity. A pooled per-step RATIO (0.935) was rejected: the observed per-rank
# ratios flatten (0.89 → 0.94 → 0.98 → 0.98), so a geometric model misfits the head and the tail at
# once, while a LINEAR decrement reproduces all three week sizes.
# 🟢 OWNER-MEASURED MAGNITUDE / DOCTRINE ORDER, and it moves no magnitude: the weekly total, the long
# run and every governor bound are untouched — this only redistributes the SHORT easy budget.
# ⚠ THE ORDER IS NOT CALENDAR ORDER, and the same 161 weeks say so. Each easy day over its week's
# mean easy day grades hard by SIZE RANK (1.426 / 1.225 / 1.044 / 0.925 / 0.818 / 0.704, R² 0.545)
# and NOT AT ALL by weekday (1.038 / 1.112 / 0.949 / 1.034 / 1.005 / 0.965, R² 0.054). So the rungs
# are his and the order is doctrine: longest easy furthest from any long run, short rungs flanking
# one — which is also the order §JR already sheds in ("nearest the long run first — the freed day
# doubles as pre-long freshness"), and the one calendar bucket his data does separate (0.929 ± 0.031
# the day after a long run vs 1.086 ± 0.037 two days out). Laying them in calendar order instead put
# the head rung on the day the §H1 peak brake pins and cost a live week 6.3 km — see PROJECT_LOG §60.
EASY_LADDER_STEP = 0.11     # each successive easy day is this much of the longest easy shorter
EASY_LADDER_FLOOR = 0.15    # ...but never below this fraction of it (the fitted model's own floor)

# §3.1 — biomechanical load axis (Davis, ENGINE_SCIENCE.md §3.1 + §6.1). eq_km = a DAMAGE-EQUIVALENT
# distance: km × f(pace), f rising steeply with speed because tissue damage ≈ loading cycles(steps) ×
# load-per-step(↑ with speed) — fast running does far more damage per km than easy, a biomechanical axis
# TRIMP/ACWR structurally CANNOT see. f is CALIBRATED TO HIS DATA (2026-07-02, full 4.7yr/1078-run corpus
# replay): the original Davis-literature grid (1.8/3.5/5.0) kept the one true biomechanical catch (the
# 2022-03 calf/hip escalation week, eq-ratio 1.44 — invisible to volume brakes at km-ratio 1.16) but
# falsely braked 7 quality weeks he demonstrably absorbed (CTL-138 era); this softer grid keeps the catch
# (ratio 1.40 > 1.30) and cuts the false brakes to 3. Harsher grids were strictly worse. The governor stays
# contained: SOFT (a ceiling with margin), ASSERTIVE-ONLY, caution byte-identical, and it only ever REDUCES
# load — and because injury is PROBABILISTIC not deterministic (§6.1) it RESHAPES the week (drops the fast
# slice to easy, reusing the §H2 mechanism) rather than hard-gating. Re-calibrate when the corpus grows a
# real fast-session/injury history. A biomechanical jump-cap that flanks the ACWR gate.
EQ_KM_FACTOR = {"easy": 1.0, "long": 1.0, "marathon": 1.4, "threshold": 2.5, "interval": 3.5}
BIO_EQ_STEP = 1.30          # max weekly eq_km jump vs the trailing-window max (the soft biomechanical ceiling)
BIO_EQ_WINDOW = 4           # trailing weeks whose MAX eq_km sets the biomechanical (chronic) baseline
# §PRO17 — the SAME biomechanical step, applied at SESSION grain: no prescribed session's eq_km may
# exceed this × the largest single session of the trailing BIO_EQ_WINDOW weeks. Deliberately NOT a new
# number — one biomechanical step, read at two grains (week and session), so nobody has to reason about
# why they differ. §PRO9 keeps the LONG RUN at its own tighter 1.10 on raw km, because that is the case
# the Aarhus cohort literally measured (longest-run jumps predicted injury; weekly-mileage jumps did not).
# ⛔ CALIBRATED ON DEMONSTRATED BEHAVIOUR, NOT ON INJURY — his corpus contains no injuries to fit against
# (4 gaps ≥14d in 4.7 years, longest 21d). Over 1205 logged sessions, each measured against the largest
# of the trailing 30 days in eq_km with PERIOD-CORRECT zones (`_zones_asof`): p90 0.907 · p95 1.010 ·
# p97.5 1.118 · p99 1.377. This refuses 1.49% of them. It says "he has done this without incident",
# never "this is safe". See PROJECT_LOG §50 and ENGINE_SCIENCE.
SESSION_EQ_STEP = BIO_EQ_STEP
# §PRO17 — the §H1 rescue's own threshold, decoupled from the governor it used to double as. §H1 was
# written for a low-CTL pathology its docstring measures at ~1.5–1.6 (a quality session's fixed TRIMP
# floor becoming a huge day among small ones); ACWR_HARD=1.30 was never that number, and using it made a
# dormant rescue into the primary volume governor (§49: it bound 11 of 21 searches while the rescue itself
# fired 0 times in 19 weeks). ⚠ JUDGMENT, not a fit: the low end of the measured pathology range.
H1_RESCUE_ACWR = 1.50
BASE_DOWN_FRAC = 0.75      # down-week volume vs the carried build trajectory
BASE_LONG_FRAC = 0.25      # long-run target as a fraction of weekly km (capped at LONG_RUN_MAX_FRAC).
                           # §PRO18 (2026-07-29) — Daniels/Hansons band. Was 0.42, itself raised from
                           # 0.32 on 2026-06-20 toward his own long-run share; see LONG_RUN_MAX_FRAC
                           # for why published doctrine now outranks that fit.

# Quality / polarized model (§6f Step C) — the structured-workout machinery + the polarized "knob".
# The knob is a HARD FRACTION of a week's TRIMP delivered as quality (threshold/interval) work; the
# rest is easy/long. Because the ACWR governor caps TOTAL weekly TRIMP, raising intensity just
# concentrates the same governed load into fewer minutes — it never breaches the ceiling. Quality is
# strictly OPT-IN per shape week (a `quality` list); weeks without it stay pure easy, so the re-base
# is byte-identical (§6f Step A regression). Polarized = easy-dominant: hard work is a small,
# concentrated slice, never a target to fill (echoes §6f's "ACWR is a ceiling, not a target").
QUALITY_WU_MIN = 10        # easy warm-up minutes bracketing each quality session
QUALITY_CD_MIN = 10        # easy cool-down minutes
POLARIZED_EASY_MIN = 0.80  # invariant: easy share of weekly TRIMP must stay ≥ this (the "80")
PHASE_HARD_CAP = {         # invariant ceiling on the hard (threshold+interval) share, per phase
    "rebase": 0.0, "base": 0.15, "build": 0.25, "peak": 0.25, "taper": 0.20}
HARD_ZONES = ("threshold", "interval")  # zones that count toward the "hard" (polarized) share

# §T2 — the four-component fitness model (Tier-2). Marathon fitness decomposes into VO₂max ×
# running economy × SSmax/LT2 (the Mayo three-factor model) + physiological RESILIENCE — how little
# the first three decay over 42 km (Jones 2024; the framing follows John Davis). Every quality
# session is TAGGED by the component it chiefly builds (metadata, both regimes — surfaced, never
# steering on its own), and the ASSERTIVE regime re-periodizes the quality mix around the components:
# VO₂max is developed EARLY and then merely maintained (red-cell persistence makes it cheap to hold),
# marathon-pace work GROWS through the build (SSmax + near-MP economy), and the Peak pivots to
# resilience — the MP segment of the long run extends week-over-week at CONSTANT speed ("longer, not
# faster"). Caution keeps the legacy shapes byte-identical: the component periodization is earned the
# same way every other assertive lever is.
COMPONENT_BY_KIND = {      # primary component per session kind (quality + the plain long run)
    "interval": "vo2max", "tempo": "ssmax", "long_mp": "resilience", "long": "economy"}
DAVIS_BASE_VO2_FRAC = 0.05   # Base (assertive): the quality slot becomes a SHORT VO₂ touch. Halved
                             # from the tempo slot's .10 (calibrated 2026-07-04 on his live corpus):
                             # during the steep post-restart volume rebuild the ramp-back week's plain
                             # km already sits ~98% of the eq_km step, so a .10 touch self-tripped the
                             # §3.1 brake; at .05 the touch fits the biomech budget (zero fires) and
                             # the projection is unchanged — the dose was never the fitness lever.
DAVIS_BASE_VO2_REP_MIN = 2   # ≥2-min reps develop VO₂max; short reps keep the on-ramp touch gentle
DAVIS_INT_FRAC = 0.12        # Build+Peak mid-week interval session — FULL size, matching the legacy
                             # dev dose. "Maintain late" was tried at .06 and REVISED by evidence
                             # (2026-07-04, live replay): under the peak-day-capped governor the
                             # mid-week session FLATTENS the week, so shrinking it made every week
                             # peakier and cost ~8% of total safe load (race CTL 42→36) with no
                             # offsetting benefit. The session's ROLE still shifts dev→maintenance
                             # (it never grows); only its size stays. Excess VO₂ stimulus is benign.
DAVIS_BUILD_MP_START, DAVIS_BUILD_MP_END = 0.07, 0.10   # MP finish grows across Build's non-down weeks
DAVIS_PEAK_MP_START, DAVIS_PEAK_MP_END = 0.10, 0.13     # …and keeps extending through Peak (resilience).
                             # Work tops at .12+.13=.25 — fine: the polarized easy floor is MP-EXEMPT
                             # by design (hard = thr+int only, .12 ≤ PHASE_HARD_CAP), and the MP slice
                             # is bounded by the load cap, not the polarization floor.


def _mp_prog(i, n, lo, hi):
    """MP-finish frac for progression step i of n (constant-speed duration extension, §T2)."""
    return round(lo + (hi - lo) * (i / (n - 1)), 3) if n > 1 else round(hi, 3)


def _phase_builds(weeks):
    """§T2 — the distinct fitness components a generated phase's sessions build, in first-seen
    order (derived from the session tags, so the surface can never drift from the prescription).
    Empty for untagged phases (the re-base)."""
    comps = []
    for w in weeks:
        for s in (w.get("sessions") or []):
            c = s.get("component")
            if c and c not in comps:
                comps.append(c)
    return comps

# Base on-ramp quality (§6f Step C): a single short *light tempo* per build week, introduced after
# the first couple of weeks (neuromuscular on-ramp, after strides) and never on a down week. Kept
# deliberately light — a small hard fraction at threshold ("cruise") — the conservative masters /
# post-illness posture. Build's heavier interval/MP menu is Step D.
BASE_TEMPO_FRAC = 0.10     # hard fraction of weekly TRIMP for the Base light tempo (well under cap)
BASE_TEMPO_ZONE = "threshold"
BASE_TEMPO_FROM_WEEK = 3   # no tempo in the first 2 Base weeks (ease into quality after strides)


def base_shape(n_weeks, start_km, runs=BASE_RUNS, davis=False):
    """Parametric Base-phase shape (§6f Step B/C): easy-aerobic volume growth launched from the
    re-base end volume, with a 3:1 down-week cadence. INTENT only — `generate_block` clips any week
    the ACWR ceiling won't allow, so this is the target trajectory, not the guaranteed one. The
    build trajectory advances only on build weeks (a down week absorbs, it doesn't regress the
    trend). Strides carry over from the re-base on-ramp; Step C layers a single quality session per
    build week (from BASE_TEMPO_FROM_WEEK) as the on-ramp — easy-dominant, polarized (~90/10).
    `davis` (§T2, assertive-only — caution byte-identical): the quality slot becomes a SHORT VO₂
    touch instead of the cruise tempo — develop VO₂max early while mileage rises (it's cheap to hold
    later), same hard frac, still under PHASE_HARD_CAP["base"]; the eq_km governor watches the km."""
    shape, km = [], float(start_km)
    for i in range(n_weeks):
        wk = i + 1
        down = (wk % BASE_DOWN_EVERY == 0)
        if down:
            this_km = max(1, round(km * BASE_DOWN_FRAC))
        else:
            this_km = max(1, round(km))
            km *= (1 + BASE_WEEKLY_RAMP)
        quality = []
        if not down and wk >= BASE_TEMPO_FROM_WEEK:
            quality = ([{"kind": "interval", "zone": "interval", "frac": DAVIS_BASE_VO2_FRAC,
                         "structure": "intervals", "rep_min": DAVIS_BASE_VO2_REP_MIN, "rec_min": 2,
                         "label": "short VO₂ touch", "component": "vo2max"}] if davis else
                       [{"kind": "tempo", "zone": BASE_TEMPO_ZONE, "frac": BASE_TEMPO_FRAC,
                         "structure": "continuous", "label": "light cruise tempo",
                         "component": "ssmax"}])
        shape.append({"wk": wk, "km": this_km, "runs": runs,
                      "long": round(this_km * BASE_LONG_FRAC), "strides": 0 if down else 2,
                      "quality": quality,
                      "phase": "base", "role": "down" if down else "build",   # §P1
                      "intent": "Down week — absorb the block" if down
                      else ("General — aerobic volume + early VO₂ (build it now, hold it cheap)"
                            if davis else "Easy aerobic base — build durable volume")})
    return shape


# Build phase (§6f Step D) — SPECIFIC work. Volume held / lightly growing; two quality sessions a
# week (VO₂ intervals + a marathon-pace long-run finish), 3:1 down weeks. Frequency holds at
# BASE_RUNS (frequency-advance is the banking-gated §6e step, still deferred). Quality fracs sum to
# < (1 − POLARIZED_EASY_MIN) so the week stays easy-dominant by construction; the threshold/interval
# slice alone stays under PHASE_HARD_CAP["build"].
BUILD_WEEKLY_RAMP = 0.045  # §PRO10 (2026-07-23, owner-approved) — raised 0.02→0.045 (matches Base):
#                            "lightly growing" build intents + the 3:1 troughs nearly cancelled, so
#                            even the caution shape asked for a flat Build; specificity AND volume
#                            grow together for an athlete whose trailing history dwarfs his chronic
#                            load. Intent only — the governor still clips what the ceiling won't allow.
BUILD_DOWN_EVERY = 4
BUILD_DOWN_FRAC = 0.75
BUILD_LONG_FRAC = 0.28       # §PRO18 — Daniels/Hansons band (was 0.45, raised from 0.34 2026-06-20).
                             # The long run is still the cornerstone; it is now a smaller SHARE of a
                             # much larger week, which is the whole point (see LONG_RUN_MAX_FRAC).
BUILD_INTERVAL_FRAC = 0.12   # VO₂ intervals (interval zone) — the hard slice
BUILD_MP_FRAC = 0.07         # marathon-pace long-run finish (marathon zone, attached to the long run)

# Peak / sharpen — trimmed volume, race specificity. The long run is at its largest the runway
# allows (bounded by PEAK_LONG_FRAC of the week + the ACWR ceiling — honest about a detrained
# masters runway, CTL-gated, not a textbook 32–35 km). The "~12–13 km" figure this note used to
# quote was measured off CTL ~24 (2026-06-20); at the CTL 51→58 of the current block it lands at
# ~17 km. The number tracks CTL — it is not a constant, and it is not a suppressed cap: the binding
# constraint is the weekly volume the ACWR governor allows, NOT LONG_RUN_MAX_FRAC (audited
# 2026-07-28, peak long ran 44–45% of the week against a 50% ceiling that never bound; §PRO18 has
# since replaced that ceiling with the 0.30 doctrine cap, which DOES bind).
PEAK_WEEKLY_RAMP = -0.04     # trim volume into race specificity
PEAK_LONG_FRAC = 0.30        # §PRO18 — top of the Daniels/Hansons band (was 0.48, raised from 0.35
                             # on 2026-06-20 to "push the long run to its CTL-safe ceiling" — under
                             # §PRO17 the ceiling that binds is biomechanical, not CTL).
PEAK_MP_FRAC = 0.10
PEAK_INTERVAL_FRAC = 0.06

# Taper — drop volume ~40–60% over the taper, keep sharpness with short race-pace touches; the race
# week is the lightest and carries no structured quality (just freshening).
TAPER_LONG_FRAC = 0.30
TAPER_SHARP_FRAC = 0.06      # short race-pace touch (threshold), neuromuscular sharpness only
TAPER_TOP, TAPER_BOTTOM = 0.75, 0.40   # week-1 vs race-week volume as a fraction of the peak end


def build_shape(n_weeks, start_km, runs=BASE_RUNS, davis=False):
    """Parametric Build-phase shape (§6f Step D): lightly-growing specific work off the Base end
    volume, with a 3:1 down-week cadence. Each build week carries two quality sessions — VO₂
    intervals (mid-week) and a marathon-pace finish on the long run — as a small polarized slice;
    down weeks drop quality to absorb. INTENT only — `generate_block` clips to the ACWR ceiling.
    `davis` (§T2, assertive-only — caution byte-identical): VO₂max was developed back in Base, so
    the interval slot shifts to a MAINTENANCE role — same full session size (shrinking it made the
    week peakier and cost total safe load; see DAVIS_INT_FRAC), it just never grows — while the MP
    finish GROWS across the phase's non-down weeks (SSmax + near-MP economy) — more minutes at the
    same pace, never a faster pace."""
    shape, km = [], float(start_km)
    nd_total = sum(1 for i in range(n_weeks) if (i + 1) % BUILD_DOWN_EVERY != 0)
    nd_seen = 0
    for i in range(n_weeks):
        wk = i + 1
        down = (wk % BUILD_DOWN_EVERY == 0)
        if down:
            this_km = max(1, round(km * BUILD_DOWN_FRAC))
        else:
            this_km = max(1, round(km))
            km *= (1 + BUILD_WEEKLY_RAMP)
        if down:
            quality = []
        elif davis:
            mp = _mp_prog(nd_seen, nd_total, DAVIS_BUILD_MP_START, DAVIS_BUILD_MP_END)
            nd_seen += 1
            quality = [
                {"kind": "interval", "zone": "interval", "frac": DAVIS_INT_FRAC,
                 "structure": "intervals", "rep_min": 3, "rec_min": 2,
                 "label": "VO₂ intervals", "component": "vo2max"},
                {"kind": "long_mp", "zone": "marathon", "frac": mp,
                 "attach": "long", "label": "marathon-pace long run (progressive)",
                 "component": "resilience"}]
        else:
            quality = [
                {"kind": "interval", "zone": "interval", "frac": BUILD_INTERVAL_FRAC,
                 "structure": "intervals", "rep_min": 3, "rec_min": 2, "label": "VO₂ intervals",
                 "component": "vo2max"},
                {"kind": "long_mp", "zone": "marathon", "frac": BUILD_MP_FRAC,
                 "attach": "long", "label": "marathon-pace long run", "component": "resilience"}]
        shape.append({"wk": wk, "km": this_km, "runs": runs,
                      "long": round(this_km * BUILD_LONG_FRAC), "strides": 0, "quality": quality,
                      "phase": "build", "role": "down" if down else "build",   # §P1
                      "intent": "Down week — absorb the block" if down
                      else ("Supportive — growing MP work + VO₂ maintenance" if davis
                            else "Build — specific: VO₂ intervals + marathon-pace long run")})
    return shape


def peak_shape(n_weeks, start_km, runs=BASE_RUNS, davis=False):
    """Parametric Peak-phase shape (§6f Step D): trim volume into race specificity. Volume eases each
    week; the long run carries a race-pace finish and there's a light interval touch for sharpness.
    The long run is bounded by LONG_RUN_MAX_FRAC + the ACWR ceiling — the runway, not a textbook
    peak-long-run number, decides its length. `davis` (§T2, assertive-only — caution byte-identical):
    the phase pivots to RESILIENCE — the long run's MP segment keeps extending at constant speed
    while the interval slot stays a maintenance touch."""
    shape, km = [], float(start_km)
    for i in range(n_weeks):
        wk = i + 1
        this_km = max(1, round(km))
        km *= (1 + PEAK_WEEKLY_RAMP)
        if davis:
            # §T2 marathon-specific: the long-fast run IS the workout — the MP segment keeps
            # extending at constant speed (resilience: economy decay resistance). The mid-week
            # interval session keeps its FULL size in a maintenance ROLE: shrinking it made the
            # week peakier and cost total safe load (see DAVIS_INT_FRAC), verified on his corpus.
            quality = [
                {"kind": "interval", "zone": "interval", "frac": DAVIS_INT_FRAC,
                 "structure": "intervals", "rep_min": 3, "rec_min": 2,
                 "label": "VO₂ intervals — maintenance", "component": "vo2max"},
                {"kind": "long_mp", "zone": "marathon",
                 "frac": _mp_prog(i, n_weeks, DAVIS_PEAK_MP_START, DAVIS_PEAK_MP_END),
                 "attach": "long", "label": "long-fast resilience run", "component": "resilience"}]
        else:
            quality = [
                {"kind": "interval", "zone": "interval", "frac": PEAK_INTERVAL_FRAC,
                 "structure": "intervals", "rep_min": 3, "rec_min": 2,
                 "label": "sharpening intervals", "component": "vo2max"},
                {"kind": "long_mp", "zone": "marathon", "frac": PEAK_MP_FRAC,
                 "attach": "long", "label": "race-pace long run", "component": "resilience"}]
        shape.append({"wk": wk, "km": this_km, "runs": runs,
                      "long": round(this_km * PEAK_LONG_FRAC), "strides": 0, "quality": quality,
                      # §P1 — the §PRO6 deload exemption used to sniff the "Peak" PREFIX of the
                      # sentence below (the peak rides into the taper; the taper IS its recovery).
                      # `phase` carries that now, so the sentence is free to be reworded.
                      "phase": "peak", "role": "build",
                      "intent": ("Peak — marathon-specific: long-fast resilience + VO₂ maintenance" if davis
                                 else "Peak — race specificity: race-pace long run + sharpening")})
    return shape


def taper_shape(n_weeks, start_km, runs=BASE_RUNS, race_zone="threshold"):
    """Parametric Taper-phase shape (§6f Step D): volume falls from ~TAPER_TOP to ~TAPER_BOTTOM of
    the peak-end volume over the taper, while a short race-pace touch keeps the legs sharp. The race
    week (last) is the lightest and carries no structured quality — just easy freshening.
    §TT — `race_zone` is the pace of that touch, and it must BE race pace. It was hardcoded
    "threshold": right-ish for 10k/HM, WRONG for the marathon — a marathon taper was prescribing the
    block's only threshold-zone reps, a pace absent from all 17 prior weeks, two weeks before the
    race, under a label that said "race-pace". The marathon's race pace is the marathon zone — the
    pace every MP long run has rehearsed for 9 weeks — and at Davis's damage grid it is also the
    LIGHTER touch (f 1.4 vs 2.5/km), which is what a taper wants. Component stays "ssmax": per
    ENGINE_SCIENCE §1, SSmax ← MP / HM / sub-threshold work — true at either pace. Default preserves
    the old zone for every other race type and every legacy caller."""
    shape = []
    for i in range(n_weeks):
        wk = i + 1
        frac = (TAPER_TOP - (TAPER_TOP - TAPER_BOTTOM) * i / (n_weeks - 1)) if n_weeks > 1 else TAPER_BOTTOM
        this_km = max(1, round(start_km * frac))
        race_week = (wk == n_weeks)
        quality = [] if race_week else [
            {"kind": "tempo", "zone": race_zone, "frac": TAPER_SHARP_FRAC,
             "structure": "intervals", "rep_min": 2, "rec_min": 2, "label": "short race-pace touch",
             "component": "ssmax"}]
        shape.append({"wk": wk, "km": this_km, "runs": runs,
                      "long": round(this_km * TAPER_LONG_FRAC), "strides": 0 if race_week else 2,
                      "quality": quality,
                      "phase": "taper", "role": "race" if race_week else "taper",   # §P1
                      "intent": "Race week — freshen up, stay loose" if race_week
                      else "Taper — drop volume, keep sharpness"})
    return shape


def _qblock(effort, zname, minutes, pace, detail):
    """One rep inside a structured session — carries its own zone/pace/min/km/TRIMP so the UI (Step
    F) and the polarized self-test read the distribution structurally. `effort=="work"` is the only
    non-easy effort (warmup/cooldown/recovery/easy_base are all easy), so the polarized invariant is
    just: work TRIMP ≤ cap, everything else is the easy share."""
    return {"effort": effort, "zone": zname, "minutes": minutes,
            "km": round(minutes * 60 / pace, 1) if pace else 0.0,
            "trimp": round(minutes * est_trimp(1, zname), 1),
            "pace_zone": f"{fmt_pace(pace)}/km {zname}", "detail": detail}


def _session_from_reps(date, kind, zone, zpace, reps, note):
    return {"date": date, "kind": kind, "zone": zone,
            "km": round(sum(r["km"] for r in reps), 1),
            "minutes": sum(r["minutes"] for r in reps),
            "trimp": round(sum(r["trimp"] for r in reps), 1), "reps": reps,
            "pace_zone": f"{fmt_pace(zpace)}/km {zone}", "note": note}


def _build_quality(spec, work_trimp, start_date, dow, zones, easy_pace_sec):
    """§6f Step C/D — expand one mid-week quality spec into a STRUCTURED session: easy warm-up +
    work reps at the target zone + easy cool-down. `structure="intervals"` emits multiple work reps
    (rep_min each) with easy recovery jogs between them; otherwise a single continuous work block
    (tempo/cruise). `work_trimp` is the hard slice allotted to this session's WORK; the easy wu/cd
    (and recovery jogs) are counted on top, so the session's total TRIMP = work + easy overhead."""
    from datetime import timedelta
    zone = spec["zone"]
    zpace = (zones or {}).get(zone) or easy_pace_sec
    per_min_zone = est_trimp(1, zone) or EASY_TRIMP_PER_MIN
    work_min = max(1, round(work_trimp / per_min_zone))
    reps = [_qblock("warmup", "easy", QUALITY_WU_MIN, easy_pace_sec, "easy warm-up")]
    if spec.get("structure") == "intervals":
        rep_min, rec_min = spec.get("rep_min", 3), spec.get("rec_min", 2)
        n_reps = max(1, round(work_min / rep_min))
        for i in range(n_reps):
            reps.append(_qblock("work", zone, rep_min, zpace, f"{rep_min}min @ {zone}"))
            if i < n_reps - 1:
                reps.append(_qblock("recovery", "easy", rec_min, easy_pace_sec, "easy jog recovery"))
        desc = f"{n_reps}×{rep_min}min @ {zone} w/ {rec_min}min jog"
    else:
        reps.append(_qblock("work", zone, work_min, zpace, f"{work_min}min continuous @ {zone}"))
        desc = f"{work_min}min @ {zone}"
    reps.append(_qblock("cooldown", "easy", QUALITY_CD_MIN, easy_pace_sec, "easy cool-down"))
    date = (start_date + timedelta(days=dow)).isoformat()
    note = f"{spec.get('label', spec['kind'])} — {QUALITY_WU_MIN}min easy wu + {desc} + {QUALITY_CD_MIN}min easy cd"
    sess = _session_from_reps(date, spec["kind"], zone, zpace, reps, note)
    comp = spec.get("component") or COMPONENT_BY_KIND.get(spec["kind"])   # §T2 component tag
    if comp:
        sess["component"] = comp
    return sess


def _build_long_mp(date, easy_trimp, work_trimp, spec, zones, easy_pace_sec):
    """§6f Step D — a long run with a MARATHON-PACE finish: an easy aerobic base then a MP segment.
    The MP work is part of the week's quality budget (the polarized hard slice); the easy base is the
    long run's normal easy allotment (`easy_trimp`). The easy base counts as easy, the MP rep as
    work, so the polarized accounting (work ≤ cap) treats this like any other quality session."""
    zone = spec["zone"]                                   # "marathon"
    zpace = (zones or {}).get(zone) or easy_pace_sec
    per_min_zone = est_trimp(1, zone) or EASY_TRIMP_PER_MIN
    base_min = max(1, round(easy_trimp / EASY_TRIMP_PER_MIN))
    mp_min = max(1, round(work_trimp / per_min_zone))
    reps = [_qblock("easy_base", "easy", base_min, easy_pace_sec, "easy aerobic base"),
            _qblock("work", zone, mp_min, zpace, f"{mp_min}min @ marathon pace finish")]
    note = f"{spec.get('label', 'long run')} — {base_min}min easy base + {mp_min}min @ MP finish"
    sess = _session_from_reps(date, "long_mp", zone, zpace, reps, note)
    sess["component"] = spec.get("component") or COMPONENT_BY_KIND["long_mp"]   # §T2 component tag
    return sess


def _distribute_week(wk, start_monday, week_trimp, easy_pace_sec, zones=None, days_override=None,
                     long_km_cap=None, av_blocked=None, q_days=None, long_km_aim=None,
                     free_from=None, ladder=False):
    """Lay `week_trimp` across the week's runs and converting each session's TRIMP back to
    minutes/km. The POLARIZED split (§6f Step C): a `quality` spec carves a small HARD slice of the
    governed weekly TRIMP for structured work (at zone pace), the rest stays easy/long — so total
    weekly TRIMP is unchanged (the ACWR governor still bounds it), intensity is just concentrated.
    Quality needs zone paces, so with `zones=None` (the re-base path) the week stays PURE EASY,
    byte-identical to before. `days_override` lets the caller place runs on an explicit set of
    week-offsets (e.g. only today-onward days for a partially-elapsed week, §6o) instead of the
    frequency's default layout; the last offset is still the long-run slot. `long_km_cap` (§PRO9,
    default None ⇒ byte-identical) caps the single long run's distance: the long-run SHARE is reduced
    (never raised) so its km ≤ the cap, and the freed budget flows to the short easy runs via the
    existing (1−long_w) split — the weekly total is untouched. `long_km_aim` (§PRO15, default None ⇒
    byte-identical) is the opposite lever: the long run's TARGET distance for this week, which only
    ever RAISES the long-run share (the cap above still clips it afterwards). It exists for the §6o
    straddle remainder, where the share-of-budget model sizes the long run off the LEFTOVERS instead
    of off the week. `av_blocked` (§AV, default None ⇒
    byte-identical) is the week's blocked (away) day-offsets: extra easy days may never be laid on
    them, and the mid-quality slot walk keeps the hard-gap invariant (no hard session adjacent to
    the long or to another quality day) — needed because an §AV re-laid day set isn't a vetted
    template. Returns (sessions, day_trimps)."""
    from datetime import timedelta
    days = list(days_override) if days_override is not None else list(_run_days(wk["runs"]))
    n = len(days)                                        # last slot = the long run
    quality = (wk.get("quality") or []) if zones else []
    mid_q = [q for q in quality if q.get("attach") != "long"]
    long_q = next((q for q in quality if q.get("attach") == "long"), None)
    # mid-week quality on the earliest mid slots (Tue, Thu, …) — off slot 0 (first run back) and the
    # long slot; the MP finish (long_q) rides the long run itself. `q_days` (§6o-QF, default None ⇒
    # byte-identical) PINS mid-quality to explicit day-offsets instead of the walk — used by the
    # straddle remainder to keep a still-ahead quality session on its own laid day (slot 0 is
    # allowed there: mid-week the athlete isn't on a "first run back").
    if q_days is not None:
        pins = [days.index(d) for d in q_days if d in days and d != days[n - 1]]
        q_slots = pins[:len(mid_q)]
        mid_q = mid_q[:len(q_slots)]
    else:
        q_slots, s = [], 1
        for _q in mid_q:
            if av_blocked is not None:                   # §AV — hard-gap guard on re-laid day sets
                while s <= n - 2 and ((days[n - 1] - days[s]) < 2
                                      or (q_slots and (days[s] - days[q_slots[-1]]) < 2)):
                    s += 1
            if s <= n - 2:
                q_slots.append(s); s += 1
    mid_q = mid_q[:len(q_slots)]
    q_by_slot = dict(zip(q_slots, mid_q))

    # build mid quality first; total weekly TRIMP stays == week_trimp, so easy_budget is whatever is
    # left after the WORK slices and each quality session's own easy overhead (wu/cd + recovery jogs).
    sessions, day_trimps, mid_total = [], {}, 0.0
    for slot, spec in q_by_slot.items():
        sess = _build_quality(spec, week_trimp * spec["frac"], start_monday, days[slot],
                              zones, easy_pace_sec)
        sessions.append(sess); mid_total += sess["trimp"]
        day_trimps[sess["date"]] = day_trimps.get(sess["date"], 0.0) + sess["trimp"]

    mp_work = week_trimp * long_q["frac"] if long_q else 0.0
    easy_budget = max(0.0, week_trimp - mid_total - mp_work)   # → easy runs + the long-run easy base

    # easy + long runs over the remaining slots — long gets the weighted share (capped so a single
    # day can't spike fatigue); strides ride the first easy run, as in the re-base.
    easy_slots = [i for i in range(n) if i not in q_by_slot]
    long_idx = n - 1
    # re-base is the pure-easy (zones=None) block — keep its original conservative long-run cap so the
    # post-illness restart stays byte-identical; the recalibrated cap applies to the marathon-prep phases.
    long_cap = _long_share_cap(wk, zones)      # §PRO26
    long_w = min(wk["long"] / wk["km"], long_cap) if wk["km"] else 0.0
    n_short = len(easy_slots) - 1                        # the long slot is always present
    # Both §PRO15's aim and §PRO9's cap are TOTAL long-run distances, and the MP finish rides on top
    # of the easy base — so both convert to a base target by subtracting the MP km. Computed once.
    mp_km = 0.0
    if long_q:
        _per = est_trimp(1, long_q["zone"]) or EASY_TRIMP_PER_MIN
        _zp = (zones or {}).get(long_q["zone"]) or easy_pace_sec
        mp_km = round(max(1, round(mp_work / _per)) * 60 / _zp, 1)
    # §PRO15 — the long run is sized off the WEEK, not off what is left of it. `long_w` is a SHARE
    # of the budget, so when the §6o remainder governs a fraction of the week (mid-week regeneration,
    # over-run early days) the long run takes the same proportional haircut as the easy days — the
    # one session that should not. `long_km_aim` is what a non-straddling week of this intent would
    # lay; raising the share to hit it lets the §JR floor below SHED an easy day instead, which is
    # the durability principle already used in reverse when §PRO9 clips a long run and spreads the
    # freed budget over MORE easy days. Deliberately NOT bounded by `long_cap` (LONG_RUN_MAX_FRAC is
    # a fraction of the WEEK; the aim is already week-derived, and the remainder is not the week).
    # Only ever RAISES — the §PRO9 clip below runs after it and still owns the ceiling.
    # The aim is clamped by the §PRO9 cap HERE rather than left to the clip below, so the share it
    # asks for is one the cap would allow. Asking past the cap and clipping afterwards loses volume:
    # a share near 1.0 starves the shorts, §JR sheds them all, and the clip then cuts the long with
    # nothing left to receive the freed budget.
    if long_km_aim and easy_budget > 0:
        _aim = min(long_km_aim, long_km_cap) if long_km_cap else long_km_aim
        _aim_tr = max(0.0, _aim - mp_km) * easy_pace_sec / 60.0 * EASY_TRIMP_PER_MIN
        long_w = max(long_w, min(_aim_tr / easy_budget, 1.0))
    # §PRO21 — the long run must BE the week's longest run, and a SHARE does not guarantee that. The
    # long slot takes `long_w` of the easy budget while each short takes (1−long_w)/n_short, so the two
    # are EQUAL at long_w = 1/(n_short+1) — and BASE_LONG_FRAC is 0.25 against 3 short easy days, i.e.
    # exactly there. Measured in the building fixture: easy 6.7 km ×3 and a "long run" of 6.3 km, the
    # long run SHORTER than every easy day, labelled long and passing the LONG_RUN_MIN_KM floor. Raise
    # the share to the least that clears LONG_RUN_MIN_RATIO: long_km ≥ R × short_km with the algebra
    # above gives long_w ≥ R/(n_short+R). Bounded by `long_cap` (the Daniels/Hansons share ceiling) so
    # this can never buy a long run the doctrine forbids, and only ever RAISES — §PRO9's clip runs
    # after it and still owns the ceiling, exactly as it does for §PRO15's aim. Where the clip then
    # binds (his real weeks) the long run cannot rise, so the easy-day clamp below takes over instead:
    # one principle, two levers, chosen by whether the biomechanical cap has the long run pinned.
    # BOTH levers aim at the same target — 1/LONG_RUN_EASY_FRAC — and never at LONG_RUN_MIN_RATIO
    # itself: aiming AT the honesty threshold leaves nothing for the round-to-0.1 km at the end, and a
    # week built to exactly 1.15 rounds under it and gets relabelled by the very check it satisfied.
    # (Seen: fixture weeks 5 and 6 built to 1.150 and relabelled.) The margin is the whole reason the
    # constants are a PAIR rather than one number.
    if n_short > 0 and easy_budget > 0:
        _shape_w = 1.0 / LONG_RUN_EASY_FRAC
        long_w = max(long_w, min(_shape_w / (n_short + _shape_w), long_cap))
    # §PRO23 part 2 — THE LADDER MUST BE REACHED, OR IT IS A FIXED POINT. Part 1 bounds the week at
    # `long_km_cap / BASE_LONG_FRAC`, which delivers the Daniels/Hansons share ONLY IF the long run
    # actually rises to the cap. Nothing made it: every lever above aims at a RATIO (§PRO21's
    # 1/LONG_RUN_EASY_FRAC) or at the skeleton's share, and §PRO9 below only ever LOWERS. So a long run
    # could settle strictly under its own ceiling — and then `LONG_RUN_STEP_CAP × laid` reproduces the
    # same ceiling next week, for ever. MEASURED on det/regime-plan's fixture (every seeded run 6.0 km):
    # laid long 6.0 against a 6.6 ladder ⇒ the base block froze at 26.2 km for TEN weeks at a 22.9%
    # share, under the floor part 1 was supposed to guarantee. ⭐ THE SAME FIXED POINT that killed the
    # first cut of part 1 (which bounded on the laid long and froze his real base at 44.3 km / ladder
    # 12.3 / laid long 11.2 for four weeks) — one level out, and invisible to a share-only test.
    # So: raise the long run TO its ladder, bounded by `long_cap` — the same Daniels/Hansons share
    # ceiling every other lever here respects. With part 1 holding the week at ladder/BASE_LONG_FRAC and
    # this holding the long run at the ladder, the long run lands INSIDE the 25–30% band by construction
    # and the ladder advances +10%/wk instead of quoting itself. Only ever RAISES; `long_km_cap` is None
    # outside the assertive regime, so caution never evaluates it ⇒ byte-identical, as for part 1.
    if long_km_cap and easy_budget > 0 and n_short > 0:
        _w_ladder = (max(0.0, long_km_cap - mp_km) * easy_pace_sec / 60.0 * EASY_TRIMP_PER_MIN) / easy_budget
        long_w = max(long_w, min(_w_ladder, long_cap))
    # §PRO9 — long-run progression cap. Clip the long-run SHARE so its distance ≤ `long_km_cap`; the
    # freed budget flows to the short easies through the (1−long_w) split below (weekly total untouched).
    # Needs somewhere to redistribute (n_short>0) and a positive easy budget; the MP-finish km rides on
    # top of the easy base, so the base is capped to leave room for it (base + MP ≤ cap). Only ever lowers
    # long_w — never raises it — so a set cap can only shrink the long run, never inflate it.
    long_step_capped = False
    cap_short_trimp = None
    if long_km_cap and easy_budget > 0 and n_short > 0:
        base_cap_km = max(0.0, long_km_cap - mp_km)
        w_cap = (base_cap_km * easy_pace_sec / 60.0 * EASY_TRIMP_PER_MIN) / easy_budget
        if w_cap < long_w:
            long_w, long_step_capped = w_cap, True
        # §PRO19 — this clamp+spread used to live INSIDE the `w_cap < long_w` branch, i.e. it only ran
        # when the cap actually clipped the LONG SLOT. That was sufficient while the long run was 42–48%
        # of the week and therefore always the week's longest run by construction. §PRO18 dropped it to
        # 25–30%, and the guarantee broke through a door nobody had opened: with the long slot already
        # at/under the cap (so no clip, so no clamp), the SHORT easies can be laid LONGER than the long
        # run and sail past the ceiling untouched. Measured on his live plan #67: cap 9.35 km, long slot
        # 9.4 ✓, and two "easy" days at 10.6 km — 13% over the +10% ceiling, and `_week_long_km` then
        # seeded the next week's baseline off 10.6, so the ladder ratcheted 9.4 → 11.7 (+24%) and stayed
        # inflated for the whole block. This is the SAME ratchet the 2026-07-01 adversarial review
        # fixed; the fix was just conditioned on the wrong thing. The cap is a promise about the single
        # longest run of the week, whatever it is LABELLED (`_week_long_km` says so in its own
        # docstring) — so the clamp must run whenever a cap EXISTS, not only when it bit the long slot.
        # §PRO9 (fix 2026-07-01) — the freed long-run budget must NOT reappear as an OVER-CAP easy
        # run: that would breach the +10% single-longest-run promise (the whole biomechanical point)
        # AND ratchet the trailing baseline off the inflated run. So bound every short easy at the cap
        # too, spreading the volume over MORE easy days (the durability principle — more frequent,
        # shorter — not fewer bigger) so the weekly total still lands. `cap_short_trimp` = an easy run
        # at exactly the cap distance; the easy loop hard-clamps each short to it as the final guarantee.
        cap_short_trimp = long_km_cap * easy_pace_sec / 60.0 * EASY_TRIMP_PER_MIN
        # §PRO21 — the §PRO9 clamp above bounds the shorts at the LONG RUN'S OWN CEILING, so on a week
        # whose budget reaches that ceiling every day lands on the same number and the long run stops
        # being one (see LONG_RUN_EASY_FRAC). Bound each short at a fraction of the long run ACTUALLY
        # LAID instead — `long_w` is final here (the §PRO9 clip above already ran), and the MP finish
        # rides on top of the easy base, so the long run's real distance is the base share + `mp_km`.
        # Compared in KM, not TRIMP: an MP km costs more TRIMP than an easy km, and the thing that has
        # to differ between the long run and an easy day is DURATION. Only ever LOWERS the short cap
        # (min), so the +10% promise above is untouched; the spread below then lands the same weekly
        # total across more days. Guarded > 0 — a week whose long slot got no share must not clamp its
        # easy days to zero (the loop below hard-clamps to `cap_short_trimp` unconditionally).
        _pro9_short_trimp = cap_short_trimp          # the +10% ceiling, before the shape cap narrows it
        _long_km_laid = (easy_budget * long_w / EASY_TRIMP_PER_MIN) * 60.0 / easy_pace_sec + mp_km
        _shape_trimp = (LONG_RUN_EASY_FRAC * _long_km_laid) * easy_pace_sec / 60.0 * EASY_TRIMP_PER_MIN
        if _shape_trimp > 0:
            cap_short_trimp = min(cap_short_trimp, _shape_trimp)
        short_budget = easy_budget * (1 - long_w)
        if cap_short_trimp > 0:
            _need = short_budget / cap_short_trimp
            need_short = max(n_short, int(_need) + (1 if _need - int(_need) > 1e-9 else 0))
            free = [d for d in range(7) if d not in set(days)
                    and d not in (av_blocked or ())    # unused weekdays for extra easy runs (§AV:
                    #                                    an away day can never gain a run)
                    # §PRO15 — nor a day that has already HAPPENED. This spread only ever ran on
                    # full-week lays before, where every offset is in front of you; on the §6o
                    # remainder it would hand a session to a past weekday (and collide with the
                    # elapsed lay on that same date). `free_from` = the earliest legal offset.
                    and (free_from is None or d >= free_from)]
            while n_short < need_short and len(days) < 7 and free:
                days.append(free.pop(0)); easy_slots.append(len(days) - 1); n_short += 1
            # §PRO21 — the spread can run out of days (all 7 used, or §AV blocked the free ones).
            # Left alone, the shorts would then be hard-clamped below what the budget needs and the
            # week would silently SHED volume — a load decision taken by a SHAPE rule, which has no
            # business making one. Relax the shape cap to exactly what lands the weekly total, never
            # above the §PRO9 ceiling it narrowed. The week stays flat, and _mark_load_integrity's
            # LONG_RUN_MIN_RATIO check then says so out loud instead of the plan quietly under-training.
            if 0 < n_short < need_short:
                cap_short_trimp = max(cap_short_trimp,
                                      min(_pro9_short_trimp, short_budget / n_short))
    # §JR — junk-run floor: when the governed budget spread over the template's short-easy days
    # would prescribe runs under RUN_MIN_KM, shed short days (nearest the long run first — the
    # freed day doubles as pre-long freshness) until the survivors are real runs. Collapses
    # gracefully: n_short → 0 hands the whole easy budget to the long slot (one honest run, never
    # five stubs). Normal weeks never trip it; the quality slots are untouched.
    # taper is EXEMPT: race-week leg-looseners are deliberately tiny — a 2km shakeout before a race
    # is a real prescription, not a junk run (same reasoning as _mark_load_integrity's exemption)
    min_tr = 0.0 if _is_taper(wk) else \
        RUN_MIN_KM * easy_pace_sec / 60.0 * EASY_TRIMP_PER_MIN
    if 0 < easy_budget * long_w < min_tr and long_idx in easy_slots:
        easy_slots = [long_idx]                              # even the LONG's share is a stub —
        n_short = 0                                          # one honest run takes the whole budget
    while n_short > 0 and easy_budget * (1 - long_w) / n_short < min_tr:
        drop = max((i for i in easy_slots if i != long_idx), key=lambda i: days[i])
        easy_slots.remove(drop)
        n_short -= 1
    # §JR (0.26.1) — not even ONE honest run fits. The collapse above needs a POSITIVE stub
    # (`0 < …`), so a budget crushed to exactly zero sailed past it: the shorts shed, the long slot
    # survived, and the plan published a "0.0 km · long easy run" (live 2026-08-18: ACWR 1.32 ≥ the
    # hard ceiling governed the straddle remainder to nothing, and the card counted the stub as a
    # run). A zero budget lays NOTHING — taper included, a 0-km "leg-loosener" is no prescription
    # at all — and a positive budget below one honest run sheds the collapsed long slot too.
    if easy_budget <= 1e-9:
        easy_slots, n_short = [], 0
    elif long_idx in easy_slots and n_short == 0 and easy_budget < min_tr:
        easy_slots = []
    # §PRO9 (§PRO15 follow-on) — the cap is a PROMISE about the single longest run, so it has to
    # survive the §JR collapse too. The share-based clip above requires n_short > 0; once shedding
    # leaves the long slot alone it takes the WHOLE easy budget (`long_w if n_short else 1.0`) and
    # nothing was watching the cap. Latent before §PRO15 (a collapse needed a crushed budget); the
    # aim raises the long share, so shedding — and this path — became reachable on a normal week.
    long_only_cap_tr = None
    if long_km_cap and n_short == 0 and long_idx in easy_slots:
        long_only_cap_tr = max(0.0, long_km_cap - mp_km) * easy_pace_sec / 60.0 * EASY_TRIMP_PER_MIN
        if long_only_cap_tr < easy_budget:
            long_step_capped = True
    # §PRO24 — the short easies take LADDER shares of their budget instead of 1/n_short each.
    # Computed HERE, after §JR has finished shedding, so the ladder is laid over the slots that
    # actually survive (`easy_slots` and `n_short` are kept in step by the shed loop above).
    # Sums to 1 by construction ⇒ the week's total is identical to the uniform split; this moves
    # km BETWEEN days and never into or out of the week.
    # ⚠ OPT-IN, DEFAULT OFF, AND ONLY EVER ON AN ASSERTIVE FULL WEEK — the §PRO8 template.
    #   · not the §6o REMAINDER. A ladder is a property of a WEEK, and the remainder is not one — it is
    #     whatever days are left after today (Sat+Sun on his 2026-08-07 regen). §PRO15 states the same
    #     principle two screens up: "the remainder is not the week".
    #   · not CAUTION. Every other lever in this function is assertive-only for the same reason, and
    #     the caution baseline is the byte-identity guard the whole accelerator is measured against.
    #     Reaching caution ALSO broke it in a way worth recording: caution has no `long_km_cap`, so it
    #     has no `cap_short_trimp` either — nothing clamps the shorts — and the ladder's head rose
    #     ABOVE the long run on four base weeks, re-opening the exact defect §PRO21 exists to prevent
    #     (det/long-run-identity + det/building-load-integrity both caught it).
    # Default OFF so every det that calls this directly keeps its pre-§PRO24 output, and the flag is
    # passed explicitly rather than inferred from `long_km_aim`/`long_km_cap` so a future caller can't
    # switch the shape on or off as a side effect of threading an unrelated argument — and so the
    # "non-binding cap ≡ no cap" identity in det/long-run-step keeps meaning what it says.
    _short_slots = [i for i in easy_slots if i != long_idx] if ladder else []
    # ⚠⚠ THE RUNGS ARE ORDERED BY DISTANCE FROM THE NEAREST LONG RUN — *NOT* BY CALENDAR ORDER.
    # MEASURED on 161 of his own weeks (≥4 runs, easy km ÷ the week's mean easy km):
    #   · by SIZE RANK        1.426 / 1.225 / 1.044 / 0.925 / 0.818 / 0.704 → R² = 0.545
    #   · by calendar weekday 1.038 / 1.112 / 0.949 / 1.034 / 1.005 / 0.965 → R² = 0.054
    # The ladder is REAL and strong; its supposed calendar DIRECTION is not there at all. The first
    # cut of this feature laid the rungs in calendar order — unsupported by the data, and it pointed
    # the heavy end at MONDAY, the single day the §H1 peak-ACWR brake pins (the seed ATL is highest on
    # day 1 and decays all week). That cost his 2026-08-03 week 39.0 → 32.7 km of INTENT, and because
    # §6o's remainder is `intent − already run`, all 6.3 km came out of the one day left: the Sunday
    # long run collapsed 10.0 → 3.7 km and was relabelled a shakeout. A shape rule silently taking a
    # LOAD decision — measured, not reasoned.
    # So the magnitudes come from his data and the ORDER comes from doctrine, which his data does at
    # least not contradict — the one calendar bucket that separates is the day AFTER the long run
    # (0.929 ± 0.031 vs 1.086 ± 0.037 two days out, ~3σ), i.e. the classic recovery day. Rung 0 (the
    # longest easy) therefore goes to the day FURTHEST from any long run, and the short rungs fall
    # either side of it: the day after last week's long run, and the day before this week's — which is
    # the pre-long freshness principle §JR already applies when it sheds a day. Deterministic tie-break
    # on the calendar so the lay is reproducible.
    # ABSOLUTE distance on both sides: §PRO9's spread appends free days to `days`, and on an §AV-shifted
    # week the long slot is not always the last calendar day — a day landing AFTER it must read as
    # 1 day from a long run, not −1 (which would sort it to the HEAD of the ladder, the exact opposite
    # of the recovery principle).
    _prev_long_off = days[long_idx] - 7        # last week's long run, same slot, one week back
    _short_slots.sort(key=lambda s: (-min(abs(days[s] - _prev_long_off),
                                          abs(days[long_idx] - days[s])), days[s]))
    _lad = [max(EASY_LADDER_FLOOR, 1.0 - EASY_LADDER_STEP * k) for k in range(len(_short_slots))]
    _lad_tot = sum(_lad) or 1.0
    short_w = {s: _lad[k] / _lad_tot for k, s in enumerate(_short_slots)}
    # ⚠⚠ THE LADDER MUST NOT SPILL VOLUME THROUGH §PRO9'S PER-DAY CLAMP. A uniform split either cleared
    # `cap_short_trimp` on every short day or on none; a ladder puts its HEAD above the clamp first —
    # and the clamp is a `min()` that DROPS the excess on the floor. Measured on his 2026-08-03 week
    # before this guard: 30.7 → 22.0 km and the long run gone, because the clipped head was never
    # given back. WATER-FILL instead: peg the breaching days at the cap and redistribute their surplus
    # across the rest, repeating until nothing breaches (redistribution can push a survivor over too).
    if cap_short_trimp is not None and n_short > 0:
        _pool = easy_budget * (1 - long_w)
        if _pool > 0:
            _cap_w, _free = cap_short_trimp / _pool, set(short_w)
            for _ in range(len(short_w) + 1):
                _over = [s for s in _free if short_w[s] > _cap_w + 1e-12]
                if not _over:
                    break
                _spill = sum(short_w[s] - _cap_w for s in _over)
                for s in _over:
                    short_w[s] = _cap_w
                    _free.discard(s)
                _room = sum(short_w[s] for s in _free)
                if not _free or _room <= 0:
                    break                       # every day is at the cap — the week genuinely cannot
                for s in _free:                 # hold it, and §PRO9's ceiling outranks the shape
                    short_w[s] += _spill * short_w[s] / _room
    # strides land on the freshest-legs day: the short easy slot furthest (max-min day distance)
    # from every heavy session — this week's long run, each quality day, and last week's long one
    # slot back. The old rule ("the first easy run, as in the re-base") predates base-week quality
    # and put a neuromuscular sprint stimulus on the classic recovery day, sandwiching the week's
    # three hardest stimuli back-to-back-to-back: Sun long → Mon strides → Tue intervals (his
    # 2026-08-19 ask — "an increased strain on my legs… three in-a-row"; strain, not stimulus).
    # It is the same principle §PRO24's ladder measured on his own corpus — the day after a long
    # run runs LIGHT (0.929 ± 0.031 vs 1.086 two days out) — and §JR's pre-long freshness shed.
    # Ties break to the LATER day (further from last week's long). On the standard 6-run base week
    # this lands Thursday; every phase moves with the same rule (a shape rule, not a load lever —
    # deliberately NOT caution-byte-identical, weekly km untouched).
    strides_slot = None
    _s_cand = [i for i in easy_slots if i != long_idx]
    if _s_cand:
        _heavy = [days[s] for s in q_by_slot] + [days[long_idx], days[long_idx] - 7]
        strides_slot = max(_s_cand,
                           key=lambda i: (min(abs(days[i] - h) for h in _heavy), days[i]))
    for i in easy_slots:
        is_long = (i == long_idx)
        if is_long:
            tr = round(easy_budget * (long_w if n_short else 1.0), 1)
            if long_only_cap_tr is not None:             # §PRO9 — hold the ceiling on a collapsed week
                tr = min(tr, round(long_only_cap_tr, 1))
        else:
            tr = round(easy_budget * (1 - long_w) * short_w.get(i, 1.0 / n_short), 1) if n_short else 0.0
            if cap_short_trimp is not None:              # §PRO9 — a short easy may never exceed the cap
                tr = min(tr, round(cap_short_trimp, 1))
        date = (start_monday + timedelta(days=days[i])).isoformat()
        if is_long and long_q:                          # marathon-pace finish on the long run
            sess = _build_long_mp(date, tr, mp_work, long_q, zones, easy_pace_sec)
            if long_step_capped:                        # §PRO9 — tell the truth about the cap
                sess["note"] += " — held to the +10% long-run progression cap (trailing-4wk)"
                sess["long_step_capped"] = True
            sessions.append(sess)
            day_trimps[date] = day_trimps.get(date, 0.0) + sess["trimp"]
            continue
        mins = round(tr / EASY_TRIMP_PER_MIN)
        km = round(mins * 60 / easy_pace_sec, 1)
        # §PRO21 — the §PRO9 clip is enforced in TRIMP, but the session is published in km after TWO
        # roundings (TRIMP → whole minutes → km to 0.1), and rounding up through both can land the long
        # run just ABOVE the very cap that clipped it. Latent since §PRO9: the overshoot needs the
        # cap's km to sit near a minute boundary, which no fixture happened to hit — found by scanning
        # his own block after §PRO21 shifted the ladder (2026-09-07: cap 16.7, laid 16.8, and
        # det/long-run-step green throughout, because it never constructed the case). The published
        # number is what he runs and what seeds the next week's baseline, so the promise has to hold
        # on the PUBLISHED value: step whole minutes off until it does. Shorts cannot reach this — the
        # shape cap holds them a clear fraction below the ceiling. (`mp_km` is 0 on this path — the MP
        # long run returns above — so the cap needs no MP allowance here.) `tr` is re-derived from the
        # minutes actually prescribed: mins is the rounded inverse of tr, so leaving tr behind would
        # publish a session whose load did not match its own duration.
        if is_long and long_km_cap:
            while mins > 0 and km > long_km_cap + 1e-9:
                mins -= 1
                km = round(mins * 60 / easy_pace_sec, 1)
                tr = round(mins * EASY_TRIMP_PER_MIN, 1)
        note = "long easy run" if is_long else "easy run"
        carries_strides = bool(wk["strides"]) and not is_long and i == strides_slot
        if carries_strides:
            note += f" + {wk['strides']}×4–6 strides"
        if is_long and long_step_capped:                # §PRO9
            note += " — held to the +10% long-run progression cap (trailing-4wk)"
        sess = {"date": date, "kind": "long" if is_long else "easy",
                "km": km, "minutes": mins, "trimp": tr,
                "pace_zone": f"{fmt_pace(easy_pace_sec)}/km easy", "note": note}
        if carries_strides:   # structured, so the UI can pin the strides to THIS run's line
            sess["strides"] = wk["strides"]
        if is_long and zones:                           # §T2 — the building-phase long run builds economy
            sess["component"] = COMPONENT_BY_KIND["long"]   # (mileage is the economy lever; re-base untagged)
        if is_long and long_step_capped:
            sess["long_step_capped"] = True
        sessions.append(sess)
        day_trimps[date] = day_trimps.get(date, 0.0) + tr
    sessions.sort(key=lambda x: x["date"])
    return sessions, day_trimps


def _project_week(ctl, atl, week_start, day_trimps, roll_from=None, actual_floor=None):
    """Roll the projector across one full week (Mon–Sun). Returns
    (end_ctl, end_atl, eow_acwr, peak_acwr). The PRIMARY governor bound is END-OF-WEEK ACWR
    against the SOFT cap — the settled weekly state, the natural planning cadence — and normal
    long-run-day daily transients (~1.0) are deliberately tolerated. The governor ALSO bounds the
    in-week PEAK against the HARD cap (§H1): that only ever binds at low CTL, where a quality
    session's fixed TRIMP floor makes the mid-week transient pathological (~1.5–1.6); it never
    touches normal-CTL weeks, so the EOW-only soft bound above stands for the common case.
    project_forward only spans to the last planned day, so we extend rest days to Sunday.
    `roll_from` (default = week_start) is where the roll BEGINS: for a partially-elapsed week (§6o)
    pass `today` and seed (ctl, atl) with the §PRO20 end-of-yesterday state — the elapsed days' load is
    already in that seed, so we project only today-onward `day_trimps`, never double-counting them.

    §PRO20b — `actual_floor` ({date: TRIMP}, default None ⇒ byte-identical) raises a day's projected
    load to what was ACTUALLY recorded on it. It exists for one day: TODAY. §PRO20 stops the seed at
    end-of-yesterday, so today's load reaches the projection only through today's PRESCRIPTION — and
    once he has already run, the prescription is moot (on 2026-07-30 it was a rest day, 0 TRIMP,
    against a real 93). Under-reading today's load makes the rest of the week look freer than it is,
    the same direction as the §PRO20 defect. A FLOOR, not a replacement: it can only ever RAISE
    projected load, so it can only ever tighten the governor, and it is unioned in (a day the plan
    left out entirely still gets charged)."""
    from datetime import timedelta
    end = _date(week_start) + timedelta(days=6)
    start_iso = roll_from or week_start                 # where the roll begins (today for a partial week)
    if actual_floor:
        day_trimps = dict(day_trimps or {})
        for _d, _t in actual_floor.items():
            if start_iso <= _d <= end.isoformat():      # only inside the rolled span
                day_trimps[_d] = max(day_trimps.get(_d, 0.0), _t or 0.0)
    curve = project_forward(day_trimps, ctl, atl, start_iso) if day_trimps else []
    last = max(_date(d) for d in day_trimps) if day_trimps else _date(start_iso) - timedelta(days=1)
    cc, aa = (curve[-1]["ctl"], curve[-1]["atl"]) if curve else (ctl, atl)
    cur = last + timedelta(days=1)
    while cur <= end:  # carry rest days to week's end
        cc = _ewma_step(cc, 0.0, TAU_CTL); aa = _ewma_step(aa, 0.0, TAU_ATL)
        curve.append({"date": cur.isoformat(), "trimp": 0.0, "ctl": round(cc, 2),
                      "atl": round(aa, 2), "tsb": round(cc - aa, 2),
                      "acwr": round(aa / cc, 3) if cc else None})
        cur += timedelta(days=1)
    peak = max((p["acwr"] for p in curve if p["acwr"]), default=None)
    eow = curve[-1]["acwr"] if curve else None
    # §PRO16 — the SHAPE-NEUTRAL acute:chronic reading. `eow` samples the ratio on the LAST day of
    # the week, which is the long-run day — the single biggest session there is. That placement alone
    # moves the reading: at an identical, perfectly flat weekly load, the settled EOW ratio reads
    # 1.161 with the long run on Sunday, 0.974 with the same run moved to Tuesday, 1.000 spread
    # evenly, and 1.507 for a 3-run week built around one big long run. None of that is training
    # stress; it is where in the week we happen to look. Judging a 1.25 ceiling against a reading
    # carrying a structural +16% leaves ~12% of real headroom for progression instead of ~30%, and
    # the long run — being the spike that inflates the reading — ends up taxing itself.
    # Comparing the week's MEAN acute load to its MEAN chronic load removes the sampling artefact
    # exactly: at steady state both means equal the mean daily load, so every shape above reads
    # 1.000, while a genuine 8%/wk ramp still reads 1.18. It measures the CHANGE in load, which is
    # what an acute:chronic ratio was always meant to measure.
    m_ctl = sum(p["ctl"] for p in curve) / len(curve) if curve else None
    m_atl = sum(p["atl"] for p in curve) / len(curve) if curve else None
    flat = round(m_atl / m_ctl, 3) if (m_ctl and m_atl is not None) else None
    return curve[-1]["ctl"], curve[-1]["atl"], eow, peak, flat, m_ctl


def _eow_soft(eow, eow_flat, m_ctl, endctl, endatl, shape_neutral, soft_ctl_floor):
    """THE SOFT TEST'S DECISION VARIABLE — the single number the weekly governor compares against the
    ride cap. Extracted (§PRO23) so the value a week PUBLISHES cannot drift from the value the search
    DECIDED on: it used to be inline in `_max_week_trimp` and nowhere else, which meant every reader —
    including det/regime-plan — had to RECONSTRUCT it from rounded published surfaces and could only
    approximate it. That approximation had a structural error, not a rounding one, and it cost a real
    debugging session: the det reconstructed the floored ratio with `proj_ctl` (END-of-week CTL) where
    the governor divides by `m_ctl` (the week's MEAN CTL). On a steep building week at low CTL those
    differ by ~5% — measured 44.1 vs 42.02 — so a week the governor had correctly held at 1.1496 under
    a 1.15 eased cap was read back as 1.206 and looked like a breach of §PRO5's brake. It was not.
    ⭐ THE LESSON: a governor that keeps its decision variable private forces its own tests to guess.

    §PRO16 — assertive judges the SHAPE-NEUTRAL reading (mean acute / mean chronic), because the
    last-day sample is the long-run day and carries a structural offset that is placement, not stress.
    §PRO8 — the chronic DENOMINATOR is floored at `soft_ctl_floor` so the settled-week ratio stops
    being hypersensitive at low CTL. Caution passes neither ⇒ it keeps the original expression, which
    is reproduced here verbatim (including reading the ROUNDED `eow`) so caution stays byte-identical."""
    if shape_neutral and eow_flat is not None and m_ctl:
        if soft_ctl_floor and m_ctl < soft_ctl_floor:
            return (eow_flat * m_ctl) / soft_ctl_floor
        return eow_flat
    out = eow
    if soft_ctl_floor and endctl is not None and endatl is not None and endctl < soft_ctl_floor:
        out = endatl / soft_ctl_floor
    return out


def _max_week_trimp(ctl, atl, wk, start, easy_pace_sec, cap, zones=None, roll_from=None,
                    days_override=None, ramp_max=None, soft_ctl_floor=None, av_blocked=None,
                    q_days=None, prog_floor=None, shape_neutral=False,
                    session_eq_cap=None, week_eq_cap=None, long_km_cap=None, actual_floor=None,
                    ladder=False):
    """Binary-search the largest weekly TRIMP whose END-OF-WEEK projected ACWR stays ≤ cap AND whose
    in-week PEAK ACWR stays ≤ ACWR_HARD (§H1). Distributes WITH the week's quality (via `zones`) so
    the bound is on the real, intensity-distributed week. The peak/hard bound only bites at low CTL,
    where a quality session's fixed TRIMP floor spikes the mid-week transient; at normal CTL eow is
    the binding constraint and the hard ceiling is slack.
    `roll_from`/`days_override` thread through to project only today-onward days for a partially-
    elapsed week (§6o), so the remaining allowance is bounded against load already done this week.
    §PRO1 — `ramp_max` (default None) is the optional CTL-ramp-rate ceiling: when set, also reject any
    week whose projected END-CTL would exceed `ctl + ramp_max` (chronic-load growth cap, the
    connective-tissue backstop). None ⇒ byte-identical to the original ACWR-only governor; only the
    assertive regime passes it. It can only LOWER the allowance — a pure additional ceiling.
    §PRO8 — `soft_ctl_floor` (default None) floors the CTL DENOMINATOR of the SOFT (end-of-week) ACWR
    test only, at low chronic load (see ACWR_SOFT_CTL_FLOOR): the settled-week ratio stops being
    hypersensitive so the soft ceiling can rise toward demonstrated tolerance. It does NOT touch the
    in-week PEAK test, which keeps the RAW CTL (so the hard cap stays the true acute-spike brake), nor
    the ramp test. None ⇒ byte-identical; it can only RAISE the soft allowance, never the peak/ramp bound.
    §PRO10 — `prog_floor` (default None) is the progressive-overload floor: the SOFT test may not clip
    the allowance below it (allowance = min(hard/ramp-allowed, max(soft-allowed, prog_floor))). The
    PEAK and ramp tests always clip it — the floor asks for progression, the acute brakes decide how
    much of it is safe. None ⇒ byte-identical (the soft test clips freely)."""
    lo, hi = 0.0, 700.0
    for _ in range(34):
        mid = (lo + hi) / 2
        # §PRO17 — the search must evaluate the SAME distribution the week will actually be laid
        # with. It used to omit `long_km_cap`, so §PRO9's clip (and the redistribution of the freed
        # budget onto more easy days) happened only AFTER the bound was chosen: the search bounded a
        # week nobody ever ran. Latent while the long cap rarely bound; §PRO18's 0.30 doctrine share
        # makes it bind often, and it surfaced as the eased ride_cap not holding (three weeks at
        # flat 1.16 against a 1.15 cap). One distribution, evaluated once.
        _sess, dt = _distribute_week(wk, _date(start), mid, easy_pace_sec, zones,
                                     days_override=days_override, av_blocked=av_blocked,
                                     q_days=q_days, long_km_cap=long_km_cap,
                                     ladder=ladder)         # §PRO24 — §PRO17's rule: search the week
        #                                                     that will actually be laid, ladder and all
        # §PRO20b — the search must charge today's ACTUAL load, or the allowance it hands back is
        # bounded against a week he has already partly outrun. Floor-only ⇒ it can only tighten.
        endctl, endatl, eow, peak, eow_flat, m_ctl = _project_week(
            ctl, atl, start, dt, roll_from=roll_from, actual_floor=actual_floor)
        # §PRO16 — judge the SOFT test on the SHAPE-NEUTRAL reading (mean acute / mean chronic across
        # the week) instead of the last-day sample, which is the long-run day and carries a structural
        # offset of ~+16% that is placement, not stress. The PEAK test below is untouched and stays on
        # the raw per-day curve: guarding the in-week acute SPIKE is exactly what a per-day sample is
        # for, and it is the §H1 brake. Assertive-only ⇒ caution byte-identical.
        # §PRO8 — judge the SOFT test against a floored chronic denominator; the PEAK stays raw, so
        # the hard cap remains the genuine acute-spike ceiling.
        # The caution branch is TEXTUALLY the original expression, including reading the ROUNDED `eow`
        # rather than recomputing endatl/endctl: recomputing shifts the binary search by a last decimal
        # and caution stops being byte-identical (caught exactly that way — every trimp 0.1 low).
        eow_soft = _eow_soft(eow, eow_flat, m_ctl, endctl, endatl, shape_neutral, soft_ctl_floor)
        too_fast = ramp_max is not None and endctl is not None and endctl > ctl + ramp_max + 1e-9
        # §PRO10 — the soft test can't clip below the progression floor; peak/ramp always can
        soft_bad = eow_soft and eow_soft > cap and (prog_floor is None or mid > prog_floor)
        # §PRO17 — the BIOMECHANICAL bounds replace the per-day ACWR ceiling as the acute brake.
        # Injuries are biomechanical, not physiological (Davis); the per-day ratio was measuring where in
        # the week we sampled as much as what was run, and §49 showed it had become the volume governor
        # while the rescue it was supposed to trigger never fired. Two bounds, both on the damage axis:
        #   · per SESSION — no bout's eq_km may jump past SESSION_EQ_STEP × the trailing largest. This is
        #     the Aarhus finding (sharp longest-run jumps predicted injury; weekly-mileage jumps did not)
        #     generalised past the long run and expressed in damage rather than raw km.
        #   · per WEEK — the week's eq_km may not jump past the §3.1 ceiling. Without this a per-session
        #     rule is blind to five medium-hard days where nothing jumps but the week is a wall.
        # The per-day ACWR test is only STOOD DOWN where a biomechanical bound is actually in force: with
        # no trailing baseline (cold start) there is nothing to jump against, and something must still
        # bound the acute day. Caution never passes these ⇒ it keeps the old test ⇒ byte-identical.
        _bio_on = bool(session_eq_cap or week_eq_cap)
        _peak_governs = not (shape_neutral and _bio_on)
        _sess_eq = max((_session_eq_km(x) for x in _sess), default=0.0)
        _wk_eq = _week_eq_km(_sess)
        bio_bad = ((session_eq_cap and _sess_eq > session_eq_cap + 1e-9)
                   or (week_eq_cap and _wk_eq > week_eq_cap + 1e-9))
        # §PRO23 — THE TWO CLOCKS. Weekly volume grew on the ACWR/CTL clock; the long run grew on the
        # Aarhus ladder (LONG_RUN_STEP_CAP × the trailing-window longest). NOTHING TIED THEM, so the
        # week could outrun the longest run that anchors it and the long run's SHARE — the one number
        # Daniels/Hansons actually prescribe — was whatever fell out. MEASURED on his 2026-08-07 plan:
        # base weeks at 20.9 / 22.1 / 22.7 / 23.6% against the 25% doctrine floor, every week pinned to
        # the ACWR ceiling. That is not a plan with a long run in it; it is a volume distribution that
        # happens to have a longest day — and because the ceiling is a RATIO of recent load, the week
        # was a feedback trace of the last fortnight rather than a prescription (two rest days tripled
        # it). §PRO21 treated the SYMPTOM — the long run reading the same as the easy days — with two
        # more governors, and bought the ratio by spending the surplus on a sixth running day. This is
        # the cause: the clocks, coupled.
        # ONE constraint, NO new constant: the week may not exceed what the ladder can anchor at
        # BASE_LONG_FRAC — the 0.25 the skeleton already targets (§PRO18, the Daniels/Hansons band).
        # ⚠⚠ BOUND ON `long_km_cap`, NEVER ON THE LAID LONG RUN. The laid long is a fixed SHARE of the
        # week, so long/week is constant in the search variable and a laid-long form bites only on a
        # rounding edge. The first cut did exactly that and DEADLOCKED — base froze at 44.3 km for four
        # straight weeks (ladder 12.3, laid long 11.2, 1.10 × 11.2 = 12.3 forever: a fixed point where
        # the long run never reaches its own cap so the cap can never rise). Found by measuring, not by
        # reading. `long_km_cap` is exogenous and advances +10%/wk off the long runs the block itself
        # lays, so bounding on IT makes the week grow WITH the long run instead of away from it.
        # Assertive-only for free — `long_km_cap` is None unless assertive (generate_block), so caution
        # never evaluates this ⇒ byte-identical, MEASURED (91 leaves, equal md5, 393.6 km), not argued.
        # Can only ever LOWER the allowance: a pure additional ceiling, the §PRO8 template.
        # ⚠ FLOORED AT THE SHAPE'S OWN INTENT. The coupling exists to stop the ASSERTIVE path riding the
        # ACWR ceiling FAR ABOVE the designed trajectory (his base weeks ran 2.2–2.9× their skeleton) —
        # it is not a licence to cut BELOW the design. Without this floor a low-volume fixture whose
        # ladder is small (a returning athlete: longest run 5 km ⇒ bound 22 km) has its whole block
        # crushed, and det/regime-plan caught exactly that: the assertive build peak fell to 28.2 km,
        # BELOW the caution baseline it is supposed to exceed. The floor costs nothing on his real plan
        # (every base week's bound sits far above its intent) and keeps the constraint a trimmer.
        _couple_km = max(long_km_cap / BASE_LONG_FRAC, (wk.get("km") or 0.0)) if long_km_cap else None
        shape_bad = (bool(long_km_cap) and BASE_LONG_FRAC > 0
                     and sum((x.get("km") or 0.0) for x in _sess) > _couple_km + 1e-9)
        if soft_bad or bio_bad or shape_bad or (_peak_governs and peak and peak > ACWR_HARD) or too_fast:
            hi = mid
        else:
            lo = mid
    return lo


def _apply_adjustment(sessions, dt, adj):
    """Apply a clamped qualitative directive (§6c) to one week's in-window days. Scales planned
    load by the multiplier (already clamped to [0,1] — reduce-only), forces easy effort if asked,
    and turns a 0× day into rest. Returns copies + whether this week was touched. The engine,
    not the LLM, owns these numbers; this only ever lowers load, so the ACWR ceiling is safe."""
    if not adj:
        return {"sessions": sessions, "dt": dt, "touched": False}
    lo, hi, m = adj["applies_from"], adj["applies_until"], adj["volume_multiplier"]
    easy_only = adj.get("easy_only")
    out_s, out_dt, touched = [], dict(dt), False
    for d in list(out_dt):
        if lo <= d <= hi:
            out_dt[d] = round(out_dt[d] * m, 1)
            touched = True
    for s in sessions:
        if lo <= s["date"] <= hi:
            s = {**s, "trimp": round(s["trimp"] * m, 1), "km": round(s["km"] * m, 1),
                 "minutes": round(s["minutes"] * m)}
            if s.get("reps") is not None:   # an eased quality day drops its structure (reduce-only)
                s["reps"], s["zone"] = None, None
            if m == 0:
                s["kind"], s["note"] = "rest", "rest — eased by your check-in"
                s.pop("component", None)   # §T2 — an eased-away session builds nothing; don't claim it
            elif easy_only and s["kind"] not in ("easy", "rest", "long"):
                s["kind"], s["note"] = "easy", "easy only — eased by your check-in"
                s.pop("component", None)   # §T2 — no longer the component-building session
            else:
                s["note"] = "eased — " + s.get("note", "")
        out_s.append(s)
    return {"sessions": out_s, "dt": out_dt, "touched": touched}


def _week_role(w):
    """§P1 — a week's periodization ROLE, read from the field the shapers stamp. Falls back to parsing
    the human `intent` sentence ONLY for a week that predates the field (a plan JSON saved before §P1,
    or a hand-built fixture), so old stored plans keep grading correctly. Accepts a week dict or a bare
    intent string; `det/week-role` holds field and sentence in agreement on every shape the engine lays."""
    if isinstance(w, dict):
        r = w.get("role")
        if r:
            return r
        w = w.get("intent")
    t = str(w or "").lower()
    return ("down" if t.startswith("down") else
            "race" if t.startswith("race week") else
            "taper" if t.startswith("taper") else "build")


def _week_phase(w):
    """§P1 — a week's PHASE (rebase/base/build/peak/taper), read from the field the shapers stamp.
    Falls back to the "Peak"-prefix sniff the §PRO6 deload exemption used before the field existed,
    so a plan JSON saved before §P1 still exempts its peak weeks."""
    if isinstance(w, dict):
        ph = w.get("phase")
        if ph:
            return ph
        w = w.get("intent")
    t = str(w or "").lower()
    return ("peak" if t.startswith("peak") else
            "taper" if (t.startswith("taper") or t.startswith("race week")) else None)


def _long_share_cap(wk, zones):
    """§PRO26 — the long run's share ceiling for THIS week. The re-base (pure-easy, zones=None) keeps
    its own conservative cap so the post-illness restart stays byte-identical; every other phase takes
    its own entry, falling back to the block-wide §PRO18 doctrine number."""
    if not zones:
        return REBASE_LONG_CAP
    return LONG_RUN_MAX_FRAC_BY_PHASE.get(_week_phase(wk), LONG_RUN_MAX_FRAC)


def _is_down(w):
    """A week is a deliberate down/recovery week iff its ROLE says so — uniform across every shape
    (re-base wk4, base/build 3:1). The single test the banking gates + the earned lift share."""
    return _week_role(w) == "down"


def _is_taper(w):
    """A taper or race week — deliberately low-volume by design. Its short long run is the plan
    working, not a fatigue cap, so the load-integrity honesty pass must NOT relabel/flag it."""
    return _week_role(w) in ("taper", "race")


def _current_week_actuals(db, today):
    """§6e-FREQ — actual run-days + km the athlete has logged in the CALENDAR week (Mon–Sun) holding
    `today`, from owned data only (ignored/deleted excluded). Feeds the frequency-met check: once the
    current week's prescribed run COUNT *and* volume are both already met, an additional same-week run
    isn't forced (a short junk run on a met week does nothing for aerobic shape). Returns (runs, km)."""
    from datetime import timedelta
    mon = today - timedelta(days=today.weekday())
    sun = mon + timedelta(days=6)
    drop = dropped_ids(db)
    rows = db.execute(
        "SELECT id, date, distance FROM activities WHERE date>=? AND date<=? AND " + RUN_FAMILY_SQL,
        (mon.isoformat(), sun.isoformat())).fetchall()
    act_km = round(sum(r["distance"] for r in rows if r["id"] not in drop and r["distance"]), 1)
    act_runs = len({r["date"] for r in rows if r["id"] not in drop and r["distance"]})
    return act_runs, act_km


def _week_long_km(sessions):
    """§PRO9 — the longest single run (km) in a finalized week's sessions (long/long_mp/easy all count;
    the biomechanical lever is the actual longest run, whatever it's labelled). 0.0 if none."""
    return max((s.get("km") or 0.0) for s in (sessions or [])) if sessions else 0.0


def _recent_long_runs(db, before, n_weeks=LONG_RUN_STEP_WINDOW):
    """§PRO9 — the longest single logged run (km) in each of the `n_weeks` calendar weeks (Mon–Sun)
    immediately BEFORE `before` (a date), oldest-first, owned data only. Seeds the long-run progression
    cap's trailing window so the FIRST assertive building weeks are bounded against his real recent long
    runs — assertive skips the re-base, so the plan's own generated weeks don't seed it. Empty weeks
    contribute nothing (a gap can't set the baseline; the cap then binds off whatever recent runs exist)."""
    from datetime import timedelta
    drop = dropped_ids(db)
    mon0 = before - timedelta(days=before.weekday())        # Monday of `before`'s week
    out = []
    for w in range(n_weeks, 0, -1):                          # oldest → most recent
        ws = mon0 - timedelta(days=7 * w)
        we = ws + timedelta(days=6)
        rows = [r for r in db.execute(
            "SELECT id, date, date_time, distance, duration, elapsed_time FROM activities "
            "WHERE date>=? AND date<=? AND " + RUN_FAMILY_SQL,
            (ws.isoformat(), we.isoformat())).fetchall()
            if r["id"] not in drop and r["distance"]]
        # §SJ — the longest single OUTING: a deliberately split recording (save-and-restart minutes
        # apart) is one continuous biomechanical load, so a group's km SUM competes for "longest".
        longest = max((sum(p["distance"] for p in g) for g in _session_groups(rows)), default=0.0)
        if longest:
            out.append(round(longest, 1))
    return out


def _session_eq_km(sess):
    """§3.1 — damage-equivalent km for ONE session: Σ rep_km × f(rep zone) for a structured session (so a
    tempo/interval's warm-up/cool-down count as easy and only the work reps carry the fast weight), else
    km × f(session zone|kind). Fast km count for far more than easy km (Davis f grid)."""
    reps = sess.get("reps")
    if reps:
        return round(sum((r.get("km") or 0.0) * EQ_KM_FACTOR.get(r.get("zone"), 1.0) for r in reps), 2)
    z = sess.get("zone") or sess.get("kind") or "easy"
    return round((sess.get("km") or 0.0) * EQ_KM_FACTOR.get(z, 1.0), 2)


def _week_eq_km(sessions):
    """§3.1 — the week's total damage-equivalent km (Σ over its sessions). 0.0 if none."""
    return round(sum(_session_eq_km(s) for s in (sessions or [])), 2)


def _eq_factor(gap_pace_sec, zones):
    """§3.1/§PRO22 — the damage weight f for an ACTUAL run's grade-adjusted pace, INTERPOLATED between
    the EQ_KM_FACTOR anchors instead of bucketed into the fastest zone the pace met.

    The bucketed form was a step function: `gap_pace <= zone_pace` at 1 s/km granularity, so two runs a
    single second apart differed by a whole factor (1.0 → 1.4 at the marathon edge, +40%). Because the
    zone paces are derived from eVO₂max, the edges MOVE, and a run that has already happened can cross
    one. Measured on his own data (2026-08-03): his 07-22 run sat at GAP exactly 377 s/km with the
    marathon anchor at 377; a good evening run nudged eVO₂max 35.00 → 35.29, the anchor moved to 376,
    and that run's eq_km fell 8.95 → 6.39. It was the trailing window's largest bout, so
    `session_eq_cap` fell 11.635 → 11.05, the week's long bout (11.30) no longer fitted, and the
    governor's binary search cut the week from 49.7 to 42.6 km — a 15% swing off one second of pace.

    Damage per km rises CONTINUOUSLY with speed (loading cycles × load-per-step); there is no step at
    marathon pace. So f is piecewise-linear in pace between the anchors, flat outside them. The anchors
    and their values are UNCHANGED — at any anchor pace this returns exactly what the buckets returned,
    so §PRO17's calibration points still read the same. What changes is BETWEEN anchors, and it changes
    in the honest direction: a run at 6:43/km with easy 7:16 and marathon 6:17 scored 1.0 (as if it were
    a recovery jog) and now scores ~1.22. That under-count was systematic for him — his easy runs run
    hot (8 of the last 14 judged "hot"), so the axis meant to see biomechanical cost was blind to
    exactly the habit that generates it. ⚠ The calibration percentiles move with this; re-measured on
    the full corpus, see PROJECT_LOG §58."""
    if not gap_pace_sec or not zones:
        return EQ_KM_FACTOR["easy"]
    anchors = sorted(((float(zones[z]), EQ_KM_FACTOR[z])
                      for z in ("easy", "marathon", "threshold", "interval") if zones.get(z)),
                     key=lambda a: -a[0])                  # slowest pace first ⇒ f ascending
    if not anchors:
        return EQ_KM_FACTOR["easy"]
    p = float(gap_pace_sec)
    if p >= anchors[0][0]:
        return anchors[0][1]                               # at/slower than the easy anchor ⇒ f = 1.0
    if p <= anchors[-1][0]:
        return anchors[-1][1]                              # at/faster than the interval anchor
    for (p_slow, f_slow), (p_fast, f_fast) in zip(anchors, anchors[1:]):
        if p_fast <= p <= p_slow:
            span = p_slow - p_fast
            return f_slow + ((p_slow - p) / span) * (f_fast - f_slow) if span else f_fast
    return EQ_KM_FACTOR["easy"]


def _run_eq_km(km, gap_pace_sec, zones):
    """§3.1 — eq_km for an ACTUAL logged run: km weighted by `_eq_factor` at its grade-adjusted pace.
    No pace or no zones ⇒ treat as easy. Used only to SEED the biomechanical baseline from his real
    recent weeks (assertive skips the re-base). ⚠ `zones` must be the zones in force WHEN THE RUN
    HAPPENED (`_zones_asof`), never today's — see _recent_eq_km."""
    if not km:
        return 0.0
    return round(km * _eq_factor(gap_pace_sec, zones), 2)


def _recent_eq_km(db, before, zones, n_weeks=BIO_EQ_WINDOW):
    """§3.1 — the total eq_km logged in each of the `n_weeks` calendar weeks BEFORE `before`, oldest-first,
    owned data only (each run's eq_km from its grade-adjusted pace via `_run_eq_km`). Seeds the biomechanical
    jump-cap's trailing window from his real recent load — assertive skips the re-base, so the plan's own
    weeks don't seed it. Empty weeks contribute nothing.

    §PRO22 — each run is scored against `_zones_asof(its own date)`, NOT the `zones` passed in. A run's
    biomechanical cost is a property of the run: it happened at the pace he ran, against the fitness he
    had that day, and nothing that happens afterwards can change it. Scoring the trailing window with
    TODAY's zones made the whole baseline a moving target — every intraday Runalyze sync that nudged
    eVO₂max re-scored weeks of finished training, and §PRO20 deliberately keeps eVO₂max on the NEWEST
    snapshot row, so today's own run moves it. That is §PRO20's defect class one field over: the seed
    was fixed to end-of-yesterday, the biomechanical history was not. `_zones_asof` already existed for
    exactly this, and §PRO17's SESSION_EQ_STEP was CALIBRATED with it ("PERIOD-CORRECT zones") — the
    calibration assumed this reading; only the runtime path did not do it. `zones` stays in the
    signature as the fallback for a DB with no usable snapshot history."""
    from datetime import timedelta
    import json as _json
    drop = dropped_ids(db)
    mon0 = before - timedelta(days=before.weekday())
    out = []
    for w in range(n_weeks, 0, -1):
        ws = mon0 - timedelta(days=7 * w)
        we = ws + timedelta(days=6)
        rows = db.execute(
            "SELECT id, date, distance, duration, raw FROM activities WHERE date>=? AND date<=? AND " + RUN_FAMILY_SQL,
            (ws.isoformat(), we.isoformat())).fetchall()
        eq = 0.0
        for r in rows:
            if r["id"] in drop or not r["distance"]:
                continue
            raw = _json.loads(r["raw"] or "{}")
            gap = raw.get("gap")                              # grade-adjusted speed (km/h)
            gap_pace = (round(3600.0 / gap) if gap else
                        (round(r["duration"] / r["distance"]) if r["duration"] else None))
            eq += _run_eq_km(r["distance"], gap_pace, _zones_asof(db, r["date"]) or zones)
        if eq:
            out.append(round(eq, 2))
    return out


def _recent_session_eq(db, before, zones, n_weeks=BIO_EQ_WINDOW):
    """§PRO17 — the LARGEST SINGLE-SESSION eq_km in each of the `n_weeks` calendar weeks before `before`,
    oldest-first, owned data only. Session grain sibling of `_recent_eq_km` (which is week grain), and the
    biomechanical sibling of `_recent_long_runs` (which is the long run in raw km). Seeds the per-session
    step cap's trailing window from his real recent training — the plan's own weeks extend it in-phase,
    exactly as §PRO9's window does.
    A logged DAY is the unit, not a recording: §SJ joins minutes-apart parts into one session, so a run
    plus the strides that follow it are one biomechanical bout, not two. Empty weeks contribute nothing.

    §PRO22 — scored against `_zones_asof(the run's own date)`, not today's; see `_recent_eq_km` for why.
    This is the function the 2026-08-03 cliff came through: it feeds `session_eq_cap`, the bound that
    actually bit."""
    from datetime import timedelta
    import json as _json
    drop = dropped_ids(db)
    mon0 = before - timedelta(days=before.weekday())
    out = []
    for w in range(n_weeks, 0, -1):
        ws = mon0 - timedelta(days=7 * w)
        we = ws + timedelta(days=6)
        rows = db.execute(
            "SELECT id, date, distance, duration, raw FROM activities WHERE date>=? AND date<=? AND "
            + RUN_FAMILY_SQL, (ws.isoformat(), we.isoformat())).fetchall()
        by_day = {}
        for r in rows:
            if r["id"] in drop or not r["distance"]:
                continue
            raw = _json.loads(r["raw"] or "{}")
            gap = raw.get("gap")
            gap_pace = (round(3600.0 / gap) if gap else
                        (round(r["duration"] / r["distance"]) if r["duration"] else None))
            by_day[r["date"]] = (by_day.get(r["date"], 0.0)
                                 + _run_eq_km(r["distance"], gap_pace,
                                              _zones_asof(db, r["date"]) or zones))
        if by_day:
            out.append(round(max(by_day.values()), 2))
    return out


def _actual_week_caps(db, ws, we, zones):
    """§PRO9/§3.1 — what the athlete ACTUALLY logged inside one plan-week window [ws, we] (ISO,
    inclusive): (longest single run km, total eq_km). Owned data only. Feeds the progression caps'
    trailing windows for weeks already lived: an elapsed week's frozen prescription is not evidence —
    the athlete may have out- or under-run it, and the +10% step's doc'd contract is "his real recent
    long runs". Anchoring on prescription let the window slide onto fiction (2026-07-16 live case:
    cap 4.3 = 1.1 × a prescribed 3.9 km long while the actual trailing long was 8.4 km → a 7-run
    no-rest week spreading the ceiling volume over junk-sized days).
    §PRO22 — eq_km per run is scored against `_zones_asof(the run's own date)`, matching
    `_recent_eq_km`/`_recent_session_eq`: this feeds the SAME trailing windows, so scoring it on
    today's zones while they score on period-correct ones would put two different rulers in one
    window."""
    drop = dropped_ids(db)
    rows = [r for r in db.execute(
        "SELECT id, date, date_time, distance, duration, elapsed_time, raw FROM activities "
        "WHERE date>=? AND date<=? AND " + RUN_FAMILY_SQL, (ws, we)).fetchall()
        if r["id"] not in drop and r["distance"]]
    # §SJ — "longest single run" means the longest OUTING: a split recording's group-sum competes.
    # eq_km stays PER PART on purpose — each part pace-classifies on its own (sharper, if anything).
    longest = max((sum(p["distance"] for p in g) for g in _session_groups(rows)), default=0.0)
    eq = 0.0
    for r in rows:
        raw = json.loads(r["raw"] or "{}")
        gap = raw.get("gap")                                  # grade-adjusted speed (km/h)
        gap_pace = (round(3600.0 / gap) if gap else
                    (round(r["duration"] / r["distance"]) if r["duration"] else None))
        eq += _run_eq_km(r["distance"], gap_pace, _zones_asof(db, r["date"]) or zones)
    return round(longest, 1), round(eq, 2)


BANK_PLAN_SCAN = 80        # §PRO12 — how many saved plans `_laid_sessions` may consult


def _laid_sessions(db, since_iso, until_iso=None):
    """§PRO12 — {date → the session prescribed for it}, resolved across PLAN HISTORY, for dates in
    [since_iso, until_iso). A saved plan is the ROAD AHEAD (his ruling 2026-07-28 — a re-anchor
    dropping lived weeks is CORRECT, never "fix" `_rebase_start`), so it stops covering
    dates it has advanced past — by design — while a prescription lives only inside plan artifacts.
    Reading it off the current road therefore loses it exactly when the road moves. Live 2026-07-27:
    the re-base block expired, the road re-anchored to that Monday, and the effort monitor's 28-day
    window went from 19 prescriptions (3 of them interval sessions: 06-30, 07-14, 07-22) to ONE.
    Prescribed quality dates are matched and EXCLUDED from the easy score, so losing them silently
    re-graded his hardest sessions against the easy bar.

    Newest plan carrying a date wins (elapsed weeks are frozen verbatim by §6f Step E, so the newest
    carrier holds the as-lived prescription). The scan stops as soon as a plan's road starts at or
    before `since_iso` — such a plan covers the whole window, so nothing older can add to it — and
    is bounded by BANK_PLAN_SCAN regardless. No cross-call state."""
    out = {}
    if db is None:
        return out
    try:
        rows = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT ?",
                          (BANK_PLAN_SCAN,)).fetchall()
    except Exception:
        return out
    for r in rows:
        try:
            p = json.loads(r["plan"])
        except (ValueError, TypeError):
            continue
        weeks = _plan_all_weeks(p)
        for w in weeks:
            for s in w.get("sessions", []):
                d = s.get("date")
                if d and d >= since_iso and (until_iso is None or d < until_iso):
                    out.setdefault(d, s)
        starts = [w["start"] for w in weeks if w.get("start")]
        if starts and min(starts) <= since_iso:      # this road spans the window; older can't add
            break
    return out


# §PRO3/§FORM1 — training-REGIME posture, entered on BODY EVIDENCE only. The conservative re-base +
# min(intent,ceiling) posture exists for one athlete: the one returning from illness/injury. The app
# KNOWS that athlete — he tells it (readiness stop-symptoms, medical holds via check-ins) — so the
# posture keys on that evidence directly: a hold in force, a recent medical event, or a recent
# stop-symptom ⇒ caution; otherwise the plan follows measured form toward the objective (assertive),
# bounded by the physical governors (per-session/long-run eq caps, ACWR/ramp ceilings, §PRO5's
# measured-response ride). The old third clause — a banked streak of plan-adherent weeks
# ("demonstrated tolerance") — was removed 2026-08-18 (§FORM1): it inferred illness from
# DISOBEDIENCE, and a travel week (30.1 km run, cleanly absorbed, one run short of the lay) zeroed
# three banked weeks and collapsed the road into a 13 km/wk detraining re-base. Obedience is not a
# body signal. A genuinely detrained return without medical evidence needs no gate either: the
# governors ramp from his real trailing load, so the plan starts small BY MEASUREMENT.
# §PRO5 — self-calibrating shape-RESPONSE. The assertive regime rides the full ACWR ceiling by default,
# but the moderate fixed ramp can't tell whether HE, specifically, is absorbing it. This closes the loop:
# compare his MEASURED CTL now to what the PRIOR plan PROJECTED for now (stored per-week as `proj_ctl`).
# Tracking or ahead ⇒ he's responding ⇒ ride the full ceiling. Falling behind ⇒ he's not keeping up
# (under-recovering / over-reached) ⇒ ease the ride toward a floor. Bidirectional, never ABOVE the safety
# ceiling (factor ≤ 1.0), surfaced. The literal "calculate my shape-increase rate and adapt".
RESPONSE_MIN = 0.6          # floor on the ride factor ⇒ eased ride_cap never below 1.0 + 0.25·0.6 = 1.15
RESPONSE_ONTRACK = 0.98     # realised ≥ 98% of projected ⇒ on-track ⇒ full ceiling (small dead-band)
REGIME_CLEAR_DAYS = 56      # a clean window (no medical event / no stop-symptom) this long ⇒ cleared


def training_regime(db, today, prior_plan):
    """Decide the training regime → ("assertive" | "caution", reason). §FORM1: ASSERTIVE unless the
    BODY says otherwise — caution requires positive evidence:
      • a medical hold currently in force, OR a medical adjustment within REGIME_CLEAR_DAYS;
      • a stop-symptom check-in within REGIME_CLEAR_DAYS (a RED — amber/heavy legs does NOT block).
    No adherence bookkeeping: how last week compared to its prescription is not a body signal
    (§FORM1 2026-08-18 — the banked-streak clause is gone). Pure read; never writes the plan."""
    from datetime import timedelta
    td = _date(today) if isinstance(today, str) else today
    horizon = (td - timedelta(days=REGIME_CLEAR_DAYS)).isoformat()
    if active_medical_halt(db):
        return "caution", "a medical hold is in force"
    # §PRO3 fix — anchor recency on when the hold actually ENDED (`cleared_at`), not the nominal
    # `applies_until` (≤ raise+27d), which can lapse while a long hold is still in force. A still-active
    # hold is caught above; for a cleared one, `cleared_at` is the true end. Fall back to applies_until
    # for legacy rows with no cleared_at.
    recent_medical = db.execute(
        "SELECT 1 FROM adjustments WHERE medical=1 AND COALESCE(cleared_at, applies_until) >= ? LIMIT 1",
        (horizon,)).fetchone()
    if recent_medical:
        return "caution", f"a medical hold active within the last {REGIME_CLEAR_DAYS} days"
    recent_symptom = db.execute(
        "SELECT 1 FROM readiness WHERE stop_symptom=1 AND date >= ? LIMIT 1", (horizon,)).fetchone()
    if recent_symptom:
        return "caution", f"a stop-symptom check-in within the last {REGIME_CLEAR_DAYS} days"
    # §PRO3 — only a RED (stop-symptom, caught above by the 56-day window) or a medical hold blocks the
    # regime. AMBER / heavy-legs does NOT: a single tired day shouldn't drop you to conservative.
    # (Owner call 2026-06-30 — don't make him fight the tool on a tired day.)
    return "assertive", (f"no symptom or medical event in {REGIME_CLEAR_DAYS} days — "
                         "the plan follows your measured form")


def shape_response(db, today, prior_plan):
    """§PRO5 — measure how his MEASURED fitness is tracking the plan's PROJECTION, and return a ride
    factor ∈ [RESPONSE_MIN, 1.0] for the assertive ceiling. Compares today's reconstructed CTL to the
    most recent ELAPSED week's `proj_ctl` carried in the prior plan: realised ≥ projected ⇒ on/ahead of
    track ⇒ 1.0 (full ceiling); below ⇒ ease proportionally (floored). No prior projection (first plan
    after this shipped, or a fresh DB) ⇒ 1.0 (full, graceful). Pure read; never exceeds the safety cap."""
    from datetime import timedelta
    # §PRO5 reads the curve as of the plan's OWN day, not the wall clock. `reconstruct_history`
    # defaults `end` to datetime.now(), and this call used to take that default while holding `today`
    # in scope — so a plan computed for a given date measured "realised" fitness on whatever day the
    # process happened to be running. Identical in production (there `today` IS the real date) and
    # correct across midnight; the difference shows up wherever the clock is injected, which is why
    # det/clock-purity could see it and the md5 gate could not.
    td = _date(today) if isinstance(today, str) else today
    hist = reconstruct_history(db, end=td.isoformat())   # `end` is parsed with _date() — pass the ISO string
    realized = round(hist[-1]["ctl"], 1) if hist else None
    # §PRO5 fix — only FULLY-elapsed weeks (Sunday strictly before today): a week still in progress has a
    # `proj_ctl` for its END (a future value), so comparing today's mid-week CTL to it reads chronically
    # low and would ease the ride almost every plan. Require start+6d < today.
    elapsed = [(w["start"], w["proj_ctl"]) for blk in (prior_plan or {}).values()
               if isinstance(blk, dict)
               for w in blk.get("weeks", [])
               if w.get("proj_ctl") is not None and _date(w["start"]) + timedelta(days=6) < td]
    projected = max(elapsed, key=lambda c: c[0])[1] if elapsed else None
    if realized is None or not projected:
        return {"factor": 1.0, "realized": realized, "projected": projected,
                "basis": "no prior projection yet — riding the full ceiling"}
    ratio = realized / projected
    factor = 1.0 if ratio >= RESPONSE_ONTRACK else max(RESPONSE_MIN, round(ratio, 3))
    basis = ("measured fitness is tracking or ahead of projection — full ceiling" if factor >= 1.0 else
             f"measured CTL {realized} is {round(ratio * 100)}% of the projected {projected} — easing the ride")
    return {"factor": round(factor, 3), "realized": realized, "projected": projected,
            "ratio": round(ratio, 3), "basis": basis}


def _mark_load_integrity(w, zones):
    """Honesty pass over one finalized week. When the ACWR governor has clipped a plain long run below
    LONG_RUN_MIN_KM it's no longer a long run — relabel it a shakeout so the plan never calls a
    fitness-trivial session a 'long run', and in a BUILDING phase (zones supplied) flag the week so the
    UI can say the build intent was capped by recent fatigue instead of silently degrading. This ADDS
    NO LOAD — it never fights what the safety governor decided; it only tells the truth about the clip.
    Down AND taper/race weeks are exempt (deliberately light — a short long run there is the plan
    working, not a cap; flagging it would be a FALSE fatigue attribution, the opposite of honest).
    Quality long runs (long_mp) are left alone: their structure is governed elsewhere. Mutates + returns w.

    §PRO21 — the km floor above catches a long run clipped to a STUB. A long run can also stop being
    one WITHOUT shrinking at all: when the week's budget pushes every easy day up to the same ceiling,
    the Sunday session is the longest run by 0.1 km and identical in every way that matters. Same lie,
    caught by a relative test instead of an absolute one — LONG_RUN_MIN_RATIO against the week's
    longest easy run. §PRO21's two levers in _distribute_week normally PREVENT this; the check is what
    remains honest for the week neither can fix — too few short easy days for the raise to clear the
    target inside `long_cap`, and no free day left to spread onto (a 3-run week is capped at ratio
    1.077 by construction). Deliberately NOT attributed to fatigue — the fatigue wording above is a false
    attribution here, the very failure this function exists to avoid: nothing was clipped, the week
    simply has no long run to offer yet."""
    intent = w.get("intent")
    if _is_down(w) or _is_taper(w):      # §P1 — the week's role field, not its sentence
        return w
    # §JR (0.26.1) — the governor crushed this week below even ONE honest run and the junk floor
    # shed everything rather than publish 0.0-km stubs (the old path laid a "0.0 km · long easy
    # run" here). An EMPTY building week must still say why — the same promise as the relabel
    # below: deliver load, or say why it couldn't. Two exemptions, both false attributions:
    # a met week's optional-rest card is the opposite story (the load is already run), and an
    # §AV away-shed week is empty because of TRAVEL — pre-§JR it laid nothing too, and a week
    # both away-shed and fatigue-crushed stays silent rather than guess which one to blame.
    if (not [s for s in w.get("sessions", []) if (s.get("kind") or "") != "rest"]
            and not (w.get("frequency_met") or w.get("volume_met") or w.get("av_shed"))):
        w["long_capped"] = True
        if zones is not None:                  # building phase (re-base is the pure-easy zones=None block)
            w["fatigue_capped"] = True
        return w
    longs = [s for s in w.get("sessions", []) if s.get("kind") == "long"]
    if longs and (longs[0].get("km") or 0) < LONG_RUN_MIN_KM:
        s = longs[0]
        s["kind"] = "easy"
        s.pop("component", None)   # §T2 — a shakeout builds no component; don't claim economy
        s["note"] = "shakeout — long run held back by recent fatigue (ACWR ceiling)"
        w["long_capped"] = True
        if zones is not None:                  # building phase (re-base is the pure-easy zones=None block)
            w["fatigue_capped"] = True
        return w
    easies = [s for s in w.get("sessions", []) if s.get("kind") == "easy"]
    if longs and easies:
        long_km = longs[0].get("km") or 0.0
        easy_km = max((s.get("km") or 0.0) for s in easies)
        if easy_km and long_km < LONG_RUN_MIN_RATIO * easy_km:
            s = longs[0]
            s["kind"] = "easy"
            s.pop("component", None)           # §T2 — no long run, so no economy component to claim
            s["note"] = ("easy run — no long run this week: at this week's volume and run count "
                         "no session comes out meaningfully longer than the others")
            w["long_flat"] = True
    return w


def _hard_share(sessions, total_trimp):
    """HIGH-INTENSITY (threshold+interval) work TRIMP as a share of the week's total — the engine's
    standard "hard" definition (HARD_ZONES, matching PHASE_HARD_CAP). The polarized floor holds when
    this stays ≤ (1 − POLARIZED_EASY_MIN). MARATHON-PACE is deliberately EXEMPT: the MP long-run finish
    is the build's specificity cornerstone (BUILD_LONG_FRAC), it's moderate not high-intensity, and the
    hard cap already excludes it — so §H2 polices true high-intensity erosion, never the MP finish."""
    if not total_trimp:
        return 0.0
    hard = sum(r["trimp"] for s in sessions for r in (s.get("reps") or [])
               if r.get("effort") == "work" and r.get("zone") in HARD_ZONES)
    return hard / total_trimp


def generate_block(shape, block_start, ctl0, atl0, easy_pace_sec, adjust=None, zones=None, today=None,
                   week_actuals=None, regime="caution", ride_cap=ACWR_SOFT,
                   consec_hard=0, last_nondown=None, soft_ctl_floor=None, recent_longs=None,
                   recent_eq=None, week_actual_long=None, week_actual_eq=None, blocked=None,
                   recent_session_eq=None, today_trimp=None):
    """Phase-agnostic week-by-week generator (§6f) — the engine's core build machinery, shared by
    the re-base and (next) the Base/Build/Peak/Taper phases. Grows load across `shape`'s weeks,
    bounding each week's *ramp* so projected end-of-week ACWR stays under the soft cap, and carries
    CTL/ATL forward so phases CHAIN (each starts from the prior phase's end state). ACWR is a ratio
    (ATL/CTL), so the controllable lever is the week-over-week increase, not absolute scale — we cap
    each week against the carried-forward CTL/ATL and take min(volume intent, ACWR-allowed).
    Weeks are rolling 7-day windows from `block_start`; `_run_days` are offsets into that window, so
    a mid-week start just shifts the whole grid, keeping run spacing. `adjust` is an already-CLAMPED
    qualitative directive (§6c) applied to in-window days — it can only *reduce* load (multiplier
    ≤ 1), so it never breaches the ACWR ceiling. `shape` weeks need {wk, km, runs, long, strides};
    an optional `quality` list per week (§6f Step C) carves a polarized hard slice when `zones`
    (the pace-zone dict) is supplied — without it the block stays pure easy (the re-base path).
    Any extra keys pass through onto each generated week.

    `today` (§6o — within-week awareness) enables PARTIAL handling of the one week that straddles it:
    the seed (ctl0/atl0 = today's snapshot) already embodies what was done earlier this week, so the
    elapsed days are kept verbatim for matching/display while only TODAY-ONWARD days are governed and
    projected from today (model A — no double-count). The remaining days are generated EASY (a
    partially-done week's remainder is governed recovery volume; a missed quality day isn't crammed
    into the back of the week). Load already done this week therefore shrinks the remaining allowance,
    and the EOW ACWR ceiling still holds. §6o-B: when `week_actuals` is supplied, the km already RUN
    this week is also charged against the week's km intent (one-way — it only ever reduces the
    remainder), so an over-run week stops laying sessions on the remaining days instead of
    re-prescribing volume as if the week were fiction. Default None = full-week behaviour.
    §PRO20b — `today_trimp` (default None ⇒ byte-identical) is the TRIMP actually recorded today. With
    the §PRO20 seed stopping at end-of-yesterday, today's load would otherwise reach the projection
    only via today's prescription, which is moot once he has run. Applied as a FLOOR on today's
    projected load, so it can only ever tighten the governor — never as a change to what is laid."""
    from datetime import timedelta
    weeks = []
    ctl, atl = ctl0, atl0
    TRIMP_PER_KM = (easy_pace_sec / 60.0) * EASY_TRIMP_PER_MIN
    clipped_any = False
    # §PRO2 — regime: "caution" (default) = today's exact behaviour (`chosen = min(intent, ceiling)`),
    # byte-identical. "assertive" RIDES the layered ACWR + CTL-ramp ceiling (`chosen = ceiling`) on
    # non-down weeks, so the plan USES the safe headroom instead of a timid fixed ramp; down weeks keep
    # a proportional 3:1 trough off the realised (ceiling-ridden) trajectory, not the fixed-ramp km.
    assertive = regime == "assertive"
    ramp = CTL_RAMP_MAX if assertive else None
    # §PRO5 — assertive rides up to `ride_cap` (the self-calibrating shape-response cap ≤ ACWR_SOFT): full
    # ceiling when he's tracking/ahead of projection, eased toward it when his data shows he's not keeping
    # up. Caution always governs to ACWR_SOFT (its min(intent,allowed) is unchanged). Safety is preserved:
    # ride_cap ≤ ACWR_SOFT, so riding it is always at or under the safe ceiling.
    eff_cap = ride_cap if assertive else ACWR_SOFT
    # §PRO2/§PRO6 — `last_nondown` (down-week trough anchor) and `consec_hard` (consecutive near-ceiling
    # streak) are THREADED IN from the caller, so they carry ACROSS phase boundaries (each phase is a
    # separate generate_block call). Without threading the streak reset every phase and a phase that opened
    # on a down week lost its trough anchor — both fixed by accepting + returning the carry state.
    # §PRO9 — `recent_longs` seeds the long-run progression window (recent ACTUAL long runs + the caller's
    # frozen weeks); `blk_longs`/`blk_eqs` extend it in-phase, so the cap sees a continuous trailing max.
    # The straddling week contributes TRUTH-aware values (`week_actual_long`/`week_actual_eq` — what was
    # really run so far, plus the governed remainder): its elapsed planned days are display-only and may
    # never anchor the +10% step (the athlete may have out- or under-run them).
    # §PRO20b — today's ACTUAL load, as a floor on today's PROJECTED load. Built once and handed to
    # every week: `_project_week` range-checks it against the week it is rolling, so only the week
    # whose window contains today can ever match — the others are byte-identical. Deliberately NOT
    # scoped to the §6o straddle branch: `wk_start_d < today` is false when the week STARTS today, so a
    # Monday regeneration takes the full-week path and would have kept the defect one day a week.
    act_floor = ({today.isoformat(): float(today_trimp)} if (today and today_trimp) else None)
    seed_longs = list(recent_longs or [])
    seed_eq = list(recent_eq or [])            # §3.1 — trailing weekly eq_km (biomechanical chronic baseline)
    seed_seq = list(recent_session_eq or [])   # §PRO17 — trailing LARGEST-SESSION eq_km, same window
    blk_longs, blk_eqs, blk_seq = [], [], []   # per-week window contributions, appended as weeks finalize
    for wi, wk in enumerate(shape):
        wk_start_d = block_start + timedelta(weeks=wk["wk"] - 1)
        wk_start = wk_start_d.isoformat()
        intent_trimp = wk["km"] * TRIMP_PER_KM            # easy-equivalent volume intent, in TRIMP
        # §AV — away days inside this week's window re-lay the run-day slots (and may SHED runs when
        # nothing legal remains). `blocked` is a set of ISO dates from the availability table; None/
        # no-intersection ⇒ av_days stays None and every path below is byte-identical.
        av_dates, av_days, av_off, av_shed = None, None, None, 0
        if blocked:
            wk_end = (wk_start_d + timedelta(days=6)).isoformat()
            av_dates = sorted(d for d in blocked if wk_start <= d <= wk_end)
            if av_dates:
                av_off = [(_date(d) - wk_start_d).days for d in av_dates]
                av_days, av_shed = _av_run_days(wk["runs"], av_off)
            else:
                av_dates = None
        av_frac = (len(av_days) / max(1, wk["runs"])) if av_shed else 1.0
        # §6o — the week that STRADDLES today: keep elapsed days, govern only today-onward (easy).
        if today and wk_start_d < today <= wk_start_d + timedelta(days=6):
            offsets = av_days if av_days is not None else _run_days(wk["runs"])
            today_off = (today - wk_start_d).days
            rem = [o for o in offsets if o >= today_off]
            # §PRO13 — the straddling week's INTENT is the one this regime actually lays, not the
            # skeleton. §6o/§6o-B were written against the caution model (`chosen = min(intent,
            # allowed)`), where `wk["km"]` IS the intent. §PRO2's assertive regime RIDES the ceiling
            # (`chosen = allowed`), so on an assertive week `wk["km"]` understates the real intent by
            # ~40% — and every mid-week regeneration silently re-laid the current week at the caution
            # shape, then let that dip propagate: the light week depresses projected CTL, forward
            # volume is CTL-responsive, and the whole road to the race shifts down (measured on his
            # 2026-07-28 DB: 740 → 653 km, race-day CTL 59 → 54, finish 4:50:22 → 5:01:32 — purely
            # from regenerating on a Tuesday instead of a Monday).
            # This computes the SAME target the full-week path below would choose, and uses it as the
            # basis for the elapsed display, the §6e-FREQ/§6o-B "already covered" tests, and the
            # remainder prorate. It moves the INTENT only — `chosen = min(prorate, allowed)` still
            # binds the remainder to the today-onward ACWR ceiling, so no safety bound is relaxed.
            # §PRO6/§PRO11 (forced deload / re-phase) are deliberately NOT reproduced here: they
            # mutate `shape`, and the straddling week is already underway.
            # Caution keeps `intent_trimp`/`wk["km"]` verbatim ⇒ byte-identical.
            wk_intent_trimp, wk_intent_km = intent_trimp, (wk.get("km") or 0)
            if assertive and not _is_taper(wk):
                _sd, _sp = _is_down(wk), _week_phase(wk) == "peak"      # §P1
                _prog = ((1 + PROG_RAMP) * last_nondown
                         if (last_nondown and eff_cap >= ACWR_SOFT - 1e-9
                             and not _sd and not _sp) else None)
                _full_allowed = _max_week_trimp(ctl, atl, wk, wk_start, easy_pace_sec, eff_cap, zones,
                                                ramp_max=ramp, soft_ctl_floor=soft_ctl_floor,
                                                days_override=av_days, av_blocked=av_off,
                                                prog_floor=_prog, shape_neutral=assertive,
                                                actual_floor=act_floor, ladder=True)   # §PRO24
                _target = ((BUILD_DOWN_FRAC * last_nondown)
                           if (_sd and last_nondown) else _full_allowed)
                wk_intent_trimp = min(_target, _full_allowed)
                wk_intent_km = wk_intent_trimp / TRIMP_PER_KM
            # §PRO9 — the straddle path never received the long-run progression cap: every call below
            # went out without it, so the "+10% over the trailing-4wk longest" promise was simply not
            # kept on the one week a mid-week regeneration actually lays (measured on his 2026-07-28
            # plan 65: trailing longest 8.5 ⇒ cap 9.35, laid 10.5). Same expression as the full-week
            # path, assertive-only ⇒ caution byte-identical.
            _trailing = [x for x in (seed_longs + blk_longs)[-LONG_RUN_STEP_WINDOW:] if x]
            long_km_cap = (round(LONG_RUN_STEP_CAP * max(_trailing), 1)
                           if (assertive and _trailing) else None)
            # §PRO15 — the long run's aim for THIS week: the distance the full-week path below would
            # lay at this intent. The remainder is governed as a share of what is LEFT, so without an
            # aim an over-run early week demotes its long run proportionally — and because the §PRO9
            # window takes the max over the laid long runs, that shrunken Sunday then caps the next
            # four weeks' long runs too (a one-week dip costs ~7 weeks at +10%/wk to climb back).
            # Assertive-only, like the cap that bounds it: on caution there is no cap, and an
            # unclipped aim would be the wrong half of the pair.
            long_km_aim = None
            if assertive:
                _lw = min((wk["long"] / wk["km"]) if wk["km"] else 0.0,
                          _long_share_cap(wk, zones))      # §PRO26
                long_km_aim = _lw * wk_intent_km
            full, _ = _distribute_week(wk, wk_start_d, wk_intent_trimp, easy_pace_sec, zones,
                                       days_override=av_days, av_blocked=av_off,
                                       long_km_cap=long_km_cap, ladder=assertive)   # §PRO24
            elapsed = [s for s in full if s["date"] < today.isoformat()]   # for log matching / display
            # §6e-FREQ + §6o-B — what this week's ACTUALS already cover. freq_met: run COUNT *and* km
            # both logged → the remainder is optional rest (a met-week junk run does nothing for
            # aerobic shape). vol_met (§6o-B, the 2026-07-05 over-run incident): the km intent alone
            # is spent — an over-run week must NOT lay more sessions on the remaining days just
            # because the run count is short (more runs to hit a count = junk by definition). "Spent"
            # = less than one §JR-honest run left (taper exempt: a tiny shakeout is real).
            freq_met = vol_met = False
            if rem and week_actuals is not None:
                a_runs, a_km = week_actuals
                # §PRO13 — these read the week's REAL intent too: an assertive week is not "already
                # covered" at the skeleton's km, or the remainder would fall to optional rest while
                # the plan still intended a third of the week's volume.
                left_tr = max(0.0, wk_intent_km - a_km) * TRIMP_PER_KM
                min_left = 0.0 if _is_taper(wk) else \
                    RUN_MIN_KM * (easy_pace_sec / 60.0) * EASY_TRIMP_PER_MIN
                freq_met = a_runs >= (wk.get("runs") or 0) and a_km >= wk_intent_km
                vol_met = wk_intent_km > 0 and left_tr <= min_left
                if freq_met or vol_met:
                    rem = []
            if rem:
                # §6o-QF — a MID-QUALITY session whose laid day is still AHEAD isn't "missed": it
                # survives the remainder re-lay, pinned to its own day. The easy-only rule was
                # written for the quality-day-already-past case (a missed session is never crammed
                # into the back of the week — that stands, q_ahead is empty then). Invisible before
                # §AV (template quality = Tue, always past once a week straddles); §AV's relocation
                # made a future quality day normal, and the card must not lie about it.
                q_ahead = sorted({(_date(s["date"]) - wk_start_d).days for s in full
                                  if s.get("kind") in ("tempo", "interval")
                                  and s["date"] >= today.isoformat()})
                q_ahead = [o for o in q_ahead if o in rem] or None
                use_zones = zones if q_ahead else None
                allowed = _max_week_trimp(ctl, atl, wk, wk_start, easy_pace_sec, ACWR_SOFT,
                                          zones=use_zones, roll_from=today.isoformat(), days_override=rem,
                                          soft_ctl_floor=soft_ctl_floor, av_blocked=av_off,
                                          q_days=q_ahead, shape_neutral=assertive,
                                          actual_floor=act_floor)   # §PRO24 — no ladder: a remainder
                # §AV — the denominator is the TEMPLATE's run count (== len(offsets) without §AV, so
                # byte-identical): an av-shed week's blocked days contribute nothing, they don't
                # concentrate the intent into the surviving days.
                prorate = wk_intent_trimp * len(rem) / max(1, wk["runs"])
                if week_actuals is not None:
                    # §6o-B — charge the ACTUAL km already run against the week's intent: the
                    # remainder may never re-prescribe volume he has already done. One-way (min), so
                    # an under-run early week still gets only its day-prorated share — a missed day
                    # is never crammed into the back of the week. (§PRO13: charged against the
                    # regime's real intent, not the skeleton.)
                    prorate = min(prorate, max(0.0, wk_intent_km - week_actuals[1]) * TRIMP_PER_KM)
                chosen = min(prorate, allowed)
                rem_s, dt = _distribute_week(wk, wk_start_d, chosen, easy_pace_sec, use_zones,
                                             days_override=rem, av_blocked=av_off, q_days=q_ahead,
                                             long_km_cap=long_km_cap, long_km_aim=long_km_aim,
                                             free_from=today_off)   # §PRO24 — no ladder (remainder)
                if q_ahead and sum(dt.values()) > chosen + 1.0:
                    # §6o-QF fallback — the governed remainder can't carry the quality session's
                    # fixed TRIMP floor (late week / tiny budget): keep the honest easy-only lay
                    # rather than over-prescribing past the charge (§6o-B's contract wins).
                    rem_s, dt = _distribute_week(wk, wk_start_d, chosen, easy_pace_sec, None,
                                                 days_override=rem, av_blocked=av_off,
                                                 long_km_cap=long_km_cap, long_km_aim=long_km_aim,
                                                 free_from=today_off)   # §PRO24 — no ladder (remainder)
            elif freq_met or vol_met:                      # week already covered → optional, never forced
                a_runs, a_km = week_actuals
                # §6e3 — quote the intent that MADE the decision, not the shape skeleton. Both tests
                # above compare against `wk_intent_km` (§PRO13 fixed that deliberately, because an
                # assertive week is not "already covered" at the skeleton's km); the sentence went on
                # printing `wk["km"]`. On his 2026-07-30 plan that read "32.0km of 22km planned" while
                # the number the engine actually decided on was 25.6 — making the week look easier to
                # have cleared than it was. §6e2's defect, one release later: a sentence asserting
                # something adjacent to what was measured.
                _int_km = round(wk_intent_km, 1)
                what = (f"✓ Week's frequency met — {a_runs}/{wk.get('runs')} runs, "
                        f"{a_km}km ≥ {_int_km}km planned." if freq_met else
                        f"✓ Week's volume already run — {a_km}km of {_int_km}km planned, "
                        f"in {a_runs} runs.")
                rem_s = [{"date": today.isoformat(), "kind": "rest", "optional": True,
                          "km": 0.0, "minutes": 0, "trimp": 0.0,
                          "note": what + (" Today is optional: rest is prescribed, but an easy run "
                                          "is fine if you feel good.")}]
                chosen, dt = 0.0, {}
            else:                                          # today is past this week's last run → only decay
                chosen, rem_s, dt = 0.0, [], {}
            adjusted = _apply_adjustment(rem_s, dt, adjust)
            rem_s, dt = adjusted["sessions"], adjusted["dt"]
            ctl, atl, eow, peak, eow_flat, _ = _project_week(ctl, atl, wk_start, dt,
                                                             roll_from=today.isoformat(),
                                                             actual_floor=act_floor)
            sessions = sorted(elapsed + rem_s, key=lambda s: s["date"])
            # km + trimp_total cover the SAME set (elapsed-planned + governed remainder) so the week
            # summary is internally consistent; proj_acwr/peak come from the remaining-only `dt` rolled
            # from the §PRO20 end-of-yesterday seed (the safety number — elapsed load is in the seed,
            # never double-counted), with §PRO20b flooring today at what was actually run. The DISPLAY
            # numbers stay the prescription: km/trimp_total are what the plan asks for, the projection
            # is what the governor is held to. Same split the elapsed days already use.
            pweek = {**wk, "start": wk_start, "sessions": sessions,
                     # §CARD — the header counts the sessions it sits above, NOT the skeleton's
                     # template. `{**wk}` spreads the shape's `runs`, and this branch never overrode
                     # it, so a §JR shed or a §PRO9 spread put "5 runs" over a 4- or 6-run listing —
                     # the owner read it off his own card (2026-08-07, week 08-03: "35.8 km · 5 runs"
                     # above four runs). The full-week path fixed exactly this under §PRO9 ("honest
                     # count") and this hand-built dict didn't inherit the fix. Rest entries are
                     # notes, not runs — the freq-met branch's optional-rest card must not count.
                     "runs": sum(1 for s in sessions if (s.get("kind") or "") != "rest"),
                     "km": round(sum(s["km"] for s in sessions), 1),
                     "trimp_total": round(sum(s.get("trimp", 0.0) for s in sessions), 1),
                     "proj_acwr": eow, "peak_acwr": peak, "proj_ctl": round(ctl, 1),
                     "proj_acwr_flat": eow_flat,
                     # §PRO25 — the straddling week's bar, same rule as the full-week path above.
                     # `wk_intent_km` is the number §PRO13 already computed and §6e3 already prints.
                     "intent_km": (round(wk_intent_km, 1) if assertive else wk["km"]),
                     "adjusted": adjusted["touched"],
                     "clipped": False, "partial": True,
                     "frequency_met": freq_met, "volume_met": vol_met,
                     "freq_actual": list(week_actuals) if (freq_met or vol_met) else None}
            # §CARD3 — the as-laid prescription count, stamped BEFORE §CARD2 rewrites `runs` to
            # done+ahead below: the honest record of what was PRESCRIBED, kept distinct from what
            # was run (the header). Display/history provenance only since §FORM1 — no decision
            # reads it any more.
            pweek["intent_runs"] = pweek["runs"]
            # §CARD2 — THE STRADDLING WEEK'S HEADER DESCRIBES THE WEEK, NOT THE PRESCRIPTION TRAIL.
            # The old header summed the PRESCRIBED elapsed days + the governed remainder ("display
            # numbers stay the prescription"), so every km he ran OVER prescription made the current
            # week's headline SHRINK by the same amount: on 2026-08-07 his card said 35.8 km while
            # the week was really 29.1 run + 11.3 ahead = 40.4 — and it read SMALLER than the 38.3
            # down week, which he rightly called out. Owner overturned that design 2026-08-07
            # ("yes, do it"): elapsed days count at their ACTUAL distance (the per-session lines
            # already show "7.1k → 10.9k"; the header now agrees with them), remaining days at their
            # prescription. A prescription for a day ALREADY RUN is superseded by its actual
            # (§PRO20b's principle — "once he has already run, the prescription is moot"), so a
            # same-day regen never counts today twice. `km = round(km_done + km_ahead, 1)` off the
            # ROUNDED parts, so the header identity det/card-truth asserts is exact by construction.
            # `week_actuals is None` (direct det fixtures) keeps the prescription-sum header verbatim.
            if week_actuals is not None:
                _ahead = [s for s in rem_s if (s.get("kind") or "") != "rest"]
                if today_trimp:
                    _ahead = [s for s in _ahead if s["date"] > today.isoformat()]
                _ahead_km = round(sum(s.get("km") or 0.0 for s in _ahead), 1)
                pweek.update(
                    runs_done=week_actuals[0], km_done=week_actuals[1],
                    runs_ahead=len(_ahead), km_ahead=_ahead_km,
                    runs=week_actuals[0] + len(_ahead),
                    km=round(week_actuals[1] + _ahead_km, 1))
            # §PRO9 — surface the ceiling at week level too, exactly as the full-week path does; the
            # straddle branch built `pweek` by hand and never carried it, so a capped straddle week
            # showed the note on the session but nothing on the card.
            if long_km_cap and any(s.get("long_step_capped") for s in sessions):
                pweek["long_step_capped"] = long_km_cap
            if av_dates:                                   # §AV — laid around away days (PRIVATE-only
                pweek["av_dates"] = av_dates               # field; the public plan view strips it)
                if av_shed:
                    pweek["av_shed"] = av_shed
            # §PRO6 (0.26.1) — fold the straddling week into the near-ceiling streak + trough
            # anchor, judged exactly as the frozen fold will judge this same week next Monday
            # (down/taper intent resets; otherwise its proj_acwr counts against NEAR_CEILING_ACWR
            # and trimp_total anchors the trough). This branch `continue`s past the full-week
            # bookkeeping, so a down week UNDERWAY never reset the streak: on the 2026-08-19 live
            # plan, three near-ceiling lived weeks + the straddling shape down week left
            # consec_hard at 3, §PRO6 tripped on the very next week, and §PRO11 pulled the base's
            # END down week forward — two consecutive absorption weeks, no recovery left in the
            # block tail, and the miscount cascaded a second pull plus a forced pure-easy deload
            # into the build phase. Which DAY of the week the plan is regenerated on must not
            # re-phase the road — this is the fold's judgment applied at lay time.
            if assertive:
                if _is_down(wk) or _is_taper(wk):
                    consec_hard = 0
                else:
                    consec_hard = consec_hard + 1 if (eow and eow >= NEAR_CEILING_ACWR) else 0
                    if pweek["trimp_total"]:
                        last_nondown = pweek["trimp_total"]
            weeks.append(pweek)
            blk_longs.append(max(week_actual_long or 0.0, _week_long_km(rem_s)))
            blk_eqs.append(round((week_actual_eq or 0.0) + _week_eq_km(rem_s), 2))
            blk_seq.append(max((_session_eq_km(x) for x in rem_s), default=0.0))   # §PRO17
            continue
        is_down = _is_down(wk)
        is_taper = _is_taper(wk)
        is_peak = _week_phase(wk) == "peak"      # §P1 — the field, not the sentence's prefix
        # §PRO6 — force a deload when too many near-ceiling building weeks have stacked up without one.
        # EXCLUDE the PEAK/sharpen phase: it always flows straight into the taper, which IS the recovery,
        # so an extra forced deload there is redundant and would shed race fitness right before race day
        # (the limiter's real job is the long base/build grind). consec_hard still counts through peak,
        # but the taper resets it — the peak rides uninterrupted into the taper as designed.
        forced_deload = bool(assertive and not is_down and not is_taper and not is_peak
                             and last_nondown and consec_hard >= MESO_MAX_HARD)
        # §PRO11 — re-phase, don't stack: §PRO10 makes every riding week near-ceiling by construction,
        # so the streak trips on schedule; if the SHAPE already provides a down week later in this
        # block, pull it forward (swap the two weeks' fields) instead of inserting an EXTRA trough —
        # the meso keeps one recovery per cycle and the displaced building week keeps its quality.
        # No down week ahead ⇒ the original forced deload stands (the no-recovery backstop).
        deload_pulled = False
        if forced_deload:
            nxt = next((j for j in range(wi + 1, len(shape))
                        if _is_down(shape[j])), None)
            if nxt is not None:
                a, b, _MISS = wk, shape[nxt], object()
                for k in (set(a) | set(b)) - {"wk"}:
                    av_, bv_ = a.get(k, _MISS), b.get(k, _MISS)
                    a.pop(k, None); b.pop(k, None)
                    if bv_ is not _MISS:
                        a[k] = bv_
                    if av_ is not _MISS:
                        b[k] = av_
                is_down, forced_deload, deload_pulled = True, False, True
                intent_trimp = wk["km"] * TRIMP_PER_KM      # re-derive from the swapped-in down week
                if av_off:                                  # §AV — day slots follow the new run count
                    av_days, av_shed = _av_run_days(wk["runs"], av_off)
                    av_frac = (len(av_days) / max(1, wk["runs"])) if av_shed else 1.0
        # §PRO10 — the progressive-overload floor: an assertive BUILDING week's allowance may not be
        # soft-clipped below (1+PROG_RAMP)× the last realised non-down load (the state-based ceiling
        # equilibrates; progression is a demand the acute brakes then bound). Building weeks only:
        # peak trims into specificity by design, taper/down/forced-deload keep their deliberate drops.
        # SUSPENDED whenever §PRO5 has EASED the ride cap (eff_cap < ACWR_SOFT): the floor's premise
        # is continued clean absorption, and the eased cap is the engine MEASURING that absorption is
        # lagging — the responsiveness brake outranks the progression demand, always.
        # §PRO9 + §3.1/§PRO17 — the biomechanical ceilings, hoisted above the governor call because they are
        # now INPUTS to it rather than a post-hoc reshape test. Week grain and session grain, one step.
        prior_longs = seed_longs + blk_longs
        trailing = [x for x in prior_longs[-LONG_RUN_STEP_WINDOW:] if x]
        long_km_cap = (round(LONG_RUN_STEP_CAP * max(trailing), 1)
                       if (assertive and trailing) else None)
        prior_eq = seed_eq + blk_eqs
        trailing_eq = [x for x in prior_eq[-BIO_EQ_WINDOW:] if x]
        bio_cap = (BIO_EQ_STEP * max(trailing_eq)) if (assertive and trailing_eq) else None
        prior_seq = seed_seq + blk_seq
        trailing_seq = [x for x in prior_seq[-BIO_EQ_WINDOW:] if x]
        session_eq_cap = (SESSION_EQ_STEP * max(trailing_seq)) if (assertive and trailing_seq) else None
        prog = ((1 + PROG_RAMP) * last_nondown
                if (assertive and last_nondown and eff_cap >= ACWR_SOFT - 1e-9
                    and not is_down and not is_taper
                    and not is_peak and not forced_deload) else None)
        allowed = _max_week_trimp(ctl, atl, wk, wk_start, easy_pace_sec, eff_cap, zones, ramp_max=ramp,
                                  soft_ctl_floor=soft_ctl_floor, days_override=av_days, av_blocked=av_off,
                                  prog_floor=prog, shape_neutral=assertive,
                                  session_eq_cap=session_eq_cap, week_eq_cap=bio_cap,
                                  long_km_cap=long_km_cap, actual_floor=act_floor,   # §PRO20b
                                  ladder=assertive)   # §PRO24
        if assertive and not is_taper:
            # ride the layered ceiling on building weeks; hold a proportional recovery trough on down
            # weeks (BUILD_DOWN_FRAC of the last realised non-down load), always governor-capped. The
            # TAPER is excluded — it is a DELIBERATE pre-race volume drop, so it keeps the shape intent
            # (the designed taper curve), never the ceiling. A forced deload (§PRO6) recovers like a down.
            if is_down or forced_deload:
                # recovery trough off the last realised non-down load; if no anchor yet (a phase that
                # OPENS on a down week before any building week), fall back to the shape's reduced intent
                # — NEVER the full ceiling (the trough is a masters/post-illness safety guarantee).
                target = (BUILD_DOWN_FRAC * last_nondown) if last_nondown else intent_trimp
            else:
                target = allowed
            chosen = min(target, allowed)
        else:
            chosen = min(intent_trimp, allowed)            # caution (or assertive taper) — designed shape
        if av_frac < 1.0:
            chosen *= av_frac    # §AV — shed days take their load with them: a travel week is honestly
            #                      LIGHTER, never the full budget crammed into the surviving days
        # §PRO6/E — a forced deload is a genuine recovery week: strip quality (pure easy), so a
        # tissue-protection deload never prescribes the highest-stress interval session.
        wk_zones = None if forced_deload else zones
        # §PRO9 — long-run progression cap (assertive-only; caution passes None ⇒ byte-identical). The
        # baseline is the longest run of the trailing LONG_RUN_STEP_WINDOW weeks (recent actuals seeded +
        # this block's earlier weeks). The cap applies to EVERY week (down/taper included): it only ever
        # REDUCES, so a deliberately-short recovery/taper long sits below the cap and is untouched — but a
        # week whose long would JUMP past +10% is clipped even on a down week (exempting them let a
        # recovery week's naturally-larger long leap past the cap and reset the baseline for the rest).
        # §3.1 — the biomechanical soft ceiling: the week's pace-weighted eq_km may not jump beyond
        # BIO_EQ_STEP × the MAX eq_km of the trailing BIO_EQ_WINDOW weeks (recent actuals seeded + this
        # block's earlier weeks). Assertive-only; caution passes no cap ⇒ byte-identical.
        sessions, dt = _distribute_week(wk, _date(wk_start), chosen, easy_pace_sec, wk_zones,
                                        long_km_cap=long_km_cap, days_override=av_days, av_blocked=av_off,
                                        ladder=assertive)   # §PRO24
        adjusted = _apply_adjustment(sessions, dt, adjust)  # mutates copies; reduces only
        sessions, dt = adjusted["sessions"], adjusted["dt"]
        ctl_n, atl_n, eow, peak, eow_flat, m_ctl_n = _project_week(ctl, atl, wk_start, dt,
                                                                   actual_floor=act_floor)   # §PRO20b
        # §H1 — a structured quality session carries a FIXED TRIMP floor (easy wu/cd + ≥1 work rep)
        # the governor cannot shrink; at low CTL that floor's mid-week spike pushes PEAK ACWR past the
        # hard cap even while end-of-week stays under the soft cap. When it does, drop THIS week's
        # quality to pure easy (easy load scales toward zero, so the hard cap can always be met) and
        # re-govern. Quality returns automatically once CTL can afford it — self-heals as fitness
        # rebuilds. Preserves the deliberate EOW soft bound + normal-transient tolerance; the hard
        # ceiling only catches this low-CTL floor pathology, never a normal-CTL week.
        # §H2 (2026-06-29) — the SAME fixed quality floor erodes POLARIZATION the other way: when the
        # governor clips a week's easy volume hard at low CTL, the immovable HIGH-INTENSITY rep TRIMP
        # (threshold+interval — HARD_ZONES, MP exempt; see _hard_share) becomes a larger share of a
        # shrinking total, so the hard share climbs past (1 − POLARIZED_EASY_MIN) even while peak stays
        # UNDER the hard cap (§H1 never fires). That makes the plan more intense exactly when he's most
        # fragile — the one safety-negative artifact the corrected EWMA exposed. Remedy = drop the
        # quality → easy and re-govern, like §H1. (MP share is bounded by the LOAD cap, not this
        # polarization floor — by design: on a hard-clipped deep week the fixed MP rep can ride large
        # as a share, but eow ACWR still ≤ soft cap, and MP is moderate specificity he asked to keep.)
        # CRITICAL DIFFERENCE: §H1 fires on a PEAK
        # breach when EOW is already near the soft cap, so its refill barely moves total load; §H2 can
        # fire when EOW is LOW (the quality spike, not volume, was the binding constraint), so a naive
        # refill to the soft cap would ADD load at low CTL — the exact thing the brake must not allow.
        # So §H2 CAPS the re-governed all-easy week at its PRE-DROP governed TRIMP. That is a load
        # BOUND, not load-neutrality: the pure-easy layout concentrates the week on the long run, so
        # the peak-ACWR cap can bind sooner on the re-govern and the week may carry LESS than
        # pre-drop (measured on a braked week 2026-07-04: 249 vs 282 TRIMP). The guarantee is
        # one-sided — total ≤ pre-drop, the mid-week peak falls — so suppressing intensity can never
        # raise load. Self-heals as CTL rebuilds.
        # §PRO17 — the rescue's OWN threshold now, not the governor's. See H1_RESCUE_ACWR.
        breach = bool(zones and peak and peak > H1_RESCUE_ACWR)
        eroded = bool(zones and not breach
                      and _hard_share(sessions, sum(dt.values())) > 1.0 - POLARIZED_EASY_MIN + 1e-9)
        # §3.1 — biomechanical spike: the week's pace-weighted eq_km jumped past the soft bio ceiling AND
        # the excess is from FAST km (its all-easy eq_km = week_km would sit under the cap, so dropping the
        # quality slice to easy fixes it — otherwise it's a volume issue that ACWR/CTL_RAMP already own).
        # Reuses the §H2 quality→easy reshape (proven-safe, only ever reduces): remove the high-damage fast
        # km, keep the aerobic volume. Assertive-only (bio_cap is None otherwise).
        week_km = sum((s.get("km") or 0.0) for s in sessions)
        bio_over = bool(zones and bio_cap and not breach and not eroded
                        and _week_eq_km(sessions) > bio_cap and week_km <= bio_cap)
        if breach or eroded or bio_over:
            pre_total = sum(dt.values())
            # §FORM1 — the re-govern search runs under the SAME governor contract as the primary call
            # (soft CTL floor, shape-neutral soft test, bio caps, long cap): it used to omit them — a
            # pre-§PRO8 fossil, unreachable while assertive required banked weeks (CTL was never low
            # there) — so a cold-start week that stripped quality was re-governed against RAW CTL≈1
            # and pinned at ~0 km. Caution passes every added kwarg as None/False ⇒ byte-identical.
            allowed = _max_week_trimp(ctl, atl, wk, wk_start, easy_pace_sec, eff_cap, zones=None,
                                      ramp_max=ramp, soft_ctl_floor=soft_ctl_floor,
                                      shape_neutral=assertive,
                                      session_eq_cap=session_eq_cap, week_eq_cap=bio_cap,
                                      long_km_cap=long_km_cap,
                                      days_override=av_days, av_blocked=av_off,
                                      actual_floor=act_floor, ladder=assertive)   # §PRO20b/§PRO24
            # assertive still rides the (now pure-easy) ceiling; caution keeps min(intent, ceiling)
            chosen = allowed if (assertive and not is_down) else min(intent_trimp, allowed)
            if av_frac < 1.0:
                chosen *= av_frac                      # §AV — the shed prorate holds on the re-govern too
            if eroded or bio_over:
                chosen = min(chosen, pre_total)        # never add volume when suppressing intensity
            sessions, dt = _distribute_week(wk, _date(wk_start), chosen, easy_pace_sec, None,
                                            long_km_cap=long_km_cap,   # §PRO9 — keep the cap on re-govern
                                            days_override=av_days, av_blocked=av_off,
                                            ladder=assertive)   # §PRO24
            adjusted = _apply_adjustment(sessions, dt, adjust)
            sessions, dt = adjusted["sessions"], adjusted["dt"]
            ctl_n, atl_n, eow, peak, eow_flat, _ = _project_week(ctl, atl, wk_start, dt,
                                                                 actual_floor=act_floor)   # §PRO20b
        ctl, atl = ctl_n, atl_n  # carry forward the FINAL distribution, stepped exactly once
        if assertive:
            recovery_wk = is_down or forced_deload or is_taper
            if not recovery_wk:
                last_nondown = sum(dt.values())   # anchor the next trough on realised load
                consec_hard = consec_hard + 1 if (eow and eow >= NEAR_CEILING_ACWR) else 0
            else:
                consec_hard = 0                   # any recovery/taper week resets the §PRO6 streak
        # caution: "clipped" = governor reduced below the shape intent. Assertive: the ceiling IS the
        # target, so a week is never "clipped against intent" — the surfaces shouldn't cry fatigue.
        clipped = (not assertive) and chosen < intent_trimp - 1
        if clipped:
            clipped_any = True
        week = {**wk, "start": wk_start, "sessions": sessions,
                # §PRO9 — honest count (the cap can add easy days to hold volume). §CARD — non-rest
                # only: _apply_adjustment turns a 0×-eased day into a `rest` session, which stays in
                # the listing as a note but is not a run the header may claim.
                "runs": sum(1 for s in sessions if (s.get("kind") or "") != "rest"),
                "km": round(sum(s["km"] for s in sessions), 1),
                "trimp_total": round(sum(dt.values()), 1), "proj_acwr": eow, "peak_acwr": peak,
                "proj_acwr_flat": eow_flat,
                # §PRO23 — the governor's OWN decision variable, published rather than left to be
                # reconstructed. `proj_acwr` is the last-day sample and `proj_acwr_flat` the raw
                # shape-neutral one; NEITHER is what the soft test compared against the ride cap once
                # §PRO8's floor engages, because that divides by the week's MEAN CTL. Readers that
                # guessed with `proj_ctl` over-read it by ~5% on a steep low-CTL week.
                "proj_acwr_soft": (None if eow_flat is None and eow is None else
                                   round(_eow_soft(eow, eow_flat, m_ctl_n, ctl_n, atl_n,
                                                   assertive, soft_ctl_floor) or 0.0, 4)),
                "proj_ctl": round(ctl, 1),    # §PRO5 — projected end-of-week CTL (the response feedback signal)
                # §PRO25 — publish the intent the week was actually GOVERNED to, not the skeleton's
                # template km. In CAUTION `chosen = min(intent_trimp, allowed)`, so the skeleton IS the
                # ask and `clipped` carries the governor's cut — `wk["km"]` stays, byte-identical. In
                # ASSERTIVE the week rides the ceiling (`chosen = allowed`) and the template is not the
                # ask at all: measured on his 2026-08-24 plan the published intent ran 1.76–3.34× BELOW
                # the laid sheet across the whole base phase and then snapped to ~1.00× at the
                # base→build boundary — a phase-dependent discontinuity in the one field whose job is to
                # be the honest bar. §PRO13 and §6e3 already recompute this same intent for the straddle
                # path's decisions and for the sentence it prints; it was simply never PUBLISHED, so
                # every reader downstream still had to guess from the skeleton (the house rule: a
                # governor publishes its decision variable rather than making its readers reconstruct
                # it). §AV's shed is inside `chosen` deliberately — a travel week's intent IS lighter.
                # ⚠ This is the denominator any "was the block absorbed?" test must use: against the
                # skeleton, absorbed_frac read ~2.4 in base and ~1.0 in build for identical adherence.
                "intent_km": (round(chosen / TRIMP_PER_KM, 1) if assertive else wk["km"]),
                "adjusted": adjusted["touched"], "clipped": clipped}
        # §CARD3 — the as-laid prescription count. At lay time it equals the header's honest count;
        # once the week is lived, §CARD3's elapsed true-up rewrites `runs` to actuals and THIS field
        # keeps the bar the athlete was actually set (what §6e banking judges adherence against).
        week["intent_runs"] = week["runs"]
        # §PRO10 — honest label: this week's load sits where the SOFT cap alone wouldn't have put it
        # (the progression floor lifted it; the acute brakes cleared it). The soft-test value is
        # recomputed the way the governor judged it (floored denominator when §PRO8 is active).
        if prog is not None and atl_n is not None and ctl_n:
            eow_soft_final = atl_n / max(ctl_n, soft_ctl_floor or 0.0)
            if eow_soft_final > eff_cap + 1e-6:
                week["prog_ridden"] = True
        if forced_deload:                          # §PRO6 — tell the truth: a tissue-protection deload
            week["deload_forced"] = True
            week["intent"] = "Down week — forced deload (consecutive near-ceiling weeks)"
        elif deload_pulled:                        # §PRO11 — the shape's own down week, arrived early
            week["deload_pulled"] = True
        if long_km_cap and any(s.get("long_step_capped") for s in sessions):
            week["long_step_capped"] = long_km_cap   # §PRO9 — the +10% ceiling that bound this week's long
        week["eq_km"] = _week_eq_km(sessions)        # §3.1 — the week's damage-equivalent km (bio load)
        if bio_over:                                 # §3.1 — the soft bio ceiling reshaped this week to easy
            week["bio_capped"] = round(bio_cap, 1)
        if av_dates:                                 # §AV — laid around away days (PRIVATE-only field;
            week["av_dates"] = av_dates              # the public plan view strips it)
            if av_shed:
                week["av_shed"] = av_shed
        weeks.append(week)
        blk_longs.append(_week_long_km(sessions))
        blk_eqs.append(week["eq_km"])
        blk_seq.append(max((_session_eq_km(x) for x in sessions), default=0.0))   # §PRO17
    for w in weeks:                       # honesty pass — relabel governor-gutted long runs (§6f Step F)
        _mark_load_integrity(w, zones)
    # §PRO9/§3.1 carry-out — the trailing long-run + eq_km windows (seed + this block's weeks), tail-trimmed,
    # so the next phase's caps continue off a continuous history instead of resetting at each phase boundary.
    out_longs = (seed_longs + blk_longs)[-LONG_RUN_STEP_WINDOW:]
    out_eq = (seed_eq + blk_eqs)[-BIO_EQ_WINDOW:]
    return weeks, {"clipped_by_acwr": clipped_any,
                   "end_ctl": round(ctl, 1), "end_atl": round(atl, 1),
                   "consec_hard": consec_hard, "last_nondown": last_nondown,   # §PRO6 carry-out
                   "recent_longs": out_longs, "recent_eq": out_eq,
                   "recent_session_eq": (seed_seq + blk_seq)[-BIO_EQ_WINDOW:]}   # §PRO9/§3.1/§PRO17 carry-out


def generate_rebase(block_start, ctl0, atl0, easy_pace_sec, adjust=None, shape=None):
    """The Phase-0 re-base block (§6d) — `generate_block` over `REBASE_SHAPE` (or a §6e-shortened
    slice when a well-absorbed block graduates early; volumes and the ACWR ceiling are identical,
    only the week count changes). Thin wrapper kept so callers and diffs stay stable now that the
    generator is phase-agnostic for base-build (§6f)."""
    return generate_block(shape or REBASE_SHAPE, block_start, ctl0, atl0, easy_pace_sec, adjust)


# §PRO7 — finish-TIME projection. The honesty valve the owner asked for: a short runway isn't a
# refusal, it's a SLOWER projected finish — so quantify it. Anchored on the Daniels VDOT marathon-pace
# zone (the pace a runner TRAINED to that effective VO₂max holds), then slowed by an endurance penalty
# when projected race-day CTL is below the distance's healthy-finish floor: an undertrained body fades
# over 42 km. The absolute time is a rough ESTIMATE (real marathon time also rides fuelling, pacing,
# heat, the day); the ROBUST signal is the comparative — more runway ⇒ more CTL ⇒ less fade ⇒ faster.
RACE_KM = {"marathon": 42.195, "half": 21.0975, "10k": 10.0, "5k": 5.0}
FADE_PER_CTL = 0.008   # +0.8% to race pace per CTL point of endurance deficit below the floor
FADE_CAP = 1.35        # never project worse than +35% (beyond that it's a walk, not a time the model owns)

# §FT1 — Model A: race-day state → finish time. The one-sided fade above becomes one branch of a
# durability curve that is STRICTLY monotone through the whole CTL range (det/ft-monotone), so the
# objective finally has a gradient in load everywhere training lives: max safe load = min predicted
# time BY CONSTRUCTION (log §33). Below the floor the old fade is reproduced exactly (the §PER1 F2/F3
# verdicts stay byte-identical at neutral inputs); above it the gain saturates toward a fully-built
# athlete's load with the SAME slope at the floor (C¹: FT_DUR_GAIN = FADE_PER_CTL × FT_DUR_TAU).
# τ and the ladder/shrinkage priors are OUR operationalization, calibrated 2026-07-26 on the owner's
# 4-marathon corpus (CTL 67/92/98/109 → actual/VDOT-base 1.215/1.163/1.339/1.134; robust L1 fit —
# the three cleanly-executed races land within ±1.5%, the 2023-05 first-marathon blow-up (+16%) is
# band evidence for §FT3, not curve). Durability-via-the-long-run is Davis-consistent (Maunder,
# Seiler & Plews 2021 on durability; Daniels/Gilbert for the speed axis).
FT_DUR_TAU = 20.0                        # CTL pts to ~63% of the above-floor durability gain
FT_DUR_GAIN = FADE_PER_CTL * FT_DUR_TAU  # 0.16 — max fractional gain above the floor (C¹ tie)
FT_LADDER_REF = 0.70    # longest-long / race-km ratio at which the ladder term is neutral (Λ=1) —
FT_LADDER_RHO = 0.40    # his whole race corpus sits at 0.71, so the ladder SHAPE is a prior, not
FT_LADDER_L = 0.25      # a corpus fit: ~+8% at ratio 0.28 (12 km longs) vs 0.71 (30 km longs)
FT_SHRINK_K = 2         # shrinkage prior strength: the population curve counts as K pseudo-races
FT_LADDER_TRAIL_DAYS = 56   # trailing window for "longest long" race-readiness (§PRO9's lever)
# §FT9 — the SAME window governs how recent the speed anchor must be to describe TODAY. The two
# state axes must agree on what "recent" means: the ladder already degrades honestly past this
# window (no qualifying long ⇒ long_km_now None ⇒ Λ exactly 1.0, the neutral prior), while v₀ used
# to be the last EWMA point at ANY age — so a runner back after a layoff was shown a pre-layoff
# value as "off today's shape". His own corpus cannot calibrate a detraining decay to fix that
# properly (4 gaps ≥14 days, max 21, re-measurement scattering −9.5 to +0.1 pts per 30 days), and
# inventing one would be exactly the guess this engine refuses. So the model does not decay a stale
# anchor — it declines to call it today.
FT_ANCHOR_TRAIL_DAYS = FT_LADDER_TRAIL_DAYS
FT_MARA_TOL = 0.04      # historical race auto-detect: distance within ±4% of the marathon
FT_MIN_CTL = {"marathon": 45, "half": 35, "10k": 25, "5k": 20}   # healthy-finish floors (§PER1)

# §FT3 — the prediction is a BAND, never a point (owner-decided: the range is the headline; a
# point invites anchoring on false precision). Predictive spread in log-time, composed from what
# is genuinely uncertain — ln(actual) = ln(P50) + ε_race + ε_c + ε_state + ε_runway:
#   race    — race-to-race execution noise, measured as the corpus log-residual spread about its
#             own mean (his 4 marathons: 0.066 — the 2023-05 blow-up lives here, deliberately);
#   calib   — posterior spread of the per-runner correction, σ_race/√(n+K) — shrinks as races land;
#   state   — speed-projection risk, proportional to the PROJECTED eVO₂ gain (truth-anchoring ⇒
#             zero projected gain ⇒ zero state risk: the band narrows as runs land, structurally);
#   runway  — completion risk per remaining week (illness/life between now and the start line).
# Cold start (no raced corpus) is wide BY DESIGN — the "never frustrates the runner" clause.
FT3_Z = 1.2816              # 80% central band (P10–P90)
FT3_SIGMA_RACE_FLOOR = 0.05  # never claim a race varies less than ±5% — no corpus is that clean
FT3_SIGMA_RACE_COLD = 0.08   # population race-noise prior when no raced datapoint exists yet
# §FT10 — ONE measured term replaces the two invented ones. §FT3 carried `state` (∝ the projected
# speed gain, ±50%) and `runway` (0.003 log-time per remaining week): a guess at how wrong the
# projection might be, plus a guess at completion risk, both invented and — as it turned out —
# double-counting each other, since the measured dispersion of that projection IS the total error of
# that same projection. Sweeping every contiguous window of his corpus (log §38) and removing the
# EWMA's own reading noise (σ≈0.894 pts, a mid-week-vs-neighbours estimator; it stays out because
# σ_race already carries "the model's read of the runner on race day is imperfect" — putting it here
# too would double-count) gives the speed axis's genuine forecast dispersion:
#
#     h wk:     2     3     4     6     8    12    16    19    24    30
#     σ pts: 1.34  1.48  1.55  1.83  1.81  2.10  2.17  2.31  2.63  3.03      σ ≈ A·h^P
#     fit  : 1.33  1.48  1.59  1.76  1.89  2.09  2.25  2.35  2.49  2.63      A=1.12, P=0.25
#
# Sub-random-walk — dispersion grows like the FOURTH ROOT of horizon, not linearly: fitness is
# mean-reverting, so week 20 adds far less uncertainty than week 2 did. The retired term was 2.7×
# too narrow at 4 weeks and 1.7× too wide at 30, and accidentally about right at the 19-week horizon
# he happens to race at — which is why it never looked wrong. §38's first cut read P≈0.42 from a
# crude two-point log-log slope over OVERLAPPING windows; fitting properly (weighted by effective
# independent windows n/h) gives P=0.25 at rms 0.025 in log.
# ⚠ h=1 is EXCLUDED from the fit and from the per-runner measurement. De-noising removes 76% of its
# variance (raw 1.45² − 2·0.894² = 0.50), so it is hostage to the reading-noise estimate rather than
# informative about the horizon; including it drags P to 0.46 and wrecks the fit (rms 0.140 vs
# 0.025). P is structural and shared; A is measured per runner and shrunk toward the population
# value (the `c`/`resp` posture: a corpus earns its own coefficient, a cold start inherits the prior
# exactly, and the prior is itself his fit — so his own shrinkage reads as neutral).
FT10_DISP_P = 0.25          # horizon exponent — structural, shared by every runner
FT10_DISP_A0 = 1.12         # population coefficient (eVO₂ pts of dispersion at h=1 week)
FT10_DISP_K = 40            # shrinkage strength: the prior counts as K effective windows
# Horizons sampled per runner. h=1 excluded (above); h>19 dropped too — with P fixed, the per-runner
# fit is only for A, whose weight is n/h, so h=24/30 contribute effective counts of 4.2 and 2.5
# against 109 at h=2 while costing the most inner steps. They anchored the EXPONENT, which is now
# structural, so measuring them every regen buys nothing.
FT10_DISP_H = (2, 3, 4, 6, 8, 12, 16, 19)
FT10_DISP_MIN_W = 30        # a horizon needs this many windows before it may inform the estimate


def _ft_band(pred_s, weeks_away, sigma_race=None, n_races=0, sens_per_pt=0.0, disp_a=FT10_DISP_A0):
    """§FT3 — the 80% predictive band around a P50 finish time (pure, det-testable). Returns the
    payload dict; multiplicative in time (symmetric in log). Components rounded in for honesty —
    the UI can show WHY the band is wide (cold corpus vs long horizon).

    §FT10 — `horizon` is the speed axis's MEASURED forecast dispersion at `weeks_away`, converted to
    log-time by the model's own local sensitivity: it replaces §FT3's `state` and `runway` pair,
    which guessed at the projection's error and at completion risk separately while in fact
    double-counting one another. Zero at zero weeks is correct here and not an optimism: a race run
    today has no unbuilt training left to go wrong, and how well the model reads a runner ON the day
    is exactly what σ_race measures."""
    sr = max(sigma_race if (sigma_race is not None and n_races > 0) else FT3_SIGMA_RACE_COLD,
             FT3_SIGMA_RACE_FLOOR)
    sc = sr / math.sqrt(n_races + FT_SHRINK_K)
    sh = disp_a * (max(0, weeks_away or 0) ** FT10_DISP_P) * sens_per_pt
    sig = math.sqrt(sr * sr + sc * sc + sh * sh)
    lo, hi = round(pred_s * math.exp(-FT3_Z * sig)), round(pred_s * math.exp(FT3_Z * sig))
    return {"lo_seconds": lo, "hi_seconds": hi, "lo_hms": _fmt_hms(lo), "hi_hms": _fmt_hms(hi),
            "level": 80, "sigma_log": round(sig, 4),
            "components": {"race": round(sr, 4), "calibration": round(sc, 4),
                           "horizon": round(sh, 4), "disp_a": round(disp_a, 3)}}


def _ft_base_time(vo2max, typ):
    """§FT1 speed axis — the prepared-athlete finish time (seconds) at `vo2max`, per distance.
    Marathon runs the pace_zones marathon fraction CONTINUOUSLY (§33f-11); the other RACE_KM
    distances solve the Daniels/Gilbert %VO₂max-vs-duration curve at vVO₂max (fixed point on
    t = d / (vv·p(t)) — converges in a handful of iterations). None if inputs are missing.

    §33f-11 — this used to read `pace_zones(vo2max)["marathon"]`, which is rounded to a WHOLE
    sec/km for display. Over 42.195 km that quantized every marathon prediction into 42.2-second
    treads: fitness could climb ~0.15 eVO₂ with the predicted finish frozen, then jump 42 s at
    once. The ledger chart drew that staircase while its own caption promised every step meant a
    model upgrade or real movement, and the band's state term — a finite difference across the
    treads — wobbled ±15%. The fraction is shared (MARATHON_PACE_FRAC) so the displayed zone and
    the modelled pace remain the same pace; only the rounding is dropped, and only here. The zone
    grid is untouched, so §PRO7 and every plan-side consumer stay byte-identical."""
    km = RACE_KM.get(typ)
    if not km or not vo2max:
        return None
    vv = _v_at_vo2max(vo2max)                               # m/min
    if typ == "marathon":
        return km * 1000.0 / (vv * MARATHON_PACE_FRAC) * 60.0 if vv else None
    t = km * 1000.0 / vv                                    # minutes at 100% vVO₂max (seed)
    for _ in range(30):
        p = 0.8 + 0.1894393 * math.exp(-0.012778 * t) + 0.2989558 * math.exp(-0.1932605 * t)
        t_new = km * 1000.0 / (vv * p)
        if abs(t_new - t) < 1e-6:
            break
        t = t_new
    return t * 60.0


FT_TILT_REF_VDOT = 45      # §33e — reference VDOT for the distance tilt (see _ft_scale_tilt)


def _ft_daniels_time(vo2max, typ):
    """§33e reference construction — the CANONICAL Daniels/Gilbert race time: discount the VO₂
    (VDOT · p(t)) and then invert the oxygen-demand curve for velocity. This is NOT our axis; it
    exists only as a yardstick for `_ft_scale_tilt`. None on nonsense inputs."""
    km = RACE_KM.get(typ)
    if not km or not vo2max:
        return None
    t = km * 1000.0 / _v_at_vo2max(vo2max)                  # minutes, seeded at 100% vVO₂max
    for _ in range(200):
        p = 0.8 + 0.1894393 * math.exp(-0.012778 * t) + 0.2989558 * math.exp(-0.1932605 * t)
        t_new = km * 1000.0 / _v_at_vo2max(vo2max * p)      # ← the discount lands on the VO₂, not the velocity
        if abs(t_new - t) < 1e-9:
            break
        t = t_new
    return t * 60.0


@functools.lru_cache(maxsize=None)
def _ft_scale_tilt(typ):
    """§33e — how far OUR speed axis sits from the canonical Daniels construction at `typ`, as a
    time ratio (>1 = our axis predicts slower). We apply the %-vs-duration curve to VELOCITY, which
    keeps the whole shipped pace_zones grid (velocity fractions) and the axis self-consistent under
    inversion; Daniels discounts the VO₂. The two are NOT a constant apart — the gap grows with race
    duration (≈ +1% at 5k, +2% at 10k, +3.4% at half, +4.4% at the marathon), which is exactly why
    it does NOT simply "wash into c" as §33e first assumed: a per-runner correction learned on
    MARATHONS carries ~4.4% of marathon-specific tilt, and applying it unmodified to a 10k
    prediction imports ~2% of error that has nothing to do with the runner.

    Evaluated at a FIXED reference VDOT so this is a pure function of DISTANCE. The tilt also drifts
    mildly with fitness (marathon: 4.2% at VDOT 40 → 4.9% at 52), but folding that in would move a
    same-distance correction too, and the whole point here is that same-distance transfer must be
    EXACTLY neutral. Second-order, deliberately left out."""
    ours, ref = _ft_base_time(FT_TILT_REF_VDOT, typ), _ft_daniels_time(FT_TILT_REF_VDOT, typ)
    return (ours / ref) if (ours and ref) else 1.0


def _ft_transfer_correction(c, tilt_corpus, typ):
    """§33e — carry a correction learned on one set of race distances onto another. `c` measures the
    runner's demonstrated offset from the model, but it also absorbed the DISTANCE TILT of whatever
    the corpus was made of; re-scaling by tilt(target)/tilt(corpus) strips that part out and re-adds
    the target distance's own. Exactly 1.0 (byte-identical) when the corpus and the target are the
    same distance, or when there is no corpus at all — so the established all-marathon path and the
    cold-start prior are both untouched, and only genuine cross-distance transfer moves."""
    if not c or not tilt_corpus or typ not in RACE_KM:
        return c
    return c * _ft_scale_tilt(typ) / tilt_corpus


def _ft_endurance(ctl, floor, long_km=None, race_km=None):
    """§FT1 durability axis — endurance factor over (race-day CTL, long-run readiness). Below the
    floor: exactly the §PRO7 fade (capped), so 'too soon'/'earn it' states read identically. Above:
    a saturating gain, C¹ at the floor, strictly decreasing forever (the det invariant). The ladder
    term Λ is a second strict axis — a CTL-50 runner with 30 km longs and one with 12 km longs are
    different marathons — and is exactly 1.0 when the ladder is unknown (det/cold-start neutral)."""
    c = ctl or 0.0
    if c < floor:
        d = min(1.0 + FADE_PER_CTL * (floor - c), FADE_CAP)
    else:
        d = 1.0 - FT_DUR_GAIN * (1.0 - math.exp(-(c - floor) / FT_DUR_TAU))
    lam = 1.0
    if long_km and race_km:
        s = lambda r: 1.0 - math.exp(-r / FT_LADDER_RHO)
        lam = 1.0 + FT_LADDER_L * (s(FT_LADDER_REF) - s(long_km / race_km))
    return d * lam


def _project_finish_time(vo2max, ctl, typ, floor, long_km=None, correction=1.0):
    """Estimated finish time (seconds) for `typ` at projected race-day state (effective VO₂max, CTL,
    longest trailing long). §FT1: speed axis × durability axis × the per-runner shrinkage correction
    (1.0 for a new runner — the population prior). All RACE_KM distances. None on missing inputs."""
    km = RACE_KM.get(typ)
    base = _ft_base_time(vo2max, typ)
    if not km or not base:
        return None
    return round(base * _ft_endurance(ctl, floor, long_km, km) * correction)


def feasibility(objective, ctl0, vo2max, weeks_away, projected_ctl=None,
                race_long_km=None, correction=1.0, projected_vo2max=None, vo2_curve=None,
                band_inputs=None):
    """§6a.5 — a sober read on whether the objective is reachable on this runway. CTL can
    grow ~3–4%/wk sustained; from his detrained CTL that lands far short of his PB shape, so
    we separate 'finish healthy' (realistic) from 'PB/target time' (not on this runway).
    §6f Step E / §PRO7b — when `projected_ctl` is given (the engine's real projected race fitness —
    the PEAK CTL carried into the taper, realized on race day through its freshness — chained
    through the actual generated blocks under the ACWR ceiling) it is preferred over the generic
    ~3.4%/wk estimate, so the verdict 're-reads each block' instead of a hand-wave."""
    est = round(ctl0 * (1.034 ** max(0, weeks_away)), 0)         # generic ~3.4%/wk fallback
    proj = round(projected_ctl) if projected_ctl is not None else est
    src = ("the engine's projection through the planned blocks (ACWR-capped)"
           if projected_ctl is not None else "~3–4%/wk sustained")
    # §PER1 F2 — a lower bound, so the verdict can warn "too soon" instead of always promising "finish".
    # The honest "too soon" signal is the CONJUNCTION of a short runway AND a projected race-day fitness
    # too low to carry the distance — NOT either alone:
    #   • runway alone mis-fires: weeks_away shrinks as the race nears, so it would flag every race in
    #     its final weeks — exactly when a well-built athlete is most ready (false positive, common case).
    #   • CTL alone mis-fires: a long-runway marathon off a low detrained CTL is the "finish healthy off
    #     a layoff" case this function is meant to bless.
    # Together they catch only the genuine pathology (the §PER1-F1 fresh-near-race overrun): not enough
    # time AND not enough projected base. Distance-aware thresholds.
    MIN_RUNWAY = {"marathon": 14, "half": 9, "10k": 5, "5k": 4}
    typ = (objective.get("type") or "").lower()
    floor = FT_MIN_CTL.get(typ, 0)
    short_runway = weeks_away is not None and weeks_away < MIN_RUNWAY.get(typ, 6)
    low_fitness = proj < floor
    # §PRO7/§FT1 — finish-time headline + runway-sensitivity curve (more weeks ⇒ higher CTL ⇒ faster,
    # now through the WHOLE range — Model A is strictly monotone in load, so the curve genuinely moves
    # above the floor too). Extrapolate CTL at +4/+8 weeks with the same generic ~3.4%/wk the verdict
    # uses; the ladder + per-runner correction ride every point so the curve stays internally consistent.
    # §FT2 — Model B's projected race-day speed axis, when the caller provides it (the live regen
    # does; det/neutral callers omit it and get today's value — behavior unchanged). `vo2_curve`
    # extends the speed axis to the +4/+8-week points the same way projected CTL extends the load.
    v_race = projected_vo2max or vo2max
    fin_now = _project_finish_time(v_race, proj, typ, floor, race_long_km, correction)
    finish_time = None
    if fin_now:
        curve = [{"plus_weeks": n, "ctl": round(proj * (1.034 ** n)),
                  "evo2": (round((vo2_curve or {}).get(n, v_race), 1) if projected_vo2max else None),
                  "hms": _fmt_hms(_project_finish_time((vo2_curve or {}).get(n, v_race),
                                                       round(proj * (1.034 ** n)), typ, floor,
                                                       race_long_km, correction))}
                 for n in (0, 4, 8)]
        # §FT3 — the band IS the prediction; the P50 stays as the trend/detail signal. Speed-
        # projection risk needs the model's local time-sensitivity to a point of eVO₂ (numeric,
        # at the projected state) and the projected gain vs the measured v₀ (band_inputs).
        bi = band_inputs or {}
        # §FT6 — what the BUILD buys: the same model read at TODAY's measured state (current speed
        # axis, current fitness, the ladder actually behind the runner) set against the race-day
        # projection. This is the question a runner actually has — "what is this block FOR?" — and
        # the +4/+8-week runway curve never answered it: that curve prices a LATER RACE DATE, a
        # counterfactual nobody asked about, which reads as a timeline and (while the curve was
        # frozen) as "training changes nothing". None when today's state isn't knowable.
        # §FT9 — "off today's shape" is a claim about TODAY, and it needs a measurement recent
        # enough to be about today. v₀ is the last EWMA point at ANY age, so a runner back from a
        # layoff would otherwise be shown their pre-layoff speed as their current one. Past
        # FT_ANCHOR_TRAIL_DAYS — the same window past which the ladder axis already goes neutral —
        # the read is WITHHELD rather than decayed: his corpus cannot calibrate a detraining rate
        # (4 gaps ≥14 days, max 21, scatter −9.5…+0.1 pts/30d) and a guessed one would be worse
        # than silence. The race-day projection still stands; it is a projection and says so.
        v_age = bi.get("v0_age_days")
        anchor_stale = v_age is not None and v_age > FT_ANCHOR_TRAIL_DAYS
        v_now = bi.get("v0") or (None if projected_vo2max else vo2max)
        t_today = None if anchor_stale else _project_finish_time(
            v_now, round(ctl0), typ, floor, bi.get("long_km_now"), correction)
        today_read = ({"seconds": t_today, "hms": _fmt_hms(t_today),
                       "at_ctl": round(ctl0), "at_evo2": round(v_now, 1),
                       "long_km": (round(bi["long_km_now"], 1) if bi.get("long_km_now") else None),
                       "gain_seconds": t_today - fin_now} if t_today else None)
        t_up = _project_finish_time(v_race + 1, proj, typ, floor, race_long_km, correction)
        sens = math.log(fin_now / t_up) if t_up else 0.0
        band = _ft_band(fin_now, weeks_away, bi.get("sigma_race"), bi.get("n_races") or 0,
                        sens, bi.get("disp_a", FT10_DISP_A0))
        finish_time = {"distance": typ, "seconds": fin_now, "hms": _fmt_hms(fin_now),
                       "at_ctl": proj, "curve": curve, "band": band, "today": today_read,
                       "at_evo2": (round(v_race, 1) if projected_vo2max else None),
                       "long_km": (round(race_long_km, 1) if race_long_km else None),
                       "correction": round(correction, 3),
                       "anchor_stale": ({"age_days": v_age, "as_of": bi.get("v0_as_of")}
                                        if anchor_stale else None),
                       "note": ("a range, not a promise: it assumes the laid build completes, and race "
                                "day rides fuelling, pacing, weather and the day's legs. The median is "
                                "the trend signal — more runway → higher fitness → faster — and the band "
                                "narrows as runs land and raced results calibrate it.")}
    if short_runway and low_fitness:
        label = objective.get("label", "the race")
        msg = (f"That's only **{weeks_away} week{'s' if weeks_away != 1 else ''}** to {label}"
               f"{f' (a {typ})' if typ else ''}, with projected race-day fitness ≈ CTL {proj:.0f} "
               f"(from {ctl0:.0f} now) — too little time AND base to build to a healthy finish. Consider "
               f"a later date or a shorter distance; the engine still builds you safely toward it and "
               f"re-reads this each block as fitness returns.")
        if finish_time:
            b = finish_time["band"]
            msg += (f" At this fitness the honest read is **{b['lo_hms']}–{b['hi_hms']}** — and it "
                    f"gets faster the more runway you give it (see the curve).")
        return {"verdict": "too soon", "projected_ctl": proj, "estimate_ctl": est,
                "finish_time": finish_time, "note": msg}
    if low_fitness:
        # §PER1 F3 — runway is long enough, but the engine's OWN projection lands BELOW the floor a
        # healthy finish needs. Don't promise a flat "finish" on a number the conservative plan doesn't
        # deliver (the floor-projection deliberately ignores the opt-in earned levers / CTL floor, which
        # the real plan WILL trigger as measured fitness returns). Honest middle verdict — reachable, but
        # only if you build into it — not a red "too soon". Closes the "CTL 16 · finish" incongruity.
        label = objective.get("label", "the race")
        msg = (f"Projected race-day fitness ≈ CTL {proj:.0f} (from {ctl0:.0f} now, via {src}) — below the "
               f"~CTL {floor:.0f} a healthy {typ or 'race'} finish needs. The "
               f"**{weeks_away}-week** runway makes {label} reachable, but only if you **build into it**: "
               f"the engine lifts volume as your measured fitness proves itself (the CTL floor and the "
               f"earned levers) and re-reads this each block — the conservative floor-projection alone "
               f"doesn't get you there yet.")
        if finish_time:
            b = finish_time["band"]
            msg += (f" Projected finish **{b['lo_hms']}–{b['hi_hms']}** (median {finish_time['hms']}) "
                    f"at this fitness, faster as the build banks (the curve shows +4 / +8 weeks).")
        return {"verdict": "earn it", "projected_ctl": proj, "estimate_ctl": est,
                "finish_time": finish_time, "note": msg}
    verdict = "finish"  # projection clears the distance floor — a healthy finish is the honest call
    # §FT3 copy review — this prose is a PUBLIC surface and must generalize: no baked-in runner
    # history (the old text hardcoded "6-month layoff" + "your sub-4 PB"), and it predicts what
    # the max-safe-load trajectory is worth — it never prescribes ambition.
    msg = (f"Projected fitness by race day ≈ CTL {proj:.0f} (from {ctl0:.0f} now, via "
           f"{src}) — at/above the ~CTL {floor:.0f} a healthy {typ or 'race'} finish needs. "
           f"That supports **finishing {objective.get('label','the race')} healthy**; the engine "
           f"re-reads this each block as measured fitness moves.")
    if finish_time:
        b = finish_time["band"]
        msg += (f" Projected finish **{b['lo_hms']}–{b['hi_hms']}** (median {finish_time['hms']}) — "
                f"the range narrows as runs land.")
    return {"verdict": verdict, "projected_ctl": proj, "estimate_ctl": est,
            "finish_time": finish_time, "note": msg}


# ── §FT1 — per-runner calibration: the race corpus pulls the population curve personal ──────────
# A raced datapoint is (state the runner was in, time they actually ran). The correction is the
# shrunk mean log-ratio actual/predicted over the corpus: a new runner starts at the population
# prior (c=1, wide band — §FT3), every raced datapoint pulls the curve toward their demonstrated
# reality. This is the generality mechanism (log §33 thread ii), not a bolt-on.

def _ft_state_at(db, race_iso):
    """The runner's reconstructed state on race MORNING (race day excluded from every channel):
    (effective VO₂max, CTL, longest trailing group-summed long). eVO₂ mirrors vo2max_trend's EWMA
    (α=0.25 over `use_vo2max` runs); CTL is the trimp-EWMA reconstruction; the ladder is the
    longest §SJ-group-summed run in the trailing FT_LADDER_TRAIL_DAYS."""
    sm = None
    for iso, v in _ft_vo2_series(db):          # §FT2 — the one model-scale eVO₂ series
        if iso >= race_iso:
            break
        sm = v
    day_before = (_date(race_iso) - timedelta(days=1)).isoformat()
    hist = reconstruct_history(db, end=day_before)
    ctl = hist[-1]["ctl"] if hist else None
    drop = dropped_ids(db)
    since = (_date(race_iso) - timedelta(days=FT_LADDER_TRAIL_DAYS)).isoformat()
    rows = [r for r in db.execute(
        "SELECT id, date, date_time, distance, duration, elapsed_time FROM activities WHERE " +
        RUN_FAMILY_SQL + " AND date >= ? AND date < ? AND distance > 0 AND duration > 0",
        (since, race_iso)).fetchall() if r["id"] not in drop]
    long_km = max((sum(p["distance"] for p in g) for g in _session_groups(rows)), default=None)
    return sm, ctl, long_km


def _race_seconds(act):
    """§33f-4 — a race result is gun-to-mat, not moving time: the elapsed clock when the row carries
    one, the moving duration otherwise. ONE definition for every consumer — the corpus that
    CALIBRATES the model (`_ft_race_corpus`) and the ledger that SCORES it (`_ft_prediction_score`,
    the §6s reckoning) must measure the same thing, or every stopped-clock second at an aid station
    reads back as prediction error and quietly biases the shrinkage. Accepts a sqlite3.Row or the
    §SJ split-group dict from `_race_day_activity`."""
    if act is None:
        return None
    try:
        e = act["elapsed_time"]
    except (KeyError, IndexError):        # §SJ group dict assembled without an elapsed sum
        e = None
    return e or act["duration"]


def _ft_race_corpus(db):
    """The runner's raced datapoints: historical marathons auto-detected by distance (within
    ±FT_MARA_TOL of 42.195 — a marathon is never a casual training distance; shorter races are NOT
    auto-detected, a 21.1 km row is usually just a long run) + resolved race objectives with a
    finished result (§RL outcome), deduped by date. Sorted oldest-first."""
    lo, hi = RACE_KM["marathon"] * (1 - FT_MARA_TOL), RACE_KM["marathon"] * (1 + FT_MARA_TOL)
    drop = dropped_ids(db)
    races = {}
    for r in db.execute("SELECT id, date, distance, duration, elapsed_time FROM activities WHERE " +
                        RUN_FAMILY_SQL + " AND distance BETWEEN ? AND ?", (lo, hi)).fetchall():
        secs = _race_seconds(r)                     # gun-to-mat, the one race-time definition
        if r["id"] in drop or not secs or not (150 <= secs / 60 <= 480):   # jog/walk sanity gates
            continue
        races[r["date"]] = {"date": r["date"], "type": "marathon", "seconds": secs}
    for o in db.execute("SELECT date, type, outcome FROM objectives WHERE status='done'").fetchall():
        try:
            oc = json.loads(o["outcome"] or "{}")
        except (ValueError, TypeError):
            continue
        typ = (o["type"] or "").lower()
        if oc.get("status") == "finished" and oc.get("actual_seconds") and typ in RACE_KM:
            races.setdefault(o["date"], {"date": o["date"], "type": typ,
                                         "seconds": oc["actual_seconds"]})
    return sorted(races.values(), key=lambda r: r["date"])


def _ft_shrunk_correction(log_ratios, k=FT_SHRINK_K):
    """Shrinkage estimator (pure, det-testable): exp(Σ ln(actual/pred) / (n + k)) — the population
    prior counts as `k` pseudo-races at ratio 1.0, so one noisy race nudges, a consistent corpus
    converges on the runner's demonstrated offset."""
    if not log_ratios:
        return 1.0
    return math.exp(sum(log_ratios) / (len(log_ratios) + k))


def _ft_correction(db):
    """§FT1/§FT3 — the runner's personal speed-vs-VDOT correction, shrunk toward the population
    prior, PLUS the band's race-noise inputs: returns (c, sigma_race, n_races, tilt_corpus).
    Predictions at each race use the PRIOR model (c=1) at the reconstructed race-morning state, so
    the estimator is well-defined (never fit against itself). sigma_race = the log-residual spread
    about the corpus's own mean (None below 2 races — the cold prior takes over). Owner's corpus
    2026-07-26: 4 marathons → c≈1.255, sigma_race≈0.066.

    §33e — `tilt_corpus` is the geometric-mean distance tilt of the races that produced `c`, handed
    to `_ft_transfer_correction` so a marathon-learned correction can be carried onto a 10k without
    importing marathon-specific scale error. The spread is measured on DE-TILTED residuals for the
    same reason: pooling a 10k and a marathon raw would book the ~2.4% gap between their tilts as
    race-day noise the runner never produced. Both are exactly neutral on a single-distance corpus
    (a constant shift moves no spread), so the owner's all-marathon numbers are byte-identical."""
    lrs, tilts = [], []
    for race in _ft_race_corpus(db):
        vo2, ctl, long_km = _ft_state_at(db, race["date"])
        pred = _project_finish_time(vo2, ctl, race["type"], FT_MIN_CTL.get(race["type"], 0), long_km)
        if pred:
            lrs.append(math.log(race["seconds"] / pred))
            tilts.append(math.log(_ft_scale_tilt(race["type"])))
    n = len(lrs)
    sigma_race = None
    if n >= 2:
        free = [r - t for r, t in zip(lrs, tilts)]      # residuals on one common scale
        m = sum(free) / n
        sigma_race = math.sqrt(sum((r - m) ** 2 for r in free) / n)
    tilt_corpus = math.exp(sum(tilts) / n) if n else None
    return _ft_shrunk_correction(lrs), sigma_race, n, tilt_corpus


# ── §FT2 — Model B speed side: project eVO₂ along the build, truth-anchored every regen ─────────
# The model's speed axis is the PER-RUN-CORPUS scale: the α=0.25 EWMA over `use_vo2max` per-SESSION
# values — the exact scale the §FT1 race-corpus states are stated in, so calibration and prediction
# can never scale-drift. (Runalyze's `effective_vo2max` snapshot applies the user's correction
# factor and its own smoothing — a DIFFERENT vocabulary, kept for training zones; the two are never
# mixed inside Model A/B.) Response model, RE-calibrated 2026-07-28 on his 225 consecutive training
# week-pairs (2021→2026): dv/wk = R·resp·(T_wk/100)·max(0, 1 − v/ceiling) — intensity-weighted
# load drives the response (TRIMP is composition-sensitive by construction: the §T2 quality mix is
# what lifts a week's TRIMP at equal km), saturating toward the runner's demonstrated ceiling.
# Fit + replay are 1-week-ahead OLS through the origin; rmse 1.45/wk (weekly noise → the §FT3 band).
# §FT10 — an earlier note here claimed the model "undershoots sustained builds" (−2.5 over 4 weeks).
# WITHDRAWN: that measured windows selected BECAUSE they rose, which is conditioning on the outcome.
# Swept over every contiguous window the model is unbiased (mean +0.09 / +0.18 / +0.30 eVO₂ at 4 / 8
# / 12 weeks); rising windows read −2.15 and falling ones +3.35, which is exactly what an unbiased
# predictor must do. The real finding that sweep produced was about the BAND, not the point — see
# FT10_DISP_* and log §38.
# Truth-anchoring is structural: every regen re-bases v₀ on the MEASURED corpus EWMA and projects
# only the remaining weeks — a fast- or slow-responder can never drift from reality (§PRO9-style),
# and the shrunk response factor pulls the population rate toward the runner's own measured slope.
# §FT8 — R was refit when the series became per-SESSION (0.169 → 0.139, rmse 1.64 → 1.45/wk). It is
# a POPULATION PRIOR: leaving the old value would have let his personal shrinkage silently absorb
# the change (his slope fell 1.00 → 0.83, the same rate wearing a different hat) while every runner
# with an empty corpus — who gets resp = 1.0 exactly — inherited a rate fit to a series that no
# longer exists. Refit restores his slope to 1.000, which is the self-consistency check: he IS the
# calibration population, so his own shrinkage must read as neutral.
FT2_R = 0.139            # eVO₂ pts/wk per 100 weekly TRIMP at zero saturation (his-corpus fit)
FT2_CEIL_HEADROOM = 1.15  # ceiling floor: never below v₀ × this, so today's high can't freeze the axis
FT2_SHRINK_K = 8         # response-factor prior strength (pseudo week-pairs at slope 1.0)
FT2_EWMA_A = 0.25        # per-SESSION smoothing of the per-run estimates (see §FT8 below)
# §FT8 — what may inform the speed axis at all. The per-run VO₂max estimate is a pace-vs-HR
# inference, and it needs a steady state to infer FROM. A short part of a split session is the one
# recording that never has one: in this corpus those parts are overwhelmingly STRIDE sets (§RD reads
# them back as such — "13× strides @4:59/km" inside a 1.3 km recording), i.e. bursts at 3:40–5:00/km
# threaded through jog floats, with HR lagging the bursts it is being divided by. The resulting
# estimate is not noisy so much as meaningless, and it scatters BOTH ways: 26.0 and 26.0 on two
# stride sets, 50.1 on a 1.3 km piece, 51.2 on a 1.9 km one. Measured against the EWMA of everything
# before it, his corpus splits hard at 4 km — robust spread (MAD) 4.2–6.2 below vs 1.25–1.44 at and
# above, a 3–4× collapse — and the duration axis agrees (break at ~20–25 min) but blunter, so
# distance is the gate. Its ceiling is the shortest distance the model must never lose:
# min(RACE_KM) = 5 km, so a 5 km race still anchors the axis it is direct evidence for.
# (§RD's own `kind` would be the finer gate — strides/interval fragments refused by name — but a
# cached structure needs streams and a token, so it is not always there; distance always is.)
FT_VO2_MIN_KM = 4.0      # a recording under this may not inform the speed axis (calibrated 2026-07-28)


def _ft_vo2_series(db):
    """The model-scale eVO₂ series: (date, EWMA over `use_vo2max` per-SESSION values), full history,
    oldest-first. The single source for v₀ (truth-anchor), the ceiling, the response factor and the
    §FT1 race-corpus states — one scale, no mixing. De-duped against `dropped_ids` like every other
    projector consumer (2258): a duplicated row would otherwise double-step the EWMA and drag v₀,
    the ceiling and the response fit off the owned truth.

    §FT8 — the unit is the SESSION, not the recording, for exactly that reason. He deliberately
    splits a session into parts (§SJ), so a raw-row EWMA steps twice for one training day and lands
    on whichever fragment was saved last: on 2026-07-27 a 1.3 km STRIDE set recorded 87 s after a
    6 km easy run carried an estimate of 26.0 and, being last, WAS v₀ — dragging the anchor
    38.0 → 34.4 and the race-day median 4:45:41 → 4:59:00 on thirteen 20-second bursts. The read
    side already knew what that recording was (§RD: "13× strides"); the speed axis was the one
    consumer that never asked. Parts are grouped by the same §SJ rule
    the read side uses (one definition), each part must clear FT_VO2_MIN_KM to contribute, and the
    session's value is the distance-weighted mean of the parts that do — evidence in proportion to
    how much running it is. A session with no qualifying part steps the EWMA not at all: the honest
    reading of a day we cannot measure is silence, not a guess (and a runner whose whole history is
    short recordings gets an empty series ⇒ the §FT5 cold start, which is what that path is for)."""
    drop = dropped_ids(db)
    rows = [r for r in db.execute(
        "SELECT id, date, date_time, distance, duration, elapsed_time, raw FROM activities WHERE " +
        RUN_FAMILY_SQL + " ORDER BY date ASC").fetchall() if r["id"] not in drop]
    out, sm = [], None
    for grp in _session_groups(rows):
        num = den = 0.0
        for r in grp:
            km = r["distance"] or 0.0
            if km < FT_VO2_MIN_KM:
                continue
            try:
                d = json.loads(r["raw"])
            except (ValueError, TypeError):
                continue
            if d.get("use_vo2max") and d.get("vo2max"):
                num += float(d["vo2max"]) * km
                den += km
        if not den:
            continue
        v = num / den
        sm = v if sm is None else sm + FT2_EWMA_A * (v - sm)
        out.append((grp[0]["date"], sm))
    return out


def _ft_weekly_series(db):
    """Weekly (Mon-keyed) view of the model eVO₂ + TRIMP history: ({monday: end-of-week eVO₂},
    {monday: weekly TRIMP}). The end-of-week sampling is the calibration's own aggregation AND the
    glitch filter — a bad-data spike (e.g. a mislabeled ride inflating per-run VO₂max) dies inside
    its week under the EWMA, while a genuinely sustained peak survives to the week boundary."""
    drop = dropped_ids(db)
    wk_v, wk_t = {}, {}
    for iso, v in _ft_vo2_series(db):
        d = _date(iso)
        wk_v[(d - timedelta(days=d.weekday())).isoformat()] = v
    # §33f-8 — RUN-ONLY, to match what the projection is fed: the laid plan's weekly TRIMPs are
    # run sessions, so calibrating the response against whole-body load (his non-run share ≈ 5.6%)
    # would fit a rate the plan side can never deliver. §33f-2 — and de-duped, like every sibling.
    for r in db.execute("SELECT id, date, trimp FROM activities WHERE " + RUN_FAMILY_SQL +
                        " AND date != '' AND trimp IS NOT NULL").fetchall():
        if r["id"] in drop:
            continue
        d = _date(r["date"])
        mon = (d - timedelta(days=d.weekday())).isoformat()
        wk_t[mon] = wk_t.get(mon, 0.0) + (r["trimp"] or 0.0)
    return wk_v, wk_t


def _ft_weekly_response_pairs(wk_v, wk_t, ceiling):
    """Consecutive training week-pairs (pred_dv_at_resp1, actual_dv) for the response-factor fit:
    end-of-week model eVO₂ + the NEXT week's TRIMP, weeks separated by exactly 7 days (layoff
    jumps excluded — a gap week is not a response datapoint). MUST share the caller's ceiling —
    a mismatched ceiling silently rescales every prediction and fakes a slow/fast responder."""
    weeks = sorted(wk_v)
    pairs = []
    for a, b in zip(weeks, weeks[1:]):
        if (_date(b) - _date(a)).days != 7:
            continue
        pred = FT2_R * (wk_t.get(b, 0.0) / 100.0) * max(0.0, 1.0 - wk_v[a] / ceiling)
        pairs.append((pred, wk_v[b] - wk_v[a]))
    return pairs


def _ft_shrunk_slope(pairs, k=FT2_SHRINK_K):
    """Shrinkage slope (pure, det-testable): least-squares slope of actual on predicted weekly
    ΔeVO₂, with `k` pseudo-pairs of average leverage at slope 1.0 — no data ⇒ exactly 1.0, a
    consistent corpus converges on the runner's measured response rate. Clamped to [0.25, 2.5]
    (beyond that it's data trouble, not physiology)."""
    if not pairs:
        return 1.0
    spp = sum(p * p for p, _ in pairs)
    spa = sum(p * a for p, a in pairs)
    m = spp / len(pairs) if spp else 1.0
    return min(2.5, max(0.25, (spa + k * m) / (spp + k * m)))


def _ft_project_evo2(v0, weekly_trimps, ceiling, resp=1.0):
    """§FT2 — race-day model eVO₂ from today's measured value through the remaining laid weeks.
    Saturating and monotone-safe: the rate is never negative (at/over the ceiling the projection
    PLATEAUS — more load never predicts a slower runner), so det/ft-monotone extends cleanly."""
    v = v0
    for t in weekly_trimps:
        v += FT2_R * resp * ((t or 0.0) / 100.0) * max(0.0, 1.0 - v / ceiling)
    return v


def _ft_speed_state(db):
    """The model-scale speed state for this regen: (v₀ = current corpus-EWMA eVO₂, ceiling =
    demonstrated historical peak sampled at week boundaries — one number shared by the projection
    AND the response-pair fit, response factor = shrunk measured slope, and the DATE v₀ was last
    measured on). (None, ...) on an empty corpus — the caller falls back to the frozen effective
    value. §FT9 — the date is returned because the anchor's age is part of its meaning: v₀ is the
    last EWMA point whatever its age, so without it the caller cannot tell a value measured today
    from one measured before a layoff."""
    series = _ft_vo2_series(db)
    if not series:
        return None, None, 1.0, None
    v0 = series[-1][1]
    wk_v, wk_t = _ft_weekly_series(db)
    # §33f-1 — the ceiling is the demonstrated peak but NEVER v₀ itself: a runner sitting at their
    # own all-time EWMA high would get 1 − v/ceiling = 0, i.e. zero projected gain AND a zeroed band
    # state term — the §31 frozen-curve pathology, re-grown on the speed axis. The headroom floor
    # therefore applies at EVERY corpus size (it used to switch on below 10 runs, making the freeze
    # depend on run count — an undocumented cliff); a runner whose measured peak already clears
    # v₀ × headroom keeps that peak, so a real history still owns the ceiling.
    ceiling = max(max(wk_v.values()), v0 * FT2_CEIL_HEADROOM)
    return (v0, ceiling, _ft_shrunk_slope(_ft_weekly_response_pairs(wk_v, wk_t, ceiling)),
            series[-1][0])


def _ft_reading_noise(wk_v, weeks):
    """§FT10 — the eVO₂ series' own READING noise (pts), from local smoothness: a week compared with
    the mean of its two neighbours. If true fitness is locally linear that residual is pure noise,
    and var(residual) = 1.5σ². Kept OUT of the band (σ_race already carries race-day read error) but
    needed here, so a runner's measured dispersion isn't inflated by the ruler."""
    res = [wk_v[b] - 0.5 * (wk_v[a] + wk_v[c])
           for a, b, c in zip(weeks, weeks[1:], weeks[2:])
           if (_date(b) - _date(a)).days == 7 and (_date(c) - _date(b)).days == 7]
    if len(res) < 10:
        return None
    m = sum(res) / len(res)
    return math.sqrt(sum((x - m) ** 2 for x in res) / len(res) / 1.5)


def _ft_dispersion(db):
    """§FT10 — this runner's speed-axis forecast-dispersion coefficient A, shrunk toward the
    population FT10_DISP_A0. σ(h) = A·h^FT10_DISP_P eVO₂ points, so a residual standardised by
    h^P has sd A whatever the horizon — pooled across horizons, that is one estimate from all of
    them. Windows OVERLAP (every start, not every h-th): overlap makes the samples correlated, not
    the variance biased, so it is the efficient estimator — but each horizon is then weighted by its
    EFFECTIVE independent count n/h, or a thin corpus counting the same seasons h times over would
    outvote the prior. The reading noise comes out in quadrature at both endpoints (the projection
    starts from a noisy read and is judged against another). Empty/thin corpus ⇒ exactly the prior,
    which is what a cold start must inherit."""
    wk_v, wk_t = _ft_weekly_series(db)
    weeks = sorted(wk_v)
    if len(weeks) < FT10_DISP_MIN_W:
        return FT10_DISP_A0
    ceiling = max(wk_v.values())
    sig_read = _ft_reading_noise(wk_v, weeks)
    if sig_read is None:
        return FT10_DISP_A0
    num = den = 0.0
    for h in FT10_DISP_H:
        e2s = []
        for i in range(len(weeks) - h):
            seq = weeks[i + 1:i + 1 + h]
            if len(seq) < h or any((_date(b) - _date(a)).days != 7
                                   for a, b in zip([weeks[i]] + seq, seq)):
                continue
            v = wk_v[weeks[i]]
            for w in seq:
                v += FT2_R * (wk_t.get(w, 0.0) / 100.0) * max(0.0, 1.0 - v / ceiling)
            e2s.append((v - wk_v[seq[-1]]) ** 2)
        if len(e2s) < FT10_DISP_MIN_W:
            continue
        g2 = max(0.0, sum(e2s) / len(e2s) - 2 * sig_read * sig_read)   # de-noise the ruler out
        n_eff = len(e2s) / h                                  # overlap ⇒ correlated, not free
        num += n_eff * g2 / (h ** (2 * FT10_DISP_P))
        den += n_eff
    if den <= 0:
        return FT10_DISP_A0
    return math.sqrt((num + FT10_DISP_K * FT10_DISP_A0 ** 2) / (den + FT10_DISP_K))


def _ft_plan_weekly_trimps(plan, today, race_date_iso):
    """The remaining laid weekly TRIMPs between today and race day, oldest-first — the load
    trajectory Model B projects the speed response through. §33f-7 — summed from the SESSIONS still
    ahead (Monday-keyed), never from week totals: the current week's already-run days are ALREADY
    inside the measured v₀ (truth-anchor), so a whole-week total would count them twice and project
    a gain the runner has in fact already banked; and race day's own session is the race, not
    training toward it. Weeks with nothing left ahead simply drop out (a zero week adds zero gain)."""
    if not race_date_iso:
        return []
    rd = _date(race_date_iso)
    wk = {}
    for blk in plan.values():
        if not (isinstance(blk, dict) and isinstance(blk.get("weeks"), list)):
            continue
        for w in blk["weeks"]:
            for s in w.get("sessions") or []:
                try:
                    sd = _date(s.get("date"))
                except (ValueError, TypeError):
                    continue
                if today <= sd < rd:
                    mon = (sd - timedelta(days=sd.weekday())).isoformat()
                    wk[mon] = wk.get(mon, 0.0) + (s.get("trimp") or 0.0)
    return [t for _, t in sorted(wk.items())]


def _ft_plan_race_long(plan, race_date_iso):
    """§FT1 — the projected race-day ladder read off the LAID plan itself: the longest single
    prescribed session (km) dated within the trailing FT_LADDER_TRAIL_DAYS of race day (race day
    excluded — that session is the race). The biomechanical lever is the longest run whatever it's
    labelled (§PRO9's rule), so this is a plain max over session km. None when nothing qualifies."""
    if not race_date_iso:
        return None
    rd = _date(race_date_iso)
    lo = rd - timedelta(days=FT_LADDER_TRAIL_DAYS)
    best = 0.0
    for blk in plan.values():
        if not (isinstance(blk, dict) and isinstance(blk.get("weeks"), list)):
            continue
        for w in blk["weeks"]:
            for s in w.get("sessions") or []:
                try:
                    sd = _date(s.get("date"))
                except (ValueError, TypeError):
                    continue
                if lo <= sd < rd:
                    best = max(best, s.get("km") or 0.0)
    return best or None


# ── §FT5 — cold start: the "any runner" path (log §33). Age + one race-distance effort + an
# objective seed a usable state; the caution→assertive machinery IS the safe-learning path (no new
# safety code), the §FT3 band is wide by design, and every §PRO9-style truth-anchor re-reads the
# seeds away as real weeks land. ──────────────────────────────────────────────────────────────────
FT5_RACE_TOL = 0.10        # a seeding effort must sit within ±10% of a RACE_KM distance
FT5_PACE_SANE = (150, 620)  # sec/km sanity gates for a seeding effort (2:30–10:20 /km)
FT5_SEED_TRAIL_DAYS = 365  # §33f-3 — a seeding effort must be RECENT: today's shape, not a career PB


def _ft_vo2_from_race(seconds, typ):
    """§FT5 — VDOT inversion: the eVO₂ (model scale) whose predicted `typ` time equals `seconds`.
    Bisection over the strictly-monotone speed axis (_ft_base_time). None on nonsense inputs."""
    if not seconds or seconds <= 0 or typ not in RACE_KM:
        return None
    lo, hi = 20.0, 90.0
    if not (_ft_base_time(hi, typ) < seconds < _ft_base_time(lo, typ)):
        return None
    for _ in range(50):
        mid = (lo + hi) / 2
        if _ft_base_time(mid, typ) > seconds:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _athlete_age(db):
    """The runner's age from Settings (meta → SH_ATHLETE_AGE env → none). None unless a plausible
    whole number — the age is only ever a cold-start PRIOR, so absence is fine."""
    try:
        v = int(str(_resolve_setting(db, SETTINGS_BY_KEY["athlete_age"])[0]).strip())
        return v if 10 <= v <= 100 else None
    except (ValueError, TypeError, KeyError, sqlite3.OperationalError):
        return None      # incl. a fixture db with no meta table — no settings ⇒ no prior


def _ft_cold_start(db, today=None):
    """§FT5 — the cold-start intake: from a bare db (no shape snapshot yet), seed the engine state
    the spec names: eVO₂ = VDOT inversion of the runner's best race-distance effort (any RACE_KM
    distance ±10%, normalized to the exact distance; the FASTEST qualifying effort defines the
    seed), CTL₀/ATL₀ = the trimp-EWMA reconstruction of whatever little history exists (truth over
    prior — even two runs beat a magic constant; empty ⇒ 0, the §PER1 floors own the verdict),
    HRmax prior = Tanaka (208 − 0.7·age) when age is set. LTHR needs no seeding here — the existing
    derive gates already run over the pool as data lands. None when no qualifying effort exists.
    §33f-3 — the effort must fall inside the trailing FT5_SEED_TRAIL_DAYS window: a 3-year-old PB
    describes a runner who no longer exists, and seeding today's paces off it prescribes ~20–25%
    hot while the reconstructed CTL₀ (which IS recent) says detrained — the two seeds must
    describe the same person."""
    drop = dropped_ids(db)
    # Both reads here take the PLAN'S day, not the process's: the trailing window that decides which
    # race still describes this runner, and the CTL0/ATL0 reconstruction below. They used to read
    # datetime.now() while the caller held `today` in scope, so a cold-start plan computed for a given
    # date was seeded against whatever day the process happened to run on (det/clock-purity).
    td = today or datetime.now().date()
    since = (td - timedelta(days=FT5_SEED_TRAIL_DAYS)).isoformat()
    best = None
    for r in db.execute("SELECT id, date, distance, duration, elapsed_time FROM activities "
                        "WHERE " + RUN_FAMILY_SQL + " AND distance > 0 AND duration > 0 "
                        "AND date >= ?", (since,)).fetchall():
        if r["id"] in drop:
            continue
        secs = r["elapsed_time"] or r["duration"]
        if not (FT5_PACE_SANE[0] <= secs / r["distance"] <= FT5_PACE_SANE[1]):
            continue
        for typ, km in RACE_KM.items():
            if abs(r["distance"] - km) / km > FT5_RACE_TOL:
                continue
            vo2 = _ft_vo2_from_race(secs * (km / r["distance"]), typ)
            if vo2 and (best is None or vo2 > best["vo2_seed"]):
                best = {"activity_id": r["id"], "date": r["date"], "race_type": typ,
                        "distance_km": round(r["distance"], 2), "seconds": round(secs),
                        "vo2_seed": round(vo2, 1)}
    if not best:
        return None
    hist = reconstruct_history(db, end=td.isoformat())
    m = hist[-1] if hist else None
    best["ctl0"] = round(m["ctl"], 1) if m else 0.0
    best["atl0"] = round(m["atl"], 1) if m else 0.0
    age = _athlete_age(db)
    if age:
        best["age"] = age
        best["hrmax_prior"] = round(208 - 0.7 * age)   # Tanaka et al. 2001 — a prior, never a measurement
    return best


def _ft_prediction_score(db, race_date_iso, race_type, actual_s):
    """§FT4 — settle the LAST pre-race prediction for this race against the clock. Finds the most
    recent saved plan generated strictly before race day whose ANCHOR is this race (same date +
    type — the founding-road matching rule) and carries a finish_time, then scores it: P50 log
    error always; when the plan carried a §FT3 band, also in_band + the Gaussian log score
    (a PROPER score — an over-tight band is punished exactly like an over-wide one, so the band
    can't cheat its way to looking calibrated). None when no scorable plan exists."""
    if not (actual_s and race_date_iso):
        return None
    for r in db.execute("SELECT id, for_date, plan FROM plans WHERE for_date < ? ORDER BY id DESC",
                        (race_date_iso,)).fetchall():
        try:
            p = json.loads(r["plan"])
        except (ValueError, TypeError):
            continue
        o = p.get("objective") or {}
        ft = (p.get("feasibility") or {}).get("finish_time") or {}
        if (o.get("date") != race_date_iso or (o.get("type") or "").lower() != (race_type or "").lower()
                or not ft.get("seconds")):
            continue
        out = score_finish(ft, actual_s)      # §TR owns the maths — one scorer, two callers
        if out:
            out.update(plan_id=r["id"], for_date=r["for_date"])
        return out
    return None


def score_finish(ft, actual_s):
    """Score one saved finish-time prediction against the clock. P50 log error always; when the
    prediction carried a §FT3 band, also in_band and the Gaussian log score — a PROPER score, so an
    over-tight band is punished exactly as an over-wide one and a band cannot cheat its way to
    looking calibrated. Returns None if the prediction has no P50 to score."""
    if not (ft and ft.get("seconds") and actual_s):
        return None
    band = ft.get("band") or {}
    err_log = math.log(actual_s / ft["seconds"])
    out = {"p50_seconds": ft["seconds"], "p50_hms": ft.get("hms"), "actual_seconds": actual_s,
           "err_log": round(err_log, 4), "err_pct": round((math.exp(err_log) - 1) * 100, 1),
           "lo_hms": band.get("lo_hms"), "hi_hms": band.get("hi_hms"),
           "in_band": (band["lo_seconds"] <= actual_s <= band["hi_seconds"]
                       if band.get("lo_seconds") else None),
           "log_score": None}
    sig = band.get("sigma_log")
    if sig:
        out["log_score"] = round(0.5 * math.log(2 * math.pi * sig * sig)
                                 + err_log ** 2 / (2 * sig * sig), 3)
    return out


REBASE_GAP_WEEKS = 2   # consecutive run-free weeks that count as a real break between training blocks


def _derive_block_start(db, today):
    """Machine-INDEPENDENT re-base anchor for a FRESH db (no stored `rebase_start`): the Monday the
    CURRENT training block resumed, derived purely from the synced run history so every machine — and a
    rebuilt db — agrees. (The old fresh-plan default keyed off the week the APP first ran on that
    machine, which differs Mac↔Manjaro off identical data.) Walk back from this week through run-weeks,
    tolerating isolated empty weeks (a down or taper week) but stopping at a real gap (≥ REBASE_GAP_WEEKS
    consecutive run-free weeks); the anchor is the earliest week of that contiguous block. Continuous
    training all the way back through the trailing re-base window = an ESTABLISHED block (no real re-base
    to do) → this week's Monday, the prior default. Bounded to the window and never after today, so the
    anchor always sits inside the non-elapsed re-base horizon."""
    from datetime import timedelta
    this_mon = _monday(today)
    window_start = this_mon - timedelta(weeks=len(REBASE_SHAPE) - 1)
    active = set()
    try:
        rows = db.execute(
            "SELECT date FROM activities WHERE " + RUN_FAMILY_SQL + " AND date>=? AND date<=?",
            (window_start.isoformat(), today.isoformat())).fetchall()
    except sqlite3.OperationalError:
        rows = []                                   # no activities table (a bare test db) ⇒ no runs
    for r in rows:
        try:
            active.add(_monday(_date(r["date"])))
        except (ValueError, TypeError):
            continue
    block_start, empties, broke, wk = this_mon, 0, False, this_mon
    while wk >= window_start:
        if wk in active:
            block_start, empties = wk, 0
        else:
            empties += 1
            if empties >= REBASE_GAP_WEEKS:
                broke = True
                break
        wk -= timedelta(weeks=1)
    return block_start if broke else this_mon   # no real gap back through the window ⇒ established ⇒ now


def _rebase_start(db, today):
    """The re-base start day — stored once and reused across regenerations so changing an
    objective re-periodizes the road *ahead* without sliding the block's start (a simple
    'freeze the past' approximation, §6b). The block anchors to a **Monday**, so weeks are calendar
    Mon–Sun: run-day layouts map to real weekdays and the long run lands on the actual weekend (offset
    6 = Sunday). Storing it keeps the anchor stable across regenerations; a FRESH db derives the anchor
    from the run history (`_derive_block_start`) so it's the same on every machine, not keyed off the
    week the app first ran here.

    A legacy non-Monday anchor (the old 'starts today' scheme) is migrated to its **containing**
    Monday — back-only, never forward. Back-only is the safe direction: it never pushes block_start
    past `today` (so the runner is never shown a pre-start tile) and never *un*-elapses a week (so a
    frozen week is never re-opened). The cost of this one-time re-grid: the already-elapsed
    week(s) no longer start-date-match the prior saved plan, so they regenerate onto the calendar grid
    (flagged elapsed-but-not-frozen) rather than being carried verbatim — an accepted, deliberate
    trade for aligning the live block now; actual runs still match by their real date in the log, and
    every week from here on freezes normally. If the stored start has fully elapsed, reset to this
    week's Monday."""
    from datetime import timedelta
    stored = get_meta(db, "rebase_start")
    if stored:
        s = _date(stored)
        if s + timedelta(weeks=len(REBASE_SHAPE)) > today:
            mon = _monday(s)                   # containing Monday — BACK-ONLY (never shifts forward)
            if mon != s:                       # one-time re-grid of an in-flight block onto the calendar
                set_meta(db, "rebase_start", mon.isoformat())
                db.commit()
            return mon
    start = _derive_block_start(db, today)      # fresh db: data-derived block start (machine-independent)
    set_meta(db, "rebase_start", start.isoformat())
    db.commit()
    return start


# §6q — Combined multi-A periodization. When several A-races are upcoming, periodize the whole CHAIN
# toward the FINAL one (the ultimate peak), with intermediate peaks/tapers, instead of only toward the
# nearest. Each earlier A-race's ROLE is set by the gap to the NEXT A vs. how long THIS race's type
# needs to recover before another peak: gap ≥ recovery → CO-EQUAL peak (its own full taper + a re-build
# bridge into the next); gap < recovery → SUBORDINATE (a short sharpen/mini-taper, not a full peak it
# can't recover from). The threshold scales with the EARLIER race's distance — a marathon needs far
# longer than a 10k before a second peak, and the ACWR governor can't see connective-tissue recovery.
# Adjudication stays HUMAN: this reads the priorities the owner set; it does not auto-rank A vs B.
RACE_RECOVERY_WEEKS = {"5k": 3, "10k": 3, "half": 4, "marathon": 6, "custom": 4}
RACE_RECOVERY_DEFAULT = 4


def _fmt_hms(seconds):
    """Seconds → 'H:MM:SS' (drop the hour when 0).

    THE ONLY ONE. A second module-level `_fmt_hms` used to sit in the §FT block above — always
    H:MM:SS, "—" for None/0 — and Python's later-def-wins made it dead code from the day it was
    written: every caller, including the §FT band and curve reads physically above it, resolved
    here. Removed 2026-08-22 (cloud review). Dropping the hour is the RIGHT shape and the one that
    has always shipped: a 5k band reads "19:23", not "0:19:23". Don't "restore" the other one."""
    if seconds is None:
        return None
    s = int(round(seconds))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _recovery_weeks(race_type):
    """Weeks the given race type needs before a second peak can be co-equal (else the earlier race
    is subordinated to a mini-taper). Keyed on the EARLIER race's distance."""
    return RACE_RECOVERY_WEEKS.get((race_type or "").lower(), RACE_RECOVERY_DEFAULT)


def select_chain(objs, today):
    """§6q — order the upcoming A-races into a periodization CHAIN toward the FINAL A (the ultimate
    peak), tagging each earlier A's role by separation. Returns (chain, tune_ups):
      chain    — ordered list of {**objective, "role": ...} with role ∈ {goal, coequal, subordinate};
                 the LAST entry is always 'goal'. With no A flagged, falls back to [nearest race].
      tune_ups — upcoming NON-chain races (B/C) on or before the final anchor's date.
    Pure function of (objectives, today) — adjudication stays human (reads set priorities)."""
    future = sorted((o for o in objs if _date(o["date"]) > today), key=lambda o: _date(o["date"]))
    a_races = [o for o in future if o.get("priority") == "A"]
    if not a_races:                                   # no A → nearest race is the lone peak (legacy)
        if not future:
            return [], []
        peak = future[0]
        return ([{**peak, "role": "goal"}],
                [o for o in future if o["id"] != peak["id"] and _date(o["date"]) <= _date(peak["date"])])
    chain = []
    for i, a in enumerate(a_races):
        if i == len(a_races) - 1:
            role = "goal"
        else:
            gap = weeks_until(a_races[i + 1]["date"], _date(a["date"]))
            role = "coequal" if gap >= _recovery_weeks(a.get("type")) else "subordinate"
        chain.append({**a, "role": role})
    final = a_races[-1]
    tune_ups = [o for o in future
                if o.get("priority") != "A" and _date(o["date"]) <= _date(final["date"])]
    return chain, tune_ups


def _prior_weeks_by_start(prior_plan, key):
    """Map a saved plan's phase weeks by start date, for verbatim freezing on regenerate (§6f E)."""
    blk = (prior_plan or {}).get(key) or {}
    return {w.get("start"): w for w in blk.get("weeks", []) if w.get("start")}


def _prior_weeks_all(prior_plan):
    """§H6 — every prior week mapped by start date across ALL phase blocks (rebase/base/build/bridge/
    peak/taper…), not just one key. Calendar drift slides phase boundaries as a race nears, so a Monday
    lived under 'base' can land in 'build' on the next regenerate; a per-phase lookup would miss it and
    REGENERATE an already-lived week (history corruption, §6f E violation). Freezing by start across the
    whole prior plan carries each elapsed week verbatim regardless of which phase now owns its slot.
    Phases tile the calendar contiguously, so each start belongs to exactly one prior week (no clashes)."""
    out = {}
    for v in (prior_plan or {}).values():
        if isinstance(v, dict) and isinstance(v.get("weeks"), list):
            for w in v["weeks"]:
                if w.get("start"):
                    out[w["start"]] = w
    return out


def _split_freeze(shape, phase_start, gen_seed, easy_pace_sec, adjust, zones, prior_by_start, today,
                  week_actuals=None, regime="caution", ride_cap=ACWR_SOFT,
                  consec_hard=0, last_nondown=None, soft_ctl_floor=None, recent_longs=None,
                  recent_eq=None, db=None, pace_zones=None, blocked=None, recent_session_eq=None,
                  today_trimp=None):
    """§6f Step E (continuity) — generate one phase block with the past FROZEN. A week whose 7-day
    window has fully elapsed (end < today) is carried **verbatim** from `prior_by_start` (matched on
    start date), so a mid-block regeneration never rewrites weeks already lived. Today-onward weeks
    are generated FRESH from `gen_seed` — the §PRO20 END-OF-YESTERDAY CTL/ATL, which embodies what the
    frozen past actually did, so the future seeds from measured state rather than from re-simulating
    history. (It used to be "today's snapshot", which had already advanced the EWMA through today and
    therefore applied today TWICE once the roll added it again — §PRO20, log §55.) `today_trimp` is the
    TRIMP actually recorded today, floored into the projection by §PRO20b.
    An elapsed week with no prior record (e.g. a rebuilt DB) is
    regenerated best-effort and flagged elapsed-but-not-frozen. Each week is tagged {elapsed, frozen}
    for the surfaces (Step F). Returns (weeks_in_order, end_ctl, end_atl, generated_any)."""
    from datetime import timedelta
    future_sub, frozen, missing = [], [], []
    for wk in shape:
        wstart = phase_start + timedelta(weeks=wk["wk"] - 1)
        if wstart + timedelta(days=6) < today:               # fully elapsed → freeze
            prior_w = prior_by_start.get(wstart.isoformat())
            if prior_w:
                frozen.append({**prior_w, "elapsed": True, "frozen": True})
            else:
                missing.append(wk)
        else:
            future_sub.append(wk)
    end_ctl, end_atl, generated_any = gen_seed[0], gen_seed[1], False
    # §PRO6 — fold this phase's already-lived (frozen) weeks into the carried near-ceiling streak +
    # trough anchor, in calendar order, so the limiter & trough see a continuous history across the
    # frozen→future seam, not just a per-call reset.
    for w in sorted(frozen, key=lambda w: w["start"]):
        if _is_down(w) or _is_taper(w):
            consec_hard = 0
        else:
            a = w.get("proj_acwr")
            consec_hard = consec_hard + 1 if (a and a >= NEAR_CEILING_ACWR) else 0
            if w.get("trimp_total"):
                last_nondown = w["trimp_total"]
    # §PRO9/§3.1 — carry the trailing long-run + eq_km windows across the frozen→generated seam. They thread
    # out of the last generate_block call; if this phase generates nothing (all frozen), they carry the seed.
    carried_longs = list(recent_longs or [])
    carried_eq = list(recent_eq or [])
    carried_seq = list(recent_session_eq or [])              # §PRO17
    backfilled = []
    if missing:                                              # no history — regenerate best-effort
        mweeks, mbound = generate_block(missing, phase_start, end_ctl, end_atl,
                                        easy_pace_sec, adjust, zones, regime=regime, ride_cap=ride_cap,
                                        consec_hard=consec_hard, last_nondown=last_nondown,
                                        soft_ctl_floor=soft_ctl_floor, recent_longs=recent_longs,
                                        recent_eq=recent_eq, blocked=blocked,
                                        recent_session_eq=recent_session_eq)
        backfilled = [{**w, "elapsed": True, "frozen": False} for w in mweeks]
        end_ctl, end_atl, generated_any = mbound["end_ctl"], mbound["end_atl"], True
        consec_hard, last_nondown = mbound["consec_hard"], mbound["last_nondown"]
    fresh = []
    if future_sub:                                           # today-onward, seeded from live state
        # §PRO9/§3.1 — seed the future weeks' caps off the ACTUAL elapsed history: recent actuals +
        # what was really RUN inside this phase's already-lived week windows, in date order, tail-
        # trimmed to each window. The frozen weeks' planned sessions are NOT evidence — the athlete
        # may have out- or under-run them, and the +10% step's contract is "his real recent long
        # runs". Anchoring on prescription let the window slide onto fiction (2026-07-16 live case:
        # cap 4.3 = 1.1 × a prescribed 3.9 while his actual trailing long was 8.4 → a 7-run no-rest
        # week). No db (det fixtures) ⇒ the planned sessions stand in, as before; weeks he skipped
        # entirely contribute nothing (a gap can't set the baseline — same as `_recent_long_runs`).
        elapsed_now = sorted(frozen + backfilled, key=lambda w: w["start"])
        if db is not None:
            caps = [_actual_week_caps(db, w["start"],
                                      (_date(w["start"]) + timedelta(days=6)).isoformat(), pace_zones)
                    for w in elapsed_now]
            elapsed_longs = [c[0] for c in caps if c[0]]
            elapsed_eqs = [c[1] for c in caps if c[1]]
        else:
            elapsed_longs = [_week_long_km(w.get("sessions") or []) for w in elapsed_now]
            elapsed_eqs = [_week_eq_km(w.get("sessions") or []) for w in elapsed_now]
        fresh_seed = (list(recent_longs or []) + elapsed_longs)[-LONG_RUN_STEP_WINDOW:]
        fresh_seed_eq = (list(recent_eq or []) + elapsed_eqs)[-BIO_EQ_WINDOW:]
        # the straddling week's truth: the longest run / eq_km already LOGGED in its window so far,
        # so its display-only elapsed planned days never anchor the caps for the weeks after it.
        wal = wae = None
        if db is not None:
            for wk in future_sub:
                ws_d = phase_start + timedelta(weeks=wk["wk"] - 1)
                if ws_d < today <= ws_d + timedelta(days=6):
                    wal, wae = _actual_week_caps(db, ws_d.isoformat(), today.isoformat(), pace_zones)
                    break
        fweeks, fbound = generate_block(future_sub, phase_start, end_ctl, end_atl,
                                        easy_pace_sec, adjust, zones, today=today,   # §6o partial week
                                        week_actuals=week_actuals, regime=regime,    # §6e-FREQ + §PRO3 regime
                                        ride_cap=ride_cap,                           # §PRO5 shape-response
                                        consec_hard=consec_hard, last_nondown=last_nondown,  # §PRO6 carry
                                        soft_ctl_floor=soft_ctl_floor,               # §PRO8 low-CTL soft floor
                                        recent_longs=fresh_seed, recent_eq=fresh_seed_eq,  # §PRO9/§3.1 caps
                                        recent_session_eq=recent_session_eq,               # §PRO17
                                        week_actual_long=wal, week_actual_eq=wae,
                                        today_trimp=today_trimp,                     # §PRO20b today's actual
                                        blocked=blocked)                             # §AV — away days
        fresh = [{**w, "elapsed": False, "frozen": False} for w in fweeks]
        end_ctl, end_atl, generated_any = fbound["end_ctl"], fbound["end_atl"], True
        consec_hard, last_nondown = fbound["consec_hard"], fbound["last_nondown"]
        carried_longs, carried_eq = fbound["recent_longs"], fbound["recent_eq"]
        carried_seq = fbound.get("recent_session_eq")            # §PRO17
    elif missing:
        carried_longs, carried_eq = mbound["recent_longs"], mbound["recent_eq"]
        carried_seq = mbound.get("recent_session_eq")            # §PRO17
    weeks = sorted(frozen + backfilled + fresh, key=lambda w: w["start"])
    return (weeks, round(end_ctl, 1), round(end_atl, 1), generated_any, consec_hard, last_nondown,
            carried_longs, carried_eq, carried_seq)


def _trim_post_race(plan, chain, block_start):
    """§PER1 — display-side cleanup after the race-week-inclusive periodization: the final taper week
    of each segment now SPANS the race day (so the taper bottom lands ON race week), but we don't
    prescribe training in the dead days between the race and that week's Sunday. Drop any session dated
    strictly after a race up to and including that race's Monday-week Sunday. Pure read-model edit — the
    CTL projection already ran during generation off the full (untrimmed) week, so removing these tail
    sessions changes only what's shown, never the chained fitness seed."""
    from datetime import timedelta
    blocks = [v for v in plan.values() if isinstance(v, dict) and "weeks" in v]
    for c in chain:
        R = _date(c["date"])
        wk_end = block_start + timedelta(days=((R - block_start).days // 7) * 7 + 6)
        for blk in blocks:
            for w in blk.get("weeks", []):
                kept = [s for s in w.get("sessions", [])
                        if not (R < _date(s["date"]) <= wk_end)]
                if len(kept) == len(w.get("sessions", [])):
                    continue                     # untouched weeks keep their generator-built header
                w["sessions"] = kept
                # §CARD — this is a read-model edit, and the header IS part of the read model: the
                # race week's card kept quoting the untrimmed week ("5 runs" over the single
                # remaining shakeout, on the owner's live plan). Recompute every summary this
                # function's own trim invalidated. The CTL projection deliberately stays untrimmed
                # (see docstring); proj_* fields describe the projection, not the listing, so they
                # are left alone.
                w["runs"] = sum(1 for s in kept if (s.get("kind") or "") != "rest")
                w["km"] = round(sum(s.get("km") or 0.0 for s in kept), 1)
                w["trimp_total"] = round(sum(s.get("trimp") or 0.0 for s in kept), 1)
                # §CARD3 — the trimmed listing IS the week's prescription: the dropped tail was
                # never asked of the athlete, so the adherence bar shrinks with it.
                w["intent_runs"] = w["runs"]


def _card_truth_elapsed(plan, db, today):
    """§CARD3 — a week whose window has fully closed states what actually HAPPENED: header runs/km
    are recomputed from the activity log (the same Mon–Sun owned-data read the engine's own evidence
    tests use) and the done/ahead split settles to done-only. The SESSIONS stay the frozen as-lived
    prescription (§6f Step E — a plan is the road ahead, past prescriptions come from plan history);
    this is a read-model edit in _trim_post_race's class, never a history rewrite.

    Motive (the owner's fossil, found 2026-08-15): §CARD/§CARD2 fixed every PUBLISHER, but a week
    frozen from a pre-fix artifact is carried verbatim past all of them — week 07-27 kept saying
    "48.6 km · 5 runs" over a 6-session listing he actually ran as 42.4 km, and it would have said
    so for the life of the block. Recomputing at the read-model seam self-heals every carried week
    on the next regen (and keeps healing if a late sync adds a run to a past week).

    Evidence is preserved FIRST: intent_runs/intent_km keep the as-laid bar — the honest record of
    what was PRESCRIBED, distinct from the actuals the header now states (display/history
    provenance; since §FORM1 no decision reads it). For pre-§CARD2 weeks the old header IS
    the only surviving prescription count, so it is preserved into intent_runs before being replaced."""
    from datetime import timedelta
    if db is None:
        return
    for blk in plan.values():
        if not (isinstance(blk, dict) and blk.get("weeks")):
            continue
        for w in blk["weeks"]:
            ws = w.get("start")
            if not ws:
                continue
            try:
                wd = _date(ws)
            except (TypeError, ValueError):
                continue
            if wd + timedelta(days=6) >= today:
                continue                 # straddle + future weeks: §CARD2 / the lay govern those
            try:
                runs, km = _current_week_actuals(db, wd)
            except sqlite3.OperationalError:
                return                   # §PRO22 — a read of history must never take the plan down
            if w.get("intent_runs") is None:
                w["intent_runs"] = w.get("runs")
            if w.get("intent_km") is None:
                w["intent_km"] = w.get("km")
            w.update(runs=runs, km=km, runs_done=runs, km_done=km,
                     runs_ahead=0, km_ahead=0)


def generate_plan(db, force_regime=None, today=None):
    """Engine entry point (§6b): a pure function of (today, current shape, objectives), with the
    PAST frozen (§6f Step E). Re-periodizes forward to the nearest A-race; falls back to a
    maintenance block when no objective remains. Every call is re-runnable and versioned, so
    adding/removing an objective reshapes the road ahead and the change is diff-able against the
    prior version — while weeks already lived are carried verbatim from the last saved plan.

    `today` overrides the clock. Production never passes it; the integration dets do, so a fixture
    built around a fixed date is judged on that date instead of half-pinned against the real clock —
    the gap that let §PRO12 (and, in §33d, a §PRO10 contract breach) reach a saved plan unseen."""
    today = today or datetime.now().date()
    # §PRO20 — seed = end-of-yesterday (see plan_seed). Resolved BEFORE the seed read because the
    # seed is now a function of `today`; nothing else here depended on the old ordering.
    seed = plan_seed(db, today)
    cold = None
    if not seed:
        # §FT5 — the cold-start path: no snapshot yet, but one qualifying race-distance effort
        # seeds the state (VDOT inversion + reconstructed CTL₀). Regime starts caution by
        # construction (nothing banked), the §FT3 band is wide by design, and every truth-anchor
        # replaces these seeds with measurement as runs land.
        cold = _ft_cold_start(db, today)
        if not cold:
            return {"ok": False, "error": ("no shape snapshot — Sync first (or, for a cold start: "
                                           "sync one hard race-distance effort — 5k/10k/half — from "
                                           "the last 12 months and set an objective; age in Settings "
                                           "sharpens the HR prior)")}
        vo2, ctl0, atl0 = cold["vo2_seed"], cold["ctl0"], cold["atl0"]
        seed_meta = None
    else:
        vo2, ctl0, atl0, seed_meta = seed
    zones = pace_zones(vo2)

    block_start = _rebase_start(db, today)
    adj = active_adjustment(db, today.isoformat())   # §6c — clamped directive or None
    adj_dir = adj["directive"] if adj else None
    av_blocked = _av_blocked_dates(db, today)        # §AV — away days the layout must respect
    # §6q — select the A-race chain up front: the runway to the first race is needed to clamp the
    # re-base length just below (§PER1 F1). Pure function of (objectives, today); no side effects.
    objs = [dict(r) for r in db.execute(
        "SELECT * FROM objectives WHERE status='upcoming' ORDER BY date").fetchall()]
    chain, tune_ups = select_chain(objs, today)   # §6q — full A-race chain toward the FINAL peak
    anchor = chain[-1] if chain else None

    prior = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    prior_plan = json.loads(prior["plan"]) if prior else None   # §6f E — source of frozen weeks
    # §PRO3/§FORM1 — body-evidence regime. Assertive rides the safe headroom AND skips the re-base
    # (the medical-confirmation block exists for the post-illness return only); caution keeps the full
    # re-base + min(intent,ceiling) and is entered on medical/symptom evidence alone. Decided BEFORE
    # the re-base length so assertive can drop it.
    regime, regime_reason = training_regime(db, today, prior_plan)
    if force_regime in ("caution", "assertive"):   # §PRO10 — counterfactual regime for the drift overlay
        regime, regime_reason = force_regime, f"counterfactual ({force_regime})"   # pure: never persisted
    # §PRO5 — self-calibrating ride cap from his measured-vs-projected CTL response (assertive only).
    resp = shape_response(db, today, prior_plan)
    ride_cap = round(1.0 + (ACWR_SOFT - 1.0) * resp["factor"], 3) if regime == "assertive" else ACWR_SOFT

    natural_len = len(REBASE_SHAPE)   # §FORM1 — fixed template; no adherence-earned graduation
    # §PER1 F1 — clamp the re-base to the runway so the phases can't overrun the first race (a taper
    # scheduled AFTER race day). When the first race is closer than re-base + taper, shrink the re-base
    # (the conservative phases collapse first) so re-base + taper ≤ runway and the taper bottom lands ON
    # race week. Provably a NO-OP on an ample runway: total − taper ≫ natural_len ⇒ rebase_eff ==
    # natural_len. The clamped length threads through to periodize_chain (rebase_weeks=rebase_weeks_n),
    # so the phase list and the actually-generated re-base block agree.
    rebase_eff = natural_len
    if chain:
        # §PER1 — clamp in the SAME block_start-anchored, race-week-inclusive units periodize_chain
        # now uses (`_plan_span`), so the clamped re-base and the periodized phase list agree and the
        # taper bottom lands ON race week (not the old today-floored count that ended ~1–2 wk short).
        total0 = _plan_span(block_start, chain[0]["date"])
        taper0 = _seg_taper(total0, _full_peak(chain[0]["role"]))
        rebase_eff = min(natural_len, max(0, total0 - taper0))
    if regime == "assertive":
        rebase_eff = 0          # §PRO3 — skip the medical-confirmation re-base once cleared
    shape = REBASE_SHAPE[:rebase_eff]
    rebase_weeks_n = len(shape)

    # §6f Step E — the live seed for the FIRST today-onward week is today's snapshot CTL/ATL (the
    # snapshot already embodies the frozen past); `started` flips once any future week is generated,
    # after which later phases chain off the previous phase's projected end.
    # §FORM1 — the RESTART DOSE: with no runs at all in the trailing windows (a cold start, or a
    # long healthy gap), every bio ladder is empty — no bio cap is in force, §PRO17's peak stand-down
    # doesn't apply, and the hard per-day ACWR on a decayed raw CTL pins every week at ~zero (the
    # old design escaped this only because everyone started in the gated caution re-base, whose
    # FIXED templates need no trailing history). Form-driven needs a floor, not a gate: an empty
    # window seeds at the conservative re-base's FIRST RUNG — the dose the post-illness block
    # prescribes anyone on day one, so it is safe by construction — and the governed ladder ramps
    # from there by measurement. A window with ANY real run keeps its honest measured seed.
    _rb0 = REBASE_SHAPE[0]
    live = {"ctl": ctl0, "atl": atl0, "started": False,
            "consec_hard": 0, "last_nondown": None,   # §PRO6 — tissue streak + trough anchor across phases
            # §PRO9/§3.1 — trailing long-run + biomechanical eq_km windows, seeded from his real recent weeks
            # (assertive skips the re-base, so the plan's own weeks won't seed the first building weeks) and
            # carried across phases.
            "recent_longs": _recent_long_runs(db, block_start) or [float(_rb0["long"])],
            # at re-base (easy) pace the eq factor is 1.0 ⇒ rung eq == rung km; largest bout = its long
            "recent_session_eq": _recent_session_eq(db, block_start, zones)
                                 or [float(_rb0["long"])],                     # §PRO17
            "recent_eq": _recent_eq_km(db, block_start, zones) or [float(_rb0["km"])]}

    prior_all = _prior_weeks_all(prior_plan)   # §H6 — freeze elapsed weeks by start across ALL phases,
    # not just the same key, so a week that crossed a phase boundary (calendar drift) is still carried
    # verbatim instead of being regenerated from today's CTL.
    week_actuals = _current_week_actuals(db, today)   # §6e-FREQ — runs+km logged this calendar week
    # §PRO20b — the TRIMP actually recorded today. With the §PRO20 seed stopping at end-of-yesterday,
    # today's load would otherwise reach the projection only as a PRESCRIPTION, which is moot once he
    # has already run (2026-07-30: prescribed rest, ran 93 TRIMP). Floored into the projection only —
    # never into what is laid — so it can only tighten the governor. None on a day with no runs yet
    # ⇒ byte-identical.
    today_trimp = daily_trimp_series(db).get(today.isoformat()) or None
    # §PRO8 — the live ASSERTIVE plan floors the SOFT-cap CTL denominator at low chronic load so the
    # ceiling can build instead of pinning at ~maintenance; caution passes None (byte-identical). The
    # re-base is always caution, so it never receives it.
    soft_floor = ACWR_SOFT_CTL_FLOOR if regime == "assertive" else None
    def _gen_phase(key, phase_start, shape_, zones_, regime_="caution", ride_cap_=ACWR_SOFT,
                   soft_floor_=None):
        seed = (live["ctl"], live["atl"])
        weeks_, ec, ea, gen, ch, ln, rl, req, rsq = _split_freeze(shape_, phase_start, seed, zones["easy_top"],
                                                             adj_dir, zones_, prior_all, today, week_actuals,
                                                             regime_, ride_cap_,
                                                             live["consec_hard"], live["last_nondown"],
                                                             soft_ctl_floor=soft_floor_,
                                                             recent_longs=live["recent_longs"],   # §PRO9
                                                             recent_eq=live["recent_eq"],         # §3.1
                                                             recent_session_eq=live["recent_session_eq"],
                                                             db=db, pace_zones=zones,  # §PRO9/§3.1 — elapsed
                                                             # weeks anchor the caps on ACTUALS, not plan
                                                             today_trimp=today_trimp,  # §PRO20b
                                                             blocked=av_blocked)       # §AV — away days
        if gen:
            live["ctl"], live["atl"], live["started"] = ec, ea, True
        live["consec_hard"], live["last_nondown"] = ch, ln   # §PRO6 carry across phases
        live["recent_longs"], live["recent_eq"] = rl, req    # §PRO9/§3.1 carry across phases
        if rsq:
            live["recent_session_eq"] = rsq                  # §PRO17 — same, at session grain
        blk = {"start": phase_start.isoformat(), "weeks": weeks_, "end_ctl": ec, "end_atl": ea,
               "clipped_by_acwr": any(w.get("clipped") for w in weeks_)}
        comps = _phase_builds(weeks_)                 # §T2 — what this phase builds (from the tags)
        if comps:
            blk["builds"] = comps
        return blk, ec

    # re-base is the conservative pure-easy block — always caution, never ridden (skipped in assertive)
    rb, _rb_end = _gen_phase("rebase", block_start, shape, None)

    plan = {
        "ok": True,
        "generated_at": _now_iso(),
        # §PRO14 — which engine built this artifact. Compared at serve time against the running
        # ENGINE_VERSION so the view can say "regenerate to re-read" instead of silently showing
        # numbers from code that is no longer deployed.
        "engine_version": ENGINE_VERSION,
        # §FT5 — present only on a cold-started plan: WHAT seeded the state (the runner can see the
        # engine's assumptions; each seed is replaced by measurement as data lands). None otherwise.
        **({"cold_start": cold} if cold else {}),
        # §PRO20 — say which day the load state was seeded from, and how many missing snapshot days
        # had to be bridged by measurement. A saved plan then carries its own provenance instead of
        # leaving "what was this built from?" answerable only by forensics on `plans.inputs`.
        "shape": {"effective_vo2max": vo2, "ctl": ctl0, "atl": atl0,
                  **({"seed": seed_meta} if seed_meta else {})},
        "pace_zones": {k: f"{fmt_pace(v)}/km" for k, v in zones.items()},
        "rebase": {**rb, "full_len": len(REBASE_SHAPE)},
        # §PRO3 — which regime drove this plan + why (auto-flips, so it's surfaced, never silent)
        "regime": {"mode": regime, "reason": regime_reason},
        # §PRO5 — self-calibrating shape-response: how his measured fitness tracks the projection + the
        # resulting assertive ride cap (full 1.25 when on track, eased when he's falling behind)
        "shape_response": {**resp, "ride_cap": ride_cap},
        # §PRO10 — the progressive-overload floor on the assertive ceiling. Surfaced, never silent:
        # the drawn trajectory now COMPOUNDS instead of equilibrating, and that assumes continued
        # clean absorption — the acute brakes (hard peak ACWR, ramp cap, forced deloads) still bound
        # every week, and the live regen re-anchors on reality each Monday.
        "prog": ({"ramp": PROG_RAMP,
                  "note": "building weeks progress ≥{:.0%}/wk over the last realised non-down load "
                          "(soft-cap floor; hard caps still bind) — assumes continued clean "
                          "absorption, re-anchored on your actuals at every regen".format(PROG_RAMP)}
                 if regime == "assertive" else None),
        "tune_ups": [{"label": o["label"], "date": o["date"], "type": o["type"],
                      "priority": o["priority"]} for o in tune_ups],
        "note": ("Easy pace ~%s/km — if your easy runs are habitually faster than this they're "
                 "really threshold effort; the re-base deliberately runs slower to build the aerobic "
                 "base. (See the Effort-discipline panel for how your actual easy runs measure up.)"
                 % fmt_pace(zones["easy_top"])),
        "adjustment": ({"note": adj["note"], **adj["directive"], "clamp": adj.get("clamp"),
                        "medical": adj["directive"].get("medical_flag", False)}
                       if adj else None),
    }

    if anchor:
        from datetime import timedelta
        # §6q — periodize the whole A-race CHAIN toward the FINAL peak (single race ≡ the old single-A
        # path: same base/build/peak/taper keys + week counts). Each later race adds a bridge/peak/taper
        # segment; generate_plan WALKS the resulting phase list, chaining the live CTL seed segment-to-
        # segment with the past frozen (§6f E), so the multi-peak road is one continuous, diff-able plan.
        phases, total_weeks = periodize_chain(today, chain, rebase_weeks=rebase_weeks_n,
                                              block_start=block_start)
        plan["mode"] = "race"
        plan["objective"] = {"label": anchor["label"], "date": anchor["date"],
                             "type": anchor.get("type"),   # §6s — needed to match the race-day activity
                             "target": anchor.get("target"), "priority": anchor.get("priority"),
                             "weeks_away": total_weeks}
        plan["phases"] = phases
        plan["chain"] = [{"label": c["label"], "date": c["date"], "type": c.get("type"),
                          "role": c["role"]} for c in chain]   # §6q multi-A surface (1 entry = single-A)

        # §6f Step B/C/D/E — generate each phase on the calendar, past frozen, today-onward chained off
        # the live seed. `zones` activates the polarized quality model (§6f C/D); each block holds the
        # ACWR ceiling regardless of intensity. A bridge (post-race re-build) reuses the Build shaper.
        SHAPERS = {"base": base_shape, "build": build_shape, "bridge": build_shape,
                   "peak": peak_shape, "taper": taper_shape}
        cur_start = block_start + timedelta(weeks=rebase_weeks_n)
        cur_km = (rb["weeks"][-1]["intent_km"] if rb["weeks"] else REBASE_SHAPE[-1]["km"])
        proj_end_ctl = rb["end_ctl"]
        # §PRO7b — race fitness = the PEAK CTL carried INTO the taper (end of the last building/peak
        # phase), realized on race day through the taper's FRESHNESS — not the depressed taper-bottom CTL.
        # The taper trades a little chronic load for a lot of freshness; reading the trough treated the
        # taper as pure detraining and hid the build's payoff. This is what feasibility/finish-time use.
        peak_ctl = rb["end_ctl"]
        race_proj = {}   # §6q — projected end-CTL at each race (end of its taper), for the surfaces
        for ph in phases:
            kind, key, n_wk = ph["kind"], ph["key"], ph["weeks"]
            if kind == "rebase" or n_wk <= 0:
                continue   # the re-base block is already generated above as `rb`
            # §T2 — the Davis component periodization rides the ASSERTIVE regime only (earned, like
            # every other assertive lever); caution keeps the legacy shapes byte-identical. Taper is
            # regime-agnostic (freshening is freshening).
            # §TT — the taper's sharpening touch runs at the RACE'S pace: marathon → the marathon
            # zone (rehearsed all build; lighter per-km damage — a taper is no place for the block's
            # first threshold reps); everything else keeps the threshold touch it always had. Applies
            # per segment in a §PER1 chain, so a marathon→10k double sharpens each taper differently.
            # Regime-agnostic like the taper itself ⇒ deliberately NOT caution-byte-identical: the
            # zone contradicted the session's own label, and both regimes deserve the true race pace.
            sh = (SHAPERS[kind](n_wk, cur_km,
                                race_zone=("marathon" if (ph.get("type") or "").lower() == "marathon"
                                           else "threshold")) if kind == "taper"
                  else SHAPERS[kind](n_wk, cur_km, davis=(regime == "assertive")))
            # (§6h CTL-responsive floor removed 2026-06-30 — it was a dormant follower: the re-base decay
            # kept it below its activation band in real plans, and the §PRO assertive ride is the proper
            # fitness-tracker now. Caution is a clean conservative ramp; assertive rides the ceiling.)
            # (§FORM1 2026-08-18 — the §6e earned volume lift and 6th-run advance are gone with the
            # banked-streak machinery: volume responsiveness is §PRO5's ride, frequency spreads are
            # §PRO9's. Nothing is unlocked by adherence bookkeeping.)
            block, end_ctl = _gen_phase(key, cur_start, sh, zones, regime, ride_cap, soft_floor)
            plan[key] = block
            cur_start = cur_start + timedelta(weeks=n_wk)
            # §PRO4 — chain the next phase off the volume this phase actually REACHED. In caution that's
            # the shape's intent_km (the fixed ramp it followed); in ASSERTIVE the weeks rode the ceiling
            # FAR above intent_km, so chain off the realised peak instead — otherwise the taper (which
            # scales TAPER_TOP..BOTTOM × `cur_km`) would shrink from the tiny fixed-ramp number and crash
            # CTL into the race. Realised chaining makes the taper a true cut-back from the real peak.
            if block["weeks"]:
                if regime == "assertive":
                    nd = [w["km"] for w in block["weeks"] if not _is_down(w)]
                    cur_km = max(nd) if nd else cur_km
                else:
                    cur_km = block["weeks"][-1]["intent_km"]
            proj_end_ctl = end_ctl
            if kind == "taper":
                race_proj[key] = peak_ctl  # §PRO7b — the fitness carried INTO this taper (not its trough)
            else:
                peak_ctl = end_ctl         # building/peak phases raise the carried race fitness

        # §6f Step E / §PRO7b — feasibility re-reads the engine's REAL projected race fitness — the PEAK
        # CTL carried into the final taper (chained through every segment under the ceiling), realized on
        # race day through the taper's freshness — not the generic growth estimate, and not the taper trough.
        # §FT1 — plus the other two state axes Model A reads: the projected race-day LADDER (the longest
        # long the laid plan itself puts within the trailing window of race day — the plan and the
        # prediction are one object) and the per-runner shrinkage correction from the race corpus.
        # §FT2 — and Model B's speed side: v₀ = the MEASURED corpus-scale eVO₂ (truth-anchored every
        # regen — never last regen's projection), projected through the laid weeks' TRIMPs to race
        # day; the +4/+8-week curve points keep training at the build's peak weekly load. Falls back
        # to the frozen effective value only when the corpus is empty (fresh/synthetic db).
        race_long = _ft_plan_race_long(plan, anchor.get("date"))
        _, _, long_now = _ft_state_at(db, today.isoformat())   # §FT6 — the ladder already behind him
        ft_corr, ft_sigma, ft_n, ft_tilt = _ft_correction(db)
        # §33e — carry the correction onto THIS race's distance. Neutral (byte-identical) when the
        # corpus is the same distance as the objective, which is the established path.
        ft_corr = _ft_transfer_correction(ft_corr, ft_tilt, (anchor.get("type") or "").lower())
        v0, v_ceil, v_resp, v_asof = _ft_speed_state(db)
        # §FT9 — how old the anchor is, in the same window the ladder axis already uses
        v0_age = (today - _date(v_asof)).days if v_asof else None
        vo2_star, vo2_curve = None, None
        if v0:
            wk_trimps = _ft_plan_weekly_trimps(plan, today, anchor.get("date"))
            ext = max(wk_trimps, default=0.0)     # "+n weeks" = keep training at the peak laid load
            vo2_star = _ft_project_evo2(v0, wk_trimps, v_ceil, v_resp)
            vo2_curve = {n: _ft_project_evo2(v0, wk_trimps + [ext] * n, v_ceil, v_resp)
                         for n in (0, 4, 8)}
        plan["feasibility"] = feasibility(anchor, ctl0, vo2, total_weeks, projected_ctl=peak_ctl,
                                          race_long_km=race_long, correction=ft_corr,
                                          projected_vo2max=vo2_star, vo2_curve=vo2_curve,
                                          band_inputs={"sigma_race": ft_sigma, "n_races": ft_n,
                                                       "v0": v0, "long_km_now": long_now,
                                                       "v0_age_days": v0_age, "v0_as_of": v_asof,
                                                       "disp_a": _ft_dispersion(db)})
        # §6q/§PRO7b — annotate each chain race with its own projected race fitness (the PEAK CTL carried
        # into that race's taper). Map by the segment's taper KEY (chain index i → "taper"/"taper{i}"),
        # not the human label, since two races can share a label.
        for i, c in enumerate(plan["chain"]):
            tk = "taper" if i == 0 else f"taper{i}"
            if tk in race_proj:
                c["proj_ctl"] = round(race_proj[tk], 1)
                # #2 — a per-race feasibility verdict on each chain segment, so a multi-A build surfaces
                # WHERE each race lands (not just the final peak). Same feasibility() as the final anchor,
                # re-read on that race's own runway + its projected race fitness (peak into the taper).
                c["feasibility"] = feasibility(c, ctl0, vo2, weeks_until(c["date"], today),
                                               projected_ctl=race_proj[tk]).get("verdict")
        # §PER1 — drop any prescribed session dated strictly AFTER a race within that race's own
        # Monday-week (the race-week-inclusive span means the final taper week now spans race day; we
        # don't prescribe training in the days between the race and that Sunday). Display-only: the CTL
        # projection already ran during generation, so trimming these tail sessions doesn't re-seed it.
        _trim_post_race(plan, chain, block_start)
    else:  # §6b maintenance fallback — no objective: hold fitness, ACWR centred, no taper
        plan["mode"] = "maintenance"
        plan["objective"] = None
        plan["phases"] = [{"phase": "Re-base (Phase 0)", "weeks": rebase_weeks_n},
                          {"phase": "Maintenance — hold", "weeks": 0}]
        plan["feasibility"] = {
            "verdict": "maintain", "projected_ctl": None,
            "note": ("No objective set — the plan holds fitness with an easy aerobic base "
                     "(ACWR centred, no taper). Add a race and the engine re-periodizes "
                     "the road ahead toward it."),
        }
    # §CARD3 — last read-model pass, both branches: every fully-lived week's header states what
    # actually happened (sessions stay the as-lived prescription; evidence kept in intent_*).
    _card_truth_elapsed(plan, db, today)
    return plan


def _adj_directive(adj):
    """The clamped directive out of a stored `adjustment` block (which may be {note,directive,clamp}
    or the bare directive), or None."""
    if not adj:
        return None
    return adj.get("directive") if isinstance(adj, dict) and "directive" in adj else adj


def _adj_fingerprint(d):
    """What materially defines an adjustment for change-detection: its load multiplier, medical flag,
    window and easy-only force. (Summary/situation prose is cosmetic — not part of the fingerprint.)"""
    if not d:
        return None
    try:
        m = round(float(d.get("volume_multiplier", 1.0)), 2)
    except (TypeError, ValueError):
        m = 1.0
    return (m, bool(d.get("medical_flag")), d.get("scope_days"), bool(d.get("easy_only")))


def _adj_summary(d):
    """A short human label for an adjustment directive ('none', '×0.6 14d', '×0 medical 28d')."""
    if not d:
        return "none"
    m = d.get("volume_multiplier", 1.0)
    bits = [f"×{m:g}"]
    if d.get("medical_flag"):
        bits.append("medical")
    if d.get("scope_days"):
        bits.append(f"{d['scope_days']}d")
    return " ".join(bits)


def diff_plans(old, new):
    """Summarize how a regeneration changed the road ahead (§6b — so the owner sees it)."""
    if not old:
        return {"first": True, "summary": "First plan generated."}
    changes = []
    oo, no = old.get("objective") or {}, new.get("objective") or {}
    if (oo.get("label"), oo.get("date")) != (no.get("label"), no.get("date")):
        a = f"{oo.get('label')} ({oo.get('date')})" if oo else "maintenance"
        b = f"{no.get('label')} ({no.get('date')})" if no else "maintenance"
        changes.append(f"Anchor: {a} → {b}")
    # §PRO3 — a training-regime flip (caution↔assertive) is a material change in posture; surface it so
    # the auto-gate is never silent. (Pre-§PRO3 plans have no `regime` → no phantom flip on first re-plan.)
    om, nm = (old.get("regime") or {}).get("mode"), (new.get("regime") or {}).get("mode")
    if om and nm and om != nm:
        changes.append(f"Regime: {om} → {nm} ({(new.get('regime') or {}).get('reason', '')})")
    # §6q — key phases by their stable `key` (unique per chain segment), not the display name, so a
    # re-labelled race or two same-label races don't read as phantom structural changes. (Pre-§6q
    # saved plans have no key → fall back to the name; one transitional diff, then stable.)
    op = {(p.get("key") or p["phase"]): p for p in old.get("phases", [])}
    npz = {(p.get("key") or p["phase"]): p for p in new.get("phases", [])}
    for k in sorted(set(op) | set(npz)):
        ow, nw = (op.get(k) or {}).get("weeks", 0), (npz.get(k) or {}).get("weeks", 0)
        if ow != nw:
            name = (npz.get(k) or op.get(k))["phase"]
            changes.append(f"{name}: {ow}w → {nw}w")
    if oo.get("weeks_away") != no.get("weeks_away"):
        wa = lambda v: f"{v}w" if v is not None else "no race"
        changes.append(f"Runway: {wa(oo.get('weeks_away'))} → {wa(no.get('weeks_away'))}")
    # §H5 — the diff above is purely STRUCTURAL (objective, phase week-counts, runway). A re-plan can
    # change the LOAD PROFILE — per-week volume, an applied/cleared adjustment — while leaving that
    # structure identical (the §6e earned lift, the §PRO assertive ride, the §6e frequency advance, and a §6c
    # ease/medical hold all do exactly this). Without a load fingerprint those re-plans falsely report
    # "No change". Compare peak weekly intent_km per phase (over NON-frozen weeks, so a frozen carry
    # isn't read as a phantom change) and the active adjustment, and surface what actually moved.
    def _peak(plan, key, field):
        wks = [w for w in (plan.get(key) or {}).get("weeks", []) if not w.get("frozen")]
        vals = [w.get(field) for w in wks if w.get(field) is not None]
        return max(vals) if vals else None
    for k in sorted(set(op) | set(npz)):
        name = (npz.get(k) or op.get(k))["phase"]
        a, b = _peak(old, k, "intent_km"), _peak(new, k, "intent_km")
        if a is not None and b is not None and abs(a - b) >= 1:
            changes.append(f"{name} volume: {a:g} → {b:g} km/wk")
        # §6e frequency advance changes RUNS at constant volume — invisible to the km fingerprint above.
        ra, rb = _peak(old, k, "runs"), _peak(new, k, "runs")
        if ra is not None and rb is not None and ra != rb:
            changes.append(f"{name}: {ra:g} → {rb:g} runs/wk")
    oa, na = _adj_directive(old.get("adjustment")), _adj_directive(new.get("adjustment"))
    if _adj_fingerprint(oa) != _adj_fingerprint(na):
        changes.append(f"Adjustment: {_adj_summary(oa)} → {_adj_summary(na)}")
    # No-op re-plan (the plan already matched the request — e.g. a priority set to what it already was,
    # or a re-generate with nothing new): say so plainly, so it doesn't read as "your action failed".
    return {"first": False, "changes": changes or ["The plan already matched — your objectives and priorities are unchanged."],
            "summary": (f"{len(changes)} change(s) to the road ahead"
                        if changes else "No change — the plan was already up to date")}


def plan_baseline(db):
    """A throwaway plan for *today* under the CURRENT state — captured BEFORE a triggering
    change (add/remove objective, apply/clear adjustment) so the diff can isolate that change.
    Comparing two plans both computed for today makes pure calendar drift (runway 25→24w, a
    phase shrinking as the race nears) cancel out, instead of masquerading as 'changes you
    made'. Returns the plan dict or None if it can't be built."""
    p = generate_plan(db)
    return p if p.get("ok") else None


AV_HORIZON_DAYS = 400   # §AV — expansion bound: an availability range never yields more dates than
#                         this past today (a typo'd year can't inflate the blocked set)


def _av_blocked_dates(db, today):
    """§AV — the active away-day set as ISO dates, from the Monday of `today`'s week onward (the
    straddling week keeps its blocked days visible until the week closes, so an elapsed away day
    still anchors the week's re-laid layout for display/matching), horizon-bounded. Read by
    generate_plan so the plan stays a pure function of its inputs. Returns a (possibly empty) set."""
    from datetime import timedelta
    monday = today - timedelta(days=today.weekday())
    rows = db.execute(
        "SELECT date_from, date_to FROM availability WHERE active=1 AND date_to >= ?",
        (monday.isoformat(),)).fetchall()
    out, horizon = set(), today + timedelta(days=AV_HORIZON_DAYS)
    for r in rows:
        try:
            d, end = _date(r["date_from"]), min(_date(r["date_to"]), horizon)
        except (ValueError, TypeError):
            continue                                   # malformed row can't poison the plan
        d = max(d, monday)
        while d <= end:
            out.add(d.isoformat())
            d += timedelta(days=1)
    return out


def active_adjustment(db, today):
    """The current clamped adjustment still in its window (most recent active), or None. Read by
    generate_plan so the plan stays a pure function of (today, shape, objectives, adjustments).
    §H3 — a MEDICAL hold dominates and ignores the calendar window: its load reduction (full rest)
    stays in force open-ended until cleared, matching the open-ended gate (so the plan can't resume
    prescribing load after the 28-day window while the gate still reads halt)."""
    row = db.execute(
        "SELECT note, directive FROM adjustments "
        "WHERE active=1 AND medical=1 ORDER BY id DESC LIMIT 1").fetchone() or db.execute(
        "SELECT note, directive FROM adjustments "
        "WHERE active=1 AND applies_until >= ? ORDER BY id DESC LIMIT 1", (today,)
    ).fetchone()
    if not row:
        return None
    try:
        directive = json.loads(row["directive"])
    except (ValueError, TypeError):
        return None
    return {"note": row["note"], "directive": directive, "clamp": directive.get("clamp")}


def active_medical_halt(db):
    """§H3 — is a medical hold (a flagged exertional symptom) currently in force? A medical hold lives
    on its own DOMINANT track (`medical=1`): it ignores the calendar window (persists across days until
    explicitly cleared, never expiring back to green) AND is not deactivated by a later routine
    adjustment (see `_save_adjustment`) — so it's strictly 'until cleared', not 'until superseded'.
    Read by today_readiness to keep the gate red, and by active_adjustment to keep the load at rest."""
    return db.execute(
        "SELECT 1 FROM adjustments WHERE active=1 AND medical=1 LIMIT 1").fetchone() is not None


def _plan_all_weeks(plan):
    """Every generated week across EVERY phase block of a plan, pk-tagged with its phase key, in
    calendar order (re-base first, then the phases walk — chain segments included via their own
    keys). THE single reader for 'the plan's weeks': the assertive regime skips the re-base, so
    any rebase-only read silently drops the whole road — the 2026-07-04 family of bugs (the log
    overlay, the readiness tile's phantom 'No active plan', the explainer's empty week list)."""
    weeks = [{**w, "pk": "rebase"} for w in (plan.get("rebase") or {}).get("weeks", [])]
    keyed = False
    for ph in plan.get("phases", []):
        key = ph.get("key")
        if key and key != "rebase":
            weeks += [{**w, "pk": key} for w in (plan.get(key) or {}).get("weeks", [])]
            keyed = True
    if not keyed:   # LEGACY saved plan (pre-§6q phases carry no keys): the classic single-A blocks.
        # Matters because prior_plan rows feed DECISIONS (§PRO5's measured-vs-projected ride reads
        # proj_ctl off them): a stale-format row must not silently blind the response right after
        # an upgrade.
        for key in ("base", "build", "peak", "taper"):
            weeks += [{**w, "pk": key} for w in (plan.get(key) or {}).get("weeks", [])]
    return weeks


def _monday(d):
    from datetime import timedelta
    return d - timedelta(days=d.weekday())
