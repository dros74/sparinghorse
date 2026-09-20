"""Sparing Horse — §BEAT: cadence-matched playlists for the plan's sessions (0.61.0).

A module, not a feature of the app file: `SparingHorse.py` imports it INSIDE a try and calls
`register(app, host)` when it is there. It ships in the public mirror since 0.69.0 and remains
optional regardless: a tree without this file (and `static/music.*`) boots without the page and
never sees it. Everything the module needs from the app arrives through `host` (the app
module object, whatever name it was imported under), and nothing in the app reads back from here.

What it does, in one paragraph. Every plan session is cut into segments (warm-up, work, recovery,
cool-down; the race into settle / cruise / grind). Each segment gets a TARGET CADENCE from the
athlete's own speed–cadence curves — the recent one and the trained one — stepped up a little, never
past what this runner has already done at that speed, never past the comfort ceiling. Tracks from
the athlete's listening history (last.fm scrobbles, Spotify saved and top tracks) are scored for
tempo (ReccoBeats reproduces the audio features Spotify withdrew) and picked into the segments:
tempo inside the entrainment window, energy above the segment's floor, taste weight breaking ties.
The result is written back as a private Spotify playlist and the run's cadence is read back from
`run_metrics` on the next sync, so the ladder climbs from what was RUN, not from what was asked.

The science this leans on (ENGINE_SCIENCE §10.9 carries the constants):
  • Davis (2026), cadence guide: cadence is meaningless without speed; a runner's own curve is the
    reference; retrain +5–10 % at most, on easy runs, never at race pace.
  • Van Dyck et al. 2015: spontaneous entrainment to music saturates beyond ~2–3 % tempo deviation.
  • de Ruiter et al. 2014: trained runners self-select ~3 % under their energetic optimum.
  • Bood et al. 2013: cadence-synced sound extends time to exhaustion; the music, not the beat,
    lowers perceived effort.
  • Karageorghis et al. 2011: preferred intensity of music rises with exercise intensity.
"""
import json
import math
import re
import secrets
import sqlite3
import statistics
import threading
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

import requests

# ── Calibration (ENGINE_SCIENCE §10.9) ───────────────────────────────────────
MUSIC_TEMPO_WINDOW = 0.02      # ±2 % — the entrainment basin (Van Dyck 2015); a track outside it
#                                is a track the legs will not follow without being told to.
#                                §BEAT11: the picker's second rung, opened only when the lock band is dry
MUSIC_STEP_PCT = 1.5           # default ladder step over the recent curve, per build — inside the basin
MUSIC_MAX_STEP_PCT = 3.0       # the setting's ceiling: beyond ~3 % nothing entrains spontaneously
MUSIC_WORK_STEP_PCT = 3.0      # §BEAT5 (0.65.0) — WORK segments take the whole basin as their step. At
#                                threshold pace the athlete sat ~10 spm under the trained line and
#                                overstrode to hold the pace (1.12 m per step against 1.05 at the same
#                                pace on the 2024 race), asked for a higher cadence, and had been handed
#                                the plain 1.5 % step (172.8 for legs that showed 169.5). The easy step
#                                stays the setting's: the fast end of the curve is where the gap costs.
MUSIC_WORK_EFFORTS = ("work", "race_settle", "race_cruise", "race_grind")   # the efforts that take it…
MUSIC_WORK_ZONES = ("marathon", "threshold", "interval")                    # …and the table rows that show it
MUSIC_CLIMB_EFFORTS = ("long_3", "race_grind")   # §BEAT10 (0.68.6) — the segments that climb to the line: they
                                                 # pick FIRST, take songs at or above the target first, and play
                                                 # them in rising tempo
MUSIC_TAIL_MIN = 15.0          # §BEAT5 — a run-home tail after the session's last segment, at the session's
#                                easiest pace: the 09-08 tempo ran 42 min on a 35-min list and the
#                                phone's autoplay filled the last seven with tempos the legs then fought
MUSIC_RAMP_DAYS = 84           # §BEAT4 — the ramp from the recent line to the trained line runs twelve weeks…
MUSIC_RAMP_LEAD_DAYS = 21      # …and lands this long before the A-race: the taper rehearses the race's cadence
MUSIC_TRAINED_TOP_FRAC = 0.33  # the fittest third of runs (by CTL at the time) defines the trained curve
MUSIC_RECENT_DAYS = 56         # the recent curve's window — long enough to fit, short enough to move
MUSIC_MIN_FIT_RUNS = 12        # below this a curve is a median plus the trained slope, not a fit
MUSIC_COMFORT_PCTL = 0.90      # comfort ceiling when unset: the runner's own 90th-percentile cadence
MUSIC_CAD_SPM_RANGE = (120, 220)   # plausible steps-per-minute; a value under 120 is a one-leg count
MUSIC_SEGMENT_FILL = 1.10      # fill a segment to 110 % of its minutes — a song may run past the seam
MUSIC_MIN_SEGMENT_MIN = 4.0    # a rep shorter than a song cannot carry one: consecutive reps under this
#                                are one block at the work pace (10 × 2 min plays as one 20-min block)
MUSIC_LONG_SPLIT_MIN = 75      # a plain long run at or over this is cut in thirds with rising energy
MUSIC_RACE_SETTLE_FRAC = 0.24  # the race opens on a quarter of the distance at the lowest energy…
MUSIC_RACE_GRIND_FRAC = 0.29   # …and closes on the last ~29 % (30 km of a marathon) at the highest
MUSIC_ENERGY_FLOOR = {"warmup": 0.45, "easy": 0.55, "easy_base": 0.55, "recovery": 0.50,
                      "work": 0.70, "cooldown": 0.40, "long_1": 0.50, "long_2": 0.55,
                      "long_3": 0.65, "race_settle": 0.55, "race_cruise": 0.65, "race_grind": 0.78,
                      "tail": 0.40}
MUSIC_FLOOR_MIN = 0.40             # §BEAT7 — the one energy gate left: under it a track is ambient, not a running track
MUSIC_WIDEN_STEPS = (0.03, 0.04)   # a dry window widens to ±3 % then ±4 %, saying so
MUSIC_SCORE_TASTE = 2.0        # picking score weights: taste, the legs' own verdict, tempo closeness…
MUSIC_SCORE_FOLLOW = 1.0       # §BEAT7 — …the followability the runner's cadence has shown the track (−1.5…1)
MUSIC_SCORE_CLOSE = 0.5
MUSIC_SCORE_SERVED = 1.0       # §BEAT7 — …minus this per list the track was on inside the rotation window
MUSIC_ROTATION_DAYS = 28       # §BEAT7 — the rotation window: lists built inside it count against a track
MUSIC_ENTRAIN_PCT = 0.01       # §BEAT7 — a song run within this of its tempo: the legs followed it.
#                                §BEAT11: the picker's first rung — fill from here before the basin
CADENCE_SENSOR_BIAS_SPM = 2.4  # §BEAT12 (0.68.9) — the watch's per-second cadence field reads this many
#                                spm under its own stride counter (three original Suunto FITs, 23 Aug /
#                                6 Sep / 16 Sep 2026: 2.1 / 2.3 / 2.7). Every cadence the app holds is in
#                                that unit; song tempos are not. Applied only where legs meet songs (the
#                                picker's tempo gate, the read-back's entrainment) and nowhere else — the
#                                curve, the targets and the rung compare legs with legs. Re-measure from an
#                                original Suunto FIT (Runalyze's re-export drops the counter).
MUSIC_DIP_SPM = 4.0            # §BEAT7 — a cadence drop under the song's median by at least this…
MUSIC_DIP_MIN_S = 4            # …lasting at least this long is a dip: the beat lost the legs
MUSIC_STEADY_SPM = 3.0         # §BEAT7 — samples within this of the song's median are steady
MUSIC_SONG_SEAM_S = 20         # §BEAT2 read-back: the first seconds of a song are the seam from the
#                                last one — the legs are still on the old beat; excluded per song
MUSIC_SONG_MIN_S = 60          # …and a song with under a minute inside the run is not read at all
MUSIC_PRESS_PAIR_S = 6         # §BEAT3: lap presses within this many seconds are ONE signal — since §BEAT7
#                                one press says "the beat lost me here", two say "never again"; the verdict
#                                lands on the song playing
MUSIC_GAP_MIN_S = 30           # §BEAT8 (0.68.3): a gap between two aligned songs at least this long held a
#                                song the play history dropped, and is read from the list order (the 09-12
#                                run's four gaps ran 45–190 s; the seam plus five samples need 25)
MUSIC_WORK_READ_MIN_S = 30     # §BEAT9 (0.68.5): a song inside a coalesced reps block is read over the REP spans
                               # only; under this many seconds inside the reps it is left ungraded (a jog song)
MUSIC_STRUCT_SLACK_S = 60      # §BEAT9 — the structure's segments must add up to the run's length within this,
                               # or a pause the frames dropped would shift every rep; then the whole window is read


# ── Plumbing (ENGINE_SCIENCE §10.6) ──────────────────────────────────────────
RB_BASE = "https://api.reccobeats.com/v1"
RB_BATCH = 40                      # audio-features ids per call — the API's own cap (probed 2026-09-06)
LFM_BASE = "https://ws.audioscrobbler.com/2.0/"
LFM_PAGE = 200                     # last.fm rows per period
SP_API = "https://api.spotify.com/v1"
SP_ACCOUNTS = "https://accounts.spotify.com"
SP_SCOPES = ("playlist-modify-private playlist-read-private user-library-read user-top-read "
             "user-read-recently-played")   # 0.62.0 — the play history that lines songs up with a run
SP_PLAYED_AT_IS_END = True         # Spotify stamps `played_at` when a track ENDS; the start is that minus its length
MUSIC_PLAYED_KEEP_DAYS = 400       # play history kept in music.db (Spotify itself only serves the last 50)
MUSIC_READBACK_RUNS = 8            # recent runs offered for a read-back on the page
SP_PAGE = 50                       # saved/top tracks per page
MUSIC_LIBRARY_MAX = 1000           # saved tracks walked per refresh
MUSIC_PLAYLISTS_MAX = 40           # of the athlete's own playlists walked per refresh…
MUSIC_PLAYLIST_ITEMS_MAX = 300     # …and items read from each (the ones built here are skipped)
MUSIC_LOOKUPS_PER_REFRESH = 120    # last.fm → Spotify searches per refresh (the rest wait a click)
MUSIC_FEATURE_MISS_TTL_DAYS = 60   # a track ReccoBeats had no features for is asked again after this
MUSIC_SEARCH_MISS_TTL_DAYS = 30    # a last.fm track Spotify could not find is searched again after this
MUSIC_HTTP_TIMEOUT = 20
MUSIC_FIT_MAX_BYTES = 20 * 1024 * 1024   # fetch_fit streams and abandons past this — no cap meant a 200 response of any size was buffered whole
MUSIC_OAUTH_STATE_TTL_S = 600
MUSIC_UPCOMING_DAYS = 10
MUSIC_DISCOVERY_SIZE = 60          # recommendations asked per dry segment
MUSIC_DESC_MAX = 300               # Spotify's playlist description cap
MUSIC_PLAYLIST_CHUNK = 100         # uris per playlist write
LB_BASE = "https://api.listenbrainz.org/1"
MUSIC_LB_NEIGHBOURS = 20           # §DISCO (0.67.0) — similar ListenBrainz listeners read per refresh…
MUSIC_LB_PER_USER = 100            # …and each one's top recordings of the year
MUSIC_LB_CF = 200                  # collaborative-filtering picks read per refresh
MUSIC_LB_WEEKLY_LISTS = 4          # the newest weekly-exploration lists read
MUSIC_LB_RADIO_SEEDS = 6           # LB radio seeded on the listener's top artists of the year (needs the token)
MUSIC_LB_LOOKUPS = 120             # candidate → Spotify searches per refresh (the rest wait for the next)
MUSIC_LB_NAMES_BATCH = 50          # recordings named per metadata call
MUSIC_FRESH_QUOTA = 4              # §DISCO — songs never run to that each list carries when the window holds them:
                                   # measured 2026-09-11 on one listener's ListenBrainz sources, ~150 runnable fresh songs
                                   # and ~45 per target, so four a list refills for months without a repeat

SP_TOKENS_KEY = "spotify_oauth_tokens"   # internal secrets-store row (JSON); never in the UI spec

S = None                     # the app module, bound by register()
_oauth_state = {}            # state nonce → issue time (single-user; CSRF guard)
_job = {"running": False, "step": "", "done": 0, "total": 0, "error": None,
        "started": None, "finished": None, "summary": None}
_job_lock = threading.Lock()


# ── Storage: a database of its own, beside the main one ──────────────────────
def music_db_path():
    """`music.db` next to the main DB: the cache and the playlist ledger are rebuildable and private,
    so they stay out of the shared schema (and out of the export) entirely."""
    return S.DB_PATH.with_name("music.db")


def _mdb():
    conn = sqlite3.connect(music_db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    had_follow = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='follow'").fetchone())
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS track(spotify_id TEXT PRIMARY KEY, title TEXT, artist TEXT,
        duration_ms INTEGER, tempo REAL, energy REAL, danceability REAL, valence REAL, loudness REAL,
        features_at TEXT, feature_miss_at TEXT, discovered INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS taste(spotify_id TEXT PRIMARY KEY, lfm_playcount INTEGER DEFAULT 0,
        lfm_rank INTEGER, loved INTEGER DEFAULT 0, saved INTEGER DEFAULT 0, top_rank INTEGER,
        playlisted INTEGER DEFAULT 0, weight REAL DEFAULT 0, updated_at TEXT);
      CREATE TABLE IF NOT EXISTS lfm_map(artist TEXT, title TEXT, spotify_id TEXT, searched_at TEXT,
        PRIMARY KEY(artist, title));
      CREATE TABLE IF NOT EXISTS playlist(key TEXT PRIMARY KEY, spotify_id TEXT, name TEXT, url TEXT,
        built_at TEXT, spec TEXT);
      CREATE TABLE IF NOT EXISTS setting(key TEXT PRIMARY KEY, value TEXT);
      CREATE TABLE IF NOT EXISTS played(played_at TEXT PRIMARY KEY, spotify_id TEXT, title TEXT, artist TEXT,
        duration_ms INTEGER, pulled_at TEXT);
      CREATE TABLE IF NOT EXISTS rating(spotify_id TEXT PRIMARY KEY, rating TEXT, note TEXT, rated_at TEXT);
      CREATE TABLE IF NOT EXISTS readback(run_id INTEGER PRIMARY KEY, date TEXT, computed_at TEXT, payload TEXT);
      CREATE TABLE IF NOT EXISTS rating_run(run_id INTEGER, spotify_id TEXT, rating TEXT, at_s INTEGER,
        PRIMARY KEY(run_id, spotify_id));
      CREATE TABLE IF NOT EXISTS follow(run_id INTEGER, spotify_id TEXT, spm REAL, tempo REAL, delta_pct REAL,
        entrained INTEGER, steady REAL, dips INTEGER, dip_s INTEGER, breaks INTEGER, read_s INTEGER,
        PRIMARY KEY(run_id, spotify_id));
      CREATE TABLE IF NOT EXISTS disco(mbid TEXT PRIMARY KEY, artist TEXT, title TEXT, sources TEXT,
        first_seen TEXT, searched_at TEXT, spotify_id TEXT);
      -- §BEAT14: the athlete's own word after the run ("leave it out"), kept apart from rating_run
      -- because readback() DELETEs and rewrites that table whole on every recompute
      CREATE TABLE IF NOT EXISTS manual_verdict(run_id INTEGER, spotify_id TEXT, rating TEXT, role TEXT, at TEXT,
        PRIMARY KEY(run_id, spotify_id));
    """)
    # 0.67.0 — §DISCO: a track that came in from ListenBrainz carries where it came from (the sources,
    # comma-joined), so the legs' verdicts can be read per source
    if "source" not in {r[1] for r in conn.execute("PRAGMA table_info(track)")}:
        conn.execute("ALTER TABLE track ADD COLUMN source TEXT")
        conn.commit()
    # 0.63.0 — a rating carries where it came from: 'couch' (rated at rest) or 'run' (a lap press)
    if "source" not in {r[1] for r in conn.execute("PRAGMA table_info(rating)")}:
        conn.execute("ALTER TABLE rating ADD COLUMN source TEXT DEFAULT 'couch'")
        conn.commit()
    # 0.65.0 — a run verdict carries the ROLE of the segment it landed on (work / easy), so a skip or
    # a two-press can set the track aside for that kind of segment and no other
    if "role" not in {r[1] for r in conn.execute("PRAGMA table_info(rating_run)")}:
        conn.execute("ALTER TABLE rating_run ADD COLUMN role TEXT")
        conn.commit()
    # 0.66.0 — the lap presses changed meaning (§BEAT7). The day this database first carried the
    # legs' table is the day the new words apply from: a run before it is read under the old ones,
    # so a two-press "relaxes" from an earlier run never becomes a "never again"
    if not had_follow:
        conn.execute("INSERT OR IGNORE INTO setting(key, value) VALUES('press_protocol_from', ?)", (date.today().isoformat(),))
        conn.commit()
    return conn


def press_protocol_from(conn):
    r = conn.execute("SELECT value FROM setting WHERE key='press_protocol_from'").fetchone()
    return r["value"] if r else None


SETTING_DEFAULTS = {"lastfm_user": "", "listenbrainz_user": "", "max_spm": "", "step_pct": str(MUSIC_STEP_PCT),
                    "half_time": "0", "discovery": "0"}
# discovery is OFF by default: ReccoBeats' recommendations ignore tempo, and measured on 2026-09-06
# the local window kept 1 track of 264 returned for targets of 166–178 bpm — a lottery, not a source


def get_settings(conn=None):
    own = conn is None
    conn = conn or _mdb()
    try:
        rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM setting")}
    finally:
        if own:
            conn.close()
    return {k: rows.get(k, v) for k, v in SETTING_DEFAULTS.items()}


def validate_music_setting(key, value):
    """(ok, error) for one music setting — refused out loud while the human is still looking."""
    v = ("" if value is None else str(value)).strip()
    if key in ("lastfm_user", "listenbrainz_user"):
        if v and not re.fullmatch(r"[A-Za-z0-9_\-.]{1,64}", v):
            return False, f"a {'last.fm' if key == 'lastfm_user' else 'ListenBrainz'} username (letters, digits, _ - .)"
    elif key == "max_spm":
        if v and not (v.isdigit() and MUSIC_CAD_SPM_RANGE[0] <= int(v) <= MUSIC_CAD_SPM_RANGE[1]):
            return False, f"a whole number {MUSIC_CAD_SPM_RANGE[0]}–{MUSIC_CAD_SPM_RANGE[1]} spm (or empty)"
    elif key == "step_pct":
        try:
            f = float(v) if v else MUSIC_STEP_PCT
        except ValueError:
            return False, "a number of percent"
        if not (0 <= f <= MUSIC_MAX_STEP_PCT):
            return False, f"0–{MUSIC_MAX_STEP_PCT} % — beyond that the legs stop following the beat"
    elif key in ("half_time", "discovery"):
        if v and v not in ("0", "1"):
            return False, "1 (on) or 0 (off)"
    else:
        return False, "unknown setting"
    return True, None


def save_settings(values):
    conn = _mdb()
    try:
        for k, v in values.items():
            if k not in SETTING_DEFAULTS:
                continue
            conn.execute("INSERT INTO setting(key, value) VALUES(?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (k, ("" if v is None else str(v)).strip()))
        conn.commit()
    finally:
        conn.close()


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fmt_pace(sec):
    return f"{int(sec // 60)}:{int(sec % 60):02d}" if sec else "—"


# ── The cadence curves ───────────────────────────────────────────────────────
def cadence_rows(db, today=None):
    """(date, km/h, spm, ctl) per run from `run_metrics` — cadence normalised to steps per minute
    by MAGNITUDE (Runalyze relays a one-leg count from some devices; nobody runs under 120 spm and
    no leg turns over 110 times a minute, so the doubling is unambiguous).

    CTL comes from the row's own snapshot when it has one and from the engine's reconstructed
    fitness curve otherwise: `run_metrics.ctl_snapshot` is only stamped from the day the table was
    introduced, so on a long history the stamped rows are the LAST few months — exactly the runs the
    trained line must not be drawn from (0.61.1: on the live corpus the "trained" third was 20 runs
    of the 2026 rebuild, and the ladder stood still)."""
    ctl_by_date = {}
    try:
        ctl_by_date = {h["date"]: h["ctl"] for h in S.reconstruct_history(db, end=today.isoformat() if today else None)}
    except Exception:
        ctl_by_date = {}
    out = []
    # speed: the upstream field when it is there, else the run's own km over its own seconds — a
    # seeded or freshly-backfilled row can carry the distance and the duration and no speed column
    for r in db.execute("SELECT date, km, cadence, ctl_snapshot, "
                        "COALESCE(speed_kmh, CASE WHEN dur_s > 0 THEN km * 3600.0 / dur_s END) AS kmh "
                        "FROM run_metrics WHERE cadence IS NOT NULL AND km >= 3"):
        cad = float(r["cadence"])
        if cad < MUSIC_CAD_SPM_RANGE[0]:
            cad *= 2
        if not (MUSIC_CAD_SPM_RANGE[0] <= cad <= MUSIC_CAD_SPM_RANGE[1]) or not r["kmh"] or r["kmh"] <= 0:
            continue
        ctl = r["ctl_snapshot"] if r["ctl_snapshot"] is not None else ctl_by_date.get(r["date"])
        out.append((r["date"], float(r["kmh"]), cad, ctl))
    return out


def _fit(xs, ys):
    """Least-squares line (intercept, slope); a flat line through the mean when x does not vary."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    if vx <= 1e-9:
        return my, 0.0
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx
    return my - b * mx, b


def _pctl(vals, p):
    v = sorted(vals)
    if not v:
        return None
    k = (len(v) - 1) * p
    f = int(k)
    return v[f] + (v[min(f + 1, len(v) - 1)] - v[f]) * (k - f)


def cadence_curve(rows, today, settings=None):
    """The runner's two speed–cadence lines and the comfort ceiling.

    `trained`: the fittest third of runs by CTL at the time (or, without CTL, everything older than
    the recent window). `recent`: the last MUSIC_RECENT_DAYS. Under MUSIC_MIN_FIT_RUNS a line is not
    fitted — it is the window's median cadence carried on the trained slope (or the pooled slope),
    so a runner with six runs still gets a target and never an extrapolated cliff. A runner with no
    rows at all gets None: the page says so rather than inventing a curve."""
    settings = settings or {}
    if not rows:
        return None
    cutoff = (today - timedelta(days=MUSIC_RECENT_DAYS)).isoformat()
    recent = [r for r in rows if r[0] >= cutoff]
    older = [r for r in rows if r[0] < cutoff]
    with_ctl = [r for r in rows if r[3] is not None]
    # the fittest third by CTL is only a "trained" set when CTL covers the history — a partial
    # stamp (the last months only) would pick the fittest RECENT runs and call them trained
    if len(with_ctl) >= MUSIC_MIN_FIT_RUNS * 2 and len(with_ctl) >= 0.5 * len(rows):
        thr = _pctl([r[3] for r in with_ctl], 1 - MUSIC_TRAINED_TOP_FRAC)
        trained_rows = [r for r in with_ctl if r[3] >= thr]
    else:
        trained_rows = older or rows
    pooled_a, pooled_b = _fit([r[1] for r in rows], [r[2] for r in rows]) if len(rows) >= 2 else (rows[0][2], 0.0)

    def line(sel, fallback_slope):
        if len(sel) >= MUSIC_MIN_FIT_RUNS:
            a, b = _fit([r[1] for r in sel], [r[2] for r in sel])
            return {"a": round(a, 2), "b": round(b, 3), "n": len(sel), "fitted": True}
        if not sel:
            return None
        med_c = statistics.median(r[2] for r in sel)
        med_v = statistics.median(r[1] for r in sel)
        return {"a": round(med_c - fallback_slope * med_v, 2), "b": round(fallback_slope, 3),
                "n": len(sel), "fitted": False}

    trained = line(trained_rows, pooled_b)
    recent_line = line(recent, (trained or {}).get("b", pooled_b))
    if recent_line is None:
        recent_line = dict(trained, n=0, fitted=False)
    all_cad = [r[2] for r in rows]
    comfort_src = "setting"
    try:
        comfort = float(settings.get("max_spm") or 0) or None
    except ValueError:
        comfort = None
    if not comfort:
        comfort, comfort_src = round(_pctl(all_cad, MUSIC_COMFORT_PCTL)), "observed p90"
    return {"trained": trained, "recent": recent_line, "comfort_max": comfort,
            "comfort_source": comfort_src, "n_rows": len(rows), "n_recent": len(recent),
            "recent_from": cutoff, "recent_median": round(statistics.median(r[2] for r in recent)) if recent else None}


def _line_at(ln, kmh):
    return ln["a"] + ln["b"] * kmh


def target_spm(curve, pace_sec, step_pct=None, ramp=None, work=False):
    """The segment's target cadence at `pace_sec`/km: the recent line lifted by the step — on a WORK
    segment by MUSIC_WORK_STEP_PCT when the setting's step is smaller (§BEAT5) — or, on a
    road with a race ahead, lifted to where the ramp stands between the recent and the trained line
    (§BEAT4) — capped by the trained line (never past what this runner has done at this speed), by
    the comfort ceiling, and by the rung: one entrainment step over what the legs last showed on a
    read-back, the last target itself when they ran under it, one step over the recent line before
    any read-back exists. Never under the recent line. Returns a dict so the page can show WHY."""
    if not curve or not pace_sec or pace_sec <= 0:
        return None
    step = MUSIC_STEP_PCT if step_pct is None else float(step_pct)
    why_step = f"recent +{step:g} %"
    if work and step < MUSIC_WORK_STEP_PCT:
        step, why_step = MUSIC_WORK_STEP_PCT, f"work step +{MUSIC_WORK_STEP_PCT:g} % — the fast end of the curve first"
    kmh = 3600.0 / pace_sec
    rec = _line_at(curve["recent"], kmh)
    tr = _line_at(curve["trained"], kmh) if curve.get("trained") else rec
    lifted = rec * (1 + step / 100.0)
    cap = max(tr, rec)
    comfort = curve["comfort_max"]
    # the caps in the order a tie is named: the ceiling, the runner's own curve, then the rung
    cands = [(comfort, "comfort ceiling"),
             (cap, "trained curve" if tr >= rec else "recent line — already above the trained curve")]
    asked, ramped, why_asked = lifted, None, why_step
    f = float((ramp or {}).get("fraction") or 0.0)
    if ramp and f > 0 and tr > rec:
        ramped = rec + f * (tr - rec)
        rung = ramp.get("rung")
        if rung is None:
            rung_lift, rung_why = MUSIC_MAX_STEP_PCT / 100.0, "rung — one step over the recent line until a read-back lands"
        elif rung["held"]:
            rung_lift = max(float(rung["lift_ran"]), 0.0) + MUSIC_MAX_STEP_PCT / 100.0
            rung_why = f"rung — one step over the {rung['date']} read-back ({rung['ran']:.0f} spm)"
        else:
            rung_lift = max(float(rung["lift_target"]), step / 100.0)
            rung_why = f"rung waits — the {rung['date']} read-back ran {rung['ran']:.0f} against {rung['target']:.0f}"
        cands.append((rec * (1 + rung_lift), rung_why))
        if ramped > lifted:
            asked = ramped
            if f >= 1.0:
                cands[1] = (cap, "trained curve — the ramp has landed")
                why_asked = "trained curve — the ramp has landed"
            else:
                why_asked = f"ramp — week {ramp.get('week')} of {ramp.get('weeks')} to the trained curve"
    target = min([asked] + [v for v, _ in cands])
    target = max(target, min(rec, comfort))
    why = why_asked
    for v, label in cands:
        if v <= target + 1e-9 and asked > v + 1e-9:
            why = label
            break
    return {"spm": round(target, 1), "recent": round(rec, 1), "trained": round(tr, 1),
            "comfort": comfort, "why": why, "ramp": round(ramped, 1) if ramped is not None else None}


def cadence_table(curve, zones, step_pct=None, ramp=None):
    """The page's cadence card rows: one per pace zone the plan prescribes."""
    rows = []
    for name in ("easy", "lt1", "marathon", "threshold", "interval"):
        p = _pace_sec(zones.get(name)) if zones else None
        if not p:
            continue
        t = target_spm(curve, p, step_pct, ramp, work=(name in MUSIC_WORK_ZONES))
        if t:
            rows.append({"zone": name, "pace": zones[name], **t})
    return rows


# ── §BEAT4 (0.64.0) — the ramp: the target climbs to the trained line on the race's calendar ──
def ramp_clock(objective, today):
    """PURE — where today stands on the ramp to the road's race, or None when no race is ahead.
    The ramp lands MUSIC_RAMP_LEAD_DAYS before the race and runs MUSIC_RAMP_DAYS up to that day:
    fraction 0 before it starts (the plain ladder), 1 once it has landed (the trained line, as far as
    the rungs allow). The objective is the plan's own — the A-race the road is laid to."""
    if not objective or not objective.get("date"):
        return None
    try:
        race = date.fromisoformat(str(objective["date"])[:10])
    except ValueError:
        return None
    if race < today:
        return None
    end = race - timedelta(days=MUSIC_RAMP_LEAD_DAYS)
    start = end - timedelta(days=MUSIC_RAMP_DAYS)
    elapsed = (today - start).days
    f = min(1.0, max(0.0, elapsed / float(MUSIC_RAMP_DAYS)))
    weeks = MUSIC_RAMP_DAYS // 7
    phase = "before" if elapsed < 0 else ("landed" if elapsed >= MUSIC_RAMP_DAYS else "ramp")
    return {"race": race.isoformat(), "label": objective.get("label") or objective.get("type") or "race",
            "start": start.isoformat(), "end": end.isoformat(), "lead_days": MUSIC_RAMP_LEAD_DAYS,
            "fraction": round(f, 3), "phase": phase, "weeks": weeks,
            "week": 0 if elapsed < 0 else min(weeks, elapsed // 7 + 1),
            "days_to_race": (race - today).days, "rung": None}


def held_rung(readbacks, curve):
    """PURE — the last rung the legs were asked for, and what they did with it. `readbacks`: stored
    read-backs newest first, each {date, km, minutes, target_spm, songs[{spm, read_s}]}. The newest
    one that carries a playlist target and at least one read song is the evidence — a read-back
    without a target (a run with no playlist) proves nothing about the rung and is skipped. `ran` is
    the weighted cadence over the songs; `held` whether it reached the target inside the tempo
    window; the two lifts are ratios over the recent line at the run's own speed, which is what the
    next target is built from. None without evidence.
    §BEAT9 — a song inside a reps block weighs its rep seconds, and a song that fell on the
    recoveries proves nothing about the rung."""
    for rb in readbacks or []:
        target = rb.get("target_spm")
        songs = [sg for sg in (rb.get("songs") or []) if sg.get("spm") and (sg.get("read_s") or 0) > 0]
        km, minutes = float(rb.get("km") or 0), float(rb.get("minutes") or 0)
        if not target or not songs or km <= 0 or minutes <= 0:
            continue
        # §BEAT5 — the PLAYLIST's songs, each against ITS segment's target. A run's plays include
        # whatever the phone went on to after the list ended (autoplay, the 09-08 tempo: seven
        # minutes of random tempos the legs fought), and a warm-up song and a work song were not
        # asked for the same cadence. A read-back that tags no song (an older payload) reads every
        # song against the run's target as before; one whose plays were all off the playlist proves
        # nothing about the rung and is skipped.
        tagged = [sg for sg in songs if sg.get("seg_target")]
        if not tagged and any(sg.get("in_playlist") is not None for sg in songs):
            continue
        weight = lambda sg: sg["work_s"] if "work_s" in sg else sg["read_s"]     # §BEAT9
        pool = [sg for sg in (tagged or songs) if weight(sg) > 0 and not sg.get("jogs_only")]
        if not pool:
            continue
        kmh = km / (minutes / 60.0)
        rec_run = _line_at(curve["recent"], kmh)
        if rec_run <= 0:
            continue

        def _rec(sg):
            p = sg.get("pace_sec")
            r = _line_at(curve["recent"], 3600.0 / p) if p and p > 0 else rec_run
            return r if r > 0 else rec_run
        w = sum(weight(sg) for sg in pool)
        ran = sum(sg["spm"] * weight(sg) for sg in pool) / w
        tgt = sum(float(sg.get("seg_target") or target) * weight(sg) for sg in pool) / w
        lift_ran = sum((sg["spm"] / _rec(sg) - 1) * weight(sg) for sg in pool) / w
        lift_target = sum((float(sg.get("seg_target") or target) / _rec(sg) - 1) * weight(sg) for sg in pool) / w
        return {"date": rb.get("date"), "run_id": rb.get("run_id"), "kmh": round(kmh, 2),
                "target": round(tgt, 1), "ran": round(ran, 1),
                "held": ran >= tgt * (1 - MUSIC_TEMPO_WINDOW),
                "lift_ran": round(lift_ran, 4), "lift_target": round(lift_target, 4),
                "songs": len(pool), "off_playlist": (len(songs) - len(pool)) if tagged else 0}
    return None


def ramp_state(plan, curve, today, conn=None):
    """The ramp clock for the road's race with the last rung read from the stored read-backs, or
    None when there is no race ahead or no curve to climb."""
    clock = ramp_clock((plan or {}).get("objective"), today)
    if not clock or not curve:
        return None
    own = conn is None
    conn = conn or _mdb()
    try:
        rows = []
        for r in conn.execute("SELECT run_id, date, payload FROM readback ORDER BY date DESC, run_id DESC LIMIT 8"):
            try:
                p = json.loads(r["payload"] or "{}")
            except ValueError:
                continue
            rows.append({"run_id": r["run_id"], "date": r["date"], "km": p.get("km"), "minutes": p.get("minutes"),
                         "target_spm": p.get("target_spm"), "songs": p.get("songs")})
    finally:
        if own:
            conn.close()
    clock["rung"] = held_rung(rows, curve)
    return clock


# ── Segments: a session as the sequence the music has to fit ─────────────────
def _pace_sec(txt):
    """'6:43/km easy' → 403 (seconds per km), or None."""
    m = re.match(r"\s*(\d+):(\d\d)", str(txt or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _rep_pace(rep):
    if rep.get("km") and rep.get("minutes"):
        return rep["minutes"] * 60.0 / rep["km"]
    return _pace_sec(rep.get("pace_zone"))


def session_segments(session):
    """One plan session → [{label, effort, minutes, pace_sec, floor}] in running order. Structured
    sessions follow their reps; a plain easy run is one segment; a long run at or over
    MUSIC_LONG_SPLIT_MIN is three rising thirds; the race is settle / cruise / grind by distance.
    Rest and a session without distance return []."""
    kind = session.get("kind") or "run"
    km, minutes = float(session.get("km") or 0), float(session.get("minutes") or 0)
    if kind == "rest" or km <= 0 or minutes <= 0:
        return []
    if session.get("race") or kind == "race":
        pace = minutes * 60.0 / km
        settle = km * MUSIC_RACE_SETTLE_FRAC
        grind = km * MUSIC_RACE_GRIND_FRAC
        cruise = km - settle - grind
        parts = [("Settle — first %.0f km" % settle, "race_settle", settle),
                 ("Cruise — to %.0f km" % (settle + cruise), "race_cruise", cruise),
                 ("Grind — the last %.0f km" % grind, "race_grind", grind)]
        return [{"label": lb, "effort": ef, "minutes": round(d * pace / 60.0, 1), "pace_sec": pace,
                 "floor": MUSIC_ENERGY_FLOOR[ef]} for lb, ef, d in parts if d > 0]
    reps = session.get("reps")
    if reps:
        out, wi = [], 0
        n_work = sum(1 for r in reps if r.get("effort") == "work")
        for r in reps:
            ef = r.get("effort") or "work"
            if ef == "work":
                wi += 1
            label = (f"Work {wi}/{n_work}" if ef == "work" and n_work > 1 else
                     {"warmup": "Warm-up", "recovery": "Recovery", "cooldown": "Cool-down",
                      "easy_base": "Easy base"}.get(ef, "Work"))
            det = r.get("detail")
            out.append({"label": f"{label} — {det}" if det else label, "effort": ef,
                        "minutes": float(r.get("minutes") or 0), "pace_sec": _rep_pace(r),
                        "floor": MUSIC_ENERGY_FLOOR.get(ef, MUSIC_ENERGY_FLOOR["work"])})
        return _with_tail(_coalesce_short([s for s in out if s["minutes"] > 0 and s["pace_sec"]]))
    pace = minutes * 60.0 / km
    if kind.startswith("long") and minutes >= MUSIC_LONG_SPLIT_MIN:
        third = minutes / 3.0
        return _with_tail([{"label": lb, "effort": ef, "minutes": round(third, 1), "pace_sec": pace,
                            "floor": MUSIC_ENERGY_FLOOR[ef]}
                           for lb, ef in (("Long — settle in", "long_1"), ("Long — the middle", "long_2"),
                                          ("Long — finish strong", "long_3"))])
    return _with_tail([{"label": "Easy run" if kind == "easy" else kind.replace("_", " ").title(), "effort": "easy",
                        "minutes": minutes, "pace_sec": pace, "floor": MUSIC_ENERGY_FLOOR["easy"]}])


def _with_tail(segs):
    """§BEAT5 — a run-home tail after the session's last segment: MUSIC_TAIL_MIN minutes at the
    session's easiest pace and the tail's own floor, so a road longer than the card keeps a beat
    the legs can follow instead of whatever the phone plays next. The race gets none: it ends at
    the line. Not counted in the session's minutes or its overall target."""
    if not segs:
        return segs
    pace = max(s["pace_sec"] for s in segs)
    return segs + [{"label": "Run home — past the card's end", "effort": "tail",
                    "minutes": float(MUSIC_TAIL_MIN), "pace_sec": pace, "floor": MUSIC_ENERGY_FLOOR["tail"]}]


def _coalesce_short(segs):
    """Reps shorter than a song cannot each carry one — a 2-minute rep given a 3½-minute track puts
    every second song on the wrong rep and the whole playlist minutes behind the session by the
    fourth. Consecutive segments under MUSIC_MIN_SEGMENT_MIN (warm-up and cool-down excepted) are
    one block at the WORK pace and the highest floor among them, sized to the block's true length."""
    out, i = [], 0
    is_short = lambda s: s["minutes"] < MUSIC_MIN_SEGMENT_MIN and s["effort"] not in ("warmup", "cooldown")
    while i < len(segs):
        if not is_short(segs[i]):
            out.append(segs[i])
            i += 1
            continue
        j = i
        while j < len(segs) and is_short(segs[j]):
            j += 1
        block = segs[i:j]
        work = [b for b in block if b["effort"] == "work"] or block
        seg = {"label": f"Reps — {len(work)} × work with recovery" if len(block) > 1 else block[0]["label"],
               "effort": "work", "minutes": round(sum(b["minutes"] for b in block), 1),
               "pace_sec": min(b["pace_sec"] for b in work), "floor": max(b["floor"] for b in block)}
        if len(block) > 1:
            seg["reps"] = len(work)              # §BEAT9 — a coalesced block: read the reps only, not the recoveries
        out.append(seg)
        i = j
    return out


# ── Picking: tracks into segments ────────────────────────────────────────────
def _tempo_hit(tempo, target, window, half_time):
    """The relation of a track's tempo to the target: 'full' inside the window, 'half' when the
    track at twice its tempo lands inside it (one beat per stride), else None."""
    if not tempo or not target:
        return None
    if abs(tempo - target) <= window * target:
        return "full"
    if half_time and abs(tempo * 2 - target) <= window * target:
        return "half"
    return None


def _closeness(tempo, target, window, hit):
    t = tempo * 2 if hit == "half" else tempo
    return max(0.0, 1.0 - abs(t - target) / (window * target)) if window * target else 0.0


def pick_for_segments(segments, pool, targets, half_time=False, discover=None, set_aside=None,
                      follow=None, served=None, fresh=None, fresh_quota=0):
    """Fill each segment with tracks. `pool` rows: {id, title, artist, duration_ms, tempo, energy,
    weight, discovered}. `targets`: spm per segment index. `discover(target, floor, window)` may
    return extra rows when a window runs dry. A track is used once per playlist. Returns (segments
    with their picks, notes), the segments always in running order. Order inside a segment: settle
    and warm-up calmest first, everything else by score.
    §BEAT10 (0.68.6) — `race_grind` and the long run's last third (`long_3`, `MUSIC_CLIMB_EFFORTS`)
    pick FIRST, before the segments that run earlier, so the earlier thirds do not spend the window's
    upper half before the finish gets a turn; inside a climbing segment, candidates at or above the
    target are taken before the rest of the window, and the picks then play in rising tempo. The
    13 Sep long run list is the case this closed: picking in running order, the finish third picked
    last and got the window's leftovers (166–168 spm against the middle third's 171–172), and the
    old energy-only sort put the calmest of those leftovers first — climbing to the line on tracks
    that were already the slowest in the list.
    §BEAT5 — `set_aside` ({spotify_id: roles}, from `set_aside()`) keeps a track out of every
    segment of a role earlier runs ruled it out for, and the notes say how many; a half-time hit
    never lands on a WORK segment, whatever the setting (a beat per stride carried the easy days and
    not the tempo, 2026-09-08).
    §BEAT7 — the tempo window is the only gate. The order inside it is taste, the legs' own verdict
    (`follow`, {spotify_id: −1.5…1 from `follow_scores()`}: a track the cadence sat on scores up, one
    it broke on or dipped through scores down), tempo closeness, and a penalty per list the track was
    on inside the rotation window (`served`, {spotify_id: n} from `served_counts()`), so a song comes
    round again only once the fresher ones have played. Both are looked up by id and by song name,
    so another edition of the same song counts as the song. Energy no longer selects a track (the
    2026-09-11 call: the feel is nuanced at best, and rating the same songs again biased the ratings);
    a flat plausibility floor (MUSIC_FLOOR_MIN) still keeps ambient tracks whose analysed tempo
    happens to hit off a running list, and energy still orders the calm-first and climbing segments.
    The notes say how many of a segment's picks are new to the window.
    §BEAT11 — the gate opens in rungs: the lock band (`MUSIC_ENTRAIN_PCT`) first, the basin
    (`MUSIC_TEMPO_WINDOW`) when it runs dry, then the widening steps, so the songs the read-back can
    call followed are taken before the ones it never could.
    §DISCO (0.67.0) — `fresh` (a set of ids no list has carried and no run has read, from
    `never_run()`) and `fresh_quota`: the list owes that many never-run songs, spread over its core
    segments (the tail carries none), taken first inside each window in score order and never past
    the window — a fresh song that does not fit the cadence is not on the list. What the window
    holds no more of stays owed, and the notes say so."""
    used, used_names, notes, out = set(), set(), [], {}
    follow, served, fresh = follow or {}, served or {}, fresh or set()
    quota_left, fresh_taken = (int(fresh_quota or 0) if fresh else 0), 0
    if CADENCE_SENSOR_BIAS_SPM:        # §BEAT12 — once per list, not per segment
        notes.append(f"songs sit {CADENCE_SENSOR_BIAS_SPM:.1f} bpm above the target: the watch counts cadence that much low")
    # §BEAT10 — the climbing segments (MUSIC_CLIMB_EFFORTS) fill first so they see the window before
    # the earlier thirds spend it, but the returned list stays in running order (built by index below).
    order = [i for i, s in enumerate(segments) if s.get("effort") in MUSIC_CLIMB_EFFORTS] + \
            [i for i, s in enumerate(segments) if s.get("effort") not in MUSIC_CLIMB_EFFORTS]

    def eff(t, h):
        return t["tempo"] * 2 if h == "half" else t["tempo"]

    for pos, i in enumerate(order):
        seg = segments[i]
        target = targets[i]
        song_t = target + CADENCE_SENSOR_BIAS_SPM   # §BEAT12 — legs meet songs here; target_spm stays the display value
        need = seg["minutes"] * 60_000 * MUSIC_SEGMENT_FILL
        picks = []
        role = "work" if seg.get("effort") in MUSIC_WORK_EFFORTS else "easy"
        climbing = seg.get("effort") in MUSIC_CLIMB_EFFORTS
        aside_ids = set()
        floor = seg.get("floor") or 0.0           # only discovery still hears the floor
        # §BEAT11 (0.68.7) — the band the legs measurably lock on (MUSIC_ENTRAIN_PCT, ±1 %) fills first; the
        # basin (MUSIC_TEMPO_WINDOW, ±2 %) and the widening steps follow only when it runs dry, so a song picked
        # at the basin's edge — which the read-back could never call followed — is a last resort, not a peer
        attempts = [MUSIC_ENTRAIN_PCT, MUSIC_TEMPO_WINDOW] + list(MUSIC_WIDEN_STEPS)
        cands, k, tried_discovery, have = list(pool), 0, False, 0
        while k < len(attempts):
            window = attempts[k]
            scored = []
            for t in cands:
                if t["id"] in used or not t.get("tempo") or not t.get("duration_ms"):
                    continue
                if (_norm(t.get("artist")), _norm(t.get("title"))) in used_names:
                    continue                      # the same song under another id (single vs album)
                if set_aside and role in (set_aside.get(t["id"]) or ()):
                    aside_ids.add(t["id"])
                    continue                      # §BEAT5 — ruled out for this role by an earlier run
                hit = _tempo_hit(t["tempo"], song_t, window, half_time and role != "work")
                if not hit or (t.get("energy") or 0) < MUSIC_FLOOR_MIN:
                    continue
                fw, sv = _by_song(follow, t), _by_song(served, t)
                score = (MUSIC_SCORE_TASTE * (t.get("weight") or 0)
                         + MUSIC_SCORE_FOLLOW * float(fw or 0.0)
                         + MUSIC_SCORE_CLOSE * _closeness(t["tempo"], song_t, window, hit)
                         - MUSIC_SCORE_SERVED * int(sv or 0))
                scored.append((score, hit, t, fw, sv))
            scored.sort(key=lambda x: -x[0])
            if quota_left > 0 and seg.get("effort") != "tail":
                segs_left = sum(1 for j in order[pos:] if segments[j].get("effort") != "tail")
                take = -(-quota_left // max(1, segs_left))          # ceil — the quota spread over what is left
                for score, hit, t, fw, sv in scored:
                    if take <= 0 or have >= need:
                        break
                    name = (_norm(t.get("artist")), _norm(t.get("title")))
                    if t["id"] not in fresh or t["id"] in used or name in used_names:
                        continue
                    picks.append({**t, "hit": hit, "score": round(score, 3), "follow": fw, "served": int(sv or 0),
                                  "never_run": True})
                    used.add(t["id"])
                    used_names.add(name)
                    have += t["duration_ms"]
                    take -= 1
                    quota_left -= 1
                    fresh_taken += 1
            passes = ([lambda h, t: eff(t, h) >= song_t, lambda h, t: True] if climbing else [lambda h, t: True])
            for cond in passes:
                for score, hit, t, fw, sv in scored:
                    if have >= need:
                        break
                    name = (_norm(t.get("artist")), _norm(t.get("title")))
                    if t["id"] in used or name in used_names or not cond(hit, t):
                        continue
                    picks.append({**t, "hit": hit, "score": round(score, 3), "follow": fw, "served": int(sv or 0)})
                    used.add(t["id"])
                    used_names.add(name)
                    have += t["duration_ms"]
                if have >= need:
                    break
            if have >= need:
                break
            # §BEAT11 — discovery still asks at the basin (±2 %), not the lock band: a narrower search would
            # return fewer candidates for the same dry window
            if discover is not None and not tried_discovery and target and window >= MUSIC_TEMPO_WINDOW:
                tried_discovery = True
                extra = discover(song_t, floor, window) or []
                known = {c["id"] for c in cands}
                fresh = [e for e in extra if e["id"] not in used and e["id"] not in known]
                if fresh:
                    cands += fresh
                    continue                      # same rung again, with the new candidates
            k += 1
            if k < len(attempts) and attempts[k] > MUSIC_TEMPO_WINDOW:
                notes.append(f"{seg['label']}: window widened to ±{attempts[k] * 100:.0f} %")
        if have < need:
            notes.append(f"{seg['label']}: {have / 60000:.0f} of {seg['minutes']:.0f} min filled — "
                         f"the library is short of tracks near {song_t:.0f} bpm")
        if aside_ids:
            notes.append(f"{seg['label']}: {len(aside_ids)} track{'s' if len(aside_ids) > 1 else ''} set aside — "
                         f"skipped on a run, or two presses")
        n_new = sum(1 for p in picks if not p["served"])
        if picks and served:
            notes.append(f"{seg['label']}: {n_new} of {len(picks)} track{'s' if len(picks) > 1 else ''} new to the "
                         f"last {MUSIC_ROTATION_DAYS} days")
        ef = seg["effort"]
        if ef in ("warmup", "race_settle", "long_1", "cooldown"):
            picks.sort(key=lambda p: p.get("energy") or 0)
        elif climbing:
            picks.sort(key=lambda p: (eff(p, p.get("hit")), p.get("energy") or 0))   # §BEAT10 — rising to the line
        out[i] = {**seg, "target_spm": target, "tracks": picks,
                    "fresh": sum(1 for p in picks if not p["served"]),
                    "never_run": sum(1 for p in picks if p.get("never_run")),
                    "filled_min": round(sum(p["duration_ms"] for p in picks) / 60000.0, 1)}
    if fresh_quota and fresh:
        if fresh_taken >= int(fresh_quota):
            notes.append(f"{fresh_taken} song{'s' if fresh_taken != 1 else ''} never run to, from ListenBrainz — the fresh quota")
        else:
            notes.append(f"{fresh_taken} of {int(fresh_quota)} never-run songs — the windows held no more from ListenBrainz")
    return [out[i] for i in range(len(segments))], notes


def song_key(artist, title):
    """The song behind the editions: normalised artist|title, or None when either is missing."""
    a, t = _norm(artist), _norm(title)
    return f"{a}|{t}" if a and t else None


def _by_song(d, t):
    """A per-track value looked up by the track's id, then by its song name (the larger wins)."""
    vals = [v for v in (d.get(t.get("id")), d.get(song_key(t.get("artist"), t.get("title")))) if v is not None]
    return max(vals) if vals else None


def served_counts(conn, today=None, exclude_key=None, days=MUSIC_ROTATION_DAYS):
    """§BEAT7 — how many lists inside the rotation window each track was on, from the playlist
    ledger. The list being rebuilt (`exclude_key`) does not count against its own tracks: a rebuild
    before the run is not a new run. `days=None` (§DISCO) reads the whole ledger."""
    since = ((today or date.today()) - timedelta(days=days)).isoformat() if days is not None else ""
    out = {}
    for r in conn.execute("SELECT key, spec FROM playlist WHERE built_at >= ?", (since,)):
        if r["key"] == exclude_key:
            continue
        try:
            spec = json.loads(r["spec"] or "{}")
        except ValueError:
            continue
        seen = set()
        for sg in spec.get("segments") or []:
            for t in sg.get("tracks") or []:
                if t.get("id"):
                    seen.add(t["id"])
                k = song_key(t.get("artist"), t.get("title"))
                if k:
                    seen.add(k)
        for sid in seen:
            out[sid] = out.get(sid, 0) + 1
    return out


def follow_row_score(entrained, breaks, dips):
    """PURE — one song on one run, in −1.5…1: a point for a cadence that sat on the song's tempo,
    a point off for a press that said the beat lost the legs (capped at one), half a point off for
    a dip the stream shows (capped at one)."""
    return (1.0 if entrained else 0.0) - min(1.0, float(breaks or 0)) - 0.5 * min(1.0, float(dips or 0))


def follow_scores(conn):
    """§BEAT7 — the legs' verdict per track over every run read back: the read-seconds-weighted
    mean of `follow_row_score`. {spotify_id: score}; a track never run with a known tempo is absent."""
    acc = {}
    for r in conn.execute("SELECT f.spotify_id, f.entrained, f.breaks, f.dips, f.read_s, t.artist, t.title FROM follow f "
                          "LEFT JOIN track t ON t.spotify_id = f.spotify_id WHERE f.spotify_id IS NOT NULL"):
        w = float(r["read_s"] or 0)
        if w <= 0:
            continue
        sc = follow_row_score(r["entrained"], r["breaks"], r["dips"])
        for k in (r["spotify_id"], song_key(r["artist"], r["title"])):
            if k:
                a = acc.setdefault(k, [0.0, 0.0])
                a[0] += sc * w
                a[1] += w
    return {k: round(v[0] / v[1], 3) for k, v in acc.items() if v[1] > 0}


def never_run(conn, exclude_key=None):
    """§DISCO — the ids of the ListenBrainz-sourced tracks no list has ever carried (whichever
    edition; the list being rebuilt left out) and no run has read or pressed on: the fresh quota
    draws from these and from nothing else."""
    served = served_counts(conn, exclude_key=exclude_key, days=None)
    run = {r[0] for r in conn.execute("SELECT spotify_id FROM follow")} | \
          {r[0] for r in conn.execute("SELECT spotify_id FROM rating_run")}
    out = set()
    for r in conn.execute("SELECT spotify_id, artist, title FROM track WHERE discovered=2"):
        if r[0] in run or r[0] in served or (song_key(r[1], r[2]) in served):
            continue
        out.add(r[0])
    return out


def source_scores(conn):
    """§DISCO — how the legs took each source's songs: per ListenBrainz source, tracks in the pool
    with a tempo, tracks a list has carried, songs read back on a run, the share the cadence
    followed, and the read-seconds-weighted mean of the legs' score. A track from two sources
    counts for both."""
    out = {}
    served = served_counts(conn, days=None)
    rows = conn.execute("SELECT t.spotify_id, t.source, t.tempo, f.entrained, f.breaks, f.dips, f.read_s "
                        "FROM track t LEFT JOIN follow f ON f.spotify_id = t.spotify_id "
                        "WHERE t.discovered=2 AND t.source IS NOT NULL").fetchall()
    seen = set()
    for r in rows:
        for src in (r["source"] or "").split(","):
            if not src:
                continue
            o = out.setdefault(src, {"tracks": 0, "with_tempo": 0, "served": 0, "read": 0, "followed": 0,
                                     "_w": 0.0, "_s": 0.0})
            if (r["spotify_id"], src) not in seen:
                seen.add((r["spotify_id"], src))
                o["tracks"] += 1
                o["with_tempo"] += 1 if r["tempo"] else 0
                o["served"] += 1 if served.get(r["spotify_id"]) else 0
            w = float(r["read_s"] or 0)
            if r["read_s"] is not None and w > 0:
                o["read"] += 1
                o["followed"] += 1 if r["entrained"] else 0
                o["_w"] += w
                o["_s"] += w * follow_row_score(r["entrained"], r["breaks"], r["dips"])
    for o in out.values():
        o["score"] = round(o["_s"] / o["_w"], 2) if o["_w"] > 0 else None
        o["followed_share"] = round(o["followed"] / o["read"], 2) if o["read"] else None
        del o["_w"], o["_s"]
    return out


# ── Names ────────────────────────────────────────────────────────────────────
KIND_LABEL = {"easy": "Easy", "long": "Long run", "long_mp": "Long run + MP", "tempo": "Tempo",
              "interval": "Intervals", "progression": "Progression", "race_pace": "Race pace",
              "race": "Race", "strides": "Strides"}


def session_key(session):
    return f"{session.get('date')}-{session.get('kind') or 'run'}"


def playlist_name(session, target):
    d = date.fromisoformat(session["date"])
    kind = session.get("kind") or "run"
    if session.get("race"):
        return f"SH · {session.get('note') or 'Race'} · {d:%-d %b} · {session.get('km')} km · {target:.0f} spm"
    return f"SH · {d:%a %-d %b} · {KIND_LABEL.get(kind, kind.title())} {session.get('km')} km · {target:.0f} spm"


def playlist_description(segments, notes=None):
    parts = [f"{s['label'].split(' — ')[0]} {s['minutes']:.0f} min @ {s['target_spm']:.0f} bpm"
             for s in segments]
    txt = "Cadence-matched by Sparing Horse · " + " · ".join(parts)
    return txt[:MUSIC_DESC_MAX]


# ── Spotify ──────────────────────────────────────────────────────────────────
def _sp_conf():
    return {k: S._resolve_secret(S.SECRET_BY_KEY[k])[0] for k in ("spotify_client_id", "spotify_client_secret")}


def _sp_tokens():
    raw = S._stored_secret(SP_TOKENS_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _save_sp_tokens(tok):
    if S.READONLY:
        return False
    try:
        conn = S._secrets_conn()
        if tok:
            conn.execute("INSERT INTO secret(key,value) VALUES(?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (SP_TOKENS_KEY, S._enc(json.dumps(tok))))
        else:
            conn.execute("DELETE FROM secret WHERE key=?", (SP_TOKENS_KEY,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[music] token store write failed: {e}")
        return False


def _sp_token_request(form):
    conf = _sp_conf()
    r = requests.post(f"{SP_ACCOUNTS}/api/token", data=form,
                      auth=(conf["spotify_client_id"], conf["spotify_client_secret"]),
                      headers={"Accept": "application/json", "User-Agent": S.USER_AGENT},
                      timeout=MUSIC_HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"token endpoint {r.status_code}: {r.text[:200]}")
    d = r.json()
    return {"access_token": d["access_token"], "refresh_token": d.get("refresh_token"),
            "expires_at": time.time() + float(d.get("expires_in", 3600)), "scope": d.get("scope")}


def sp_access_token():
    """A valid access token, refreshed through the stored refresh token near expiry; None when not
    connected (or the refresh failed — reconnect from the page)."""
    tok = _sp_tokens()
    if not tok:
        return None
    if time.time() < tok.get("expires_at", 0) - 120:
        return tok["access_token"]
    try:
        new = _sp_token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        if not new.get("refresh_token"):
            new["refresh_token"] = tok["refresh_token"]
        new["user"] = tok.get("user")
        _save_sp_tokens(new)
        return new["access_token"]
    except Exception as e:
        print(f"[music] spotify token refresh failed: {e}")
        return None


def _sp_call(method, path, token, params=None, body=None, retry=True):
    """One Web API call. A 429 is waited out once (Retry-After, capped) — the library refresh makes
    hundreds of these and the limiter is the normal case, not an error."""
    r = requests.request(method, f"{SP_API}{path}", params=params, json=body,
                         headers={"Authorization": f"Bearer {token}", "User-Agent": S.USER_AGENT},
                         timeout=MUSIC_HTTP_TIMEOUT)
    if r.status_code == 429 and retry:
        try:
            wait = min(float(r.headers.get("Retry-After", "2") or 2), 15.0)
        except ValueError:              # Retry-After MAY be an HTTP-date instead of a delta-seconds int
            wait = 2.0
        time.sleep(wait)
        return _sp_call(method, path, token, params, body, retry=False)
    return r


def sp_me(token):
    r = _sp_call("GET", "/me", token)
    return r.json() if r.status_code == 200 else {}


def sp_saved_tracks(token, limit=MUSIC_LIBRARY_MAX):
    out, offset = [], 0
    while offset < limit:
        r = _sp_call("GET", "/me/tracks", token, params={"limit": SP_PAGE, "offset": offset})
        if r.status_code != 200:
            break
        items = r.json().get("items") or []
        for it in items:
            t = it.get("track") or {}
            if t.get("id"):
                out.append(_sp_track_row(t))
        if len(items) < SP_PAGE:
            break
        offset += SP_PAGE
    return out


def sp_top_tracks(token):
    out = []
    for rng in ("short_term", "medium_term", "long_term"):
        r = _sp_call("GET", "/me/top/tracks", token, params={"time_range": rng, "limit": SP_PAGE})
        if r.status_code != 200:
            continue
        for rank, t in enumerate(r.json().get("items") or [], 1):
            if t.get("id"):
                out.append({**_sp_track_row(t), "top_rank": rank, "range": rng})
    return out


def sp_own_playlists(token):
    """The athlete's own playlists (id, name), the ones this module wrote left out. A runner who
    built 160/170/175/180-bpm playlists by hand has already curated the pool the picker wants."""
    out, offset = [], 0
    while len(out) < MUSIC_PLAYLISTS_MAX:
        r = _sp_call("GET", "/me/playlists", token, params={"limit": SP_PAGE, "offset": offset})
        if r.status_code != 200:
            break
        items = r.json().get("items") or []
        for p in items:
            if p.get("id") and not str(p.get("name") or "").startswith("SH · "):
                out.append({"id": p["id"], "name": p.get("name") or ""})
        if len(items) < SP_PAGE:
            break
        offset += SP_PAGE
    return out[:MUSIC_PLAYLISTS_MAX]


def sp_playlist_tracks(token, playlist_id, limit=MUSIC_PLAYLIST_ITEMS_MAX):
    out, offset = [], 0
    while offset < limit:
        r = _sp_call("GET", f"/playlists/{playlist_id}/items", token, params={"limit": SP_PAGE, "offset": offset})
        if r.status_code != 200:
            break
        items = r.json().get("items") or []
        for it in items:
            t = it.get("track") or it.get("item") or {}
            if t.get("id") and t.get("type", "track") == "track":
                out.append(_sp_track_row(t))
        if len(items) < SP_PAGE:
            break
        offset += SP_PAGE
    return out


def _sp_track_row(t):
    return {"id": t["id"], "title": t.get("name") or "", "duration_ms": t.get("duration_ms") or 0,
            "artist": ", ".join(a.get("name", "") for a in (t.get("artists") or [])),
            "artists": [a.get("name", "") for a in (t.get("artists") or [])]}


def _norm(s):
    s = re.sub(r"\(.*?\)|\[.*?\]", "", (s or "").lower())
    s = re.sub(r"\bfeat\.?\b.*$|\bft\.?\b.*$", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def sp_search_track(token, title, artist, strict=False):
    """The Spotify track for a scrobble: the first result whose artist matches, else the first.
    `strict` (§DISCO) returns None instead of the first: a song from another listener's history
    must not enter the pool under a stranger's name."""
    q = f"track:{title} artist:{artist}"
    r = _sp_call("GET", "/search", token, params={"q": q, "type": "track", "limit": 5})
    if r.status_code != 200:
        return None
    items = ((r.json().get("tracks") or {}).get("items")) or []
    want = _norm(artist)
    for t in items:
        if any(_norm(a.get("name")) == want for a in t.get("artists") or []):
            return _sp_track_row(t)
    if strict:
        return None
    return _sp_track_row(items[0]) if items else None


def sp_write_playlist(token, playlist_id, name, description, uris):
    """Create or replace: an existing playlist (by stored id) gets its name, description and items
    replaced; a missing one is created private. Returns (id, url)."""
    if playlist_id:
        r = _sp_call("PUT", f"/playlists/{playlist_id}", token, body={"name": name, "description": description})
        if r.status_code == 404:
            pass    # the playlist was deleted on Spotify — fall through and create a fresh one
        elif r.status_code not in (200, 201):
            raise RuntimeError(f"rename playlist {r.status_code}: {r.text[:200]}")
        else:
            r2 = _sp_call("PUT", f"/playlists/{playlist_id}/items", token, body={"uris": uris[:MUSIC_PLAYLIST_CHUNK]})
            if r2.status_code not in (200, 201):
                raise RuntimeError(f"replace playlist items {r2.status_code}: {r2.text[:200]}")
            for i in range(MUSIC_PLAYLIST_CHUNK, len(uris), MUSIC_PLAYLIST_CHUNK):
                _sp_call("POST", f"/playlists/{playlist_id}/items", token,
                         body={"uris": uris[i:i + MUSIC_PLAYLIST_CHUNK]})
            return playlist_id, f"https://open.spotify.com/playlist/{playlist_id}"
    r = _sp_call("POST", "/me/playlists", token, body={"name": name, "public": False, "description": description})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"create playlist {r.status_code}: {r.text[:200]}")
    pid = r.json().get("id")
    for i in range(0, len(uris), MUSIC_PLAYLIST_CHUNK):
        r3 = _sp_call("POST", f"/playlists/{pid}/items", token, body={"uris": uris[i:i + MUSIC_PLAYLIST_CHUNK]})
        if r3.status_code not in (200, 201):
            raise RuntimeError(f"add items {r3.status_code}: {r3.text[:200]}")
    return pid, f"https://open.spotify.com/playlist/{pid}"


def spotify_status():
    conf = _sp_conf()
    tok = _sp_tokens()
    granted = set(((tok or {}).get("scope") or "").split())
    missing = sorted(set(SP_SCOPES.split()) - granted) if tok else []
    return {"configured": all(conf.values()), "connected": bool(tok), "user": (tok or {}).get("user"),
            "scopes_missing": missing, "needs_reconnect": bool(tok and missing)}


# ── §BEAT2 (0.62.0) — the play history, and reading a run back song by song ──
def sp_recently_played(token):
    """Spotify's last 50 plays: (played_at ISO, track id, title, artist, duration_ms). Spotify keeps
    only fifty, so the caller stores them; a run's songs are gone from Spotify by the next evening."""
    r = _sp_call("GET", "/me/player/recently-played", token, params={"limit": 50})
    if r.status_code != 200:
        raise RuntimeError(f"recently-played {r.status_code}: {r.text[:160]}")
    out = []
    for it in r.json().get("items") or []:
        t = it.get("track") or {}
        if t.get("id") and it.get("played_at"):
            out.append({"played_at": it["played_at"], **_sp_track_row(t)})
    return out


def pull_played():
    """Store the last 50 plays (idempotent on played_at); returns how many were new. Silent when not
    connected or the scope was never granted — the page says which."""
    token = sp_access_token()
    st = spotify_status()
    if not token or "user-read-recently-played" in st["scopes_missing"]:
        return 0
    rows = sp_recently_played(token)
    conn = _mdb()
    try:
        before = conn.execute("SELECT COUNT(*) FROM played").fetchone()[0]
        for r in rows:
            conn.execute("INSERT OR IGNORE INTO played(played_at, spotify_id, title, artist, duration_ms, pulled_at) "
                         "VALUES(?,?,?,?,?,?)", (r["played_at"], r["id"], r["title"], r["artist"], r["duration_ms"], _now_iso()))
            _upsert_track(conn, r)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=MUSIC_PLAYED_KEEP_DAYS)).isoformat()
        conn.execute("DELETE FROM played WHERE played_at < ?", (cutoff,))
        conn.commit()
        return conn.execute("SELECT COUNT(*) FROM played").fetchone()[0] - before
    finally:
        conn.close()


def _epoch(iso):
    """Seconds since the epoch for an ISO stamp with or without an offset ('Z' included)."""
    s = str(iso).replace("Z", "+00:00")
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def _seg_role(seg):
    """The role a playlist segment plays for the set-aside rule: 'work' for the efforts that take
    the work step, 'easy' otherwise. A spec written before 0.65.0 carries no effort; its label says."""
    ef = seg.get("effort")
    if ef:
        return "work" if ef in MUSIC_WORK_EFFORTS else "easy"
    lb = (seg.get("label") or "").lower()
    return "work" if lb.startswith(("work", "reps")) else "easy"


def _seg_is_block(seg):
    """§BEAT9 — whether a playlist segment is a coalesced reps block (reps and recoveries under one
    target). A spec written before 0.68.5 carries no `reps`; its label says."""
    return bool(seg.get("reps")) or (seg.get("label") or "").lower().startswith("reps")


def set_aside(conn):
    """§BEAT5 — tracks earlier runs have ruled out, by segment role, from the run verdicts: a track
    SKIPPED from the headset is set aside for the role it was skipped in (both roles when the run
    had no playlist to say which). §BEAT7 — two presses say "never again": set aside for both roles.
    §BEAT14 — a verdict given after the run ("leave it out") sets aside for both roles too, exactly
    as a double press does. §BEAT16 — "keep" pardons the run's own reading: a (run_id, spotify_id)
    marked kept is skipped here even though its `rating_run` row still says skip or never. Rows
    written under the older press protocol ('pushes' / 'relaxes', up to 0.65.x) rule nothing.
    Returns {spotify_id: set(roles)}."""
    kept = {(r["run_id"], r["spotify_id"])
            for r in conn.execute("SELECT run_id, spotify_id FROM manual_verdict WHERE rating='keep'")}
    out = {}
    for r in conn.execute("SELECT run_id, spotify_id, rating, role FROM rating_run"):
        if (r["run_id"], r["spotify_id"]) in kept:
            continue
        if r["rating"] == "skip":
            roles = {r["role"]} if r["role"] in ("work", "easy") else {"work", "easy"}
        elif r["rating"] == "never":
            roles = {"work", "easy"}
        else:
            continue
        if r["spotify_id"]:
            out.setdefault(r["spotify_id"], set()).update(roles)
    for r in conn.execute("SELECT spotify_id FROM manual_verdict WHERE rating='never'"):
        if r["spotify_id"]:
            out.setdefault(r["spotify_id"], set()).update({"work", "easy"})
    return out


def apply_manual_verdicts(conn, run_id, payload):
    """§BEAT14 — overlay the athlete's own after-the-run verdicts onto a read-back payload: a song
    marked "never" in `manual_verdict` for this run reads as `run_rating` "never" and carries
    `manual: "never"`, on top of whatever the run itself produced. §BEAT16 — "keep" leaves the run's
    own reading in place (`run_rating`, `skipped`, `breaks` untouched, so the page still shows what
    the run itself found) and only sets `manual: "keep"`, uncounting the song from `set_aside` when
    the run had counted it there (a skip, or a double press). Called both where a read-back is
    computed and where a stored one is served, so a verdict given after the payload was stored still
    shows — the manual table survives a recompute by construction, since it lives beside
    `rating_run`, not in it."""
    rows = conn.execute("SELECT spotify_id, rating FROM manual_verdict WHERE run_id=? AND rating IN ('never', 'keep')",
                        (run_id,))
    never, kept = set(), set()
    for r in rows:
        (never if r["rating"] == "never" else kept).add(r["spotify_id"])
    if not never and not kept:
        return payload
    for sg in payload.get("songs") or []:
        sid = sg.get("spotify_id")
        if sid in never:
            if sg.get("run_rating") != "never":
                payload["set_aside"] = payload.get("set_aside", 0) + 1
            sg["run_rating"] = "never"
            sg["manual"] = "never"
        elif sid in kept:
            sg["manual"] = "keep"
            if sg.get("skipped") or sg.get("run_rating") == "never":
                payload["set_aside"] = max(0, payload.get("set_aside", 0) - 1)
    return payload


def align_songs(run_start, run_end, played, samples):
    """PURE — which song was playing when, and what the legs did during it.

    `played`: rows with played_at (ISO), duration_ms, title, artist, spotify_id — `played_at` is the
    END of the play (SP_PLAYED_AT_IS_END), so a song starts its length before that, or where the
    previous play ended if that is later: a track skipped from the headset ends early, and the next
    one starts there — the skip is recorded as `skipped`. `samples`: (t_abs_seconds, spm, dist_m)
    from the run's streams. A song counts when at least MUSIC_SONG_MIN_S of it fall inside the run;
    its first MUSIC_SONG_SEAM_S seconds are the seam from the previous song and are left out.
    Returns one row per song in play order: start offset into the run, seconds read, mean spm,
    pace from distance over time, and whether the play was cut short."""
    out, prev_end = [], None
    for p in sorted(played, key=lambda r: r["played_at"]):
        dur = (p.get("duration_ms") or 0) / 1000.0
        if dur <= 0:
            continue
        end = _epoch(p["played_at"])
        start = end - dur if SP_PLAYED_AT_IS_END else end
        if SP_PLAYED_AT_IS_END and prev_end is not None and prev_end > start:
            start = prev_end                      # the previous track ran into this one's slot: a skip
        else:
            end = start + dur
        skipped = (end - start) < dur - 5
        prev_end = end
        lo, hi = max(start, run_start), min(end, run_end)
        if hi - lo < MUSIC_SONG_MIN_S:
            continue
        rd = _read_span(lo, hi, samples)
        if rd is None:
            continue
        out.append({"spotify_id": p.get("spotify_id"), "title": p.get("title"), "artist": p.get("artist"),
                    "start_s": round(lo - run_start), "read_s": round(hi - lo - MUSIC_SONG_SEAM_S),
                    **rd, "skipped": skipped})
    return out


def work_spans(st, run_start, run_end, slack_s=MUSIC_STRUCT_SLACK_S):
    """PURE — §BEAT9: the absolute (lo, hi) second spans of the reps in a run, from its §RD structure
    (`segments` in running order, each `sec` long, role 'work' = a rep). Frames are contiguous
    15-second slabs from the run's first sample, so the offsets are the running sum of `sec`; a
    structure whose segments do not add up to the run's length within `slack_s` (a pause the frames
    dropped) is not trusted, and neither is a missing, failed or rep-less one — [] then, and the
    caller reads the whole window as before."""
    if not st or not st.get("ok") or not st.get("segments"):
        return []
    segs = st["segments"]
    spans, t = [], run_start
    for s in segs:
        sec = s.get("sec") or 0
        if s.get("role") == "work":
            spans.append((t, t + sec))
        t += sec
    if abs(sum(s.get("sec") or 0 for s in segs) - (run_end - run_start)) > slack_s:
        return []
    return spans


def _read_span(lo, hi, samples, spans=None):
    """The legs between two absolute seconds, the seam at the head excluded: mean spm, pace from the
    distance covered over the window's time (not sample-wise speed), the sample count, steadiness and
    dips — or None when fewer than five samples fall inside. One reader for the aligned songs (§BEAT2),
    the inferred ones (§BEAT8), and a reps-block song read over its rep spans only (§BEAT9): `spans`
    ((lo, hi) absolute seconds, from `work_spans`) keeps only the samples that also fall inside one of
    them, and pace is summed span by span — each span contributes its own (last − first) seconds and
    metres, a span under two selected samples or with no distance contributes none — rather than over
    the window's own ends, since the spans are not contiguous. Without `spans` the read is unchanged."""
    sel = [s for s in samples if lo + MUSIC_SONG_SEAM_S <= s[0] < hi and s[1]]
    if spans:
        sel = [s for s in sel if any(a <= s[0] < b for a, b in spans)]
    if len(sel) < 5:
        return None
    spm = statistics.mean(s[1] for s in sel)
    if spans:
        secs = metres = 0.0
        for a, b in spans:
            span_sel = [s for s in sel if a <= s[0] < b]
            if len(span_sel) < 2 or span_sel[0][2] is None or span_sel[-1][2] is None:
                continue
            secs += span_sel[-1][0] - span_sel[0][0]
            metres += span_sel[-1][2] - span_sel[0][2]
        pace = secs / (metres / 1000.0) if metres > 0 else None
    else:
        d0, d1 = sel[0][2], sel[-1][2]
        secs = sel[-1][0] - sel[0][0]
        pace = secs / ((d1 - d0) / 1000.0) if d1 is not None and d0 is not None and d1 > d0 and secs > 0 else None
    steady, dips, dip_s = cadence_steadiness(sel)
    return {"spm": round(spm, 1), "pace_sec": round(pace) if pace else None, "n": len(sel),
            "steady": steady, "dips": dips, "dip_s": dip_s}


def infer_gap_songs(songs, spec, durations, run_start, run_end, samples, min_gap_s=MUSIC_GAP_MIN_S):
    """PURE — §BEAT8 (0.68.3): the songs the play history dropped, read from the list order.
    Spotify's recently-played omits a play skipped early and, some evenings, a full one. Run 207508555
    (2026-09-12): thirteen songs aligned, four gaps of 45–190 s, each between two aligned songs that sit
    two apart on the list, and three of the four lap presses fell inside them — attributed to no song.
    A gap between two aligned songs can only have held what the list holds between them, so it is read
    as that: the list tracks strictly between the two neighbours (before the first for a leading gap,
    after the last for a trailing one), not already aligned, take the gap in list order, each
    `min(its duration, what is left)`, until less than `min_gap_s` remains. `songs` are `align_songs`
    rows; `durations` is {spotify_id: seconds}. Returns rows shaped like `align_songs` output with
    `inferred` True, `skipped` when the next song cut the window short (a window cut by the run's end
    is not a skip). A window with too few samples is consumed but not returned — the same rule as an
    aligned song. Nothing is inferred without a list, where a neighbour is off the list or the list
    was played out of order, or into a gap under `min_gap_s`."""
    order = [t for sgp in (spec.get("segments") or []) for t in (sgp.get("tracks") or [])]
    if not order or not songs:
        return []
    pos_by_id = {t.get("id"): i for i, t in enumerate(order) if t.get("id")}
    pos_by_name = {(_norm(t.get("artist")), _norm(t.get("title"))): i for i, t in enumerate(order)}

    def _pos(sg):
        p = pos_by_id.get(sg.get("spotify_id"))
        return p if p is not None else pos_by_name.get((_norm(sg.get("artist")), _norm(sg.get("title"))))

    aligned = sorted(songs, key=lambda x: x["start_s"])
    taken = {_pos(x) for x in aligned} - {None}
    total = run_end - run_start
    out = []
    bounds = [(None, aligned[0])] + list(zip(aligned, aligned[1:])) + [(aligned[-1], None)]
    for left, right in bounds:
        g0 = (left["start_s"] + left["read_s"] + MUSIC_SONG_SEAM_S) if left else 0
        g1 = right["start_s"] if right else total
        if g1 - g0 < min_gap_s:
            continue
        lp = _pos(left) if left else -1
        rp = _pos(right) if right else len(order)
        if lp is None or rp is None or rp <= lp + 1:
            continue                      # a neighbour off the list, or the list played out of order
        at = g0
        for i in range(lp + 1, rp):
            if g1 - at < min_gap_s:
                break
            if i in taken:
                continue
            t = order[i]
            dur = durations.get(t.get("id"))
            if not dur or dur <= 0:
                break                     # unsized: the rest of the gap cannot be laid out
            win = min(dur, g1 - at)
            rd = _read_span(run_start + at, run_start + at + win, samples)
            if rd is not None:
                out.append({"spotify_id": t.get("id"), "title": t.get("title"), "artist": t.get("artist"),
                            "start_s": round(at), "read_s": round(win - MUSIC_SONG_SEAM_S), **rd,
                            "skipped": bool(right) and win < dur - MUSIC_SONG_SEAM_S, "inferred": True})
            taken.add(i)
            at += win
    return out


def cadence_steadiness(sel):
    """PURE — §BEAT7: how steadily the legs held a song, from its samples [(t_abs, spm, dist)]:
    the share of samples within MUSIC_STEADY_SPM of the song's median, and the dips — spans at
    least MUSIC_DIP_MIN_S long where the cadence sat MUSIC_DIP_SPM or more under that median (a fill
    that ends off the beat, a chorus that changes tempo: the stream shows them as drops). Returns
    (steady 0–1, dips, seconds in dips)."""
    vals = [s[1] for s in sel if s[1]]
    if len(vals) < 5:
        return None, 0, 0
    med = statistics.median(vals)
    steady = sum(1 for v in vals if abs(v - med) <= MUSIC_STEADY_SPM) / len(vals)
    dips, dip_s, start, last = 0, 0, None, None
    for t, v, _ in sel:
        if v and v < med - MUSIC_DIP_SPM:
            start, last = (t if start is None else start), t
        else:
            if start is not None and last - start >= MUSIC_DIP_MIN_S:
                dips, dip_s = dips + 1, dip_s + round(last - start)
            start = last = None
    if start is not None and last - start >= MUSIC_DIP_MIN_S:
        dips, dip_s = dips + 1, dip_s + round(last - start)
    return round(steady, 3), dips, dip_s


# ── §BEAT3 (0.63.0) — the watch's lap presses as in-run verdicts, read from the original FIT ──
def fetch_fit(run_id):
    """The original FIT Runalyze holds for a run (its own REST serves it), or None. Streamed with a
    cap (MUSIC_FIT_MAX_BYTES) rather than buffered whole — an unbounded body has no business being
    read fully into memory just because the server sent a 200."""
    tok = S.config().runalyze_token
    if not tok:
        return None
    r = requests.get(f"{S.RUNALYZE_BASE}/activity/{int(run_id)}/fit",
                     headers={"token": tok, "Accept": "*/*", "User-Agent": S.USER_AGENT},
                     timeout=MUSIC_HTTP_TIMEOUT, stream=True)
    if r.status_code != 200:
        return None
    data = bytearray()
    for chunk in r.iter_content(65536):
        data += chunk
        if len(data) > MUSIC_FIT_MAX_BYTES:
            print(f"[music] FIT for run {run_id} exceeded {MUSIC_FIT_MAX_BYTES} bytes — abandoned")
            r.close()
            return None
    if b".FIT" not in bytes(data[:16]):
        return None
    return bytes(data)


def parse_fit(data):
    """→ {start, samples [(t_abs, spm, dist_m)], laps [(t_end_abs, dist_m, timer_s, trigger)]} from a
    FIT, or None. Records give the one-second cadence (a per-leg count is doubled by magnitude, like
    everywhere else here). Laps are returned raw: which of them are presses is `press_times`' call —
    the file Runalyze serves is its own re-export (manufacturer 'development', one lap per kilometre
    and a final stub, no trigger field), not the watch's, so a lap is not a press by itself."""
    try:
        import fitdecode
    except ImportError:
        return None
    import io
    samples, laps, start = [], [], None
    with fitdecode.FitReader(io.BytesIO(data)) as f:
        for fr in f:
            if fr.frame_type != fitdecode.FIT_FRAME_DATA:
                continue
            g = lambda k: fr.get_value(k) if fr.has_field(k) else None
            if fr.name == "record":
                ts = g("timestamp")
                if ts is None:
                    continue
                t = ts.timestamp()
                start = t if start is None else start
                cad, dist = g("cadence"), g("distance")
                spm = None
                if cad is not None:
                    spm = float(cad) + float(g("fractional_cadence") or 0)
                    if spm < MUSIC_CAD_SPM_RANGE[0]:
                        spm *= 2
                samples.append((t, spm, float(dist) if dist is not None else None))
            elif fr.name == "lap":
                ts = g("timestamp")
                if ts is not None:
                    laps.append((ts.timestamp(), float(g("total_distance") or 0), float(g("total_timer_time") or 0),
                                 g("lap_trigger")))
            elif fr.name == "session" and g("start_time") is not None:
                start = g("start_time").timestamp()
    return {"start": start, "samples": samples, "laps": laps} if samples else None


MUSIC_AUTOLAP_TOL = 0.05       # laps within ±5 % of the modal distance are the watch's automatic ones…
MUSIC_AUTOLAP_SHARE = 0.6      # …when at least this share of the laps sit there; a press cuts one short
MUSIC_AUTOLAP_UNITS_M = (1000.0, 1609.344)   # §BEAT13 — km/mile: the units a watch autolaps on, for the
#                                short-run fallback below, where there are too few laps to find a modal one
MUSIC_AUTOLAP_UNIT_TOL = 0.02  # §BEAT15: the under-3-laps fallback's tolerance as a fraction of the UNIT
#                                itself (±20 m on a km, ±32 m on a mile). A watch's own autolap lands
#                                within metres of the unit (19 Sep: ten laps at 995–1003 m). The old ±5 %
#                                of the MULTIPLE grew with the lap: it called 3272 m "2 miles" (±161 m)
#                                and 11598 m "12 km" (±600 m), and past ~9.5 km every lap was near some
#                                kilometre — the 20 Sep press was lost to it.
MUSIC_GUIDE_LAP_TOL_S = 5.0    # §BEAT15: a lap whose activity timer sits within this of a guide step
#                                lap (`SparingHorse.guide_lap_offsets`) is the guide's, never a press.
MUSIC_GUIDE_LAP_DECODE_WINDOW_S = (45.0, 5.0)   # §BEAT15 — a lap from this long BEFORE to this long
#                                AFTER a decoded work span's start is the guide's lap too: the plan's
#                                offsets drift on a lived day (the 20 Sep session re-priced 95 → 94 min
#                                after the run, the wrist's guide still lapped at 95:00) and can
#                                disagree in count (the wrist keeps the guide it first downloaded),
#                                while the §RD decode reads the run itself; the window is asymmetric
#                                because the legs speed up AFTER the beep, so the decode's start lags
#                                the lap (30 s on 20 Sep: lap 5700, decoded work start 5730).
MUSIC_LAP_END_GRACE_S = 15     # a lap ending within this of the run's end is the closing stub, never a press
MUSIC_FIT_CLOCK_TOL_S = 60     # §BEAT6 (0.65.1) — a FIT whose start sits further than this from the run's own
#                                start is re-clocked onto it: Runalyze's re-export writes the local wall time
#                                into the timestamps, two hours late on the 09-06 and 09-08 files


def anchor_fit(fit, run_start):
    """PURE — the FIT's timeline anchored to the run's own start. The file Runalyze serves is its
    own re-export, and it writes the LOCAL wall time into the timestamps: the 09-06 and 09-08 files
    both read two hours late against the activity's start (which carries its offset), so every song
    fell into the wrong window — the tempo's read-back found no song at all, the long run's matched
    the evening's couch listening. When the file's start sits further than MUSIC_FIT_CLOCK_TOL_S
    from the run's, every sample and lap is shifted by the difference. Returns (fit, shift_s)."""
    if not fit or not fit.get("start") or run_start is None:
        return fit, 0.0
    shift = float(run_start) - float(fit["start"])
    if abs(shift) <= MUSIC_FIT_CLOCK_TOL_S:
        return fit, 0.0
    return {"start": float(fit["start"]) + shift,
            "samples": [(t + shift, spm, d) for t, spm, d in fit["samples"]],
            "laps": [(t + shift, d, timer, trig) for t, d, timer, trig in fit["laps"]]}, shift


def _near_autolap_unit(dist_m):
    """§BEAT13 — is `dist_m` close to a whole (≥ 1) multiple of a km or a mile? §BEAT15 — the
    tolerance is MUSIC_AUTOLAP_UNIT_TOL of the UNIT ITSELF, not of the multiple: a watch's own
    autolap lands within metres of the unit whatever the lap count (995–1003 m laps on a ten-lap
    run), while a tolerance scaled by the multiple grows without bound — at 5 % of the multiple,
    3272 m read as "2 miles" (±161 m) and every lap past ~9.5 km was "near" some kilometre. The
    zero-th multiple is excluded: a lap of a few metres sits "near" 0 × anything, which is not the
    watch's pattern, just a short lap."""
    for unit in MUSIC_AUTOLAP_UNITS_M:
        k = round(dist_m / unit)
        if k >= 1 and abs(dist_m - k * unit) <= MUSIC_AUTOLAP_UNIT_TOL * unit:
            return True
    return False


def guide_laid(laps, guide_s=(), decoded_s=()):
    """PURE — §BEAT15: one bool per lap, in file order, true when the lap is the guide's own lap,
    not a press. A lap's own position on the activity timer is the running sum of `timer_s` over the
    laps in file order; a lap whose `timer_s` is None has no known position and is never laid. Two
    independent sources say a position is the guide's: within MUSIC_GUIDE_LAP_TOL_S of a value in
    `guide_s` (the CURRENT plan's own step offsets, `SparingHorse.guide_lap_offsets`), or within
    MUSIC_GUIDE_LAP_DECODE_WINDOW_S of a value in `decoded_s` (the run's own §RD-decoded work-span
    starts, in the same seconds-from-run-start frame as `guide_s`). The plan drifts on a lived day —
    it can be re-priced after the run (20 Sep: 95 min became 94) and it can disagree with the wrist
    in COUNT (an update never re-downloads, so the watch keeps whichever guide it first fetched) —
    while the decode reads what the run itself did, so the two sources catch different failures of
    the same kind."""
    before, after = MUSIC_GUIDE_LAP_DECODE_WINDOW_S
    pos, out = 0.0, []
    for l in laps:
        timer = l[2]
        if timer is None:
            out.append(False)
            continue
        pos += timer
        laid = any(abs(pos - g) <= MUSIC_GUIDE_LAP_TOL_S for g in guide_s) \
            or any(d - before <= pos <= d + after for d in decoded_s)
        out.append(laid)
    return out


def press_times(laps, run_end, guide_s=(), decoded_s=()):
    """PURE — which laps are presses. `laps`: (t_end_abs, dist_m, timer_s, trigger). A lap whose
    trigger says 'manual' is a press; one with any other named trigger is not; without a trigger
    (the re-exported file), the watch's automatic kilometre laps are recognised by their
    regularity — most laps within ±5 % of the modal distance — and only the laps cut short of that
    count, since a press ends the running lap early. Irregular laps throughout are all presses.
    §BEAT13 — with fewer than 3 untriggered laps the modal rule has nothing to average over (a very
    short run, or Runalyze's splits fallback with one or two auto-kilometre laps), so each
    untriggered lap is judged on its own instead: one that lands near a whole multiple of
    MUSIC_AUTOLAP_UNITS_M (km or mile) is the watch's own autolap, not a press — a lap landing on
    the boundary itself (the zero-th multiple) is not a pattern and stays a press. A lap closing
    within MUSIC_LAP_END_GRACE_S of the run's end is the final stub.

    §BEAT15 — `guide_s`/`decoded_s`: two sources for the guide's own laps (`guide_laid`), for the
    reps day and long-run-with-a-work-step cases where the WATCH cuts a lap the athlete never
    pressed. A lap `guide_laid` marks true is dropped before anything else — before the end-grace
    filter, out of the modal vote, out of the fallback, and even when its trigger reads 'manual' (an
    original FIT names a createManualLap lap 'manual' too, guide-laid or not). The 20 Sep long run
    carried both a genuine press (a single lap at 1215 s) and the guide's own lap at the
    marathon-pace step's start (5700 s, 11598 m — read as "12 km" by the old multiple-scaled
    tolerance and lost either way, and off the re-priced plan's own offset by 60 s, caught instead by
    the §RD decode's work span at 5730 s): dropping the guide's lap by position, rather than by
    shape, recovers the press regardless of either source drifting on its own."""
    laid = guide_laid(laps, guide_s, decoded_s)
    laps = [l for l, g in zip(laps, laid) if not g]
    body = [l for l in laps if run_end is None or l[0] < run_end - MUSIC_LAP_END_GRACE_S]
    out = []
    untriggered = [l for l in body if l[3] is None]
    auto, modal = False, None
    if len(untriggered) >= 3:
        ds = sorted(l[1] for l in untriggered)
        modal = ds[len(ds) // 2]
        near = sum(1 for d in ds if modal and abs(d - modal) <= MUSIC_AUTOLAP_TOL * modal)
        auto = modal >= 400 and near / len(ds) >= MUSIC_AUTOLAP_SHARE
    for t, d, timer, trig in body:
        if trig == "manual":
            out.append(t)
        elif trig is None:
            if len(untriggered) < 3:
                if not _near_autolap_unit(d):
                    out.append(t)
            elif not auto or (modal and d < (1 - 2 * MUSIC_AUTOLAP_TOL) * modal):
                out.append(t)
    return sorted(out)


def press_times_from_splits(splits, run_start, guide_s=(), decoded_s=()):
    """The same, from Runalyze's own `splits` (the rounds of the original file): tolerant of the
    field names, cumulative durations give the end times, distances feed the same autolap rule.
    `guide_s`/`decoded_s` pass through to `press_times` unchanged."""
    laps, t = [], run_start
    for sp in splits or []:
        if not isinstance(sp, dict):
            continue
        dur = next((sp[k] for k in ("duration", "time", "seconds", "s") if isinstance(sp.get(k), (int, float))), None)
        dist = next((sp[k] for k in ("distance", "km", "meters", "m") if isinstance(sp.get(k), (int, float))), 0)
        if dur is None:
            continue
        t += float(dur)
        laps.append((t, float(dist) * (1000.0 if dist and dist < 100 else 1.0), float(dur), None))
    return press_times(laps, None, guide_s=guide_s, decoded_s=decoded_s)


def press_ratings(presses, songs, run_start, pair_s=MUSIC_PRESS_PAIR_S, legacy=False):
    """PURE — lap presses → in-run verdicts. Presses within `pair_s` of each other are one signal.
    §BEAT7 (the 2026-09-11 protocol): a single press says "the beat lost me here" — a BREAK, stamped
    at the second it happened so the stream can be looked at there; two or more say "never again".
    A song may carry several breaks; one "never" outranks them all. The verdict lands on the song
    playing at the first press of the group (its span: start, seam included, to its end). Returns
    ({spotify_id: rating}, events) and writes `presses` / `breaks` / `run_rating` onto the songs.
    `legacy` reads a run from before the protocol changed under the old words — one press "pushes",
    two "relaxes" — which rule nothing and count no break."""
    groups, cur = [], []
    for t in sorted(presses):
        if cur and t - cur[-1] <= pair_s:
            cur.append(t)
        else:
            if cur:
                groups.append(cur)
            cur = [t]
    if cur:
        groups.append(cur)
    per, events = {}, []
    for gp in groups:
        at = gp[0] - run_start
        song = next((s for s in songs if s["start_s"] <= at < s["start_s"] + s["read_s"] + MUSIC_SONG_SEAM_S), None)
        rating = ("pushes" if len(gp) == 1 else "relaxes") if legacy else ("break" if len(gp) == 1 else "never")
        events.append({"at_s": round(at), "presses": len(gp), "rating": rating,
                       "spotify_id": song["spotify_id"] if song else None, "title": song["title"] if song else None})
        if song:
            song["presses"] = (song.get("presses") or 0) + len(gp)
            if rating == "break":
                song["breaks"] = (song.get("breaks") or 0) + 1
                song.setdefault("break_at", []).append(round(at - song["start_s"]))
            if per.get(song["spotify_id"]) != "never":
                per[song["spotify_id"]] = rating
            song["run_rating"] = per[song["spotify_id"]]
    return per, events


def playlist_membership(spec):
    """PURE — §BEAT7: which segment each track of a playlist spec belongs to, by id AND by name.
    Spotify may report a play under another edition's id than the one on the list (Boys Don't Cry:
    two editions in the library, one on the list, the other in the play history, on every run),
    so a song is matched by id first and by normalised (artist, title) after. Each value is
    (label, target_spm, role, tempo, hit, block)."""
    by_id, by_name = {}, {}
    for sgp in spec.get("segments") or []:
        for t in sgp.get("tracks") or []:
            val = (sgp.get("label"), sgp.get("target_spm"), _seg_role(sgp), t.get("tempo"), t.get("hit"),
                   _seg_is_block(sgp))
            by_id.setdefault(t.get("id"), val)
            by_name.setdefault((_norm(t.get("artist")), _norm(t.get("title"))), val)
    return by_id, by_name


def segment_for(song, by_id, by_name):
    """The membership entry a played song matches, by id or by name, or None."""
    return by_id.get(song.get("spotify_id")) or by_name.get((_norm(song.get("artist")), _norm(song.get("title"))))


def readback(db, run_id):
    """One run, song by song: the stored plays that overlap it, the songs the play history dropped read
    from the list order (§BEAT8), the cadence stream between the seams, the rating each song carries,
    and the playlist target if a playlist was built for that day."""
    a = db.execute("SELECT id, date, date_time, distance, duration, raw FROM activities WHERE id=?", (run_id,)).fetchone()
    if not a:
        return {"ok": False, "error": "no such run"}
    src = None
    try:
        src = json.loads(a["raw"] or "{}").get("source")
    except Exception:
        pass
    run_start = _epoch(a["date_time"])
    run_end = run_start + float(a["duration"] or 0)
    conn = _mdb()
    try:
        lo = datetime.fromtimestamp(run_start - 4 * 3600, timezone.utc).isoformat()
        hi = datetime.fromtimestamp(run_end + 4 * 3600, timezone.utc).isoformat()
        played = [dict(r) for r in conn.execute("SELECT * FROM played WHERE played_at BETWEEN ? AND ? ORDER BY played_at", (lo, hi))]
        if not played:
            return {"ok": False, "error": "no plays stored around this run — pull the play history soon after a run "
                                          "(Spotify keeps only the last fifty)"}
        # §BEAT15 — the guide's own laps (one at the start of each work step) are not presses. Two
        # sources, since either can drift out from under the wrist's own guide: the CURRENT plan's
        # offsets (a lived day can be re-priced after the run, or the wrist can be running a rep
        # count the plan has since dropped), and the §RD decode of the run itself (cached-only: a
        # read-back must not fan out into stream fetches; superseded semantics are not evidence,
        # §RD9b). Both are read against the run's OWN run_start/run_end, before the FIT re-anchoring
        # below — a lap's own position (the running sum of `timer_s`) is in the same frame.
        sess = _session_on(db, a["date"])
        guide_s = S.guide_lap_offsets(sess) if sess else []
        try:
            st, _ = S._structure_cached(db, run_id, a["date"], fetch=False, stale_ok=False)
        except Exception as e:
            st = None
            print(f"[music] structure unreadable for {run_id}: {e}")
        decoded_s = [a_ - run_start for a_, b in work_spans(st, run_start, run_end)]
        # the original FIT first: one-second cadence and the lap presses; the MCP streams otherwise
        fit, stream_source = None, "streams"
        try:
            raw = fetch_fit(run_id)
            fit = parse_fit(raw) if raw else None
        except Exception as e:
            print(f"[music] fit read failed for {run_id}: {e}")
        presses, clock_shift, n_guide_laps = [], 0.0, 0
        if fit:
            fit, clock_shift = anchor_fit(fit, run_start)     # §BEAT6 — the file's clock, anchored to the run's
            samples = fit["samples"]
            run_start = fit["start"] or run_start
            run_end = samples[-1][0]
            stream_source = "fit"
            presses = press_times(fit["laps"], run_end, guide_s=guide_s, decoded_s=decoded_s)
            n_guide_laps = sum(guide_laid(fit["laps"], guide_s, decoded_s))   # §BEAT15 — laps actually dropped
        else:
            try:
                act = S.activity_details(run_id)
            except Exception as e:
                return {"ok": False, "error": f"Runalyze did not answer for this run "
                                              f"({type(e).__name__}) — try again"}
            s = act.get("streams") or {}
            try:
                presses = press_times_from_splits(act.get("splits"), run_start, guide_s=guide_s, decoded_s=decoded_s)
            except Exception as e:
                print(f"[music] splits unreadable for {run_id}: {e}")
            tim, cad, dist = s.get("time") or [], s.get("cadence") or [], s.get("distance") or []
            if not tim or not cad:
                return {"ok": False, "error": "the run has no cadence stream"}
            mult = 2 if S.cadence_is_halved(src) else 1
            km_units = bool(dist) and max(d for d in dist if d is not None) <= 100
            samples = [(run_start + tim[i], (cad[i] or 0) * mult if i < len(cad) and cad[i] is not None else None,
                        (dist[i] * (1000.0 if km_units else 1.0)) if i < len(dist) and dist[i] is not None else None)
                       for i in range(len(tim)) if tim[i] is not None]
        songs = align_songs(run_start, run_end, played, samples)
        # §BEAT5 — which playlist segment each song belonged to, if any: its target and its role;
        # §BEAT7 — matched by id or by name (another edition's id is the same song)
        pl = conn.execute("SELECT name, spec FROM playlist WHERE key LIKE ? ORDER BY built_at DESC LIMIT 1", (a["date"] + "-%",)).fetchone()
        spec = json.loads(pl["spec"]) if pl else {}
        target = spec.get("target_spm") if pl else None
        # §BEAT8 — the songs the play history dropped, read from the list order into the gaps between
        # the aligned ones, and merged HERE so they take the same path as an aligned song from this
        # point on: segment and tempo, the follow row, the skip verdict, and the presses — which used
        # to land on no song at all (three of four on the 2026-09-12 run)
        if pl:
            durs = {r["spotify_id"]: r["duration_ms"] / 1000.0
                    for r in conn.execute("SELECT spotify_id, duration_ms FROM track WHERE duration_ms > 0")}
            songs = sorted(songs + infer_gap_songs(songs, spec, durs, run_start, run_end, samples),
                           key=lambda x: x["start_s"])
        by_id_m, by_name_m = playlist_membership(spec)
        # §BEAT9 — the reps of a reps day, from the same §RD decode read above (`st`), against
        # run_start/run_end as they stand HERE — the FIT path has already re-anchored them
        spans = work_spans(st, run_start, run_end) if pl else []
        tempos = {r["spotify_id"]: r["tempo"] for r in conn.execute("SELECT spotify_id, tempo FROM track WHERE tempo IS NOT NULL")}
        for sg in songs:
            hit = segment_for(sg, by_id_m, by_name_m) if pl else None
            sg["in_playlist"] = bool(hit) if pl else None
            sg["segment"], sg["seg_target"], sg["role"] = hit[:3] if hit else (None, None, None)
            if hit and hit[5] and spans:                      # §BEAT9 — a song in a reps block: read the reps only
                lo_s = run_start + sg["start_s"]
                hi_s = lo_s + sg["read_s"] + MUSIC_SONG_SEAM_S
                work_s = sum(max(0.0, min(hi_s, b) - max(lo_s + MUSIC_SONG_SEAM_S, a_)) for a_, b in spans)
                sg["spm_all"], sg["work_s"] = sg["spm"], round(work_s)
                rd = _read_span(lo_s, hi_s, samples, spans=spans) if work_s >= MUSIC_WORK_READ_MIN_S else None
                if rd:
                    sg.update(rd)
                    sg["jogs_only"] = False
                else:
                    sg["jogs_only"] = True
            # the tempo the legs were offered: the list's (doubled for a half-time hit), else the library's
            tempo = (hit[3] * 2 if hit[4] == "half" else hit[3]) if hit and hit[3] else tempos.get(sg["spotify_id"])
            sg["tempo"] = round(tempo, 1) if tempo else None
            if tempo and sg.get("spm") and not sg.get("jogs_only"):
                sg["delta_pct"] = round((sg["spm"] + CADENCE_SENSOR_BIAS_SPM - tempo) / tempo, 4)
                sg["entrained"] = abs(sg["delta_pct"]) <= MUSIC_ENTRAIN_PCT
            else:
                sg["delta_pct"], sg["entrained"] = None, None
        since = press_protocol_from(conn)
        per_run, events = press_ratings(presses, songs, run_start, legacy=bool(since and a["date"] < since))
        by_id = {sg["spotify_id"]: sg for sg in songs}
        conn.execute("DELETE FROM rating_run WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM follow WHERE run_id=?", (run_id,))
        n_aside = 0
        for sg in songs:                          # a skip is a verdict too: the track did not carry
            if sg.get("skipped") and sg.get("spotify_id"):
                conn.execute("INSERT OR REPLACE INTO rating_run(run_id, spotify_id, rating, at_s, role) VALUES(?,?,?,?,?)",
                             (run_id, sg["spotify_id"], "skip", sg["start_s"], sg.get("role")))
                n_aside += 1
        for ev in events:                         # a press on a skipped song is the louder word
            if ev["spotify_id"]:
                role = (by_id.get(ev["spotify_id"]) or {}).get("role")
                conn.execute("INSERT OR REPLACE INTO rating_run(run_id, spotify_id, rating, at_s, role) VALUES(?,?,?,?,?)",
                             (run_id, ev["spotify_id"], per_run.get(ev["spotify_id"], ev["rating"]), ev["at_s"], role))
        n_aside += sum(1 for sid, r in per_run.items() if r == "never" and not (by_id.get(sid) or {}).get("skipped"))
        couch = {r["spotify_id"]: r["rating"] for r in conn.execute("SELECT spotify_id, rating FROM rating")}
        for sg in songs:
            sg["rating"] = couch.get(sg["spotify_id"])
            sg.setdefault("run_rating", None)
            sg.setdefault("presses", 0)
            sg.setdefault("breaks", 0)
            sg["vs_target"] = (round(sg["spm"] - sg["seg_target"], 1) if sg.get("seg_target")
                               else (round(sg["spm"] - target, 1) if target and not pl else None))
            sg["follow"] = (round(follow_row_score(sg["entrained"], sg["breaks"], sg["dips"]), 2)
                            if sg.get("entrained") is not None else None)
            if sg.get("spotify_id") and sg.get("entrained") is not None:
                conn.execute("INSERT OR REPLACE INTO follow(run_id, spotify_id, spm, tempo, delta_pct, entrained, steady, "
                             "dips, dip_s, breaks, read_s) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                             (run_id, sg["spotify_id"], sg["spm"], sg["tempo"], sg["delta_pct"], int(sg["entrained"]),
                              sg.get("steady"), sg.get("dips") or 0, sg.get("dip_s") or 0, sg["breaks"],
                              sg["work_s"] if "work_s" in sg else sg["read_s"]))
        with_tempo = [sg for sg in songs if sg.get("entrained") is not None]
        followed = {"songs": len(with_tempo), "entrained": sum(1 for sg in with_tempo if sg["entrained"]),
                    "breaks": sum(sg.get("breaks") or 0 for sg in songs),
                    "dips": sum(sg.get("dips") or 0 for sg in songs),
                    "never": sum(1 for r in per_run.values() if r == "never"),
                    "jogs_only": sum(1 for sg in songs if sg.get("jogs_only"))}    # §BEAT9
        res = {"ok": True, "run_id": run_id, "date": a["date"], "km": a["distance"], "minutes": round((a["duration"] or 0) / 60),
               "playlist": pl["name"] if pl else None, "target_spm": target, "songs": songs,
               "followed": followed, "presses": events, "stream_source": stream_source,
               "skipped": sum(1 for sg in songs if sg.get("skipped")),
               "off_playlist": sum(1 for sg in songs if pl and not sg.get("in_playlist")),
               "inferred": sum(1 for sg in songs if sg.get("inferred")),          # §BEAT8
               "reps_read": bool(spans), "work_spans": len(spans),                # §BEAT9
               "set_aside": n_aside, "clock_shift_s": round(clock_shift),
               "seam_s": MUSIC_SONG_SEAM_S, "sensor_bias_spm": CADENCE_SENSOR_BIAS_SPM,  # §BEAT12
               "guide_laps": n_guide_laps,                                       # §BEAT15
               "computed_at": _now_iso()}
        res = apply_manual_verdicts(conn, run_id, res)     # §BEAT14 — survives the recompute by construction
        conn.execute("INSERT INTO readback(run_id, date, computed_at, payload) VALUES(?,?,?,?) "
                     "ON CONFLICT(run_id) DO UPDATE SET computed_at=excluded.computed_at, payload=excluded.payload",
                     (run_id, a["date"], res["computed_at"], json.dumps(res)))
        conn.commit()
        return res
    finally:
        conn.close()


def recent_runs(db, n=MUSIC_READBACK_RUNS):
    """The last runs the page can read back, with whether a read-back is stored already."""
    rows = db.execute("SELECT id, date, date_time, distance, duration FROM activities WHERE sport=? "
                      "ORDER BY date_time DESC LIMIT ?", (S.RUNNING_SPORT, n)).fetchall()
    conn = _mdb()
    try:
        done = {r["run_id"]: r["computed_at"] for r in conn.execute("SELECT run_id, computed_at FROM readback")}
        names = {r["key"][:10]: r["name"] for r in conn.execute("SELECT key, name FROM playlist")}
    finally:
        conn.close()
    return [{"id": r["id"], "date": r["date"], "km": r["distance"], "minutes": round((r["duration"] or 0) / 60),
             "playlist": names.get(r["date"]), "readback_at": done.get(r["id"])} for r in rows]


def ratings_file():
    return music_db_path().with_name("music-ratings.json")


def import_ratings(rows, source="couch"):
    """Upsert [{spotify_id, rating, note}] — ratings are the athlete's own words about a song and
    the ground truth every groove measure has to beat. `source` says where the words were said:
    'couch' at rest, 'run' from a lap press (those live in rating_run per run as well)."""
    ok = {"pushes", "relaxes", "neutral", "mixed"}
    conn = _mdb()
    n = 0
    try:
        for r in rows:
            if not r.get("spotify_id") or r.get("rating") not in ok:
                continue
            conn.execute("INSERT INTO rating(spotify_id, rating, note, rated_at, source) VALUES(?,?,?,?,?) "
                         "ON CONFLICT(spotify_id) DO UPDATE SET rating=excluded.rating, note=excluded.note, "
                         "rated_at=excluded.rated_at, source=excluded.source",
                         (r["spotify_id"], r["rating"], r.get("note") or "", r.get("rated_at") or _now_iso(), r.get("source") or source))
            n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def nightly():
    """The scheduler's hook: bank the day's plays before Spotify forgets them, and read back any run
    of the day that has none yet. Never raises — the night belongs to the sync."""
    try:
        n = pull_played()
        if n:
            print(f"[music] play history: {n} new plays")
    except Exception as e:
        print(f"[music] play history pull failed: {e}")
    try:                                          # §DISCO — ListenBrainz publishes its lists on Mondays:
        if date.today().weekday() == 0 and get_settings().get("listenbrainz_user") and sp_access_token():
            print(f"[music] weekly library refresh {'started' if start_refresh() else 'already running'}")
    except Exception as e:
        print(f"[music] weekly refresh failed: {e}")
    try:
        db = S.connect_db()
        try:
            conn = _mdb()                         # §BEAT7 — the taste weights follow the night's plays
            try:
                reweigh(conn, db)
            finally:
                conn.close()
            today = date.today().isoformat()
            conn = _mdb()
            try:
                done = {r["run_id"] for r in conn.execute("SELECT run_id FROM readback")}
                # §BEAT7 — a read-back stored before the legs' verdict existed is read once more, so
                # the picker learns from the runs already on file without a click
                stale = [r["run_id"] for r in conn.execute(
                    "SELECT run_id FROM readback WHERE payload NOT LIKE '%\"followed\"%' ORDER BY date DESC LIMIT ?",
                    (MUSIC_READBACK_RUNS,))]
            finally:
                conn.close()
            todays = [r["id"] for r in db.execute("SELECT id FROM activities WHERE sport=? AND date=?", (S.RUNNING_SPORT, today)).fetchall()]
            for rid in [i for i in todays if i not in done] + stale:
                res = readback(db, rid)
                print(f"[music] read-back {rid}: {'ok, ' + str(len(res['songs'])) + ' songs' if res.get('ok') else res.get('error')}")
        finally:
            db.close()
    except Exception as e:
        print(f"[music] read-back failed: {e}")


# ── last.fm ──────────────────────────────────────────────────────────────────
def _lfm_key():
    return S._resolve_secret(S.SECRET_BY_KEY["lastfm_api_key"])[0]


def _lfm(method, **params):
    key = _lfm_key()
    if not key:
        return None
    r = requests.get(LFM_BASE, params={"method": method, "api_key": key, "format": "json", **params},
                     headers={"User-Agent": S.USER_AGENT}, timeout=MUSIC_HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"last.fm {method} {r.status_code}: {r.text[:160]}")
    d = r.json()
    if "error" in d:
        raise RuntimeError(f"last.fm {method}: {d.get('message')}")
    return d


def lfm_top_tracks(user, period):
    d = _lfm("user.gettoptracks", user=user, period=period, limit=LFM_PAGE)
    rows = ((d or {}).get("toptracks") or {}).get("track") or []
    return [{"title": t.get("name") or "", "artist": (t.get("artist") or {}).get("name") or "",
             "playcount": int(t.get("playcount") or 0), "rank": int((t.get("@attr") or {}).get("rank") or 0)}
            for t in rows]


def lfm_loved(user):
    d = _lfm("user.getlovedtracks", user=user, limit=LFM_PAGE)
    rows = ((d or {}).get("lovedtracks") or {}).get("track") or []
    return [{"title": t.get("name") or "", "artist": (t.get("artist") or {}).get("name") or ""} for t in rows]


# ── ListenBrainz: what the neighbours run to (§DISCO, 0.67.0) ────────────────
def _lb_token():
    return S._resolve_secret(S.SECRET_BY_KEY["listenbrainz_token"])[0]


def _lb(path, token=None, **params):
    """One ListenBrainz call → its JSON, or None on anything but 200 (a neighbour whose stats are
    private, a list that expired). One retry on a rate limit, honouring the reset the API names."""
    h = {"User-Agent": S.USER_AGENT}
    if token:
        h["Authorization"] = f"Token {token}"
    r = None
    for attempt in range(2):
        r = requests.get(f"{LB_BASE}/{path}", params=params, headers=h, timeout=max(MUSIC_HTTP_TIMEOUT, 60))
        if r.status_code == 429 and attempt == 0:
            time.sleep(min(float(r.headers.get("X-RateLimit-Reset-In", "2") or 2) + 0.5, 10.0))
            continue
        break
    return r.json() if r is not None and r.status_code == 200 else None


def _rec_mbid(track):
    """The recording MBID behind a JSPF track (the identifier is a MusicBrainz URL)."""
    ids = track.get("identifier") or []
    ids = ids if isinstance(ids, list) else [ids]
    for u in ids:
        if "/recording/" in str(u):
            return str(u).rsplit("/", 1)[-1]
    return None


def lb_merge(batches):
    """PURE — [(source, [{mbid, artist, title}])] → {mbid: {artist, title, sources}}: one row per
    recording, its sources in the order they named it, the names from the first batch that had
    them. Rows without an MBID are dropped (nothing to dedupe them on)."""
    out = {}
    for src, rows in batches:
        for r in rows or []:
            m = r.get("mbid")
            if not m:
                continue
            c = out.setdefault(m, {"artist": None, "title": None, "sources": []})
            if src not in c["sources"]:
                c["sources"].append(src)
            if not (c["artist"] and c["title"]) and r.get("artist") and r.get("title"):
                c["artist"], c["title"] = r["artist"], r["title"]
    return out


def lb_sources(user, token=None):
    """The four ListenBrainz sources for `user` as (source, rows) batches, with per-source counts
    and the errors. Each is read on its own, so one failing leaves the others standing:
    `neighbours` — the top recordings of the year of the most similar listeners (public stats only);
    `cf` — LB's collaborative-filtering picks (bare MBIDs, named later); `weekly` — the newest
    weekly-exploration lists; `radio` — LB radio seeded on the listener's top artists of the year,
    which needs the token."""
    batches, counts, errors = [], {}, []
    try:
        sim = (((_lb(f"user/{user}/similar-users") or {}).get("payload")) or [])[:MUSIC_LB_NEIGHBOURS]
        rows, readable = [], 0
        for s_ in sim:
            d = _lb(f"stats/user/{s_.get('user_name')}/recordings", range="year", count=MUSIC_LB_PER_USER)
            if not d:
                continue
            readable += 1
            for rec in ((d.get("payload") or {}).get("recordings")) or []:
                rows.append({"mbid": rec.get("recording_mbid"), "artist": rec.get("artist_name"), "title": rec.get("track_name")})
        batches.append(("neighbours", rows))
        counts["neighbours"], counts["neighbours_users"] = len(rows), readable
    except Exception as e:
        errors.append(f"neighbours: {e}"[:160])
    try:
        d = _lb(f"cf/recommendation/user/{user}/recording", count=MUSIC_LB_CF)
        rows = [{"mbid": m.get("recording_mbid")} for m in (((d or {}).get("payload") or {}).get("mbids")) or []]
        batches.append(("cf", rows))
        counts["cf"] = len(rows)
    except Exception as e:
        errors.append(f"cf: {e}"[:160])
    try:
        d = _lb(f"user/{user}/playlists/createdfor", count=25)
        lists = []
        for p in ((d or {}).get("playlists")) or []:
            pl = p.get("playlist") or {}
            ext = (pl.get("extension") or {}).get("https://musicbrainz.org/doc/jspf#playlist") or {}
            patch = (((ext.get("additional_metadata") or {}).get("algorithm_metadata")) or {}).get("source_patch")
            if patch == "weekly-exploration":
                lists.append(pl)
        rows = []
        for pl in lists[:MUSIC_LB_WEEKLY_LISTS]:
            mbid = str(pl.get("identifier") or "").rsplit("/", 1)[-1]
            j = _lb(f"playlist/{mbid}") if mbid else None
            for t in (((j or {}).get("playlist") or {}).get("track")) or []:
                rows.append({"mbid": _rec_mbid(t), "artist": t.get("creator"), "title": t.get("title")})
        batches.append(("weekly", rows))
        counts["weekly"], counts["weekly_lists"] = len(rows), min(len(lists), MUSIC_LB_WEEKLY_LISTS)
    except Exception as e:
        errors.append(f"weekly: {e}"[:160])
    if token:
        try:
            d = _lb(f"stats/user/{user}/artists", range="year", count=MUSIC_LB_RADIO_SEEDS)
            rows = []
            for a in (((d or {}).get("payload") or {}).get("artists")) or []:
                name = re.sub(r"[()]", " ", str(a.get("artist_name") or "")).strip()
                if not name:
                    continue
                j = _lb("explore/lb-radio", token=token, prompt=f"artist:({name})", mode="medium")
                for t in (((((j or {}).get("payload") or {}).get("jspf") or {}).get("playlist") or {}).get("track")) or []:
                    rows.append({"mbid": _rec_mbid(t), "artist": t.get("creator"), "title": t.get("title")})
            batches.append(("radio", rows))
            counts["radio"] = len(rows)
        except Exception as e:
            errors.append(f"radio: {e}"[:160])
    return batches, counts, errors


def lb_names(mbids):
    """{mbid: (artist, title)} for recordings that came as bare MBIDs (the CF picks)."""
    out = {}
    for i in range(0, len(mbids), MUSIC_LB_NAMES_BATCH):
        d = _lb("metadata/recording/", recording_mbids=",".join(mbids[i:i + MUSIC_LB_NAMES_BATCH]), inc="artist")
        for m, md in (d or {}).items():
            art = md.get("artist") or {}
            name = art.get("name") or ((art.get("artists") or [{}])[0].get("name"))
            out[m] = (name, (md.get("recording") or {}).get("name"))
    return out


def discover_lb(conn, sp_token, user, lb_token=None, lookups=MUSIC_LB_LOOKUPS):
    """§DISCO — the ListenBrainz pull: read the sources, keep every recording the library does not
    already hold, resolve a bounded number of them to Spotify ids (strict: the artist must match)
    and file those as `discovered=2` with their sources as provenance. The features pass that
    follows in `refresh_library` gives them a tempo like any other track. Idempotent: a candidate
    resolved once is never searched again; a miss waits MUSIC_SEARCH_MISS_TTL_DAYS."""
    summary = {"lb_candidates": 0, "lb_new": 0, "lb_resolved": 0, "lb_unresolved": 0, "lb_sources": {}, "lb_errors": []}
    batches, counts, errors = lb_sources(user, lb_token)
    summary["lb_sources"], summary["lb_errors"] = counts, errors
    merged = lb_merge(batches)
    bare = [m for m, c in merged.items() if not (c["artist"] and c["title"])]
    if bare:
        for m, (a, t) in lb_names(bare).items():
            if m in merged:
                merged[m]["artist"], merged[m]["title"] = merged[m]["artist"] or a, merged[m]["title"] or t
    summary["lb_candidates"] = len(merged)
    known = {(_norm(r["artist"]), _norm(r["title"])): r["spotify_id"]
             for r in conn.execute("SELECT artist, title, spotify_id FROM track")}
    held = set(known.values())
    now = _now_iso()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MUSIC_SEARCH_MISS_TTL_DAYS)).isoformat()
    budget = lookups
    for mbid, c in merged.items():
        if not (c["artist"] and c["title"]):
            continue
        srcs = ",".join(c["sources"])
        conn.execute("INSERT INTO disco(mbid, artist, title, sources, first_seen) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(mbid) DO UPDATE SET sources=excluded.sources", (mbid, c["artist"], c["title"], srcs, now))
        k = (_norm(c["artist"]), _norm(c["title"]))
        row = conn.execute("SELECT spotify_id, searched_at FROM disco WHERE mbid=?", (mbid,)).fetchone()
        if known.get(k):
            if not row["spotify_id"]:
                conn.execute("UPDATE disco SET spotify_id=? WHERE mbid=?", (known[k], mbid))
            continue                                  # the library holds this song — nothing new
        if row["spotify_id"]:
            continue                                  # resolved on an earlier refresh
        summary["lb_new"] += 1
        if row["searched_at"] and row["searched_at"] > cutoff:
            summary["lb_unresolved"] += 1
            continue
        if not sp_token or budget <= 0:
            summary["lb_unresolved"] += 1
            continue
        budget -= 1
        _job_set(step=f"ListenBrainz → Spotify ({lookups - budget}/{lookups})")
        found = sp_search_track(sp_token, c["title"], c["artist"], strict=True)
        conn.execute("UPDATE disco SET searched_at=?, spotify_id=? WHERE mbid=?", (now, found["id"] if found else None, mbid))
        if not found:
            summary["lb_unresolved"] += 1
            continue
        if found["id"] in held:
            continue                                  # another spelling of a song already held
        _upsert_track(conn, found, discovered=2)
        conn.execute("UPDATE track SET source=? WHERE spotify_id=? AND source IS NULL", (srcs, found["id"]))
        known[k] = found["id"]
        held.add(found["id"])
        summary["lb_resolved"] += 1
    conn.commit()
    return summary


# ── ReccoBeats: the audio features Spotify withdrew ──────────────────────────
def rb_features(ids):
    """{spotify_id: {tempo, energy, danceability, valence, loudness}} for up to RB_BATCH ids per call.
    Ids absent from the answer are misses (the caller marks them)."""
    out = {}
    for i in range(0, len(ids), RB_BATCH):
        chunk = ids[i:i + RB_BATCH]
        r = requests.get(f"{RB_BASE}/audio-features", params={"ids": ",".join(chunk)},
                         headers={"User-Agent": S.USER_AGENT}, timeout=MUSIC_HTTP_TIMEOUT)
        if r.status_code == 429:
            time.sleep(min(float(r.headers.get("Retry-After", "2") or 2), 15.0))
            r = requests.get(f"{RB_BASE}/audio-features", params={"ids": ",".join(chunk)},
                             headers={"User-Agent": S.USER_AGENT}, timeout=MUSIC_HTTP_TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"ReccoBeats audio-features {r.status_code}: {r.text[:160]}")
        for f in (r.json().get("content") or []):
            sid = (f.get("href") or "").rsplit("/", 1)[-1]
            if sid:
                out[sid] = {k: f.get(k) for k in ("tempo", "energy", "danceability", "valence", "loudness")}
    return out


def rb_recommend(seeds, lo, hi, min_energy, size=MUSIC_DISCOVERY_SIZE):
    """Tracks near the tempo window, seeded by the athlete's own favourites. Rows carry a Spotify id
    (from the href), a title, artists and duration; features are fetched separately."""
    r = requests.get(f"{RB_BASE}/track/recommendation",
                     params={"seeds": ",".join(seeds[:5]), "size": size, "minTempo": round(lo, 1),
                             "maxTempo": round(hi, 1), "minEnergy": round(min_energy, 2)},
                     headers={"User-Agent": S.USER_AGENT}, timeout=MUSIC_HTTP_TIMEOUT)
    if r.status_code != 200:
        return []
    out = []
    for t in (r.json().get("content") or []):
        sid = (t.get("href") or "").rsplit("/", 1)[-1]
        if not sid:
            continue
        out.append({"id": sid, "title": t.get("trackTitle") or "", "duration_ms": t.get("durationMs") or 0,
                    "artist": ", ".join(a.get("name", "") for a in (t.get("artists") or []))})
    return out


def rb_reachable():
    try:
        r = requests.get(f"{RB_BASE}/audio-features", params={"ids": "0VjIjW4GlUZAMYd2vXMi3b"},
                         headers={"User-Agent": S.USER_AGENT}, timeout=8)
        return r.status_code == 200
    except requests.RequestException:
        return False


# ── The library: taste + features, cached ────────────────────────────────────
def _upsert_track(conn, row, discovered=0):
    conn.execute("INSERT INTO track(spotify_id, title, artist, duration_ms, discovered) VALUES(?,?,?,?,?) "
                 "ON CONFLICT(spotify_id) DO UPDATE SET title=excluded.title, artist=excluded.artist, "
                 "duration_ms=CASE WHEN excluded.duration_ms>0 THEN excluded.duration_ms ELSE track.duration_ms END",
                 (row["id"], row.get("title") or "", row.get("artist") or "", row.get("duration_ms") or 0, discovered))


def _taste_touch(conn, sid, **cols):
    conn.execute("INSERT OR IGNORE INTO taste(spotify_id, updated_at) VALUES(?,?)", (sid, _now_iso()))
    for k, v in cols.items():
        conn.execute(f"UPDATE taste SET {k}=?, updated_at=? WHERE spotify_id=?", (v, _now_iso(), sid))


def taste_weight(playcount, max_playcount, loved, saved, top_rank, playlisted=0):
    """0–~2.6: log-scaled scrobbles against the heaviest-played track, plus loved, saved, in one of
    the athlete's own playlists, and a Spotify top-list bonus that fades with rank. Explainable on
    the page, nothing hidden."""
    w = math.log1p(max(0, playcount)) / math.log1p(max(1, max_playcount)) if max_playcount else 0.0
    w += 0.6 if loved else 0.0
    w += 0.3 if saved else 0.0
    w += 0.3 if playlisted else 0.0
    if top_rank:
        w += max(0.0, 0.4 * (1 - (top_rank - 1) / 50.0))
    return round(w, 3)


def _job_set(**kw):
    with _job_lock:
        _job.update(kw)


def refresh_library(lookups=MUSIC_LOOKUPS_PER_REFRESH):
    """The whole pull: Spotify saved + top → last.fm top (three periods) + loved → resolve unresolved
    scrobbles to Spotify ids (bounded) → ReccoBeats features for tracks without → weights. Every step
    is idempotent and cached; a second click continues where the budget stopped."""
    summary = {"spotify_saved": 0, "spotify_top": 0, "spotify_playlisted": 0, "playlists": 0,
               "lfm_tracks": 0, "resolved": 0, "unresolved": 0, "features": 0, "feature_misses": 0,
               "lb_candidates": 0, "lb_new": 0, "lb_resolved": 0, "lb_unresolved": 0}
    settings = get_settings()
    token = sp_access_token()
    conn = _mdb()
    try:
        if token:
            _job_set(step="Spotify library")
            saved = sp_saved_tracks(token)
            for t in saved:
                _upsert_track(conn, t)
                _taste_touch(conn, t["id"], saved=1)
            summary["spotify_saved"] = len(saved)
            top = sp_top_tracks(token)
            for t in top:
                _upsert_track(conn, t)
                cur = conn.execute("SELECT top_rank FROM taste WHERE spotify_id=?", (t["id"],)).fetchone()
                best = min(t["top_rank"], cur["top_rank"]) if cur and cur["top_rank"] else t["top_rank"]
                _taste_touch(conn, t["id"], top_rank=best)
            summary["spotify_top"] = len(top)
            conn.commit()
            _job_set(step="Spotify playlists")
            for p in sp_own_playlists(token):
                summary["playlists"] += 1
                for t in sp_playlist_tracks(token, p["id"]):
                    _upsert_track(conn, t)
                    _taste_touch(conn, t["id"], playlisted=1)
                    summary["spotify_playlisted"] += 1
                conn.commit()
        user = settings.get("lastfm_user")
        if user and _lfm_key():
            _job_set(step="last.fm scrobbles")
            plays = {}
            for period in ("3month", "12month", "overall"):
                for t in lfm_top_tracks(user, period):
                    k = (_norm(t["artist"]), _norm(t["title"]))
                    if not k[0] or not k[1]:
                        continue
                    cur = plays.get(k)
                    if not cur or t["playcount"] > cur["playcount"]:
                        plays[k] = {**t, "loved": (cur or {}).get("loved", 0)}
            for t in lfm_loved(user):
                k = (_norm(t["artist"]), _norm(t["title"]))
                if k in plays:
                    plays[k]["loved"] = 1
                elif k[0] and k[1]:
                    plays[k] = {**t, "playcount": 0, "rank": None, "loved": 1}
            summary["lfm_tracks"] = len(plays)
            known = {(_norm(r["artist"]), _norm(r["title"])): r["spotify_id"]
                     for r in conn.execute("SELECT artist, title, spotify_id FROM track")}
            budget = lookups
            for k, t in plays.items():
                sid = known.get(k)
                if not sid:
                    m = conn.execute("SELECT spotify_id, searched_at FROM lfm_map WHERE artist=? AND title=?", k).fetchone()
                    if m and m["spotify_id"]:
                        sid = m["spotify_id"]
                    elif m and m["searched_at"] and m["searched_at"] > (datetime.now(timezone.utc) - timedelta(days=MUSIC_SEARCH_MISS_TTL_DAYS)).isoformat():
                        summary["unresolved"] += 1
                        continue
                    elif token and budget > 0:
                        budget -= 1
                        _job_set(step=f"resolving scrobbles ({lookups - budget}/{lookups})")
                        row = sp_search_track(token, t["title"], t["artist"])
                        conn.execute("INSERT INTO lfm_map(artist, title, spotify_id, searched_at) VALUES(?,?,?,?) "
                                     "ON CONFLICT(artist, title) DO UPDATE SET spotify_id=excluded.spotify_id, searched_at=excluded.searched_at",
                                     (k[0], k[1], row["id"] if row else None, _now_iso()))
                        if row:
                            _upsert_track(conn, row)
                            sid = row["id"]
                            summary["resolved"] += 1
                        else:
                            summary["unresolved"] += 1
                            continue
                    else:
                        summary["unresolved"] += 1
                        continue
                _taste_touch(conn, sid, lfm_playcount=t.get("playcount") or 0, lfm_rank=t.get("rank"),
                             loved=1 if t.get("loved") else 0)
            conn.commit()
        lb_user = settings.get("listenbrainz_user")
        if lb_user:                                   # §DISCO — what similar listeners play
            _job_set(step="ListenBrainz sources")
            try:
                summary.update(discover_lb(conn, token, lb_user, _lb_token()))
            except Exception as e:
                summary["lb_errors"] = [str(e)[:160]]
        _job_set(step="audio features")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=MUSIC_FEATURE_MISS_TTL_DAYS)).isoformat()
        need = [r["spotify_id"] for r in conn.execute(
            "SELECT spotify_id FROM track WHERE features_at IS NULL AND (feature_miss_at IS NULL OR feature_miss_at < ?)",
            (cutoff,))]
        _job_set(total=len(need), done=0)
        for i in range(0, len(need), RB_BATCH):
            chunk = need[i:i + RB_BATCH]
            feats = rb_features(chunk)
            for sid in chunk:
                f = feats.get(sid)
                if f and f.get("tempo"):
                    conn.execute("UPDATE track SET tempo=?, energy=?, danceability=?, valence=?, loudness=?, "
                                 "features_at=?, feature_miss_at=NULL WHERE spotify_id=?",
                                 (f["tempo"], f["energy"], f["danceability"], f["valence"], f["loudness"], _now_iso(), sid))
                    summary["features"] += 1
                else:
                    conn.execute("UPDATE track SET feature_miss_at=? WHERE spotify_id=?", (_now_iso(), sid))
                    summary["feature_misses"] += 1
            conn.commit()
            _job_set(done=min(len(need), i + RB_BATCH))
        _job_set(step="weights")
        reweigh(conn)
    finally:
        conn.close()
    return summary


def app_play_counts(conn, db=None):
    """§BEAT7 — plays the app itself caused: stored plays that fall inside a run (the run's own
    window, ten minutes of grace after it) since the first list was built. last.fm counts them like
    any other play, so a song the lists keep serving would climb the taste ladder on its own
    serving — the feedback loop named on 2026-09-09. {spotify_id: n}."""
    first = conn.execute("SELECT MIN(built_at) FROM playlist").fetchone()[0]
    if not first:
        return {}
    own = db is None
    db = db or S.connect_db()
    try:
        runs = [(_epoch(r["date_time"]), _epoch(r["date_time"]) + float(r["duration"] or 0) + 600)
                for r in db.execute("SELECT date_time, duration FROM activities WHERE sport=? AND date >= ?",
                                    (S.RUNNING_SPORT, first[:10])).fetchall() if r["date_time"]]
    finally:
        if own:
            db.close()
    out = {}
    for r in conn.execute("SELECT spotify_id, played_at, duration_ms FROM played WHERE played_at >= ? AND spotify_id IS NOT NULL", (first,)):
        end = _epoch(r["played_at"])
        start = end - (r["duration_ms"] or 0) / 1000.0
        if any(lo <= start <= hi or lo <= end <= hi for lo, hi in runs):
            out[r["spotify_id"]] = out.get(r["spotify_id"], 0) + 1
    return out


def reweigh(conn, db=None):
    """The taste weights, with the app's own plays taken off the scrobble count first (§BEAT7)."""
    app = app_play_counts(conn, db)
    rows = conn.execute("SELECT spotify_id, lfm_playcount, loved, saved, top_rank, playlisted FROM taste").fetchall()
    counts = {r["spotify_id"]: max(0, (r["lfm_playcount"] or 0) - app.get(r["spotify_id"], 0)) for r in rows}
    mx = max(counts.values(), default=0)
    for r in rows:
        conn.execute("UPDATE taste SET weight=? WHERE spotify_id=?",
                     (taste_weight(counts[r["spotify_id"]], mx, r["loved"], r["saved"], r["top_rank"], r["playlisted"]),
                      r["spotify_id"]))
    conn.commit()
    return len(app)


def _refresh_thread():
    try:
        summary = refresh_library()
        _job_set(summary=summary, error=None)
    except Exception as e:
        print(f"[music] library refresh failed: {e}")
        _job_set(error=str(e)[:300])
    finally:
        _job_set(running=False, step="", finished=_now_iso())


def start_refresh():
    with _job_lock:
        if _job["running"]:
            return False
        _job.update(running=True, step="starting", done=0, total=0, error=None,
                    started=_now_iso(), finished=None, summary=None)
    threading.Thread(target=_refresh_thread, name="music-refresh", daemon=True).start()
    return True


def library_status(conn=None):
    own = conn is None
    conn = conn or _mdb()
    try:
        n = conn.execute("SELECT COUNT(*) FROM track").fetchone()[0]
        nf = conn.execute("SELECT COUNT(*) FROM track WHERE tempo IS NOT NULL").fetchone()[0]
        nm = conn.execute("SELECT COUNT(*) FROM track WHERE tempo IS NULL AND feature_miss_at IS NOT NULL").fetchone()[0]
        nt = conn.execute("SELECT COUNT(*) FROM taste WHERE weight > 0").fetchone()[0]
        nu = conn.execute("SELECT COUNT(*) FROM lfm_map WHERE spotify_id IS NULL").fetchone()[0]
        nd = conn.execute("SELECT COUNT(*) FROM track WHERE discovered=1").fetchone()[0]
        npl = conn.execute("SELECT COUNT(*) FROM taste WHERE playlisted=1").fetchone()[0]
        lb = {"candidates": conn.execute("SELECT COUNT(*) FROM disco").fetchone()[0],
              "resolved": conn.execute("SELECT COUNT(*) FROM track WHERE discovered=2").fetchone()[0],
              "with_tempo": conn.execute("SELECT COUNT(*) FROM track WHERE discovered=2 AND tempo IS NOT NULL").fetchone()[0],
              "in_band": conn.execute("SELECT COUNT(*) FROM track WHERE discovered=2 AND "
                                      "((tempo BETWEEN 160 AND 185) OR (tempo*2 BETWEEN 160 AND 185))").fetchone()[0],
              "never_run": len([i for i in never_run(conn)
                                if conn.execute("SELECT tempo FROM track WHERE spotify_id=?", (i,)).fetchone()[0]])}
        bands = {}
        for lo in range(150, 190, 5):
            bands[f"{lo}-{lo + 4}"] = conn.execute(
                "SELECT COUNT(*) FROM track WHERE tempo >= ? AND tempo < ?", (lo, lo + 5)).fetchone()[0]
    finally:
        if own:
            conn.close()
    with _job_lock:
        job = dict(_job)
    return {"tracks": n, "with_features": nf, "feature_misses": nm, "with_taste": nt,
            "unresolved_scrobbles": nu, "discovered": nd, "playlisted": npl, "listenbrainz": lb,
            "tempo_bands": bands, "job": job}


def track_pool(conn):
    rows = conn.execute(
        "SELECT t.spotify_id AS id, t.title, t.artist, t.duration_ms, t.tempo, t.energy, t.discovered, t.source, "
        "COALESCE(ta.weight, 0) AS weight FROM track t LEFT JOIN taste ta ON ta.spotify_id=t.spotify_id "
        "WHERE t.tempo IS NOT NULL AND t.duration_ms > 0").fetchall()
    return [dict(r) for r in rows]


# ── The plan's sessions, and building one playlist ───────────────────────────
def upcoming_sessions(plan, today, days=MUSIC_UPCOMING_DAYS):
    """The next `days` of plan sessions (today inclusive) plus the race, wherever it sits. Rest days
    and sessions without distance are left out: there is nothing to play them."""
    if not plan:
        return []
    end = (today + timedelta(days=days - 1)).isoformat()
    out, seen = [], set()
    for wk in S._plan_all_weeks(plan):
        for s in wk.get("sessions") or []:
            d = s.get("date")
            if not d or s.get("kind") == "rest" or not (s.get("km") or 0):
                continue
            if (today.isoformat() <= d <= end) or s.get("race"):
                k = session_key(s)
                if k in seen:
                    continue
                seen.add(k)
                out.append({**s, "key": k, "pk": wk.get("pk")})
    out.sort(key=lambda s: s["date"])
    return out


def _find_session(plan, key):
    for s in upcoming_sessions(plan, date.today(), days=MUSIC_UPCOMING_DAYS):
        if s["key"] == key:
            return s
    for wk in S._plan_all_weeks(plan or {}):
        for s in wk.get("sessions") or []:
            if session_key(s) == key:
                return {**s, "key": key, "pk": wk.get("pk")}
    return None


def _session_on(db, day):
    """The current plan's session on `day` (lived weeks keep their lay, so a past day's reps are
    still there), or None. A `db` with no `plans` table (a bare fixture, as several music dets build
    to isolate `readback()` from the rest of the app's schema) reads the same as one with no plan."""
    try:
        row = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    plan = json.loads(row["plan"]) if row else None
    for wk in S._plan_all_weeks(plan or {}):
        for s in wk.get("sessions") or []:
            if s.get("date") == day:
                return s
    return None


def plan_for_session(session, curve, pool, settings, discover=None, ramp=None, set_aside=None,
                     follow=None, served=None, fresh=None):
    """PURE given its inputs: the session's segments, targets and picks — what the page shows before
    (and after) the playlist is written. The run-home tail is filled but counts in neither the
    session's minutes nor its overall target."""
    step = float(settings.get("step_pct") or MUSIC_STEP_PCT)
    half = str(settings.get("half_time") or "0") == "1"
    segs = session_segments(session)
    if not segs:
        return {"ok": False, "error": "nothing to play — a rest day, or a session without distance"}
    targets = []
    for sg in segs:
        t = target_spm(curve, sg["pace_sec"], step, ramp, work=(sg["effort"] in MUSIC_WORK_EFFORTS))
        targets.append(t["spm"] if t else None)
        sg["target"] = t
    if any(t is None for t in targets):
        return {"ok": False, "error": "no cadence curve yet — sync a few runs with cadence first"}
    picked, notes = pick_for_segments(segs, pool, targets, half_time=half, discover=discover, set_aside=set_aside,
                                      follow=follow, served=served, fresh=fresh, fresh_quota=MUSIC_FRESH_QUOTA)
    core = [(t, s) for t, s in zip(targets, segs) if s["effort"] != "tail"]
    overall = sum(t * s["minutes"] for t, s in core) / max(1e-9, sum(s["minutes"] for _, s in core))
    return {"ok": True, "segments": picked, "notes": notes, "target_spm": round(overall, 1),
            "minutes": round(sum(s["minutes"] for _, s in core)),
            "tracks": sum(len(s["tracks"]) for s in picked),
            "fresh": sum(s.get("fresh") or 0 for s in picked), "rotation_days": MUSIC_ROTATION_DAYS,
            "never_run": sum(s.get("never_run") or 0 for s in picked), "fresh_quota": MUSIC_FRESH_QUOTA,
            "filled_min": round(sum(s["filled_min"] for s in picked), 1),
            "sensor_bias_spm": CADENCE_SENSOR_BIAS_SPM}   # §BEAT12 — so the build/preview summary can name it


def build_playlist(db, key, write=True):
    """Segments → targets → picks → (optionally) the Spotify playlist. Discovery goes through
    ReccoBeats seeded by the heaviest-weighted tracks and lands in the cache flagged `discovered`."""
    row = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    plan = json.loads(row["plan"]) if row else None
    session = _find_session(plan, key)
    if not session:
        return {"ok": False, "error": "no such session on the current plan"}
    settings = get_settings()
    curve = cadence_curve(cadence_rows(db, date.today()), date.today(), settings)
    conn = _mdb()
    try:
        ramp = ramp_state(plan, curve, date.today(), conn)
        pool = track_pool(conn)
        # discovery seeds: the heaviest-weighted tracks whatever their features — a pool with no
        # usable tempo near the target is exactly when discovery is needed
        seeds = [r[0] for r in conn.execute("SELECT spotify_id FROM taste WHERE weight > 0 ORDER BY weight DESC LIMIT 5")]
        if not seeds:
            seeds = [t["id"] for t in sorted(pool, key=lambda t: -t["weight"])[:5]]

        def discover(target, floor, window):
            if str(settings.get("discovery") or "0") != "1" or not seeds:
                return []
            try:
                recs = rb_recommend(seeds, target * (1 - window), target * (1 + window), floor)
                if not recs:
                    return []
                feats = rb_features([r["id"] for r in recs])
                out = []
                half = str(settings.get("half_time") or "0") == "1"
                for r in recs:
                    f = feats.get(r["id"])
                    if not f or not f.get("tempo") or not r.get("duration_ms"):
                        continue
                    # ReccoBeats treats the tempo bounds as a hint, not a filter (probed 2026-09-06:
                    # a 158–182 request returned 89–149 bpm), so the window is applied HERE, on the
                    # features it reports — only a track the picker could use enters the cache
                    if not _tempo_hit(f["tempo"], target, window, half):
                        continue
                    _upsert_track(conn, r, discovered=1)
                    conn.execute("UPDATE track SET tempo=?, energy=?, danceability=?, valence=?, loudness=?, features_at=? WHERE spotify_id=?",
                                 (f["tempo"], f["energy"], f["danceability"], f["valence"], f["loudness"], _now_iso(), r["id"]))
                    out.append({**r, "tempo": f["tempo"], "energy": f["energy"], "weight": 0.0, "discovered": 1})
                conn.commit()
                return out
            except Exception as e:
                print(f"[music] discovery failed: {e}")
                return []

        res = plan_for_session(session, curve, pool, settings, discover=discover, ramp=ramp,
                               set_aside=set_aside(conn), follow=follow_scores(conn),
                               served=served_counts(conn, date.today(), exclude_key=key),
                               fresh=never_run(conn, exclude_key=key))
        if not res.get("ok"):
            return res
        res.update(session={k: session.get(k) for k in ("date", "kind", "km", "minutes", "note", "race", "pace_zone")},
                   key=key, curve={"comfort_max": curve["comfort_max"], "comfort_source": curve["comfort_source"]} if curve else None,
                   ramp=ramp)
        res["name"] = playlist_name(session, res["target_spm"])
        res["description"] = playlist_description(res["segments"], res["notes"])
        if not write:
            return res
        token = sp_access_token()
        if not token:
            res.update(written=False, error="Spotify is not connected — the plan above is what would be written")
            return res
        uris = [f"spotify:track:{t['id']}" for sg in res["segments"] for t in sg["tracks"]]
        if not uris:
            res.update(written=False, error="no tracks matched — refresh the library, or widen the comfort ceiling")
            return res
        prev = conn.execute("SELECT spotify_id FROM playlist WHERE key=?", (key,)).fetchone()
        try:
            pid, url = sp_write_playlist(token, prev["spotify_id"] if prev else None, res["name"], res["description"], uris)
        except Exception as e:
            res.update(written=False, error=f"Spotify refused the playlist: {e}"[:300])
            return res
        spec = json.dumps({"target_spm": res["target_spm"], "sensor_bias_spm": CADENCE_SENSOR_BIAS_SPM,  # §BEAT12
                           "ramp": {k: ramp[k] for k in ("fraction", "week", "weeks", "phase")} if ramp else None,
                           "rung": ramp.get("rung") if ramp else None, "segments": [
            {"label": s["label"], "effort": s.get("effort"), "minutes": s["minutes"], "target_spm": s["target_spm"],
             "reps": s.get("reps"),
             "tracks": [{"id": t["id"], "title": t["title"], "artist": t["artist"], "tempo": t["tempo"],
                         "energy": t["energy"], "hit": t["hit"], "source": t.get("source"),
                         "never_run": bool(t.get("never_run"))} for t in s["tracks"]]} for s in res["segments"]]})
        conn.execute("INSERT INTO playlist(key, spotify_id, name, url, built_at, spec) VALUES(?,?,?,?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET spotify_id=excluded.spotify_id, name=excluded.name, "
                     "url=excluded.url, built_at=excluded.built_at, spec=excluded.spec",
                     (key, pid, res["name"], url, _now_iso(), spec))
        conn.commit()
        res.update(written=True, playlist_id=pid, url=url, uri=f"spotify:playlist:{pid}")
        return res
    finally:
        conn.close()


def playlists_built(conn=None):
    own = conn is None
    conn = conn or _mdb()
    try:
        return [dict(r) for r in conn.execute("SELECT key, spotify_id, name, url, built_at FROM playlist ORDER BY key DESC")]
    finally:
        if own:
            conn.close()


def recent_cadence(db, days=14):
    """What was RUN lately — the read-back the ladder climbs from."""
    since = (date.today() - timedelta(days=days)).isoformat()
    out = []
    for d, kmh, cad, _ in cadence_rows(db, date.today()):
        if d >= since:
            out.append({"date": d, "pace": _fmt_pace(3600.0 / kmh) if kmh else None, "spm": round(cad)})
    return sorted(out, key=lambda r: r["date"])[-10:]


# ── The page: shell bits the app's renderer drops into its placeholders ───────
def render_bits(page, readonly):
    """The strings `_render_app` substitutes. Empty on the public box (private-only page). On every
    private page: the header link, the mobile tab, the stylesheet, the script and the Settings tab
    (0.68.0 — the connections, the matching knobs and the library live in Settings → Music, so the
    dialog needs the module's panel on every page that opens it); the section only on /music."""
    if readonly:
        return {"link": "", "mobnav": "", "css": "", "page": "", "js": "", "settings": ""}
    link = ('<a class="ghost" id="musicLink" href="/music" title="Music — cadence-matched playlists '
            'for the plan\'s sessions">♫ Music</a>')
    mobnav = ('<a class="mnav-btn" id="mnavmusic" href="/music" aria-current="false" aria-label="Music">'
              '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18V6l11-2v12"/>'
              '<circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/></svg><span>Music</span></a>')
    ver = S.ENGINE_VERSION
    return {"link": link, "mobnav": mobnav,
            "css": f'<link rel="stylesheet" href="/static/music.css?v={ver}">',
            "page": PAGE_HTML if page == "music" else "",
            "settings": SETTINGS_HTML,
            "js": f'<script nonce="__SH_NONCE__" src="/static/music.js?v={ver}"></script>'}


SETTINGS_HTML = """
        <section class="settab" data-tab="music" data-label="Music" role="tabpanel" id="settab-music" hidden>
          <div id="settingsMusic">
            <div class="mgrid msetgrid">
              <div class="mstatus" id="mSpotify"><div class="empty">Loading…</div></div>
              <div class="mstatus" id="mServices"><div class="empty">Loading…</div></div>
            </div>
            <details class="msub mkeys"><summary>Keys — the Spotify app, last.fm, ListenBrainz</summary>
              <div id="musicKeys"></div>
              <div class="mhint">Paste a value and press Save below. The Spotify app needs this redirect URI registered: <code id="mRedirect"></code></div>
            </details>
            <div class="sectitle" style="margin-top:16px">Matching</div>
            <div id="mMatching"></div>
            <div class="sectitle" style="margin-top:16px">Library</div>
            <div id="mLibrary"><div class="empty">Loading…</div></div>
            <div class="setbar" style="margin-top:14px"><button type="button" class="primary" id="mSave">Save</button><span class="ok" id="mSaveMsg"></span></div>
          </div>
        </section>"""


PAGE_HTML = """
    <div class="section" id="sec-music" role="region" aria-labelledby="h-music">
      <div class="mtitle">
        <h2 id="h-music">Music</h2>
        <span class="mprog" id="musicStatus">Loading…</span>
        <button type="button" class="ghost" id="musicSettingsBtn">Music settings</button>
      </div>
      <div class="panel mhero" id="musicNext"><div class="empty">Loading…</div></div>
      <div class="panel" id="musicResult" hidden></div>
      <div class="mgrid">
        <div class="panel mcard" id="musicSessions"><div class="empty">Loading…</div></div>
        <div class="panel mcard" id="musicCad"><div class="empty">Loading…</div></div>
      </div>
      <div class="panel" id="musicReadback"><div class="empty">Loading…</div></div>
      <details class="section msub" id="musicHow">
        <summary><h3>How the targets are set</h3></summary>
        <div class="panel help">
          <div id="musicCurveTable"></div>
          <p>Cadence only means something against speed, so the reference is this runner's own speed–cadence
          curve — the recent one and the trained one — and never a number from a book. Each segment of a session
          gets the recent curve's cadence at that segment's pace, lifted by the step, capped by the trained
          curve and by the comfort ceiling. Music only entrains the legs inside about ±2 % of the cadence they
          already run, so each build asks for a little more than the last, and the read-back after the run
          moves the recent curve, which moves the next target.</p>
          <p>With a race on the road the lift has a calendar: a twelve-week ramp from the recent curve to the
          trained one that lands three weeks before the race, so the taper is run at the race's cadence. The
          ramp asks for the fraction of the gap the calendar has reached, never less than the step, and never
          more than one entrainment step over what the legs showed on the last read-back with a playlist — a
          rung run under its target holds the ramp there until it is held. Without a read-back the rung is one
          step over the recent curve. Every row names which of these set it.</p>
          <p>Tracks come from the listening history: last.fm scrobbles and Spotify's saved and top tracks. With a
          ListenBrainz user set, the refresh also reads what the most similar listeners there play, ListenBrainz's
          own collaborative picks and its weekly exploration lists, and each list carries a few songs never run to
          before, the rest coming round in rotation. Tempo and energy come from ReccoBeats, which reproduces the
          audio features Spotify withdrew. A track qualifies when its tempo sits inside the window around the
          target; taste, the legs' own verdict and the rotation order the rest. Full-time means one beat per step;
          half-time, off by default, also accepts a track whose beat is one per stride. The connections, the
          matching knobs and the library live in Settings → Music.</p>
        </div>
      </details>
    </div>
"""


# ── Registration: routes and the secrets the Settings window renders ─────────
def register(app, host):
    global S
    # §BEAT graduation (0.69.0) — the demo is the full console over a synthetic athlete, but it holds
    # no secret and refuses to take one, so a music page there could only ever say "connect Spotify"
    # while its refresh routes reached last.fm, ListenBrainz and ReccoBeats on a stranger's behalf.
    # No page, no routes, no settings rows on the demo (the shell's placeholders stay empty too).
    if getattr(host, "DEMO", False):
        return
    S = host
    S.SECRET_SPEC.extend([
        {"key": "spotify_client_id", "env": "SPOTIFY_CLIENT_ID", "label": "Spotify app client ID",
         "help": "From developer.spotify.com/dashboard → an app of your own (Development Mode needs a "
                 "Premium account). Register this instance's redirect URI on it — the Music page shows "
                 "the exact address. Optional — enables cadence-matched playlists."},
        {"key": "spotify_client_secret", "env": "SPOTIFY_CLIENT_SECRET", "label": "Spotify app client secret",
         "help": "The client secret of the same app."},
        {"key": "lastfm_api_key", "env": "LASTFM_API_KEY", "label": "last.fm API key",
         "help": "From last.fm/api/account/create — the key reads public scrobbles, so it is the only "
                 "thing needed to weigh tracks by how often they were actually played. Optional."},
        {"key": "listenbrainz_token", "env": "LISTENBRAINZ_TOKEN", "label": "ListenBrainz user token",
         "help": "From listenbrainz.org/settings. With a ListenBrainz user set on the Music page, each library "
                 "refresh reads what similar listeners play, the collaborative picks and the weekly exploration "
                 "lists — those are public; the token adds LB radio around the top artists. Optional."},
    ])
    S.SECRET_BY_KEY.update({s["key"]: s for s in S.SECRET_SPEC})
    S.SECRET_VALIDATORS.update({"spotify_client_id": _validate_spotify_secret,
                                "spotify_client_secret": _validate_spotify_secret,
                                "lastfm_api_key": _validate_lfm_secret,
                                "listenbrainz_token": _validate_lb_secret})
    jsonify, request, redirect = S.jsonify, S.request, S.redirect

    def _redirect_uri():
        host_ = request.host
        scheme = "http" if host_.split(":")[0] in ("127.0.0.1", "localhost", "0.0.0.0", "[::1]") else "https"
        return f"{scheme}://{host_}/api/music/spotify/callback"

    @app.get("/music")
    def music_page():
        if S.READONLY:
            return redirect("/")
        return S._render_app("music")

    @app.get("/api/music/status")
    def api_music_status():
        db = S.get_db()
        settings = get_settings()
        curve = cadence_curve(cadence_rows(db, date.today()), date.today(), settings)
        try:                                  # §BEAT2 — bank the plays every time the page opens
            pull_played()
        except Exception as e:
            print(f"[music] play history pull failed: {e}")
        conn = _mdb()
        try:
            played = {"count": conn.execute("SELECT COUNT(*) FROM played").fetchone()[0],
                      "last": conn.execute("SELECT MAX(played_at) FROM played").fetchone()[0]}
            ratings = {"count": conn.execute("SELECT COUNT(*) FROM rating").fetchone()[0],
                       "file": ratings_file().exists()}
            sources = source_scores(conn)
        finally:
            conn.close()
        return jsonify(ok=True, spotify=spotify_status(), redirect_uri=_redirect_uri(),
                       lastfm={"user": settings.get("lastfm_user"), "key": bool(_lfm_key())},
                       listenbrainz={"user": settings.get("listenbrainz_user"), "token": bool(_lb_token()),
                                     "sources": sources, "fresh_quota": MUSIC_FRESH_QUOTA},
                       settings=settings, library=library_status(), curve=curve,
                       playlists=playlists_built(), recent=recent_cadence(db),
                       played=played, ratings=ratings,
                       constants={"window": MUSIC_TEMPO_WINDOW, "max_step": MUSIC_MAX_STEP_PCT, "work_step": MUSIC_WORK_STEP_PCT, "tail_min": MUSIC_TAIL_MIN,
                                  "seam_s": MUSIC_SONG_SEAM_S, "rotation_days": MUSIC_ROTATION_DAYS,
                                  "entrain_pct": MUSIC_ENTRAIN_PCT, "dip_spm": MUSIC_DIP_SPM, "dip_min_s": MUSIC_DIP_MIN_S,
                                  "score": {"taste": MUSIC_SCORE_TASTE, "follow": MUSIC_SCORE_FOLLOW,
                                            "close": MUSIC_SCORE_CLOSE, "served": MUSIC_SCORE_SERVED}})

    @app.post("/api/music/played/pull")
    def api_music_played_pull():
        st = spotify_status()
        if not st["connected"]:
            return jsonify(ok=False, error="Spotify is not connected"), 400
        if "user-read-recently-played" in st["scopes_missing"]:
            return jsonify(ok=False, error="reconnect Spotify to grant the play history"), 400
        try:
            n = pull_played()
        except Exception as e:
            return jsonify(ok=False, error=str(e)[:200]), 502
        return jsonify(ok=True, new=n)

    @app.post("/api/music/ratings/import")
    def api_music_ratings_import():
        f = ratings_file()
        if not f.exists():
            return jsonify(ok=False, error=f"no ratings file at {f.name} beside music.db"), 404
        try:
            rows = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            return jsonify(ok=False, error=f"ratings file unreadable: {e}"[:200]), 400
        return jsonify(ok=True, imported=import_ratings(rows if isinstance(rows, list) else []))

    @app.post("/api/music/rate")
    def api_music_rate():
        d = S.body()
        n = import_ratings([{"spotify_id": str(d.get("spotify_id") or ""), "rating": d.get("rating"), "note": d.get("note")}])
        return (jsonify(ok=True), 200) if n else (jsonify(ok=False, error="rating must be pushes, relaxes, neutral or mixed"), 400)

    @app.post("/api/music/verdict")
    def api_music_verdict():
        """§BEAT14 — "leave it out": a verdict given on the read-back page after the run, equivalent
        to two lap presses. §BEAT16 — "keep" = this song stays, whatever the run said: it pardons a
        manual never and the run's own skip or double press for that run. Kept in `manual_verdict`,
        apart from the run's own `rating_run`, so either verdict survives a "Read again"."""
        d = S.body()
        try:
            run_id = int(d.get("run_id"))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="run_id must be a number"), 400
        spotify_id = d.get("spotify_id")
        if not isinstance(spotify_id, str) or not spotify_id:
            return jsonify(ok=False, error="spotify_id is required"), 400
        verdict = d.get("verdict")
        if verdict not in ("never", "keep"):
            return jsonify(ok=False, error="verdict must be never or keep"), 400
        conn = _mdb()
        try:
            if verdict == "never":
                conn.execute("INSERT OR REPLACE INTO manual_verdict(run_id, spotify_id, rating, role, at) "
                             "VALUES(?, ?, 'never', 'both', ?)", (run_id, spotify_id, _now_iso()))
            else:
                conn.execute("INSERT OR REPLACE INTO manual_verdict(run_id, spotify_id, rating, role, at) "
                             "VALUES(?, ?, 'keep', 'both', ?)", (run_id, spotify_id, _now_iso()))
            conn.commit()
        finally:
            conn.close()
        return jsonify(ok=True, verdict=verdict)

    @app.get("/api/music/runs")
    def api_music_runs():
        return jsonify(ok=True, runs=recent_runs(S.get_db()))

    @app.get("/api/music/readback/<int:run_id>")
    def api_music_readback_stored(run_id):
        """0.68.0 — the read-back on file for a run (the nightly's, or an earlier click), so the page
        shows the last run without recomputing it; 404 when none is stored."""
        conn = _mdb()
        try:
            r = conn.execute("SELECT payload, computed_at FROM readback WHERE run_id=?", (run_id,)).fetchone()
            if not r:
                return jsonify(ok=False, error="no read-back stored for this run"), 404
            try:
                payload = json.loads(r["payload"] or "{}")
            except ValueError:
                return jsonify(ok=False, error="the stored read-back is unreadable"), 500
            payload["computed_at"] = r["computed_at"]
            payload = apply_manual_verdicts(conn, run_id, payload)     # §BEAT14 — a verdict given since
        finally:
            conn.close()
        return jsonify(**payload)

    @app.post("/api/music/readback")
    def api_music_readback():
        try:
            run_id = int(S.body().get("run_id") or 0)
        except (TypeError, ValueError):
            return jsonify(ok=False, error="run_id must be a number"), 400
        res = readback(S.get_db(), run_id)
        return jsonify(**res), (200 if res.get("ok") else 400)

    @app.get("/api/music/cadence")
    def api_music_cadence():
        db = S.get_db()
        settings = get_settings()
        curve = cadence_curve(cadence_rows(db, date.today()), date.today(), settings)
        row = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
        plan = json.loads(row["plan"]) if row else None
        zones = (plan or {}).get("pace_zones") or {}
        ramp = ramp_state(plan, curve, date.today())
        return jsonify(ok=True, curve=curve, ramp=ramp,
                       table=cadence_table(curve, zones, settings.get("step_pct"), ramp) if curve else [],
                       recent=recent_cadence(db))

    @app.post("/api/music/settings")
    def api_music_settings():
        d = S.body()
        errors = {}
        for k, v in d.items():
            ok, err = validate_music_setting(k, v)
            if not ok:
                errors[k] = err
        if errors:
            return jsonify(ok=False, errors=errors), 400
        save_settings(d)
        return jsonify(ok=True, settings=get_settings())

    @app.get("/api/music/sessions")
    def api_music_sessions():
        db = S.get_db()
        row = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
        plan = json.loads(row["plan"]) if row else None
        settings = get_settings()
        curve = cadence_curve(cadence_rows(db, date.today()), date.today(), settings)
        ramp = ramp_state(plan, curve, date.today())
        built = {p["key"]: p for p in playlists_built()}
        out = []
        for s in upcoming_sessions(plan, date.today()):
            segs = session_segments(s)
            targets = [target_spm(curve, sg["pace_sec"], settings.get("step_pct"), ramp,
                                  work=(sg["effort"] in MUSIC_WORK_EFFORTS)) for sg in segs] if curve else []
            spms = [t["spm"] for t in targets if t]
            overall = (sum(t * sg["minutes"] for t, sg in zip(spms, segs)) / max(1e-9, sum(sg["minutes"] for sg in segs))
                       if spms and len(spms) == len(segs) else None)
            out.append({k: s.get(k) for k in ("key", "date", "kind", "km", "minutes", "note", "race", "pace_zone", "pk")}
                       | {"segments": len(segs), "target_spm": round(overall, 1) if overall else None,
                          "built": built.get(s["key"]),
                          # 0.68.0 — the hero shows the segments' targets without a preview
                          "segs": [{"label": sg["label"], "effort": sg.get("effort"), "minutes": sg["minutes"],
                                    "spm": round(t["spm"], 1) if t else None, "why": (t or {}).get("why")}
                                   for sg, t in zip(segs, targets)] if curve else [],
                          "why": (targets[0] or {}).get("why") if targets and targets[0] else None})
        return jsonify(ok=True, sessions=out, has_plan=bool(plan), has_curve=bool(curve))

    @app.post("/api/music/preview")
    def api_music_preview():
        key = str(S.body().get("key") or "")
        return jsonify(**build_playlist(S.get_db(), key, write=False))

    @app.post("/api/music/build")
    def api_music_build():
        key = str(S.body().get("key") or "")
        res = build_playlist(S.get_db(), key, write=True)
        return jsonify(**res), (200 if res.get("ok") else 400)

    @app.post("/api/music/library/refresh")
    def api_music_refresh():
        if not sp_access_token() and not (_lfm_key() and get_settings().get("lastfm_user")):
            return jsonify(ok=False, error="connect Spotify, or set a last.fm user and key, first"), 400
        started = start_refresh()
        return jsonify(ok=True, started=started, library=library_status())

    @app.get("/api/music/library/status")
    def api_music_library():
        return jsonify(ok=True, library=library_status())

    @app.get("/api/music/spotify/connect")
    def api_music_connect():
        conf = _sp_conf()
        if not (conf["spotify_client_id"] and conf["spotify_client_secret"]):
            return jsonify(ok=False, error="set the Spotify client ID + secret in Settings first"), 400
        state = secrets.token_urlsafe(24)
        _oauth_state.clear()
        _oauth_state[state] = time.time()
        q = urlencode({"response_type": "code", "client_id": conf["spotify_client_id"], "scope": SP_SCOPES,
                       "redirect_uri": _redirect_uri(), "state": state})
        return redirect(f"{SP_ACCOUNTS}/authorize?{q}")

    @app.get("/api/music/spotify/callback")
    def api_music_callback():
        state, code = request.args.get("state", ""), request.args.get("code", "")
        issued = _oauth_state.pop(state, None)
        if not issued or time.time() - issued > MUSIC_OAUTH_STATE_TTL_S:
            return redirect("/music?spotify=state_error")
        if not code:
            return redirect("/music?spotify=denied")
        try:
            tok = _sp_token_request({"grant_type": "authorization_code", "code": code,
                                     "redirect_uri": _redirect_uri()})
        except Exception as e:
            print(f"[music] spotify code exchange failed: {e}")
            return redirect("/music?spotify=exchange_error")
        try:
            me = sp_me(tok["access_token"])
            tok["user"] = me.get("display_name") or me.get("id")
        except Exception:
            tok["user"] = None
        _save_sp_tokens(tok)
        print(f"[music] spotify connected as {tok.get('user')}")
        return redirect("/music?spotify=connected")

    @app.post("/api/music/spotify/disconnect")
    def api_music_disconnect():
        _save_sp_tokens(None)
        return jsonify(ok=True, spotify=spotify_status())


def _validate_spotify_secret(value):
    """The app credentials only prove themselves in the OAuth dance; once connected, a /me call
    exercises them through the refresh path. Before that: 'unknown' (the badge says 'configured')."""
    if not _sp_tokens():
        return "unknown"
    tok = sp_access_token()
    if not tok:
        return "unknown"
    r = _sp_call("GET", "/me", tok)
    if r.status_code == 200:
        return "valid"
    return "invalid" if r.status_code in (401, 403) else "unknown"


def _validate_lb_secret(value):
    """ListenBrainz answers /validate-token for the token itself."""
    try:
        r = requests.get(f"{LB_BASE}/validate-token", headers={"User-Agent": S.USER_AGENT, "Authorization": f"Token {value}"},
                         timeout=8)
        if r.status_code == 200:
            return "valid" if (r.json() or {}).get("valid") else "invalid"
        return "invalid" if r.status_code in (401, 403) else "unknown"
    except Exception:
        return "unknown"


def _validate_lfm_secret(value):
    try:
        r = requests.get(LFM_BASE, params={"method": "user.getinfo", "user": "lastfm", "api_key": value, "format": "json"},
                         headers={"User-Agent": S.USER_AGENT}, timeout=8)
        if r.status_code == 200 and "error" not in r.json():
            return "valid"
        d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        return "invalid" if d.get("error") in (10, 26) or r.status_code in (401, 403) else "unknown"
    except Exception:
        return "unknown"
