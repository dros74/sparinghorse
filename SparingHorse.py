#!/usr/bin/env python3
"""
Sparing Horse — a self-hosted, data-owning running companion built on Runalyze.

Single-file Flask + waitress app: an embedded vanilla SPA over a
locally-owned SQLite copy of your Runalyze data. Reuses Runalyze's computed
sports-science metrics ("current shape") and will grow a dynamic, objective-driven
training-plan engine on top (see PROJECT_LOG.md).

This file is the scaffold: config + SQLite store + Runalyze REST ETL + the dashboard
shell. The plan engine, objectives, and health-markers views come next.

Run locally:   RUNALYZE_TOKEN=... python3 SparingHorse.py   # http://127.0.0.1:8770
Production:    waitress-serve --listen=0.0.0.0:8770 SparingHorse:app
"""
import html
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from flask import Flask, g, jsonify, request
from requests.adapters import HTTPAdapter, Retry

# ── Config ──────────────────────────────────────────────────────────────────
PORT = int(os.environ.get("SH_PORT", "8770"))
DB_PATH = Path(os.environ.get("SH_DB", "sparinghorse.db"))
RUNALYZE_BASE = os.environ.get("RUNALYZE_BASE", "https://runalyze.com/api/v1")
RUNALYZE_TOKEN = os.environ.get("RUNALYZE_TOKEN", "")  # personal API token (token: header)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")  # LLM adjustment layer (§6c)
# Default to the latest capable model; adaptive thinking + low effort for the light parsing/
# judgment tasks the engine hands off. Overridable for cost/latency experiments.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
# Public read-only mode (§ two-version deploy): the public container runs with SH_READONLY=1, a
# read-only DB mount, and NO tokens — so it physically can't sync/write. On top of that the app
# blocks every mutating endpoint, hides all inputs, and withholds the medical sections (blood
# markers + readiness). The private container (behind Cloudflare Access) runs without the flag.
READONLY = os.environ.get("SH_READONLY", "").lower() in ("1", "true", "yes")
# On the public page, an optional "Log in" link to the private (Access-protected) console.
PRIVATE_URL = os.environ.get("SH_PRIVATE_URL", "")
# Optional house/personal branding — a small back-link in the header (e.g. your homepage). Empty = off.
HOUSE_URL = os.environ.get("SH_HOUSE_URL", "")
HOUSE_NAME = os.environ.get("SH_HOUSE_NAME", "")
# Optional per-user athlete context, injected into the LLM prompts (e.g. "post-illness rebuild,
# cleared by my doctor" / "masters runner returning from injury"). Empty = a neutral generic runner.
# The medical SAFETY net (cardiac/exertional symptom → halt + see a doctor) is always on regardless.
ATHLETE_CONTEXT = os.environ.get("SH_ATHLETE_CONTEXT", "").strip()
# Optional weather widget cities: "Name,lat,lon;Name,lat,lon". Empty = the widget is hidden.
RUNNING_SPORT = "Running"  # the Runalyze sport name used to filter runs (the engine is run-focused)
# Runalyze sits behind a WAF. Two learned quirks: (1) a non-browser User-Agent gets
# tarpitted, so present a browser UA; (2) raw stdlib urllib stalls on the large chunked
# /activity response — `requests` (urllib3) handles it. We also pace requests (PAGE_DELAY)
# to stay polite and avoid the per-IP rate limiter.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 SparingHorse/0.1"
)
PAGE_DELAY = 0.6  # seconds between paginated activity requests (WAF politeness)
AUTO_SYNC_THROTTLE = 600  # seconds — opportunistic page-load sync no-ops if synced this recently

_session = None


def _http():
    global _session
    if _session is None:
        s = requests.Session()
        retries = Retry(total=2, backoff_factor=0.8,
                        status_forcelist=(429, 500, 502, 503, 504),
                        allowed_methods=frozenset(["GET"]))
        s.mount("https://", HTTPAdapter(max_retries=retries))
        s.headers.update({
            "token": RUNALYZE_TOKEN,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        })
        _session = s
    return _session

# ── Runalyze REST client ────────────────────────────────────────────────────
class RunalyzeError(RuntimeError):
    pass


def _get(path, params=None, timeout=25):
    """GET a Runalyze Personal API endpoint as JSON. Auth via the `token` header."""
    if not RUNALYZE_TOKEN:
        raise RunalyzeError("RUNALYZE_TOKEN is not set")
    url = f"{RUNALYZE_BASE}/{path.lstrip('/')}"
    try:
        r = _http().get(url, params=params, timeout=timeout)
    except requests.RequestException as e:
        raise RunalyzeError(f"network error on {path}: {e}") from e
    if r.status_code != 200:
        raise RunalyzeError(f"HTTP {r.status_code} on {path}: {r.text[:200]!r}")
    return r.json()


def fetch_statistics_current():
    """The 'current shape' object — all of Runalyze's computed metrics."""
    return _get("statistics/current")


# ── MCP client (only for per-point activity `streams`) ───────────────────────
# The REST trackdata endpoint is scope-gated (403); the per-point trace (HR/pace/cadence vs
# distance) is only reachable via the MCP server. Used solely for the latest-activity hover
# profiles — everything else stays on the REST path. Bearer auth = "pt#" + the personal token.
MCP_URL = "https://runalyze.com/mcp"
_mcp_session = None


def _mcp_headers():
    h = {"Authorization": f"Bearer pt#{RUNALYZE_TOKEN}", "Content-Type": "application/json",
         "Accept": "application/json, text/event-stream", "User-Agent": USER_AGENT}
    if _mcp_session:
        h["Mcp-Session-Id"] = _mcp_session
        h["Mcp-Protocol-Version"] = "2025-06-18"
    return h


def _mcp_parse(text):
    if text.lstrip().startswith("{"):
        return json.loads(text)
    # SSE framing → concatenate data: lines
    data = "".join(l[5:] for l in text.splitlines() if l.startswith("data:"))
    return json.loads(data)


def _mcp_init():
    global _mcp_session
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "sparinghorse", "version": "0.1"}}}
    r = _http().post(MCP_URL, json=body, headers=_mcp_headers(), timeout=30)
    _mcp_session = r.headers.get("Mcp-Session-Id")
    _http().post(MCP_URL, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                 headers=_mcp_headers(), timeout=30)


def mcp_call(tool, args):
    """Call an MCP tool, returning its structuredContent. Re-inits the session on failure."""
    if not _mcp_session:
        _mcp_init()
    body = {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": tool, "arguments": args}}
    r = _http().post(MCP_URL, json=body, headers=_mcp_headers(), timeout=45)
    d = _mcp_parse(r.text)
    if "error" in d:  # likely stale session → re-init once
        _mcp_init()
        r = _http().post(MCP_URL, json=body, headers=_mcp_headers(), timeout=45)
        d = _mcp_parse(r.text)
    res = d.get("result", {})
    return res.get("structuredContent") or json.loads(res["content"][0]["text"])


def cadence_is_halved(source):
    """Suunto logs cadence as a one-leg step count → double it for true spm. Conditioned on
    source so other devices (which report full spm) aren't wrongly doubled."""
    return (source or "").lower() == "suunto"


# Bump when activity_profile's shape changes (new channel etc.) so cached profiles in trackcache
# that predate the bump are re-fetched instead of served stale. v2 = elevation; v3 = route path (lat/long).
PROFILE_VERSION = 3


def activity_profile(activity_id, n=120):
    """Downsampled pace/HR/cadence/elevation-vs-distance profile for one activity (via MCP streams).
    Returns {dist[], pace[], hr[], cadence[], elevation[], hr_avg, v, has_*} — pace in sec/km,
    dist in km, elevation in metres."""
    det = mcp_call("get_activity_details", {"activity_id": int(activity_id)})
    act = det.get("activity", det)
    cad_mult = 2 if cadence_is_halved(act.get("source")) else 1
    s = act.get("streams") or {}
    dist, tim, hr, cad = (s.get("distance") or [], s.get("time") or [],
                          s.get("heart_rate") or [], s.get("cadence") or [])
    # DEM/barometric-corrected altitude first, raw GPS altitude as fallback (metres). Pick the first
    # channel that actually carries values — a present-but-all-null list (no DEM correction happened)
    # must not shadow a populated original.
    elev = next((a for a in (s.get("elevation_corrected"), s.get("elevation_original"))
                 if a and any(v is not None for v in a)), [])
    lat, lon = s.get("latitude") or [], s.get("longitude") or []   # GPS track for the route map
    if not dist or not tim or len(dist) != len(tim):
        return {"dist": [], "pace": [], "hr": [], "cadence": [], "elevation": [], "path": [],
                "has_gps": False, "hr_avg": act.get("average_heart_rate"), "v": PROFILE_VERSION}
    total = dist[-1] or 1
    km = total > 100  # distance likely in metres if it exceeds 100 → normalise to km
    scale = 1000.0 if km else 1.0
    out_d, out_p, out_h, out_c, out_e, out_path = [], [], [], [], [], []
    for i in range(n):
        target = total * i / (n - 1)
        # nearest index by distance
        j = min(range(len(dist)), key=lambda k: abs(dist[k] - target))
        j2 = min(len(dist) - 1, j + max(1, len(dist) // n))
        dd = (dist[j2] - dist[j]) / scale
        dt = tim[j2] - tim[j]
        pace = (dt / dd) if dd > 0 else None  # sec/km
        out_d.append(round(dist[j] / scale, 3))
        out_p.append(round(pace) if pace and pace < 1200 else None)
        out_h.append(hr[j] if j < len(hr) else None)
        cv = cad[j] if j < len(cad) else None
        out_c.append(cv * cad_mult if cv is not None else None)
        out_e.append(round(elev[j], 1) if j < len(elev) and elev[j] is not None else None)
        if j < len(lat) and j < len(lon) and lat[j] is not None and lon[j] is not None:
            out_path.append([round(lat[j], 5), round(lon[j], 5)])   # ~1 m precision, small payload
    return {"dist": out_d, "pace": out_p, "hr": out_h, "cadence": out_c, "elevation": out_e,
            "path": out_path, "hr_avg": act.get("average_heart_rate"), "v": PROFILE_VERSION,
            "has_pace": any(p for p in out_p), "has_hr": any(h for h in out_h),
            "has_cadence": any(c for c in out_c),
            "has_elevation": any(e is not None for e in out_e),
            "has_gps": len({tuple(p) for p in out_path}) >= 2}   # ≥2 distinct points = a real route


def fetch_activities_page(page=1):
    """One page (100) of activities, newest first. Returns a list."""
    data = _get("activity", {"page": page})
    return data if isinstance(data, list) else data.get("items", data.get("data", []))


# ── SQLite store ────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    id            INTEGER PRIMARY KEY,
    date_time     TEXT,
    date          TEXT,           -- YYYY-MM-DD (local), for weekly aggregation
    sport         TEXT,
    sport_id      INTEGER,
    distance      REAL,           -- km
    duration      REAL,           -- seconds (moving)
    elapsed_time  REAL,
    hr_avg        INTEGER,
    hr_max        INTEGER,
    trimp         REAL,
    training_effect REAL,
    recovery_time REAL,
    raw           TEXT,           -- full activity JSON (source of truth for the rest)
    synced_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);

-- One row per day: our owned time-series of "current shape" (the API only gives today's).
CREATE TABLE IF NOT EXISTS shape_snapshots (
    snapshot_date     TEXT PRIMARY KEY,   -- YYYY-MM-DD
    captured_at       TEXT,
    effective_vo2max  REAL,
    effective_vo2max_progress REAL,
    fitness           REAL,   -- CTL
    fatigue           REAL,   -- ATL
    performance       REAL,   -- TSB (form)
    fitness_pct       REAL,
    acwr              REAL,   -- RATIO (e.g. 0.95). Optimum band 0.8–1.3. (API mixes units!)
    marathon_shape    REAL,
    hrv_baseline      REAL,
    monotony          REAL,
    training_strain   REAL,
    raw               TEXT
);

-- Health markers (manually entered lab values + body metrics), kept local — a metabolic marker
-- (e.g. triglycerides) can precede a performance change, so these overlay against training load.
-- One row per (marker, date).
CREATE TABLE IF NOT EXISTS health_markers (
    marker   TEXT NOT NULL,        -- key from MARKERS registry, e.g. 'triglycerides'
    date     TEXT NOT NULL,        -- YYYY-MM-DD
    value    REAL NOT NULL,
    source   TEXT,                 -- 'lab' | 'manual' | 'runalyze'
    note     TEXT,
    PRIMARY KEY (marker, date)
);

-- Objectives (races/goals). Add & remove are symmetric, both reshape the plan (§6b).
CREATE TABLE IF NOT EXISTS objectives (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    type       TEXT,                 -- 5k | 10k | half | marathon | custom
    label      TEXT,
    date       TEXT,                 -- YYYY-MM-DD (the peak point)
    target     TEXT,                 -- goal time string or 'finish'
    priority   TEXT DEFAULT 'A',     -- A | B | C
    status     TEXT DEFAULT 'upcoming',  -- upcoming | done | removed | lapsed
    created_at TEXT
);

-- Versioned training plans. Each generation is a new row → diff-able history (§4).
CREATE TABLE IF NOT EXISTS plans (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT,
    for_date   TEXT,                 -- 'today' the plan was generated from
    inputs     TEXT,                 -- JSON: shape + objectives it was built from
    plan       TEXT                  -- JSON: phases + weeks + sessions + pace zones
);

-- Daily readiness check-ins (§6d gate). The subjective inputs are the safety net — esp.
-- `stop_symptom` (a stop-the-run exertional symptom), which halts the plan and flags "see a doctor".
CREATE TABLE IF NOT EXISTS readiness (
    date         TEXT PRIMARY KEY,
    energy       TEXT,             -- good | ok | heavy
    sleep        TEXT,             -- good | ok | poor
    stop_symptom INTEGER DEFAULT 0,
    note         TEXT,
    created_at   TEXT
);

-- Cached per-activity profile (pace/HR/cadence vs distance), downsampled. Fetched from the
-- MCP `streams` (the REST trackdata endpoint is scope-gated) — for the latest-activity hover.
CREATE TABLE IF NOT EXISTS trackcache (
    activity_id INTEGER PRIMARY KEY,
    profile     TEXT,
    cached_at   TEXT
);

-- Qualitative adjustments (§6c). The owner's free-text input ("knee's sore", "travelling")
-- is parsed by the LLM into a bounded directive, CLAMPED by the engine, and applied as a
-- forward window. Stored so the plan stays a pure function of (today, shape, objectives,
-- adjustments) and each change is versioned/diff-able.
CREATE TABLE IF NOT EXISTS adjustments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT,
    note         TEXT,              -- the owner's raw words
    directive    TEXT,              -- JSON: the engine-clamped directive that was applied
    applies_from TEXT,              -- YYYY-MM-DD inclusive
    applies_until TEXT,             -- YYYY-MM-DD inclusive
    active       INTEGER DEFAULT 1  -- 0 once superseded/cleared
);

-- Session log (the daily-workflow journal). A reflection on how a run felt attaches to its
-- day; it never changes the plan's forward load — that's what `adjustments` is for. "Done"
-- and actual-vs-planned are derived by matching synced `activities` to the planned session by
-- date, so only the free-text reflection needs storing here.
CREATE TABLE IF NOT EXISTS session_log (
    date       TEXT PRIMARY KEY,   -- YYYY-MM-DD the reflection is about
    note       TEXT,
    created_at TEXT
);

-- Manual data-quality ignore-list: activities the owner flags as duplicates or mis-tagged
-- that the exact-match heuristic (find_duplicates) can't catch — e.g. a re-upload whose
-- timestamp drifted a few seconds. Honored everywhere the reconstruction de-dups
-- (dropped_ids), persisted across syncs. One-click from the latest-activity tile.
CREATE TABLE IF NOT EXISTS ignored_activities (
    id         INTEGER PRIMARY KEY,   -- the activity id to exclude from the reconstruction
    reason     TEXT,
    created_at TEXT
);

-- Self-test harness (§ diagnostics). Each run is one row: summary counts for quick listing
-- + the full JSON report (scenarios with verbatim inputs/outputs). The point is the key-gated
-- §6c paths run in-process on the tokened private instance and capture the *actual* LLM output,
-- so correctness can be judged from structured results instead of relayed by hand.
CREATE TABLE IF NOT EXISTS selftest_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT,
    source      TEXT,      -- 'server' | 'client' | 'combined'
    passed      INTEGER,
    failed      INTEGER,
    skipped     INTEGER,   -- scenarios gated out (e.g. llm/* with no key)
    needs_human INTEGER,   -- scenarios whose output is captured for human/AI judgment
    llm         INTEGER,   -- was ANTHROPIC_API_KEY available for this run
    report      TEXT       -- full JSON: {summary, env, scenarios:[...]}
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Registry of trackable health markers: label, unit, reference band, and direction
# ("low" = lower is better, "high" = higher is better, "band" = stay within range).
# Generic clinical reference ranges only — no personal data here.
MARKERS = {
    "triglycerides":     {"label": "Triglycerides", "unit": "mg/dL", "ref": [None, 150], "good": "low"},
    "hdl":               {"label": "HDL cholesterol", "unit": "mg/dL", "ref": [55, None], "good": "high"},
    "ldl":               {"label": "LDL cholesterol", "unit": "mg/dL", "ref": [None, 115], "good": "low"},
    "total_cholesterol": {"label": "Total cholesterol", "unit": "mg/dL", "ref": [None, 200], "good": "low"},
    "weight":            {"label": "Weight", "unit": "kg", "ref": [None, None], "good": "band"},
    "vitamin_d":         {"label": "Vitamin D (25-OH)", "unit": "ng/mL", "ref": [30, 100], "good": "band"},
    "ferritin":          {"label": "Ferritin", "unit": "µg/L", "ref": [30, 400], "good": "band"},
    "systolic":          {"label": "Blood pressure (systolic)", "unit": "mmHg", "ref": [None, 130], "good": "low"},
}


def connect_db():
    """One place to open a connection — WAL + a busy timeout so brief read/write
    overlaps wait instead of erroring with 'database is locked'. In public read-only mode the
    connection is hard-set query_only (a DB-layer guard on top of the request guard), and we don't
    touch journal_mode (that's a write — the private side already set WAL persistently)."""
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=15000")
    if READONLY:
        db.execute("PRAGMA query_only=ON")   # this connection physically cannot write
    else:
        db.execute("PRAGMA journal_mode=WAL")
    return db


def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = g._db = connect_db()
    return db


def init_db():
    if READONLY:
        return   # public read-only: the private side owns the schema; never write here
    db = connect_db()
    db.executescript(SCHEMA)
    # Self-healing migration: deactivate any legacy *active* no-op adjustment (multiplier ≥ 1,
    # no easy-only, no medical) saved before the §6c routing fix — those were reflections that
    # got stored as an "Active adjustment" and still render a pointless banner. New no-ops are
    # already blocked at /api/adjustment/apply; this clears the historical ones. Real ease/medical
    # adjustments are never touched.
    for row in db.execute("SELECT id, directive FROM adjustments WHERE active=1").fetchall():
        try:
            if is_noop_adjustment(json.loads(row["directive"])):
                db.execute("UPDATE adjustments SET active=0 WHERE id=?", (row["id"],))
        except (ValueError, TypeError):
            continue
    db.commit()
    db.close()


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _seconds_since(iso):
    """Seconds since an ISO timestamp written by _now_iso (UTC, tz-aware); inf if unparseable."""
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except (ValueError, TypeError):
        return float("inf")


def set_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))


def get_meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


# ── Runtime settings (§ Settings panel) ───────────────────────────────────────
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
    {"key": "tz", "env": "SH_TZ", "label": "Sync timezone", "kind": "line", "default": "UTC",
     "help": "IANA zone (e.g. Europe/Luxembourg) for the nightly-sync wall clock. Applies on the next sync."},
    {"key": "private_url", "env": "SH_PRIVATE_URL", "label": "Private console URL", "kind": "url",
     "help": "The 'Log in' link shown on the PUBLIC page, pointing back to this private console. "
             "Stored here (read from the shared DB) so it survives redeploys; the public container "
             "picks up a change on its next restart."},
]
SETTINGS_BY_KEY = {s["key"]: s for s in SETTINGS_SPEC}
MAX_WEATHER_CITIES = 5   # header widget cap (mirrored client-side as MAX_CITIES in the picker JS)


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


def current_settings(db):
    """The settable set with effective value + provenance — the GET /api/settings payload."""
    out = []
    for s in SETTINGS_SPEC:
        value, source = _resolve_setting(db, s)
        out.append({"key": s["key"], "label": s["label"], "kind": s["kind"],
                    "help": s["help"], "value": value, "source": source})
    return out


def validate_setting(key, value):
    """(ok, error) for one setting's raw string, BEFORE persisting. house_* land in header HTML (now
    escaped at the render site too — this is the friendlier first line of defence + a real http(s)
    scheme check); the rest get a format/parse check so a bad value can't silently disable the widget
    or the nightly sync."""
    value = value if isinstance(value, str) else ""
    if key in ("house_url", "house_name", "private_url") and any(c in value for c in '"<>'):
        return False, "cannot contain quotes or angle brackets"
    if key in ("house_url", "private_url") and value and not re.match(r"^https?://", value):
        return False, "must start with http:// or https://"
    if key == "weather_cities" and value.strip():
        parsed = _parse_weather_cities(value)
        if not parsed:
            return False, "could not parse — use Name,lat,lon;Name,lat,lon"
        if len(parsed) > MAX_WEATHER_CITIES:
            return False, f"at most {MAX_WEATHER_CITIES} cities — remove one to add another"
    if key == "tz" and value.strip():
        try:
            ZoneInfo(value.strip())
        except Exception:
            return False, "not a valid IANA timezone (e.g. Europe/Luxembourg)"
    return True, None


def apply_settings_overrides(db):
    """Overlay the effective (meta → env → default) values onto the module globals the read-sites use.
    Called once at startup and after every save. Single-process deployment (one waitress process, many
    threads sharing these globals), so a save is visible to every request thread at once. The scheduler
    thread reads SYNC_TZ live, but only re-arms its sleep on the NEXT cycle — so a tz change lands on
    the next scheduled sync (as the help text says), not the one already counting down."""
    global ATHLETE_CONTEXT, HOUSE_URL, HOUSE_NAME, WEATHER_CITIES, SYNC_TZ, PRIVATE_URL
    ATHLETE_CONTEXT = _resolve_setting(db, SETTINGS_BY_KEY["athlete_context"])[0].strip()
    HOUSE_URL = _resolve_setting(db, SETTINGS_BY_KEY["house_url"])[0].strip()
    HOUSE_NAME = _resolve_setting(db, SETTINGS_BY_KEY["house_name"])[0].strip()
    PRIVATE_URL = _resolve_setting(db, SETTINGS_BY_KEY["private_url"])[0].strip()
    WEATHER_CITIES = _parse_weather_cities(_resolve_setting(db, SETTINGS_BY_KEY["weather_cities"])[0])
    with _weather_lock:            # cities may have changed → drop the cached bundle so the next
        _weather_cache["at"] = 0.0  # /api/weather refetches instead of serving up-to-30-min-stale cities
    tzname = _resolve_setting(db, SETTINGS_BY_KEY["tz"])[0].strip() or "UTC"
    try:
        SYNC_TZ = ZoneInfo(tzname)
    except Exception:   # validated on save; a stored zone can still be absent from this host's tzdata
        print(f"[settings] ignoring unresolvable tz {tzname!r}; keeping {SYNC_TZ.key}")


def save_settings(db, updates):
    """Validate + persist a {key: raw_string} map to meta, then re-apply the globals. Unknown keys
    are ignored; secrets can't be set (they aren't in SETTINGS_SPEC). All-or-nothing: if ANY value
    fails validation, nothing is written. Returns (ok, errors_by_key)."""
    errors, valid = {}, {}
    for key, val in (updates or {}).items():
        if key not in SETTINGS_BY_KEY:
            continue
        val = "" if val is None else str(val)
        ok, err = validate_setting(key, val)
        if ok:
            valid[key] = val
        else:
            errors[key] = err
    if errors:
        return False, errors
    for key, val in valid.items():
        set_meta(db, "set:" + key, val)
    db.commit()
    apply_settings_overrides(db)
    return True, {}


# ── ETL ─────────────────────────────────────────────────────────────────────
def upsert_activity(db, a):
    sport = a.get("sport") or {}
    sport_name = sport.get("name") if isinstance(sport, dict) else sport
    sport_id = sport.get("id") if isinstance(sport, dict) else a.get("sport_id")
    dt = a.get("date_time") or a.get("datetime") or ""
    db.execute(
        """INSERT OR REPLACE INTO activities
           (id, date_time, date, sport, sport_id, distance, duration, elapsed_time,
            hr_avg, hr_max, trimp, training_effect, recovery_time, raw, synced_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            a.get("id"), dt, dt[:10], sport_name, sport_id,
            a.get("distance"), a.get("duration"), a.get("elapsed_time"),
            a.get("hr_avg"), a.get("hr_max"), a.get("trimp"),
            a.get("fit_training_effect"), a.get("fit_recovery_time"),
            json.dumps(a, separators=(",", ":")), _now_iso(),
        ),
    )


def sync_activities(db, max_pages=60, backfill=False):
    """Pull activities into the owned local copy. Two modes:
    - incremental (default): walk newest-first, stop at the first page that adds nothing new
      (routine sync — fast, only fetches the new activities since last time).
    - backfill=True: walk ALL pages to the end regardless of known/unknown — needed for the
      one-time full-history pull, because the newest pages are already known and the
      incremental stop-condition would otherwise never reach the older history."""
    known = {r["id"] for r in db.execute("SELECT id FROM activities").fetchall()}
    added = 0
    pages = 0
    for page in range(1, max_pages + 1):
        if page > 1:
            time.sleep(PAGE_DELAY)  # WAF politeness
        items = fetch_activities_page(page)
        pages += 1
        if not items:  # past the last page → done
            break
        new_here = 0
        for a in items:
            if a.get("id") not in known:
                upsert_activity(db, a)
                known.add(a.get("id"))
                new_here += 1
        db.commit()  # commit per page → durable progress, no all-or-nothing stall
        added += new_here
        if new_here == 0 and not backfill:
            break  # caught up (incremental only; backfill keeps going to the end)
    return {"added": added, "pages_fetched": pages}


def snapshot_shape(db):
    """Append today's 'current shape' (one row per day; replace if re-run same day)."""
    s = fetch_statistics_current()
    today = datetime.now().strftime("%Y-%m-%d")
    db.execute(
        """INSERT OR REPLACE INTO shape_snapshots
           (snapshot_date, captured_at, effective_vo2max, effective_vo2max_progress,
            fitness, fatigue, performance, fitness_pct, acwr, marathon_shape,
            hrv_baseline, monotony, training_strain, raw)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            today, _now_iso(),
            s.get("effectiveVO2max"), s.get("effectiveVO2maxProgress"),
            s.get("fitness"), s.get("fatigue"), s.get("performance"),
            s.get("fitnessInPercent"), s.get("acuteChronicWorkloadRatio"),
            s.get("marathonShape"), s.get("hrvBaseline"),
            s.get("monotonyValue"), s.get("trainingStrain"),
            json.dumps(s, separators=(",", ":")),
        ),
    )
    return s


def run_sync(backfill=False):
    """Routine incremental pull (default) or a one-time full-history backfill. Backfill is
    needed whenever the local copy is partial — e.g. a fresh machine — because incremental
    stops at the first already-known page and can never reach older history behind it."""
    db = connect_db()
    try:
        act = sync_activities(db, backfill=backfill)
        snapshot_shape(db)
        set_meta(db, "last_sync", _now_iso())
        db.commit()
        return {"ok": True, "activities": act, "last_sync": get_meta(db, "last_sync"),
                "backfill": backfill}
    finally:
        db.close()


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
#  - TIME CONSTANTS are Runalyze's *documented defaults*: ATL=7d, CTL=42d, formula
#    CTL_t = CTL_{t-1}·e^(-1/τ) + TRIMP_t·(1-e^(-1/τ)) — identical to _ewma_step below.
#    (blog.runalyze.com/tutorial/runalyze-understanding-the-calculations)
#  - Reconstruction match at today (CTL 20.83/ATL 20.20 vs Runalyze 21/20) is consistent but
#    WEAK proof of τ on its own: he's at a plateau (CTL≈ATL) where ACWR≈1 regardless of τ.
#    The τ values rest on Runalyze's docs, not this single point. RE-VALIDATE as daily snapshots
#    accrue — especially rebuild weeks where CTL and ATL diverge (the discriminating data).
#  - 2026-06-21 — VALIDATED on live production data at a divergent point: NAS reconstruction
#    CTL 23.98 / ATL 31.5 vs Runalyze 26 / 33 (err −2.02 / −1.5, well inside ±5) on a day with
#    ATL≫CTL — real proof of τ, not a plateau coincidence. Caveat on the self-test, not the model:
#    the latest snapshot is dated today while the last run was a day or two earlier, so it LEADS
#    the activity frontier. On a rest lead-day that's harmless (pure decay); but if you run and
#    haven't synced, the snapshot reflects a session the reconstruction lacks → a malformed
#    comparison that false-fails (this is exactly what a stale local copy showed: ATL err −14.33,
#    a phantom run reconciled by a single impulse fitting CTL and ATL at once — a data-coverage
#    artifact, never a model error). `_stc_projector` (§6k) therefore validates only LIKE-FOR-LIKE
#    (settled rest-day snapshots behind the frontier). Same day-ahead seam the §6j scorecard de-seams.
#  - Caveat: "default" — if the owner changed his Runalyze calc settings, confirm and adjust.
TAU_CTL = 42  # days, "fitness" (CTL) time constant — Runalyze default
TAU_ATL = 7   # days, "fatigue" (ATL) time constant — Runalyze default


def _ewma_step(prev, value, tau):
    return prev + (value - prev) * (1.0 - math.exp(-1.0 / tau))


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


def delete_activity_local(db, aid):
    """Hard-remove an activity from the OWNED local copy + its derived rows (ignore-list entry,
    cached profile). `sync_activities` is insert-only, so a Runalyze deletion never propagates —
    this is the only way to drop a row Runalyze no longer holds. Returns True if a row was removed,
    False if no such id. CAVEAT: if the activity STILL exists on Runalyze, the next incremental sync
    re-inserts it (page 1 is always re-fetched) — this is for activities already removed upstream;
    an accidental delete of a live activity self-heals on re-sync (or a full backfill)."""
    if not db.execute("SELECT 1 FROM activities WHERE id=?", (aid,)).fetchone():
        return False
    db.execute("DELETE FROM activities WHERE id=?", (aid,))
    db.execute("DELETE FROM ignored_activities WHERE id=?", (aid,))
    db.execute("DELETE FROM trackcache WHERE activity_id=?", (aid,))
    db.commit()
    return True


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


def current_model(db):
    """Today's modeled CTL/ATL, with the Runalyze snapshot for comparison/validation."""
    hist = reconstruct_history(db)
    modeled = hist[-1] if hist else None
    snap = latest_snapshot(db)
    return modeled, (dict(snap) if snap else None)


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


# ── Effort-discipline monitor (§6m) — did each run land in its prescribed effort band? ───────────
# The plan is polarized (easy ≥80%) and the engine KNOWS his easy days run too hard — but it only
# said so once, at plan-gen. This measures it, every run. Core design choice: judge INTENSITY by
# HEART RATE, not pace. Pace is confounded by vertical / heat / wind; HR is the effort response that
# already internalizes them — and Runalyze's GAP / Training-Effect / decoupling are all built from
# HR. So we READ Runalyze's effort outputs, we don't re-model the confounders. HR-LED, TE only
# CORROBORATES (never gates): Firstbeat TE is intensity×DURATION, so a long easy run banks high TE
# from duration alone — gating on TE would false-flag his cleanest easy run (Apr-18, 9.6 km @ HR 138
# / TE 3.0; HR-led correctly returns ON at 73% HRmax). GAP gives a terrain-fair pace for display.
EFFORT_WINDOW_DAYS = 21
EASY_HR_FRAC = 0.78         # %HRmax ceiling for a genuinely easy run (top of Z2)
HARD_HR_FRAC = 0.85         # %HRmax above which an 'easy' run was actually threshold+ effort
TE_HARD_CORROBORATE = 3.5   # Training Effect backing a too-hard HR read → 'high' confidence
AEROBIC_KINDS = {"easy", "long"}    # the well-calibrated direction (his documented failure mode)


def _robust_hrmax(db):
    """A spike-resistant HRmax: the 95th percentile of per-run hr_max (one bad strap reading hits 210
    where his real max is ~189). HR is the gate, so this anchors the zones. None if too little data."""
    hrs = sorted(r["hr_max"] for r in db.execute(
        "SELECT hr_max FROM activities WHERE sport=? AND hr_max IS NOT NULL",
        (RUNNING_SPORT,)).fetchall() if r["hr_max"])
    if not hrs:
        return None
    return hrs[min(len(hrs) - 1, round(0.95 * (len(hrs) - 1)))]


def derive_hr_zones(db, sample=12):
    """Reconstruct the user's 5 HR zones as %HRmax. Runalyze exposes the per-activity time-in-zone
    DISTRIBUTION (get_activity_details.zone_distribution_hr) but NOT the boundaries (the `sport`
    config is 403 for the personal token). So for each recent HR-rich run we find the 4 HR values
    that split its time-weighted samples to match the distribution, express them as %HRmax, and
    pool the medians. Pure read — derives nothing into the DB; the chart wiring decides what to do
    with the result. Returns cutoffs (4 ascending %HRmax) + per-run detail for eyeballing the spread."""
    rmax = _robust_hrmax(db)
    if not rmax:
        return {"ok": False, "error": "no robust HRmax yet"}
    rows = db.execute(
        "SELECT id, date FROM activities WHERE sport=? AND hr_max IS NOT NULL AND hr_max>=? "
        "ORDER BY date DESC LIMIT ?", (RUNNING_SPORT, int(rmax * 0.8), sample)).fetchall()
    cols = [[], [], [], []]   # one list of %HRmax estimates per boundary
    per = []
    for r in rows:
        try:
            det = mcp_call("get_activity_details", {"activity_id": int(r["id"])})
        except (RunalyzeError, requests.RequestException, KeyError, ValueError):
            continue
        act = det.get("activity", det)
        strm = act.get("streams") or {}
        hr, tim = strm.get("heart_rate") or [], strm.get("time") or []
        dist = act.get("zone_distribution_hr")
        if not dist or not hr or sum(dist) == 0:
            continue
        pairs = []   # (hr, time-weight) so a paused/variable-rate stream isn't mis-counted
        for i, h in enumerate(hr):
            if h is None:
                continue
            dt = (tim[i + 1] - tim[i]) if i + 1 < len(tim) else 1
            pairs.append((h, dt if dt and dt > 0 else 1))
        pairs.sort()
        tot = sum(w for _, w in pairs) or 1
        cuts, cc = [], 0
        for z in dist[:-1]:
            cc += z
            cuts.append(cc / 100.0)
        acc, ci, b = 0, 0, [None] * 4
        for h, w in pairs:
            acc += w
            while ci < len(cuts) and acc / tot >= cuts[ci]:
                b[ci] = h
                ci += 1
            if ci >= len(cuts):
                break
        row_pct = []
        for k in range(4):
            if b[k] and dist[k] > 0:
                pct = round(100 * b[k] / rmax)
                cols[k].append(pct)
                row_pct.append(pct)
            else:
                row_pct.append(None)
        per.append({"id": r["id"], "date": r["date"], "dist": dist, "pct": row_pct})

    def med(xs):
        xs = sorted(x for x in xs if x is not None)
        return xs[len(xs) // 2] if xs else None
    return {"ok": True, "hrmax": rmax, "labels": ["Z1/Z2", "Z2/Z3", "Z3/Z4", "Z4/Z5"],
            "cutoffs_pct": [med(c) for c in cols],
            "spread": [{"n": len(c), "min": min(c), "max": max(c)} if c else None for c in cols],
            "activities": per}


def _effort_verdict(kind, hrf, te):
    """Pure per-run verdict — HR-LED, TE corroborates (returns (verdict, confidence)). `hrf` =
    hr_avg / HRmax. For an aerobic (easy/long) session: on / hot / too_hard by HR fraction, with
    confidence rising to 'high' when a too-hard HR read is backed by a high Training Effect. For a
    quality session: 'did you hit it' — too_easy if HR never reached the aerobic ceiling (sandbagged),
    else on — always LOW confidence (little compliant-quality data yet to calibrate, and his problem
    is the too-hard direction). hrf None ⇒ ('unknown','none')."""
    if hrf is None:
        return "unknown", "none"
    if kind in AEROBIC_KINDS:
        if hrf > HARD_HR_FRAC:
            return "too_hard", ("high" if (te or 0) >= TE_HARD_CORROBORATE else "moderate")
        if hrf > EASY_HR_FRAC:
            return "hot", "moderate"
        return "on", "moderate"
    return ("too_easy" if hrf < EASY_HR_FRAC else "on"), "low"


def effort_discipline(db, window_days=EFFORT_WINDOW_DAYS):
    """Per-run effort vs prescription over the recent window (§6m). Reads Runalyze's HR-derived effort
    metrics — no pace-confounder modeling: HR fraction is the intensity gate, Training Effect
    corroborates, GAP is shown as a terrain-fair pace, subjective_feeling + decoupling as context.
    Each run's prescribed kind comes from the saved plan (frozen past weeks included); an unplanned
    run defaults to 'easy' (the polarized expectation). Public-safe (effort/HR/pace, no medical).
    Returns per-run verdicts + the easy-discipline SCORE (the confident half: his easy days run hard)
    and a softer quality read."""
    from datetime import timedelta
    hrmax = _robust_hrmax(db)
    since = (datetime.now().date() - timedelta(days=window_days)).isoformat()
    drop = dropped_ids(db)
    row = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    plan = json.loads(row["plan"]) if row else {}
    kind_by_date = {}
    for ph in ("rebase", "base", "build", "peak", "taper"):
        for w in (plan.get(ph) or {}).get("weeks", []):
            for s in w.get("sessions", []):
                kind_by_date[s["date"]] = s.get("kind")
    runs = []
    for r in db.execute(
        "SELECT id, date, distance, duration, hr_avg, raw FROM activities "
        "WHERE sport=? AND date>=? ORDER BY date DESC", (RUNNING_SPORT, since)).fetchall():
        if r["id"] in drop or not r["distance"] or r["distance"] < 2 or not r["hr_avg"]:
            continue
        raw = json.loads(r["raw"] or "{}")
        kind = kind_by_date.get(r["date"]) or "easy"
        hrf = (r["hr_avg"] / hrmax) if hrmax else None
        te = raw.get("fit_training_effect")
        gap = raw.get("gap")                              # Runalyze grade-adjusted speed (km/h)
        gap_pace = (round(3600.0 / gap) if gap else
                    (round(r["duration"] / r["distance"]) if r["duration"] else None))
        verdict, conf = _effort_verdict(kind, hrf, te)
        runs.append({"date": r["date"], "km": round(r["distance"], 1), "kind": kind,
                     "hr_avg": r["hr_avg"], "hr_pct": round(hrf * 100) if hrf else None,
                     "gap_pace": gap_pace, "te": te, "feeling": raw.get("subjective_feeling"),
                     "decoupling": raw.get("aerobic_decoupling_pace"),    # context only (units TBD)
                     "verdict": verdict, "confidence": conf})
    aerobic = [x for x in runs if x["kind"] in AEROBIC_KINDS]
    quality = [x for x in runs if x["kind"] not in AEROBIC_KINDS]
    on = sum(1 for x in aerobic if x["verdict"] == "on")
    return {
        "window_days": window_days, "hrmax": hrmax,
        "easy_hr_ceiling": round(EASY_HR_FRAC * hrmax) if hrmax else None,
        "easy_score": round(100 * on / len(aerobic)) if aerobic else None,
        "easy_counts": {"judged": len(aerobic), "on": on,
                        "hot": sum(1 for x in aerobic if x["verdict"] == "hot"),
                        "too_hard": sum(1 for x in aerobic if x["verdict"] == "too_hard")},
        "quality_counts": {"judged": len(quality),
                           "too_easy": sum(1 for x in quality if x["verdict"] == "too_easy")},
        "runs": runs,
    }


# ── Plan engine v1 (deterministic; §6) ──────────────────────────────────────
# Owns the numbers. Pace zones from effective VO2max (Daniels VDOT — validated to
# reproduce Runalyze's 5k prognosis exactly), session load estimated as TRIMP, weekly
# progression bounded so projected ACWR stays under the soft cap. The LLM layer (later)
# only proposes adjustments the engine then clamps to these guardrails.
ACWR_SOFT = 1.25   # planning target ceiling (margin under the hard limit)
ACWR_HARD = 1.30   # never exceed (the model has error near the boundary, §6a-bis)
EASY_TRIMP_PER_MIN = 1.3   # calibrated from his easy runs (HR≤135 → ~1.1–1.5/min)
EASY_PACE_FRAC = 0.72      # fraction of vVO2max for easy running

# §6e — earned faster exit from Phase 0. Upward responsiveness that NEVER touches the ACWR
# ceiling or the weekly volumes: demonstrated adaptation lets the block GRADUATE sooner (the
# reward is time, not load), handing the freed week to base-build. Conservative by design for a
# post-illness rebuild: earned from completed weeks only, reset on any miss, ≤1 week ever shaved
# (shaving 2 would land the block on the down week), and always subordinate to readiness.
REBASE_GRAD_AT = 3         # banked completed (non-down) weeks needed to graduate early
REBASE_MAX_GRADUATE = 1    # most weeks the block can be shortened (keeps a non-down terminal)
BANK_ADHERENCE = 0.8       # fraction of a week's planned km that must be run to "bank" it

# §6e/§6f — earned upward responsiveness (volume), banking-gated. The deferred sibling of the
# re-base graduation above: where graduation rewards banked weeks with TIME, this rewards them with
# a small VOLUME lift on the building phases — but ONLY as an owner-confirmed opt-in, and never in a
# way that touches the safety math. Three things must ALL hold (any miss → pure no-op):
#   • the owner has opted in (EARNED_KEY in `meta`, default off — a low ACWR is a ceiling signal,
#     never a target the engine fills on its own);
#   • a streak of banked, well-absorbed weeks (same adherence+recovery+not-eased test as graduation,
#     read over the prior plan's ELAPSED weeks — earned over weeks, not days; resets on any miss);
#   • latest readiness isn't red/heavy.
# The lift is a BOUNDED intent step applied to NON-DOWN weeks of the FIRST building phase only (Base,
# or Build on a short runway); later phases inherit the lifted level through `cur_km` — a SINGLE ~F
# level-lift across the building road, never an F-per-phase compound that would silently approach F².
# Down weeks keep their recovery trough (a uniform lift would flatten the 3:1 trough up to the
# ceiling — ACWR's 7:28 ratio masks it, a masters/post-illness body feels it); Peak/Taper are left as
# designed; and the ACWR governor still hard-caps every week at ACWR_SOFT. The step SIZE is the
# limiter (not an open-ended "fill the ceiling"): at the top tier the hardest building weeks DO reach
# — but never exceed — the 1.25 cap (the default trajectory already floats ~1.17–1.20, so the earned
# headroom is modest), while lower tiers and the down weeks stay below it. Reaching the cap on hard
# weeks during an *owner-confirmed, banking-earned* faster build is the sanctioned use of headroom;
# what the design forbids is doing it automatically or at the cost of the recovery troughs.
EARNED_KEY = "earned_progression"   # meta toggle — owner opt-in, default off
EARNED_BANK_AT = 3                  # banked elapsed weeks to UNLOCK the earned volume lift
EARNED_VOLUME_STEP = 0.08           # per-tier intent lift on non-down building weeks (~+8%)
EARNED_MAX_TIERS = 2                # cap the lift at ~+16% — bounded; applied once, not per phase

# §6e — earned FREQUENCY advance: the deferred sibling of the volume lift, now built. Where the
# volume lift adds km to the same days, this adds a 6th weekly RUN to non-down Base/Build weeks at
# CONSTANT governed volume (the same load spread over one more day → shorter easy runs, not a
# heavier week — the ACWR governor still caps total load). It is NOT "safer because shorter": more
# frequency = more loading cycles = more of the connective-tissue stimulus ACWR can't see (§6f
# ~line 590). So it's its OWN owner opt-in (separate from the volume lift — he may want one, not the
# other) and earned on a STRICTER bank threshold. Binary, not tiered (5 or 6). 6 runs / 1 rest can't
# avoid two 3-run streaks, but the cap-3 layout (rest Thu) + `_distribute_week`'s placement keep the
# two hard sessions (mid-quality + long/long-MP) three days apart — never consecutive (verified in
# `det/day-spacing` for n=6). Down weeks keep their lower count (the recovery trough stays fewer-not-
# more); Peak/Taper are untouched.
FREQ_KEY = "freq_advance"           # meta toggle — owner opt-in for the 6th run, default off
FREQ_BANK_AT = 4                    # banked elapsed weeks to UNLOCK (stricter than the volume lift's 3)
FREQ_MIN_EASY_KM = 4.0              # min-distance FLOOR (owner-chosen 2026-06-21): don't add the 6th run
                                    # to a week unless its non-long runs would still clear this — i.e.
                                    # frequency is earned by VOLUME too, so the 6th run is real training,
                                    # never ~2 km junk. Proxy = (week km − long km) / BASE_RUNS (the
                                    # non-long runs at BASE_RUNS+1). Below it the week stays at BASE_RUNS;
                                    # so at his current detrained volume the lever is DORMANT (like the
                                    # §6h CTL floor) and wakes only as the rebuild grows (~35 km Base).

# Optionally seed a first objective on a fresh DB, so you don't start at a blank screen:
#   SH_SEED_OBJECTIVE="Berlin Marathon|2026-09-27|marathon|finish|A"  (label|date|type|target|priority)
# Empty = no seed; add your race in the Objectives UI. With none, the engine runs in maintenance mode.
def _parse_seed_objective(spec):
    bits = [b.strip() for b in (spec or "").split("|")]
    if len(bits) == 5 and bits[1]:
        label, date, typ, target, prio = bits
        return {"type": typ or "race", "label": label, "date": date,
                "target": target or "finish", "priority": prio or "A"}
    return None


SEED_OBJECTIVE = _parse_seed_objective(os.environ.get("SH_SEED_OBJECTIVE", ""))


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
    frac = {"easy": 0.70, "easy_top": EASY_PACE_FRAC, "marathon": 0.81,
            "threshold": 0.88, "interval": 0.97}
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


def periodize_chain(today, chain, rebase_weeks=6):
    """§6q — reverse-periodize the whole A-race CHAIN into a flat phase list. Each phase carries a
    unique `key` (what generate_plan stores its block under + the UI selects), a `kind` (which shaper
    builds it), and the `race`/`role` it serves. Segment 0 is the full Re-base→Base→Build→Peak→Taper
    toward the first race; each later race adds a re-build BRIDGE→Peak→Taper off the prior race. A
    subordinate race gets peak=0 + a 1-week sharpen instead of a full peak. Returns (phases,
    total_weeks). For a single goal race this REDUCES to periodize() (same kinds + same week counts;
    the Peak/Taper names just gain the race label). `chain` is select_chain()'s first return."""
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
    total0 = weeks_until(r0["date"], today)
    base, build, peak0, taper0 = seg0_split(total0, _full_peak(role0))
    phases = [
        {"phase": "Re-base (Phase 0)", "weeks": rebase_weeks, "kind": "rebase", "key": "rebase", "race": None, "role": None},
        {"phase": "Base — aerobic", "weeks": base, "kind": "base", "key": "base", "race": lbl0, "role": role0},
        {"phase": "Build — specific", "weeks": build, "kind": "build", "key": "build", "race": lbl0, "role": role0},
        {"phase": f"Peak → {lbl0}", "weeks": peak0, "kind": "peak", "key": "peak", "race": lbl0, "role": role0},
        {"phase": f"Taper → {lbl0}", "weeks": taper0, "kind": "taper", "key": "taper", "race": lbl0, "role": role0},
    ]
    for k in range(1, len(chain)):
        rk, prev = chain[k], chain[k - 1]
        lblk, rolek = rk.get("label", "race"), rk["role"]
        fullk = _full_peak(rolek)
        totalk = weeks_until(rk["date"], _date(prev["date"]))
        taperk = _seg_taper(totalk, fullk)        # clamped ≤ totalk → segment never overruns the race
        remk = max(0, totalk - taperk)
        peakk = min(2, remk) if fullk else 0      # short inter-race sharpen; fitness is held, no new base
        bridgek = max(0, remk - peakk)            # taperk + peakk + bridgek == totalk (no calendar drift)
        phases += [
            {"phase": f"Bridge → {lblk}", "weeks": bridgek, "kind": "bridge", "key": f"bridge{k}", "race": lblk, "role": rolek},
            {"phase": f"Peak → {lblk}", "weeks": peakk, "kind": "peak", "key": f"peak{k}", "race": lblk, "role": rolek},
            {"phase": f"Taper → {lblk}", "weeks": taperk, "kind": "taper", "key": f"taper{k}", "race": lblk, "role": rolek},
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
# it's untouched. NOTE: no explicit intraweek-peak-ACWR guard is needed *because the long run is
# CTL-limited to ~12km here* (single-day spike stays ~1.3); if this is ever combined with the volume
# push (option-1 sibling), the long run grows and that unguarded intraweek spike reopens — add a peak
# guard then. The re-base (the pure-easy, post-illness restart) keeps its ORIGINAL conservative cap
# (REBASE_LONG_CAP) so it stays byte-identical — the recalibration is for the marathon-prep phases.
LONG_RUN_MAX_FRAC = 0.50
REBASE_LONG_CAP = 0.35     # pure-easy blocks (re-base) keep the original cap — leave the cautious restart untouched
REBASE_SHAPE = [
    {"wk": 1, "km": 13, "runs": 3, "long": 5, "strides": 0, "intent": "Re-establish frequency — pure easy feel, HR controlled, no urge to stop"},
    {"wk": 2, "km": 15, "runs": 4, "long": 6, "strides": 0, "intent": "Add the 4th run if week 1 felt easy"},
    {"wk": 3, "km": 17, "runs": 4, "long": 6, "strides": 2, "intent": "First gentle neuromuscular touch — strides ×2"},
    {"wk": 4, "km": 13, "runs": 3, "long": 5, "strides": 0, "intent": "Down week — consolidate (masters + post-illness conservative)"},
    {"wk": 5, "km": 18, "runs": 4, "long": 7, "strides": 2, "intent": "Extend easy aerobic volume"},
    {"wk": 6, "km": 19, "runs": 5, "long": 7, "strides": 2, "intent": "End-of-block check → optional relaxed 5k probe, ready for base-build"},
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


# Base phase (§6f Step B) — the aerobic-base block after the re-base. A gentle, mostly-under-cap
# volume ramp (the ACWR governor in generate_block is the hard ceiling regardless), with a 3:1
# load:recovery mesocycle. Conservative posture: hold the 5-run week (frequency-advance is the
# banking-gated §6e step, deferred); long run grows but stays ≤ LONG_RUN_MAX_FRAC of the week.
BASE_RUNS = 5
BASE_WEEKLY_RAMP = 0.045   # ~4.5%/wk *intent* — keeps Base mostly below the cap (re-base posture)
BASE_DOWN_EVERY = 4        # every 4th week is a down week (3 build : 1 recovery)

# §6h — CTL-responsive volume FLOOR (2026-06-20). The fixed ramps above never read CTL, so the engine
# under-prescribes as fitness rises (verified: ~25km weeks even at CTL 90, where his real running was
# ~50km). The floor closes that gap: Base/Build weekly volume is lifted to at least K_CTL_VOLUME ×
# (the phase's measured/projected CTL) — so when his synced CTL OUTRUNS the conservative projection,
# the plan grows to match, automatically. It's a FLOOR via max(), never a target: the ACWR governor
# still caps realized load and rate, so it can't fill the ceiling or run away. K stays at the
# EMPIRICAL 0.55 from HIS history (CTL 60–110 → ~39–50km median) — that's whole-body CTL, so 0.55×CTL
# is his real RUNNING share (cross-training fills the rest); do NOT raise it toward the ~0.78
# pure-running-physics value or it would over-prescribe running and start grazing the cap. Honest
# scope: for a detrained athlete (e.g. CTL ~24) the floor (≈13km) sits BELOW the ramp (~19km) → fully
# DORMANT (plan byte-identical), and a low end-of-build projection (~CTL 27–38) keeps it latent the whole
# build — it activates only if/when measured CTL exceeds the ramp (~CTL 35–45). It's the mechanism that
# lets reality reward a faster-than-projected rebuild, not a change to today's plan. Re-base and
# Peak/Taper are excluded (the restart stays byte-identical; the taper trim must not be re-inflated).
K_CTL_VOLUME = 0.55
BASE_DOWN_FRAC = 0.75      # down-week volume vs the carried build trajectory
BASE_LONG_FRAC = 0.42      # long-run target as a fraction of weekly km (capped at LONG_RUN_MAX_FRAC);
                           # raised 0.32→0.42 (2026-06-20) toward the owner's real long-run share

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

# Base on-ramp quality (§6f Step C): a single short *light tempo* per build week, introduced after
# the first couple of weeks (neuromuscular on-ramp, after strides) and never on a down week. Kept
# deliberately light — a small hard fraction at threshold ("cruise") — the conservative masters /
# post-illness posture. Build's heavier interval/MP menu is Step D.
BASE_TEMPO_FRAC = 0.10     # hard fraction of weekly TRIMP for the Base light tempo (well under cap)
BASE_TEMPO_ZONE = "threshold"
BASE_TEMPO_FROM_WEEK = 3   # no tempo in the first 2 Base weeks (ease into quality after strides)


def base_shape(n_weeks, start_km, runs=BASE_RUNS):
    """Parametric Base-phase shape (§6f Step B/C): easy-aerobic volume growth launched from the
    re-base end volume, with a 3:1 down-week cadence. INTENT only — `generate_block` clips any week
    the ACWR ceiling won't allow, so this is the target trajectory, not the guaranteed one. The
    build trajectory advances only on build weeks (a down week absorbs, it doesn't regress the
    trend). Strides carry over from the re-base on-ramp; Step C layers a single light tempo per build
    week (from BASE_TEMPO_FROM_WEEK) as the quality on-ramp — easy-dominant, polarized (~90/10)."""
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
            quality = [{"kind": "tempo", "zone": BASE_TEMPO_ZONE, "frac": BASE_TEMPO_FRAC,
                        "structure": "continuous", "label": "light cruise tempo"}]
        shape.append({"wk": wk, "km": this_km, "runs": runs,
                      "long": round(this_km * BASE_LONG_FRAC), "strides": 0 if down else 2,
                      "quality": quality,
                      "intent": "Down week — absorb the block" if down
                      else "Easy aerobic base — build durable volume"})
    return shape


# Build phase (§6f Step D) — SPECIFIC work. Volume held / lightly growing; two quality sessions a
# week (VO₂ intervals + a marathon-pace long-run finish), 3:1 down weeks. Frequency holds at
# BASE_RUNS (frequency-advance is the banking-gated §6e step, still deferred). Quality fracs sum to
# < (1 − POLARIZED_EASY_MIN) so the week stays easy-dominant by construction; the threshold/interval
# slice alone stays under PHASE_HARD_CAP["build"].
BUILD_WEEKLY_RAMP = 0.02   # lightly growing — Build is about specificity, not volume
BUILD_DOWN_EVERY = 4
BUILD_DOWN_FRAC = 0.75
BUILD_LONG_FRAC = 0.45       # raised 0.34→0.45 (2026-06-20) — the marathon long run is the cornerstone
BUILD_INTERVAL_FRAC = 0.12   # VO₂ intervals (interval zone) — the hard slice
BUILD_MP_FRAC = 0.07         # marathon-pace long-run finish (marathon zone, attached to the long run)

# Peak / sharpen — trimmed volume, race specificity. The long run is at its largest the runway
# allows (bounded by LONG_RUN_MAX_FRAC of the week + the ACWR ceiling — honest about a detrained
# masters runway: ~12–13 km, CTL-gated, not a textbook 32–35 km), with race-pace work + light sharpening.
PEAK_WEEKLY_RAMP = -0.04     # trim volume into race specificity
PEAK_LONG_FRAC = 0.48        # raised 0.35→0.48 (2026-06-20) — push the long run to its CTL-safe ceiling
PEAK_MP_FRAC = 0.10
PEAK_INTERVAL_FRAC = 0.06

# Taper — drop volume ~40–60% over the taper, keep sharpness with short race-pace touches; the race
# week is the lightest and carries no structured quality (just freshening).
TAPER_LONG_FRAC = 0.30
TAPER_SHARP_FRAC = 0.06      # short race-pace touch (threshold), neuromuscular sharpness only
TAPER_TOP, TAPER_BOTTOM = 0.75, 0.40   # week-1 vs race-week volume as a fraction of the peak end


def build_shape(n_weeks, start_km, runs=BASE_RUNS):
    """Parametric Build-phase shape (§6f Step D): lightly-growing specific work off the Base end
    volume, with a 3:1 down-week cadence. Each build week carries two quality sessions — VO₂
    intervals (mid-week) and a marathon-pace finish on the long run — as a small polarized slice;
    down weeks drop quality to absorb. INTENT only — `generate_block` clips to the ACWR ceiling."""
    shape, km = [], float(start_km)
    for i in range(n_weeks):
        wk = i + 1
        down = (wk % BUILD_DOWN_EVERY == 0)
        if down:
            this_km = max(1, round(km * BUILD_DOWN_FRAC))
        else:
            this_km = max(1, round(km))
            km *= (1 + BUILD_WEEKLY_RAMP)
        quality = [] if down else [
            {"kind": "interval", "zone": "interval", "frac": BUILD_INTERVAL_FRAC,
             "structure": "intervals", "rep_min": 3, "rec_min": 2, "label": "VO₂ intervals"},
            {"kind": "long_mp", "zone": "marathon", "frac": BUILD_MP_FRAC,
             "attach": "long", "label": "marathon-pace long run"}]
        shape.append({"wk": wk, "km": this_km, "runs": runs,
                      "long": round(this_km * BUILD_LONG_FRAC), "strides": 0, "quality": quality,
                      "intent": "Down week — absorb the block" if down
                      else "Build — specific: VO₂ intervals + marathon-pace long run"})
    return shape


def peak_shape(n_weeks, start_km, runs=BASE_RUNS):
    """Parametric Peak-phase shape (§6f Step D): trim volume into race specificity. Volume eases each
    week; the long run carries a race-pace finish and there's a light interval touch for sharpness.
    The long run is bounded by LONG_RUN_MAX_FRAC + the ACWR ceiling — the runway, not a textbook
    peak-long-run number, decides its length."""
    shape, km = [], float(start_km)
    for i in range(n_weeks):
        wk = i + 1
        this_km = max(1, round(km))
        km *= (1 + PEAK_WEEKLY_RAMP)
        quality = [
            {"kind": "interval", "zone": "interval", "frac": PEAK_INTERVAL_FRAC,
             "structure": "intervals", "rep_min": 3, "rec_min": 2, "label": "sharpening intervals"},
            {"kind": "long_mp", "zone": "marathon", "frac": PEAK_MP_FRAC,
             "attach": "long", "label": "race-pace long run"}]
        shape.append({"wk": wk, "km": this_km, "runs": runs,
                      "long": round(this_km * PEAK_LONG_FRAC), "strides": 0, "quality": quality,
                      "intent": "Peak — race specificity: race-pace long run + sharpening"})
    return shape


def taper_shape(n_weeks, start_km, runs=BASE_RUNS):
    """Parametric Taper-phase shape (§6f Step D): volume falls from ~TAPER_TOP to ~TAPER_BOTTOM of
    the peak-end volume over the taper, while a short race-pace touch keeps the legs sharp. The race
    week (last) is the lightest and carries no structured quality — just easy freshening."""
    shape = []
    for i in range(n_weeks):
        wk = i + 1
        frac = (TAPER_TOP - (TAPER_TOP - TAPER_BOTTOM) * i / (n_weeks - 1)) if n_weeks > 1 else TAPER_BOTTOM
        this_km = max(1, round(start_km * frac))
        race_week = (wk == n_weeks)
        quality = [] if race_week else [
            {"kind": "tempo", "zone": "threshold", "frac": TAPER_SHARP_FRAC,
             "structure": "intervals", "rep_min": 2, "rec_min": 2, "label": "short race-pace touch"}]
        shape.append({"wk": wk, "km": this_km, "runs": runs,
                      "long": round(this_km * TAPER_LONG_FRAC), "strides": 0 if race_week else 2,
                      "quality": quality,
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
    return _session_from_reps(date, spec["kind"], zone, zpace, reps, note)


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
    return _session_from_reps(date, "long_mp", zone, zpace, reps, note)


def _distribute_week(wk, start_monday, week_trimp, easy_pace_sec, zones=None, days_override=None):
    """Lay `week_trimp` across the week's runs and converting each session's TRIMP back to
    minutes/km. The POLARIZED split (§6f Step C): a `quality` spec carves a small HARD slice of the
    governed weekly TRIMP for structured work (at zone pace), the rest stays easy/long — so total
    weekly TRIMP is unchanged (the ACWR governor still bounds it), intensity is just concentrated.
    Quality needs zone paces, so with `zones=None` (the re-base path) the week stays PURE EASY,
    byte-identical to before. `days_override` lets the caller place runs on an explicit set of
    week-offsets (e.g. only today-onward days for a partially-elapsed week, §6o) instead of the
    frequency's default layout; the last offset is still the long-run slot. Returns (sessions,
    day_trimps)."""
    from datetime import timedelta
    days = list(days_override) if days_override is not None else _run_days(wk["runs"])
    n = len(days)                                        # last slot = the long run
    quality = (wk.get("quality") or []) if zones else []
    mid_q = [q for q in quality if q.get("attach") != "long"]
    long_q = next((q for q in quality if q.get("attach") == "long"), None)
    # mid-week quality on the earliest mid slots (Tue, Thu, …) — off slot 0 (first run back) and the
    # long slot; the MP finish (long_q) rides the long run itself.
    q_slots, s = [], 1
    for _q in mid_q:
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
    long_cap = LONG_RUN_MAX_FRAC if zones else REBASE_LONG_CAP
    long_w = min(wk["long"] / wk["km"], long_cap) if wk["km"] else 0.0
    n_short = len(easy_slots) - 1                        # the long slot is always present
    first_easy = min(easy_slots) if easy_slots else None
    for i in easy_slots:
        is_long = (i == long_idx)
        if is_long:
            tr = round(easy_budget * (long_w if n_short else 1.0), 1)
        else:
            tr = round(easy_budget * (1 - long_w) / n_short, 1) if n_short else 0.0
        date = (start_monday + timedelta(days=days[i])).isoformat()
        if is_long and long_q:                          # marathon-pace finish on the long run
            sess = _build_long_mp(date, tr, mp_work, long_q, zones, easy_pace_sec)
            sessions.append(sess)
            day_trimps[date] = day_trimps.get(date, 0.0) + sess["trimp"]
            continue
        mins = round(tr / EASY_TRIMP_PER_MIN)
        km = round(mins * 60 / easy_pace_sec, 1)
        note = "long easy run" if is_long else "easy run"
        if wk["strides"] and not is_long and i == first_easy:
            note += f" + {wk['strides']}×4–6 strides"
        sessions.append({"date": date, "kind": "long" if is_long else "easy",
                         "km": km, "minutes": mins, "trimp": tr,
                         "pace_zone": f"{fmt_pace(easy_pace_sec)}/km easy", "note": note})
        day_trimps[date] = day_trimps.get(date, 0.0) + tr
    sessions.sort(key=lambda x: x["date"])
    return sessions, day_trimps


def _project_week(ctl, atl, week_start, day_trimps, roll_from=None):
    """Roll the projector across one full week (Mon–Sun). Returns
    (end_ctl, end_atl, eow_acwr, peak_acwr). We bound on END-OF-WEEK ACWR — the settled
    weekly state, the natural planning cadence — rather than the long-run-day daily transient.
    project_forward only spans to the last planned day, so we extend rest days to Sunday.
    `roll_from` (default = week_start) is where the roll BEGINS: for a partially-elapsed week (§6o)
    pass `today` and seed (ctl, atl) with today's snapshot — the elapsed days' load is already in
    that seed, so we project only today-onward `day_trimps`, never double-counting them."""
    from datetime import timedelta
    end = _date(week_start) + timedelta(days=6)
    start_iso = roll_from or week_start                 # where the roll begins (today for a partial week)
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
    return curve[-1]["ctl"], curve[-1]["atl"], eow, peak


def _max_week_trimp(ctl, atl, wk, start, easy_pace_sec, cap, zones=None, roll_from=None, days_override=None):
    """Binary-search the largest weekly TRIMP whose peak projected ACWR stays ≤ cap. Distributes
    WITH the week's quality (via `zones`) so the bound is on the real, intensity-distributed week.
    `roll_from`/`days_override` thread through to project only today-onward days for a partially-
    elapsed week (§6o), so the remaining allowance is bounded against load already done this week."""
    lo, hi = 0.0, 700.0
    for _ in range(34):
        mid = (lo + hi) / 2
        _, dt = _distribute_week(wk, _date(start), mid, easy_pace_sec, zones, days_override=days_override)
        _, _, eow, _peak = _project_week(ctl, atl, start, dt, roll_from=roll_from)
        if eow and eow > cap:
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
            elif easy_only and s["kind"] not in ("easy", "rest", "long"):
                s["kind"], s["note"] = "easy", "easy only — eased by your check-in"
            else:
                s["note"] = "eased — " + s.get("note", "")
        out_s.append(s)
    return {"sessions": out_s, "dt": out_dt, "touched": touched}


def _is_down(intent):
    """A week is a deliberate down/recovery week iff its intent text says so — uniform across every
    shape (re-base wk4, base/build 3:1). The single test the banking gates + the earned lift share."""
    return str(intent or "").lower().startswith("down")


def _week_banked(db, ws, we, planned_km, planned_runs, drop):
    """Shared §6e per-week test: was one fully-elapsed week well-absorbed, from owned data only?
      • adherence — ran ≥ BANK_ADHERENCE of the week's planned km AND within one of its planned runs;
      • recovery intact — no stop-symptom and ≤1 'heavy-legs' check-in that week;
      • the engine wasn't already easing it — no ease/medical adjustment overlapped the week.
    Single source of truth for BOTH the re-base graduation and the earned volume lift, so the two
    gates judge a week identically. Returns (banked, act_km, act_runs)."""
    rows = db.execute(
        "SELECT id, date, distance FROM activities WHERE date>=? AND date<=? AND sport=?",
        (ws.isoformat(), we.isoformat(), RUNNING_SPORT)).fetchall()
    act_km = sum(r["distance"] for r in rows if r["id"] not in drop and r["distance"])
    act_runs = len({r["date"] for r in rows if r["id"] not in drop and r["distance"]})
    adh = act_km >= BANK_ADHERENCE * (planned_km or 0) and act_runs >= (planned_runs or 0) - 1
    rd = db.execute("SELECT energy, stop_symptom FROM readiness WHERE date>=? AND date<=?",
                    (ws.isoformat(), we.isoformat())).fetchall()
    recovery = not any(r["stop_symptom"] for r in rd) and \
        sum(1 for r in rd if r["energy"] == "heavy") <= 1
    eased = db.execute(   # ANY adjustment that overlapped — even one later superseded — means
        "SELECT 1 FROM adjustments WHERE applies_from<=? AND applies_until>=?",  # the week was eased
        (we.isoformat(), ws.isoformat())).fetchone() is not None
    return (adh and recovery and not eased), round(act_km, 1), act_runs


def rebase_banking(db, block_start, today):
    """§6e — read how the COMPLETED re-base weeks actually went and return the earned-progression
    state (drives the early GRADUATION; the earned volume lift lives in `earned_state`). `banked` is
    a STREAK that resets to 0 on any miss (earn it back — the post-illness posture); the deliberate
    **down week neither earns nor resets** it (completing a recovery week easily is expected, not
    evidence). Only fully-elapsed weeks count, so the forward plan can't wobble day-to-day. This
    drives a faster exit ONLY — it never changes a volume or the ACWR ceiling."""
    from datetime import timedelta
    drop = dropped_ids(db)
    today_d = _date(today)
    streak, weeks = 0, []
    for wk in REBASE_SHAPE:
        ws = block_start + timedelta(weeks=wk["wk"] - 1)
        we = ws + timedelta(days=6)
        if we >= today_d:           # only fully-completed weeks are evidence
            break
        banked, act_km, act_runs = _week_banked(db, ws, we, wk["km"], wk["runs"], drop)
        is_down = wk["wk"] == 4
        weeks.append({"wk": wk["wk"], "banked": banked, "down": is_down,
                      "act_km": act_km, "act_runs": act_runs})
        if is_down:
            continue                # the down week is neutral — neither banks nor breaks the streak
        streak = streak + 1 if banked else 0
    graduate = min(REBASE_MAX_GRADUATE, 1 if streak >= REBASE_GRAD_AT else 0)
    return {"banked_streak": streak, "graduate": graduate, "weeks": weeks,
            "effective_len": len(REBASE_SHAPE) - graduate}


def _banked_streak(db, today, prior_plan):
    """The banked STREAK over the prior plan's fully-ELAPSED weeks (all phases, calendar order, same
    `_week_banked` test as graduation; down weeks neutral; resets on any miss) + whether the latest
    readiness is ok (not red/heavy). The single shared evidence both §6e upward levers read — the
    volume lift (`earned_state`) and the frequency advance (`freq_state`) judge a week identically.
    Returns (streak, ready_ok)."""
    from datetime import timedelta
    drop = dropped_ids(db)
    today_d = _date(today) if isinstance(today, str) else today
    elapsed = []
    for key in ("rebase", "base", "build", "peak", "taper"):
        for w in ((prior_plan or {}).get(key) or {}).get("weeks", []):
            ws = _date(w["start"]); we = ws + timedelta(days=6)
            if we < today_d:                       # only fully-completed weeks are evidence
                elapsed.append((ws, we, w))
    elapsed.sort(key=lambda t: t[0])
    streak = 0
    for ws, we, w in elapsed:
        if _is_down(w.get("intent")):
            continue                               # down week is neutral (as in graduation)
        banked, *_ = _week_banked(db, ws, we, w.get("intent_km", w.get("km")), w.get("runs"), drop)
        streak = streak + 1 if banked else 0
    rd = db.execute("SELECT energy, stop_symptom FROM readiness "
                    "ORDER BY date DESC LIMIT 1").fetchone()
    ready_ok = not (rd and (rd["stop_symptom"] or rd["energy"] == "heavy"))
    return streak, ready_ok


def earned_state(db, today, prior_plan):
    """§6e/§6f — earned upward responsiveness (volume) gate. Reads the shared banked streak
    (`_banked_streak`) and combines it with the owner opt-in toggle to decide a BOUNDED intent lift
    for future Base/Build weeks. Pure read — the lift is applied (and governor-capped) in
    generate_plan. Returns factor 1.0 (a pure no-op) unless ALL of: opted in · streak ≥
    EARNED_BANK_AT · latest readiness not red/heavy."""
    opted_in = str(get_meta(db, EARNED_KEY, "0")).lower() in ("1", "true", "on", "yes")
    streak, ready_ok = _banked_streak(db, today, prior_plan)
    tiers = min(EARNED_MAX_TIERS, streak - EARNED_BANK_AT + 1) if streak >= EARNED_BANK_AT else 0
    unlocked = streak >= EARNED_BANK_AT and ready_ok
    active = opted_in and unlocked and tiers > 0
    return {"opted_in": opted_in, "banked_streak": streak, "ready_ok": ready_ok,
            "unlocked": unlocked, "tiers": tiers, "active": active,
            "factor": round(1.0 + EARNED_VOLUME_STEP * tiers, 4) if active else 1.0,
            "bank_at": EARNED_BANK_AT, "step": EARNED_VOLUME_STEP, "max_tiers": EARNED_MAX_TIERS}


def freq_state(db, today, prior_plan):
    """§6e — earned FREQUENCY advance gate (the 6th weekly run on non-down Base/Build weeks). Sibling
    of `earned_state`: same shared banked-streak evidence (`_banked_streak`), but its OWN opt-in
    toggle and a STRICTER bank threshold (FREQ_BANK_AT) — added frequency means more loading cycles,
    the connective-tissue stimulus ACWR can't see, so it's earned harder. Binary (no tiers): the week
    advances to BASE_RUNS+1 or it doesn't. Pure read — the runs bump is applied (and ACWR-governed)
    in generate_plan. No-op unless ALL of: opted in · streak ≥ FREQ_BANK_AT · readiness not red/heavy.
    Emergent coupling (intended): once active, future weeks are PLANNED at 6 runs, so `_week_banked`'s
    `act_runs ≥ planned_runs−1` bar rises to ≥5 actual runs — for BOTH levers (shared `_banked_streak`).
    Defensible: opting into 6 makes 6-ish the new adherence expectation."""
    opted_in = str(get_meta(db, FREQ_KEY, "0")).lower() in ("1", "true", "on", "yes")
    streak, ready_ok = _banked_streak(db, today, prior_plan)
    unlocked = streak >= FREQ_BANK_AT and ready_ok
    active = opted_in and unlocked
    return {"opted_in": opted_in, "banked_streak": streak, "ready_ok": ready_ok,
            "unlocked": unlocked, "active": active, "bank_at": FREQ_BANK_AT,
            "runs": (BASE_RUNS + 1) if active else BASE_RUNS}


def _apply_earned_lift(shape, factor):
    """Scale NON-DOWN weeks' volume intent by `factor` (≥1). Down weeks are left untouched so the 3:1
    recovery trough survives the lift (a uniform lift would flatten it up to the ACWR ceiling — the
    one masters/post-illness risk the cap alone doesn't catch); the governor still caps each week.
    Returns a NEW shape, never mutating the caller's."""
    if factor <= 1.0:
        return shape
    return [w if _is_down(w.get("intent"))
            else {**w, "km": round(w["km"] * factor), "long": round(w["long"] * factor)}
            for w in shape]


def _freq_easy_km(w):
    """The per-week non-long run distance the min-distance floor judges: at BASE_RUNS+1 runs, the
    non-long runs (= BASE_RUNS of them) share (week km − long km). A shape-level proxy — quality
    weeks' true easy runs run a touch shorter than this average, deliberately erring permissive."""
    return ((w.get("km") or 0) - (w.get("long") or 0)) / BASE_RUNS


def _apply_freq_advance(shape, active):
    """§6e — advance a NON-DOWN week from BASE_RUNS to BASE_RUNS+1 runs (the earned 6th run) at
    CONSTANT weekly volume: only `runs` changes, so `_distribute_week` splits the same governed
    km/TRIMP across one more day — the runs get SHORTER, the week doesn't get heavier (the ACWR
    governor still caps total load; intensity and the long run keep their slices). A week advances
    only when ALL hold: it's non-down, currently at exactly BASE_RUNS, AND its non-long runs would
    still clear FREQ_MIN_EASY_KM at the higher count (the owner-chosen floor — frequency is earned by
    VOLUME too, so the 6th run is never junk; below it the week keeps BASE_RUNS, so the lever is
    dormant at low volume). Down weeks keep their lower count (recovery trough stays fewer-not-more).
    NEW shape, never mutates the caller's. No-op when inactive."""
    if not active:
        return shape
    return [{**w, "runs": BASE_RUNS + 1}
            if (not _is_down(w.get("intent")) and w.get("runs") == BASE_RUNS
                and _freq_easy_km(w) >= FREQ_MIN_EASY_KM)
            else w
            for w in shape]


def _apply_ctl_floor(shape, seed_ctl):
    """§6h — lift the block's volume so its smallest building week is at least K_CTL_VOLUME × seed_ctl
    (the athlete's fitness-matched RUNNING volume), so the plan tracks measured CTL instead of only a
    fixed ramp. Implemented as a uniform SCALE of the whole block (not a flat level-set): this keeps
    the 3-week ramp progression AND the 3:1 down-week ratio intact while raising the trajectory to
    match fitness — so down weeks stay proportional recovery troughs, never flattened or stranded too
    deep. A FLOOR: if the trajectory is already at/above the fitness level (scale ≤ 1, the normal
    low-CTL case) it's a pure NO-OP — byte-identical. The ACWR governor still caps realized load and
    rate (the floor is sub-ceiling by construction: K=0.55 is his running share of a whole-body CTL).
    Reduce-only (§6c) still wins — this only sets INTENT, which generate_block governs and then the
    readiness/medical multiplier clips; a floor can't re-inflate an eased week. NEW shape, no mutation."""
    nd = [w["km"] for w in shape if not _is_down(w.get("intent")) and w.get("km")]
    if not nd:
        return shape
    scale = (K_CTL_VOLUME * (seed_ctl or 0.0)) / min(nd)   # lift the smallest building week to the floor
    if scale <= 1.0:
        return shape                                       # trajectory already ≥ fitness floor — dormant
    return [{**w, "km": round(w["km"] * scale), "long": round(w["long"] * scale)} for w in shape]


def generate_block(shape, block_start, ctl0, atl0, easy_pace_sec, adjust=None, zones=None, today=None):
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
    and the EOW ACWR ceiling still holds. Default None = full-week behaviour (every existing caller)."""
    from datetime import timedelta
    weeks = []
    ctl, atl = ctl0, atl0
    TRIMP_PER_KM = (easy_pace_sec / 60.0) * EASY_TRIMP_PER_MIN
    clipped_any = False
    for wk in shape:
        wk_start_d = block_start + timedelta(weeks=wk["wk"] - 1)
        wk_start = wk_start_d.isoformat()
        intent_trimp = wk["km"] * TRIMP_PER_KM            # easy-equivalent volume intent, in TRIMP
        # §6o — the week that STRADDLES today: keep elapsed days, govern only today-onward (easy).
        if today and wk_start_d < today <= wk_start_d + timedelta(days=6):
            offsets = _run_days(wk["runs"])
            today_off = (today - wk_start_d).days
            rem = [o for o in offsets if o >= today_off]
            full, _ = _distribute_week(wk, wk_start_d, intent_trimp, easy_pace_sec, zones)
            elapsed = [s for s in full if s["date"] < today.isoformat()]   # for log matching / display
            if rem:
                allowed = _max_week_trimp(ctl, atl, wk, wk_start, easy_pace_sec, ACWR_SOFT,
                                          zones=None, roll_from=today.isoformat(), days_override=rem)
                chosen = min(intent_trimp * len(rem) / max(1, len(offsets)), allowed)
                rem_s, dt = _distribute_week(wk, wk_start_d, chosen, easy_pace_sec, None, days_override=rem)
            else:                                          # today is past this week's last run → only decay
                chosen, rem_s, dt = 0.0, [], {}
            adjusted = _apply_adjustment(rem_s, dt, adjust)
            rem_s, dt = adjusted["sessions"], adjusted["dt"]
            ctl, atl, eow, peak = _project_week(ctl, atl, wk_start, dt, roll_from=today.isoformat())
            sessions = sorted(elapsed + rem_s, key=lambda s: s["date"])
            # km + trimp_total cover the SAME set (elapsed-planned + governed remainder) so the week
            # summary is internally consistent; proj_acwr/peak come from the remaining-only `dt` rolled
            # from today's seed (the safety number — elapsed load is in the seed, never double-counted).
            weeks.append({**wk, "start": wk_start, "sessions": sessions,
                          "km": round(sum(s["km"] for s in sessions), 1),
                          "trimp_total": round(sum(s.get("trimp", 0.0) for s in sessions), 1),
                          "proj_acwr": eow, "peak_acwr": peak,
                          "intent_km": wk["km"], "adjusted": adjusted["touched"],
                          "clipped": False, "partial": True})
            continue
        allowed = _max_week_trimp(ctl, atl, wk, wk_start, easy_pace_sec, ACWR_SOFT, zones)
        chosen = min(intent_trimp, allowed)
        if chosen < intent_trimp - 1:
            clipped_any = True
        sessions, dt = _distribute_week(wk, _date(wk_start), chosen, easy_pace_sec, zones)
        adjusted = _apply_adjustment(sessions, dt, adjust)  # mutates copies; reduces only
        sessions, dt = adjusted["sessions"], adjusted["dt"]
        ctl, atl, eow, peak = _project_week(ctl, atl, wk_start, dt)
        weeks.append({**wk, "start": wk_start, "sessions": sessions,
                      "km": round(sum(s["km"] for s in sessions), 1),
                      "trimp_total": round(sum(dt.values()), 1), "proj_acwr": eow, "peak_acwr": peak,
                      "intent_km": wk["km"], "adjusted": adjusted["touched"],
                      "clipped": chosen < intent_trimp - 1})
    return weeks, {"clipped_by_acwr": clipped_any,
                   "end_ctl": round(ctl, 1), "end_atl": round(atl, 1)}


def generate_rebase(block_start, ctl0, atl0, easy_pace_sec, adjust=None, shape=None):
    """The Phase-0 re-base block (§6d) — `generate_block` over `REBASE_SHAPE` (or a §6e-shortened
    slice when a well-absorbed block graduates early; volumes and the ACWR ceiling are identical,
    only the week count changes). Thin wrapper kept so callers and diffs stay stable now that the
    generator is phase-agnostic for base-build (§6f)."""
    return generate_block(shape or REBASE_SHAPE, block_start, ctl0, atl0, easy_pace_sec, adjust)


def feasibility(objective, ctl0, vo2max, weeks_away, projected_ctl=None):
    """§6a.5 — a sober read on whether the objective is reachable on this runway. CTL can
    grow ~3–4%/wk sustained; from his detrained CTL that lands far short of his PB shape, so
    we separate 'finish healthy' (realistic) from 'PB/target time' (not on this runway).
    §6f Step E — when `projected_ctl` is given (the engine's real end-of-taper CTL, chained
    through the actual generated blocks under the ACWR ceiling) it is preferred over the generic
    ~3.4%/wk estimate, so the verdict 're-reads each block' instead of a hand-wave."""
    est = round(ctl0 * (1.034 ** max(0, weeks_away)), 0)         # generic ~3.4%/wk fallback
    proj = round(projected_ctl) if projected_ctl is not None else est
    src = ("the engine's projection through the planned blocks (ACWR-capped)"
           if projected_ctl is not None else "~3–4%/wk sustained")
    verdict = "finish"  # default honest verdict for a marathon off a detrained base
    msg = (f"Projected fitness by race day ≈ CTL {proj:.0f} (from {ctl0:.0f} now, via "
           f"{src}). That supports **finishing {objective.get('label','the race')} "
           f"healthy** — the right goal off a 6-month layoff. A time target near your "
           f"sub-4 PB would need a much higher chronic load than this runway allows; "
           f"the engine re-reads this each block as real fitness comes back.")
    return {"verdict": verdict, "projected_ctl": proj, "estimate_ctl": est, "note": msg}


def _rebase_start(db, today):
    """The re-base start day — stored once and reused across regenerations so changing an
    objective re-periodizes the road *ahead* without sliding the block's start (a simple
    'freeze the past' approximation, §6b). The block anchors to the **Monday** of the week it first
    runs, so weeks are calendar Mon–Sun: run-day layouts map to real weekdays and the long run lands
    on the actual weekend (offset 6 = Sunday). Storing it keeps the anchor stable across regenerations.

    A legacy non-Monday anchor (the old 'starts today' scheme) is migrated to its **containing**
    Monday — back-only, never forward. Back-only is the safe direction: it never pushes block_start
    past `today` (so the runner is never shown a pre-start tile) and never *un*-elapses a week (so a
    banked graduation streak can't be reset). The cost of this one-time re-grid: the already-elapsed
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
    start = _monday(today)                      # fresh plan: anchor to this week's Monday; freeze holds it
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


def _split_freeze(shape, phase_start, gen_seed, easy_pace_sec, adjust, zones, prior_by_start, today):
    """§6f Step E (continuity) — generate one phase block with the past FROZEN. A week whose 7-day
    window has fully elapsed (end < today) is carried **verbatim** from `prior_by_start` (matched on
    start date), so a mid-block regeneration never rewrites weeks already lived. Today-onward weeks
    are generated FRESH from `gen_seed` — the LIVE CTL/ATL as of today (Runalyze's snapshot already
    embodies what the frozen past actually did, so the future seeds from today's real state, not
    from re-simulating history). An elapsed week with no prior record (e.g. a rebuilt DB) is
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
    backfilled = []
    if missing:                                              # no history — regenerate best-effort
        mweeks, mbound = generate_block(missing, phase_start, end_ctl, end_atl,
                                        easy_pace_sec, adjust, zones)
        backfilled = [{**w, "elapsed": True, "frozen": False} for w in mweeks]
        end_ctl, end_atl, generated_any = mbound["end_ctl"], mbound["end_atl"], True
    fresh = []
    if future_sub:                                           # today-onward, seeded from live state
        fweeks, fbound = generate_block(future_sub, phase_start, end_ctl, end_atl,
                                        easy_pace_sec, adjust, zones, today=today)   # §6o partial week
        fresh = [{**w, "elapsed": False, "frozen": False} for w in fweeks]
        end_ctl, end_atl, generated_any = fbound["end_ctl"], fbound["end_atl"], True
    weeks = sorted(frozen + backfilled + fresh, key=lambda w: w["start"])
    return weeks, round(end_ctl, 1), round(end_atl, 1), generated_any


def generate_plan(db):
    """Engine entry point (§6b): a pure function of (today, current shape, objectives), with the
    PAST frozen (§6f Step E). Re-periodizes forward to the nearest A-race; falls back to a
    maintenance block when no objective remains. Every call is re-runnable and versioned, so
    adding/removing an objective reshapes the road ahead and the change is diff-able against the
    prior version — while weeks already lived are carried verbatim from the last saved plan."""
    snap = db.execute(
        "SELECT effective_vo2max, fitness, fatigue FROM shape_snapshots "
        "ORDER BY snapshot_date DESC LIMIT 1"
    ).fetchone()
    if not snap:
        return {"ok": False, "error": "no shape snapshot — Sync first"}

    vo2 = snap["effective_vo2max"]
    ctl0 = snap["fitness"] or 0.0
    atl0 = snap["fatigue"] or 0.0
    zones = pace_zones(vo2)

    today = datetime.now().date()
    block_start = _rebase_start(db, today)
    adj = active_adjustment(db, today.isoformat())   # §6c — clamped directive or None
    adj_dir = adj["directive"] if adj else None
    bank = rebase_banking(db, block_start, today.isoformat())   # §6e — earned faster exit
    shape = REBASE_SHAPE[:bank["effective_len"]]
    rebase_weeks_n = len(shape)

    prior = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    prior_plan = json.loads(prior["plan"]) if prior else None   # §6f E — source of frozen weeks
    earned = earned_state(db, today, prior_plan)   # §6e/§6f — earned volume lift (opt-in; no-op off)
    freq = freq_state(db, today, prior_plan)       # §6e — earned 6th run (opt-in; no-op off)

    # §6f Step E — the live seed for the FIRST today-onward week is today's snapshot CTL/ATL (the
    # snapshot already embodies the frozen past); `started` flips once any future week is generated,
    # after which later phases chain off the previous phase's projected end.
    live = {"ctl": ctl0, "atl": atl0, "started": False}

    def _gen_phase(key, phase_start, shape_, zones_):
        seed = (live["ctl"], live["atl"])
        weeks_, ec, ea, gen = _split_freeze(shape_, phase_start, seed, zones["easy_top"],
                                            adj_dir, zones_, _prior_weeks_by_start(prior_plan, key),
                                            today)
        if gen:
            live["ctl"], live["atl"], live["started"] = ec, ea, True
        return {"start": phase_start.isoformat(), "weeks": weeks_, "end_ctl": ec, "end_atl": ea,
                "clipped_by_acwr": any(w.get("clipped") for w in weeks_)}, ec

    rb, _rb_end = _gen_phase("rebase", block_start, shape, None)   # re-base is pure easy (no zones)

    objs = [dict(r) for r in db.execute(
        "SELECT * FROM objectives WHERE status='upcoming' ORDER BY date").fetchall()]
    chain, tune_ups = select_chain(objs, today)   # §6q — full A-race chain toward the FINAL peak
    anchor = chain[-1] if chain else None

    plan = {
        "ok": True,
        "generated_at": _now_iso(),
        "shape": {"effective_vo2max": vo2, "ctl": ctl0, "atl": atl0},
        "pace_zones": {k: f"{fmt_pace(v)}/km" for k, v in zones.items()},
        "rebase": {**rb, "banked_streak": bank["banked_streak"], "graduated": bank["graduate"],
                   "grad_at": REBASE_GRAD_AT, "full_len": len(REBASE_SHAPE)},
        "earned": earned,   # §6e/§6f earned volume lift — gate state + factor (1.0 = off / no-op)
        "freq": freq,       # §6e earned frequency advance — gate state + target runs (5 = off / no-op)
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
        phases, total_weeks = periodize_chain(today, chain, rebase_weeks=rebase_weeks_n)
        plan["mode"] = "race"
        plan["objective"] = {"label": anchor["label"], "date": anchor["date"],
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
        earned_applied = False
        ctl_floor_active, ctl_floor_anchor = False, live["ctl"]   # §6h — floor state for the surfaces
        race_proj = {}   # §6q — projected end-CTL at each race (end of its taper), for the surfaces
        for ph in phases:
            kind, key, n_wk = ph["kind"], ph["key"], ph["weeks"]
            if kind == "rebase" or n_wk <= 0:
                continue   # the re-base block is already generated above as `rb`
            building = kind in ("base", "build", "bridge")   # the volume-building phases
            sh = SHAPERS[kind](n_wk, cur_km)
            # §6h — CTL-responsive volume FLOOR (building phases): lift non-down weeks to match this
            # phase's measured/projected CTL (live["ctl"] is its seed), so the plan tracks fitness, not
            # just the fixed ramp. Dormant at low CTL (pure no-op). Applied BEFORE the earned lift so the
            # two compose predictably; both skip down weeks and the ACWR governor still caps every week.
            if building:
                if kind == "base":
                    ctl_floor_anchor = live["ctl"]
                floored = _apply_ctl_floor(sh, live["ctl"])
                ctl_floor_active = ctl_floor_active or (floored is not sh)
                sh = floored
            # §6e/§6f — earned volume lift: apply ONCE, to the FIRST Base/Build phase (the initial
            # build), NOT to a post-race bridge — so a short first-race runway can't land the banked
            # boost on a recovery re-build. Build/Peak inherit the lifted level through `cur_km`.
            if kind in ("base", "build") and not earned_applied and earned["factor"] > 1.0:
                sh = _apply_earned_lift(sh, earned["factor"])   # non-down weeks; governor still caps
                earned_applied = True
            # §6e — earned FREQUENCY advance: the 6th run on non-down building weeks (not Peak/Taper).
            # Orthogonal to the volume lifts (changes `runs`, not km), applied per phase since `runs`
            # isn't carried through `cur_km`. Constant volume; governor caps.
            if building:
                sh = _apply_freq_advance(sh, freq["active"])
            block, end_ctl = _gen_phase(key, cur_start, sh, zones)
            plan[key] = block
            cur_start = cur_start + timedelta(weeks=n_wk)
            cur_km = block["weeks"][-1]["intent_km"] if block["weeks"] else cur_km
            proj_end_ctl = end_ctl
            if kind == "taper":
                race_proj[key] = end_ctl   # keyed by the unique taper key (labels can duplicate)

        # §6h — CTL-responsive volume floor state for the surfaces (dormant until measured CTL
        # outruns the conservative ramp ~CTL 35–45; it's the mechanism that lets a faster-than-
        # projected rebuild raise volume, not a change to today's plan).
        plan["ctl_floor"] = {"k": K_CTL_VOLUME, "anchor_ctl": round(ctl_floor_anchor or 0, 1),
                             "floor_km": round(K_CTL_VOLUME * (ctl_floor_anchor or 0)),
                             "active": ctl_floor_active}

        # §6f Step E — feasibility re-reads the engine's REAL end-of-taper CTL for the FINAL race
        # (chained through every segment under the ceiling), not just the generic growth estimate.
        plan["feasibility"] = feasibility(anchor, ctl0, vo2, total_weeks, projected_ctl=proj_end_ctl)
        # §6q — annotate each chain race with its own projected end-of-taper CTL (for the surfaces).
        # Map by the segment's taper KEY (chain index i → "taper"/"taper{i}"), not the human label,
        # since two races can share a label.
        for i, c in enumerate(plan["chain"]):
            tk = "taper" if i == 0 else f"taper{i}"
            if tk in race_proj:
                c["proj_ctl"] = round(race_proj[tk], 1)
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
    return plan


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


def regenerate(db, baseline=None):
    """Regenerate the plan, save a new version, and return it with a diff. If `baseline` (a
    plan computed for today BEFORE the triggering change) is given, diff against it so only the
    change's own effect shows. Otherwise fall back to the last saved plan (a manual regenerate
    has no 'before' action to isolate)."""
    if baseline is None:
        prev = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
        baseline = json.loads(prev["plan"]) if prev else None
    plan = generate_plan(db)
    if not plan.get("ok"):
        return plan
    save_plan(db, plan)
    plan["diff"] = diff_plans(baseline, plan)
    return plan


def save_plan(db, plan):
    db.execute(
        "INSERT INTO plans (created_at, for_date, inputs, plan) VALUES (?,?,?,?)",
        (_now_iso(), datetime.now().strftime("%Y-%m-%d"),
         json.dumps(plan.get("shape", {})), json.dumps(plan)),
    )
    db.commit()


def seed_objectives(db):
    """Optionally seed ONE objective from SH_SEED_OBJECTIVE on a fresh DB (no objectives yet). Default
    is no seed — a self-hoster adds their race in the Objectives UI; with none the engine runs in
    maintenance mode."""
    if SEED_OBJECTIVE is None:
        return
    n = db.execute("SELECT COUNT(*) FROM objectives").fetchone()[0]
    if n == 0:
        o = SEED_OBJECTIVE
        db.execute(
            "INSERT INTO objectives (type,label,date,target,priority,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (o["type"], o["label"], o["date"], o["target"], o["priority"], "upcoming", _now_iso()),
        )
        db.commit()


# ── LLM adjustment layer (§6c) ───────────────────────────────────────────────
# Claude owns *language and judgment*; the deterministic engine (§6a) owns the numbers and
# clamps every suggestion. Design rules for everything in this section:
#   • OPTIONAL — the whole app must run with no ANTHROPIC_API_KEY. Each entry point degrades
#     to {"ok": False, "error": ...} and the deterministic paths keep working untouched.
#   • ADVISORY — the LLM proposes structured data; the engine/user validates before it lands.
#     We never let the model write the plan or invent numbers outside the guardrails.
# First capability: parse a runner's natural-language objective into the structured form the
# engine already validates (§5). More (plan explanation, qualitative readiness/adjustment) build
# on this same client + JSON-schema helper.

_anthropic_client = None


def _anthropic():
    """Lazy Anthropic client. Returns None (never raises) when the SDK isn't installed or no
    key is set, so the rest of the app is unaffected."""
    global _anthropic_client
    if not ANTHROPIC_API_KEY:
        return None
    if _anthropic_client is None:
        try:
            import anthropic
            _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        except Exception:
            return None
    return _anthropic_client


def llm_available():
    return _anthropic() is not None


def llm_json(system, user, schema, effort="low", max_tokens=1024):
    """One structured-output call: returns a dict validated against `schema` (Claude's JSON is
    constrained by output_config.format), or {"ok": False, "error": ...} on any failure. Kept
    deliberately small — the engine, not the model, makes the numeric decisions."""
    client = _anthropic()
    if client is None:
        return {"ok": False, "error": "LLM not configured — set ANTHROPIC_API_KEY in .env"}
    try:
        import anthropic
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort,
                           "format": {"type": "json_schema", "schema": schema}},
        )
        if resp.stop_reason == "refusal":
            return {"ok": False, "error": "LLM declined the request"}
        text = next((b.text for b in resp.content if b.type == "text"), "")
        data = json.loads(text)
        data["ok"] = True
        return data
    except anthropic.APIStatusError as e:  # auth, rate-limit, server, etc.
        return {"ok": False, "error": f"LLM error ({getattr(e, 'status_code', '?')})"}
    except Exception as e:
        return {"ok": False, "error": f"LLM error: {e}"}


OBJECTIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["5k", "10k", "half", "marathon", "custom"]},
        "label": {"type": "string", "description": "Short race name, e.g. 'Berlin Marathon'."},
        "date": {"type": "string", "format": "date",
                 "description": "Race day as YYYY-MM-DD. Resolve relative dates against today."},
        "target": {"type": "string",
                   "description": "Goal time like '3:55:00' or 'sub-45:00', or 'finish'."},
        "priority": {"type": "string", "enum": ["A", "B", "C"],
                     "description": "A=goal race (full taper/peak); B/C=tune-up."},
        "interpretation": {"type": "string",
                           "description": "One short sentence on how you read the request."},
        "confident": {"type": "boolean",
                      "description": "False if the date or target had to be guessed."},
    },
    "required": ["type", "label", "date", "target", "priority", "interpretation", "confident"],
    "additionalProperties": False,
}


def parse_objective_nl(text, today=None):
    """Turn 'sub-45 10k in October' / 'spring marathon, want to BQ' into a structured objective
    (§6c). Returns the parsed fields for the owner to review — it does NOT save; the existing
    deterministic add path (which periodizes + validates) stays the single writer."""
    today = today or datetime.now().date().isoformat()
    system = (
        "You convert a runner's natural-language race goal into a structured training objective. "
        f"Today is {today}. Resolve relative dates ('in October', 'spring', 'next month') to a "
        "concrete YYYY-MM-DD; if only a month/season is given, pick a plausible race day in it and "
        "set confident=false. type is the distance bucket (use 'custom' for anything non-standard). "
        "target is a goal time ('3:55:00', 'sub-45:00') or 'finish' if none is stated. priority: "
        "A=goal race that gets a full taper and peak, B/C=tune-up; default a marathon to A and a "
        "short race to B unless the runner clearly marks it as their main goal. Keep label short. "
        "Never invent a target the runner didn't imply — use 'finish'."
    )
    out = llm_json(system, text.strip(), OBJECTIVE_SCHEMA, effort="low")
    if not out.get("ok"):
        return out
    # clamp to the engine's enums (belt-and-suspenders; schema already constrains these)
    if out.get("type") not in ("5k", "10k", "half", "marathon", "custom"):
        out["type"] = "custom"
    if out.get("priority") not in ("A", "B", "C"):
        out["priority"] = "A"
    return out


# Qualitative adjustment (§6c) — the heart of the layer: free-text input the numeric engine
# can't model → an LLM proposal → CLAMPED by the engine before it touches the plan.
ADJUSTMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "situation": {"type": "string",
                      "enum": ["niggle_injury", "illness", "travel", "fatigue",
                               "feeling_good", "life_stress", "other"]},
        "volume_multiplier": {"type": "number",
                              "description": "Fraction of planned load to keep over the window, "
                              "0..1 (0=full rest, 0.5=half, 1=no change). You may only REDUCE or "
                              "hold — never above 1; the plan already ramps to the safe ACWR ceiling."},
        "scope_days": {"type": "integer",
                       "description": "How many days forward, including today, this applies (1..28)."},
        "easy_only": {"type": "boolean",
                      "description": "Force easy effort over the window (drop any quality)."},
        "medical_flag": {"type": "boolean",
                         "description": "True if the symptom warrants a doctor — ESPECIALLY a return "
                         "of being unable to sustain easy effort / having to stop mid-run, or chest "
                         "pain, dizziness, fainting. When unsure about cardiac/exertional symptoms, "
                         "err toward true."},
        "summary": {"type": "string", "description": "One plain sentence: what you changed and why."},
        "reply": {"type": "string",
                  "description": "A warm, specific one-or-two-sentence reply spoken TO him. For a "
                  "pure reflection (no load change) this is the whole response — acknowledge what he "
                  "felt and, where it fits, affirm it with the plan's own logic. For a real "
                  "adjustment, say plainly what you're proposing and why."},
    },
    "required": ["situation", "volume_multiplier", "scope_days", "easy_only", "medical_flag",
                 "summary", "reply"],
    "additionalProperties": False,
}


def is_noop_adjustment(d):
    """True when a directive would change nothing about the forward plan — i.e. it's a
    reflection ('felt great', 'on plan'), not a real ease/hold/medical signal. The engine
    can only ever *reduce* load, so multiplier ≥ 1 with no easy-only and no medical flag is a
    no-op. Such inputs must NOT be saved as an 'active adjustment' (that's the §6c bug that
    rendered a 1.0 multiplier as 'Load eased to 100% of plan')."""
    try:
        m = float(d.get("volume_multiplier", 1.0))
    except (TypeError, ValueError):
        m = 1.0
    return m >= 1.0 and not d.get("easy_only") and not d.get("medical_flag")


def clamp_adjustment(d, today):
    """The ENGINE's guardrail over the LLM proposal (§6c invariant). Force the directive into
    safe bounds — multiplier ∈ [0,1] (reduce-only, can never add load past the ACWR-bounded
    plan), window ∈ [1,28] days, medical flag ⇒ full rest. Returns (directive, clamp_note)."""
    from datetime import timedelta
    notes = []
    try:
        m = float(d.get("volume_multiplier", 1.0))
    except (TypeError, ValueError):
        m = 1.0
    cm = min(1.0, max(0.0, m))
    if abs(cm - m) > 1e-9:
        notes.append(f"load ×{m:g}→×{cm:g} (engine allows 0–1, reduce-only)")
    try:
        sd = int(d.get("scope_days", 1))
    except (TypeError, ValueError):
        sd = 1
    csd = min(28, max(1, sd))
    if csd != sd:
        notes.append(f"window {sd}→{csd} days (max 28)")
    medical = bool(d.get("medical_flag"))
    if medical and cm > 0:
        cm = 0.0
        notes.append("symptom flagged → full rest + see your doctor")
    directive = {
        "situation": d.get("situation", "other"),
        "volume_multiplier": round(cm, 2),
        "scope_days": csd,
        "easy_only": bool(d.get("easy_only")) or cm < 1.0,
        "medical_flag": medical,
        "summary": d.get("summary", ""),
        "applies_from": today,
        "applies_until": (_date(today) + timedelta(days=csd - 1)).isoformat(),
    }
    return directive, (" · ".join(notes) if notes else None)


def propose_adjustment(text, today=None, easy_pace=None):
    """§6c — read a masters runner's free-text status and decide what it is. Two outcomes,
    classified by the engine (not the model) from the clamped directive:
      • a *reflection* ('felt great', 'missed the joy of finishing') → kind='log': nothing to
        change, just a warm reply that affirms it with the plan's own logic. Routed to the
        session journal, never saved as an adjustment.
      • a real *adjustment* ('knee's sore', 'travelling Mon–Fri') → kind='adjust': a bounded,
        engine-clamped directive the owner confirms via apply.
    Proposal only — nothing is saved here."""
    today = today or datetime.now().date().isoformat()
    pace_line = (
        f"Their engine-set EASY target is ~{easy_pace}/km — and the plan's premise is that easy days "
        "habitually run faster than that are really THRESHOLD effort. If they reflect that an "
        "easier/slower run felt better or more sustainable, AFFIRM it: that's exactly what the plan is "
        "for. "
    ) if easy_pace else ""
    ctx_line = f"Athlete context: {ATHLETE_CONTEXT}. " if ATHLETE_CONTEXT else ""
    system = (
        "You read a runner's free-text status. Most days it's a REFLECTION on how a run felt "
        "(no change needed); sometimes it's a real signal to ease back. "
        f"Today is {today}. " + ctx_line + pace_line +
        "You can ONLY ease or hold load (volume_multiplier 0..1) and force easy effort; you CANNOT "
        "add load — the deterministic engine already ramps to the safe ACWR ceiling, so a positive "
        "reflection ('feeling great') keeps them on plan (multiplier 1, easy_only false): it does NOT "
        "unlock more, and it is NOT an adjustment. Only set multiplier<1 or easy_only=true for a "
        "genuine reason to back off. Map a real situation to a sensible multiplier and forward window: "
        "a minor niggle ~0.6 for a few days, illness/fever 0 until better, travel to whatever's "
        "realistic, general fatigue ~0.7 short. Set medical_flag=true for a stop-the-run exertional "
        "symptom, chest pain, dizziness or fainting — when unsure about cardiac/exertional "
        "symptoms, err toward true. Always write `reply` directly to them. You never diagnose or give "
        "medical advice; you flag and defer to their doctor."
    )
    out = llm_json(system, text.strip(), ADJUSTMENT_SCHEMA, effort="low")
    if not out.get("ok"):
        return out
    directive, clamp = clamp_adjustment(out, today)
    kind = "log" if is_noop_adjustment(directive) else "adjust"
    return {"ok": True, "kind": kind, "reply": out.get("reply", ""),
            "note": text.strip(), "directive": directive, "clamp": clamp}


def active_adjustment(db, today):
    """The current clamped adjustment still in its window (most recent active), or None. Read by
    generate_plan so the plan stays a pure function of (today, shape, objectives, adjustments)."""
    row = db.execute(
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


# Readiness judgment (§6c×§6d) — the LLM turns HRV + the check-in (incl. free text) into the
# amber/red call; the engine keeps a non-softenable FLOOR (the LLM may only escalate caution).
READINESS_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["green", "amber", "red"]},
        "action": {"type": "string", "description": "One sentence: what to do training-wise today."},
        "reasons": {"type": "array", "items": {"type": "string"},
                    "description": "A few short bullets behind the call."},
        "stop_symptom_detected": {"type": "boolean",
                                  "description": "True if the free-text note describes having to STOP "
                                  "mid-run / being unable to sustain easy effort, or chest pain, "
                                  "dizziness, or fainting — the 2025 exertional-intolerance pattern."},
    },
    "required": ["verdict", "action", "reasons", "stop_symptom_detected"],
    "additionalProperties": False,
}


def llm_readiness(hrv, energy, sleep, note):
    """Judgment call from today's signals + free text. Returns the LLM's proposed verdict; the
    engine (assess_readiness) clamps it to its safety floor before anything is shown."""
    state = hrv.get("state")
    hrvtxt = (f"baseline {hrv.get('baseline')} vs normal band {hrv.get('band')} → {state}"
              if state else "no HRV data")
    user = (f"HRV: {hrvtxt}\nLegs/energy: {energy}\nSleep: {sleep}\n"
            f"Their note: {note.strip() if note else '(none)'}")
    system = (
        "You make a daily training-readiness call (green/amber/red) for a runner rebuilding aerobic "
        "fitness. " + (f"Athlete context: {ATHLETE_CONTEXT}. " if ATHLETE_CONTEXT else "") +
        "green=run as planned, amber=hold (easy, no progression), red=easy walk or rest. Weigh HRV, "
        "legs, sleep, and especially their free-text note — that's where nuance the numbers miss shows "
        "up. You may only ESCALATE caution beyond the obvious; a separate deterministic floor already "
        "enforces the minimums (one poor signal ⇒ at least amber, two ⇒ at least red), so don't be "
        "afraid to be cautious. Set stop_symptom_detected=true if the note hints at having to stop "
        "mid-run / can't sustain easy effort, or any chest pain/dizziness/fainting; err toward true "
        "for cardiac/exertional signs. Never diagnose; you flag and defer to their doctor. Keep action "
        "to one sentence."
    )
    return llm_json(system, user, READINESS_SCHEMA, effort="low")


# Plan explanation (§6c) — narrate the already-computed plan and the *why* behind each change,
# in plain language. Read-only: the LLM explains the engine's numbers, it never alters them.
EXPLAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string",
                     "description": "One plain sentence: current shape → where this plan leads."},
        "points": {"type": "array", "items": {"type": "string"},
                   "description": "3–6 short plain-language bullets explaining the plan's logic."},
        "change_note": {"type": "string",
                        "description": "What the most recent re-plan/adjustment did and why; "
                        "empty string if nothing notable changed."},
    },
    "required": ["headline", "points", "change_note"],
    "additionalProperties": False,
}


def _phase_block_summary(block):
    """Compact per-phase view for the explainer (§6f Step F): volume range, the quality kinds the
    polarized model placed, projected end fitness, and how many weeks are already frozen/done."""
    if not block or not block.get("weeks"):
        return None
    ws = block["weeks"]
    kms = [w.get("intent_km", w.get("km")) for w in ws]
    quality = sorted({s["kind"] for w in ws for s in w.get("sessions", []) if s.get("reps")})
    return {"weeks": len(ws), "km_range": [min(kms), max(kms)] if kms else None,
            "quality": quality or None, "end_ctl": block.get("end_ctl"),
            "frozen_done_weeks": sum(1 for w in ws if w.get("frozen"))}


def _plan_summary_for_llm(plan, diff):
    """Compact, grounded view of the engine's plan for the explainer — numbers only, no prose to
    parrot, so the model explains rather than invents."""
    rb = plan.get("rebase", {})
    weeks = [f"wk{w['wk']}: {w['km']}km/{w['runs']} runs, end-ACWR~{w.get('proj_acwr')}"
             + (" [eased]" if w.get("adjusted") else "") + (" [clipped-to-ACWR]" if w.get("clipped") else "")
             + (" [frozen/done]" if w.get("frozen") else "")
             for w in rb.get("weeks", [])]
    return {
        "mode": plan.get("mode"),
        "objective": plan.get("objective"),
        # drop `estimate_ctl` — the generic optimistic fallback — so the narration can't anchor on
        # it and inflate the race-day CTL; `projected_race_ctl` (the real chained projection) is the
        # one authoritative number (§6f Step E/F; caught by the real-key plan-explain self-test).
        "feasibility": {k: v for k, v in (plan.get("feasibility") or {}).items()
                        if k != "estimate_ctl"},
        "phases": plan.get("phases"),
        "phase_blocks": {k: _phase_block_summary(plan.get(k))      # §6f Step F — Base→Taper detail
                         for k in ("base", "build", "peak", "taper")},
        "projected_race_ctl": (plan.get("feasibility") or {}).get("projected_ctl"),
        "shape_now": plan.get("shape"),
        "rebase_start": rb.get("start"),
        "rebase_end_ctl": rb.get("end_ctl"),
        "rebase_end_atl": rb.get("end_atl"),
        "rebase_banked_streak": rb.get("banked_streak"),
        "rebase_graduated_weeks_early": rb.get("graduated"),
        "earned_progression": plan.get("earned"),   # §6e/§6f — earned volume lift (active + factor)
        "freq_advance": plan.get("freq"),            # §6e — earned 6th run (active + target runs)
        "ctl_volume_floor": plan.get("ctl_floor"),   # §6h — volume tracking measured CTL (active when lifting)
        "weeks": weeks,
        "easy_pace": plan.get("pace_zones", {}).get("easy_top"),
        "engine_note": plan.get("note"),
        "active_adjustment": plan.get("adjustment"),
        "last_replan": diff,
    }


def explain_plan(db, diff=None):
    """§6c — plain-language 'why' for the latest plan (and the most recent change)."""
    row = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return {"ok": False, "error": "no plan yet — generate one first"}
    try:
        plan = json.loads(row["plan"])
    except (ValueError, TypeError):
        return {"ok": False, "error": "plan unreadable"}
    system = (
        "You explain an already-computed running plan to its owner — a runner rebuilding toward a goal "
        "race — in plain, warm, concrete language. " +
        (f"Athlete context: {ATHLETE_CONTEXT}. " if ATHLETE_CONTEXT else "") +
        "The numbers are FIXED by a deterministic "
        "sports-science engine: never change, recompute, or invent any; your only job is the 'why'. "
        "Cover where their current shape sits, why the re-base is easy-dominant (an easy day run faster "
        "than the easy target is really threshold effort — running slower IS the work), how weekly load "
        "ramps while projected ACWR stays in the safe band, and an honest read on the goal "
        "(finishing healthy vs a time near their PB). Walk the WHOLE phase path in phase_blocks: Base "
        "grows easy aerobic volume with a light cruise-tempo on-ramp; Build adds the specific work — "
        "VO₂ intervals plus a marathon-pace finish on the long run; Peak sharpens at race pace; Taper "
        "drops volume to arrive fresh. Stress that it stays POLARIZED (~80%+ easy every week — the "
        "hard work is a small, concentrated slice, never a target to fill). CRITICAL — race-day "
        "fitness: state ONLY projected_race_ctl, exactly as given. Do NOT compute, extrapolate, or "
        "estimate CTL growth yourself: a naive 3–4%/week extrapolation is WRONG here because the ACWR "
        "ceiling caps real growth far below that — which is the whole reason projected_race_ctl is so "
        "much lower than a back-of-envelope guess, and why the goal is finishing, not a PB. Never cite "
        "a CTL above projected_race_ctl. (You may mention a phase's end_ctl from phase_blocks when "
        "walking the path, but the race-day number is projected_race_ctl alone.) If any "
        "phase shows frozen_done_weeks, note those weeks are completed and carried verbatim — the past "
        "isn't rewritten, only the road ahead. If "
        "rebase_graduated_weeks_early > 0, note they EARNED a faster exit from Phase 0 by banking solid "
        "weeks (adherence + recovery) — the block is shorter and base-build starts sooner; stress the "
        "reward is time, not extra load, and the safe ACWR ceiling never moved. If "
        "earned_progression.active is true, note they OPTED IN to an earned faster build and, by banking "
        "weeks, have earned a small (~factor) volume bump on the HARD Base/Build weeks only — recovery "
        "(down) weeks and the ACWR ≤1.25 ceiling are deliberately left untouched, and they can turn it "
        "off anytime; do NOT imply the ceiling rose or that recovery weeks got harder. If "
        "ctl_volume_floor.active is true, note their volume has risen to track their MEASURED fitness "
        "(their CTL outran the conservative projection) — the engine rewarding a faster-than-expected "
        "rebuild; the ACWR ceiling and recovery weeks are unchanged. If freq_advance.active is true, "
        "note they OPTED IN to an earned 6th weekly run on the HARD Base/Build weeks — it's added at the "
        "SAME weekly volume (the runs get shorter, the week is not heavier), a frequency reward they "
        "earned by banking weeks; recovery (down) weeks keep fewer runs and the ACWR ceiling is "
        "unchanged. Be honest: it's more frequency for durability, not an easier or harder week; they "
        "can turn it off anytime. If last_replan or "
        "active_adjustment is set, explain what changed and why in change_note (else empty string). "
        "Encouraging, specific, never medical advice. Keep bullets short."
    )
    return llm_json(system, json.dumps(_plan_summary_for_llm(plan, diff)),
                    EXPLAIN_SCHEMA, effort="low", max_tokens=1200)


# Multi-objective conflict adjudication (§6c) — when ≥2 upcoming A-races compete, the LLM advises
# which should be the true peak and which to demote to a tune-up. ADVISORY: it recommends priority
# changes; the owner applies them, then the deterministic engine periodizes from the result.
ADJUDICATE_SCHEMA = {
    "type": "object",
    "properties": {
        "primary_id": {"type": "integer", "description": "Objective id to treat as the main A-race (the peak)."},
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "label": {"type": "string"},
                    "suggested_priority": {"type": "string", "enum": ["A", "B", "C"]},
                    "reason": {"type": "string", "description": "One short sentence."},
                },
                "required": ["id", "label", "suggested_priority", "reason"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string", "description": "One plain sentence on the call."},
    },
    "required": ["primary_id", "recommendations", "summary"],
    "additionalProperties": False,
}


def adjudicate_objectives(db, today=None):
    """§6c — judgment over competing A-races. Returns a priority recommendation per objective; the
    engine still owns periodization (it anchors to the nearest A and demotes the rest to tune-ups)."""
    today = today or datetime.now().date().isoformat()
    objs = [dict(r) for r in db.execute(
        "SELECT id,type,label,date,target,priority FROM objectives "
        "WHERE status='upcoming' ORDER BY date").fetchall()]
    future = [o for o in objs if o["date"] > today]
    if sum(1 for o in future if o["priority"] == "A") < 2:
        return {"ok": False, "error": "no A-race conflict to adjudicate"}
    snap = db.execute("SELECT effective_vo2max, fitness FROM shape_snapshots "
                      "ORDER BY snapshot_date DESC LIMIT 1").fetchone()
    shape = ({"effective_vo2max": snap["effective_vo2max"], "ctl": snap["fitness"]}
             if snap else {})
    for o in future:
        o["weeks_away"] = max(0, (_date(o["date"]) - _date(today)).days // 7)
    ctx = {"today": today, "shape": shape,
           "objectives": [{k: o[k] for k in ("id", "type", "label", "date", "target",
                                             "priority", "weeks_away")} for o in future]}
    system = (
        "You adjudicate competing race goals for a runner rebuilding toward a goal race. " +
        (f"Athlete context: {ATHLETE_CONTEXT}. " if ATHLETE_CONTEXT else "") +
        "A true A-race earns a full taper and peak — you CANNOT "
        "peak for two races within ~4 weeks of each other, so the nearer/secondary one should drop to "
        "a B/C tune-up subordinated to the main goal. Well-separated A-races (months apart) can stand "
        "as sequential peaks. Finishing the goal race healthy is the real prize; "
        "weigh that, the runway, and current shape. Recommend exactly one primary_id (the peak) and a "
        "suggested_priority + one-line reason for EVERY objective given. Only re-rank the objectives "
        "provided — never invent races. You advise; the engine periodizes from the priorities they keep."
    )
    out = llm_json(system, json.dumps(ctx), ADJUDICATE_SCHEMA, effort="medium", max_tokens=1200)
    if not out.get("ok"):
        return out
    valid_ids = {o["id"] for o in future}
    out["recommendations"] = [r for r in out.get("recommendations", [])
                              if r.get("id") in valid_ids
                              and r.get("suggested_priority") in ("A", "B", "C")]
    if out.get("primary_id") not in valid_ids:
        out["primary_id"] = None
    return out


# ── Readiness gate (§6d) ─────────────────────────────────────────────────────
# Decides whether to run today's prescribed session as-is, soften it, or rest. Combines an
# objective HRV signal (hrvBaseline vs its normal band, from statistics/current — the only
# readiness metric the personal REST API exposes; RHR/sleep trends are MCP-only) with a
# subjective daily check-in. The check-in is the safety-critical input: a returning
# "had-to-stop" exertional symptom RED-flags the day and halts the plan (his 2025 history).
def hrv_signal(db):
    """Objective HRV readiness from the latest shape snapshot: 'low' | 'ok' | 'high' | None."""
    row = latest_snapshot(db)
    if not row:
        return {"state": None}
    s = json.loads(row["raw"])
    b, rng = s.get("hrvBaseline"), s.get("hrvNormalRange")
    if b is None or not rng:
        return {"state": None}
    lo, hi = rng
    state = "low" if b < lo else "high" if b > hi else "ok"
    return {"state": state, "baseline": round(b, 1),
            "band": [round(lo, 1), round(hi, 1)]}


def assess_readiness(db, checkin):
    """Combine the HRV signal + the day's check-in → a traffic-light verdict + action.
    GREEN proceed · AMBER hold (keep easy, no progression) · RED rest/walk (and, on a
    returning stop-symptom, HALT the plan and advise the doctor)."""
    hrv = hrv_signal(db)
    energy = (checkin or {}).get("energy", "ok")
    sleep = (checkin or {}).get("sleep", "ok")
    stop = bool((checkin or {}).get("stop_symptom"))
    reasons = []

    if stop:
        return {"verdict": "red", "halt": True, "hrv": hrv,
                "action": "Stop — do not train. The exertional symptom that preceded 2025 is "
                          "back. Rest and contact your doctor before resuming.",
                "reasons": ["Returning 'had-to-stop' exertional symptom"]}

    poor = 0
    if hrv["state"] == "low":
        poor += 1; reasons.append("HRV below its normal band")
    if energy == "heavy":
        poor += 1; reasons.append("Legs/energy feel heavy")
    if sleep == "poor":
        poor += 1; reasons.append("Poor sleep")

    if poor >= 2:
        floor, action = "red", ("Easy walk or full rest today — two readiness signals are "
                                "down. Don't force the session; let it come back.")
    elif poor == 1:
        floor, action = "amber", ("Hold today — keep it easy and skip any progression "
                                  "(no strides/longer run). Re-assess tomorrow.")
    else:
        floor, action = "green", "Good to go — run today's prescribed session as planned."
    base = {"verdict": floor, "halt": False, "hrv": hrv, "action": action,
            "reasons": reasons or ["All signals normal"], "source": "engine"}

    # §6c judgment layer: the LLM may sharpen/escalate the call (reading the free-text note the
    # numbers can't), but the engine FLOOR above is never softened.
    note = (checkin or {}).get("note", "")
    llm = llm_readiness(hrv, energy, sleep, note) if llm_available() else None
    if not (llm and llm.get("ok")):
        return base
    if llm.get("stop_symptom_detected"):  # free-text safety catch → same halt as the checkbox
        return {"verdict": "red", "halt": True, "hrv": hrv,
                "action": "Stop — your note reads like the exertional symptom that preceded 2025. "
                          "Rest and contact your doctor before resuming.",
                "reasons": ["AI flagged a possible 'had-to-stop' symptom in your note"],
                "source": "llm", "engine_floor": floor}
    sev = {"green": 0, "amber": 1, "red": 2}
    ai = llm["verdict"] if llm.get("verdict") in sev else floor
    if sev[ai] >= sev[floor]:   # LLM at least as cautious → adopt its (richer) language
        return {"verdict": ai, "halt": False, "hrv": hrv,
                "action": llm.get("action") or action,
                "reasons": llm.get("reasons") or base["reasons"],
                "source": "llm", "engine_floor": floor, "ai_verdict": ai}
    # LLM tried to soften below the floor → engine holds, but record the disagreement
    return {**base, "engine_floor": floor, "ai_verdict": ai,
            "source": "engine (floor held over AI's %s)" % ai}


def runs_on_date(db, date):
    """Actual running done on `date` (duplicates excluded), summed → {km, pace} or None.
    Same match rule as block_log (any synced Running activity with distance>0 = a session
    was done) so the readiness tile and the journal never disagree. Date-based, so a logged
    session reads as 'done' for the rest of that local day and clears at midnight on its own."""
    drop = dropped_ids(db)
    km = sec = 0.0
    for r in db.execute(
        "SELECT id, distance, duration FROM activities WHERE date=? AND sport=?",
        (date, RUNNING_SPORT)
    ).fetchall():
        if r["id"] in drop or not r["distance"]:
            continue
        km += r["distance"]
        sec += (r["duration"] or 0.0)
    if km <= 0:
        return None
    pace = sec / (km * 60) if km else 0
    return {"km": round(km, 1),
            "pace": (f"{int(pace)}:{int((pace*60) % 60):02d}" if pace else None)}


def todays_session(db, today):
    """Today's prescription from the latest plan. Returns a session, a rest day, or a
    block-state marker so the readiness tile can tell apart 'no plan at all' (None) from
    'a plan exists but the block hasn't started / has finished' — the latter must NOT read
    as "no active plan". A run already logged for today marks the session `done`."""
    row = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None  # genuinely no plan generated yet
    plan = json.loads(row["plan"])
    weeks = plan.get("rebase", {}).get("weeks", [])
    if not weeks:
        return None
    if today < weeks[0]["start"]:  # plan active, but the re-base hasn't begun yet
        return {"kind": "pre", "start": weeks[0]["start"]}
    for wk in weeks:
        for s in wk.get("sessions", []):
            if s.get("date") == today:
                actual = runs_on_date(db, today)
                return {**s, "week": wk["wk"], "easy_pace": plan["pace_zones"].get("easy_top"),
                        "done": bool(actual), "actual": actual}
    # inside the block window but nothing scheduled → rest day
    last_end = max((s["date"] for w in weeks for s in w["sessions"]), default="")
    if today <= last_end:
        return {"kind": "rest", "note": "Rest day — recovery is part of the plan."}
    return {"kind": "post"}  # block complete — time to periodize the next phase


def latest_easy_pace(db):
    """The easy-pace string ('7:11') from the most recent plan, or None — fed to the §6c
    reflection reply so it can affirm 'your easy target is X, you were running threshold'."""
    row = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    try:
        z = json.loads(row["plan"]).get("pace_zones", {})
    except (ValueError, TypeError):
        return None
    return (z.get("easy_top") or "").replace("/km", "").strip() or None


def block_log(db):
    """The training log for the live re-base block: each planned session enriched with whether
    a matching run was actually done (by date), the actual km/pace, and any reflection note.
    'Done' and actual-vs-planned are DERIVED from synced `activities` — the journal only stores
    the free-text note. Returns {weeks, adherence, start, end} or None when there's no plan."""
    row = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    plan = json.loads(row["plan"])
    weeks = plan.get("rebase", {}).get("weeks", [])
    if not weeks:
        return None
    start = weeks[0]["start"]
    end = max(s["date"] for w in weeks for s in w["sessions"])
    today = datetime.now().strftime("%Y-%m-%d")
    drop = dropped_ids(db)
    # running activities in the block window: SUMMED per day for plan-vs-actual + the projector
    # (whole-body load is daily), but each run kept individually (time-ordered) so a DOUBLE shows both
    # halves, not a silent merge (§ doubles v1). The day's load is session-count-agnostic; this is
    # display only — daily_trimp_series/the governor are unchanged.
    acts = {}
    for r in db.execute(
        "SELECT id, date, distance, duration FROM activities "
        "WHERE date>=? AND date<=? AND sport=? ORDER BY date_time", (start, end, RUNNING_SPORT)
    ).fetchall():
        if r["id"] in drop or not r["distance"]:
            continue
        a = acts.setdefault(r["date"], {"km": 0.0, "sec": 0.0, "id": None, "_maxkm": 0.0, "runs": []})
        a["km"] += r["distance"]
        a["sec"] += (r["duration"] or 0.0)
        a["runs"].append({"id": r["id"], "km": r["distance"], "sec": r["duration"] or 0.0})
        if r["distance"] > a["_maxkm"]:   # representative run for the day = its longest (for the map view)
            a["_maxkm"] = r["distance"]; a["id"] = r["id"]

    def _pace_str(sec, km):
        p = sec / (km * 60) if km else 0
        return f"{int(p)}:{int((p * 60) % 60):02d}" if p else None

    def _breakdown(a):   # per-run detail for a DOUBLE (≥2 runs that day); None for a single run
        return ([{"km": round(rr["km"], 1), "pace": _pace_str(rr["sec"], rr["km"]),
                  "activity_id": rr["id"]} for rr in a["runs"]] if a and len(a["runs"]) > 1 else None)

    notes = {r["date"]: r["note"] for r in db.execute("SELECT date, note FROM session_log").fetchall()}
    sched = done = 0
    out_weeks = []
    from datetime import timedelta
    for w in weeks:
        sessions = []
        for s in w["sessions"]:
            d = s["date"]
            past = d <= today
            act = acts.get(d)
            if d < today or (d == today and act):
                sched += 1
            actual = None
            if act and act["km"] > 0:
                done += 1 if past else 0
                pace = act["sec"] / (act["km"] * 60) if act["km"] else 0
                actual = {"km": round(act["km"], 1),
                          "pace": (f"{int(pace)}:{int((pace*60) % 60):02d}" if pace else None)}
            sessions.append({**s, "done": bool(actual), "missed": past and not actual and d < today,
                             "actual": actual, "reflection": notes.get(d), "runs": _breakdown(act),
                             "activity_id": (act["id"] if (act and act["km"] > 0) else None)})
        # surface UNPLANNED runs (§ out-of-schedule): a run on a day this week with no planned
        # session — bonus volume the runner chose to do. Counted as load by the projector/governor
        # already; here we just make it VISIBLE on its day. It does NOT touch adherence (it was never
        # scheduled, so neither sched nor done move) — only the planned-session loop above feeds those.
        planned = {s["date"] for s in w["sessions"]}
        we = (_date(w["start"]) + timedelta(days=6)).isoformat()
        for d in sorted(acts):
            a = acts[d]
            if w["start"] <= d <= we and d not in planned and a["km"] > 0:
                pace = a["sec"] / (a["km"] * 60) if a["km"] else 0
                sessions.append({
                    "date": d, "km": None, "kind": "unplanned", "unplanned": True,
                    "done": True, "missed": False,
                    "actual": {"km": round(a["km"], 1),
                               "pace": (f"{int(pace)}:{int((pace*60) % 60):02d}" if pace else None)},
                    "reflection": notes.get(d), "runs": _breakdown(a), "activity_id": a["id"]})
        sessions.sort(key=lambda s: s["date"])              # unplanned runs slot into calendar order
        out_weeks.append({**w, "sessions": sessions})
    # What he actually ran across the block window (dups already excluded) — real recorded
    # distance + duration, so "ran so far" is owned data, not km×pace.
    ran = {"km": round(sum(a["km"] for a in acts.values()), 1),
           "min": round(sum(a["sec"] for a in acts.values()) / 60),
           "runs": sum(1 for a in acts.values() if a["km"] > 0)}
    return {"weeks": out_weeks, "start": start, "end": end, "today": today,
            "adherence": {"done": done, "scheduled": sched}, "ran": ran}


BONUS_ACWR_MAX = 1.0   # ACWR below this = clear headroom under the 1.25 weekly cap → an easy add is "free"


def _bonus_run_ok(verdict, session_kind, acwr):
    """§6o — on a planned REST day, is an easy 'bonus' run clearly fine to OFFER? Yes iff readiness is
    green, today is a rest day, and ACWR is low (clear headroom under the weekly cap). Pure; a NOTE
    only — it changes NO prescription, the ACWR governor still caps the week (reduce-only philosophy:
    we never auto-prescribe MORE, we just tell the runner when an opt-in easy add is safe headroom)."""
    return (verdict == "green" and session_kind == "rest"
            and acwr is not None and acwr < BONUS_ACWR_MAX)


def today_readiness(db):
    """Today's check-in (if any) + the resulting assessment + today's planned session."""
    today = datetime.now().strftime("%Y-%m-%d")
    row = db.execute("SELECT * FROM readiness WHERE date=?", (today,)).fetchone()
    checkin = dict(row) if row else None
    assessment = assess_readiness(db, checkin)
    session = todays_session(db, today)
    # A green light on a planned rest day means "follow the plan — which today is rest", not
    # "run your session". Reword so the action matches the day (engine or LLM source alike). And when
    # ACWR is low (clear headroom), surface the §6o BONUS-RUN affordance: an easy run is safe extra
    # aerobic base, not a breach — offered, never prescribed (the governor still caps the week).
    if assessment.get("verdict") == "green" and (session or {}).get("kind") == "rest":
        snap = latest_snapshot(db)
        acwr = (snap["fatigue"] / snap["fitness"]) if (snap and snap["fitness"]) else None
        if _bonus_run_ok("green", "rest", acwr):
            assessment = {**assessment, "bonus": True, "acwr": round(acwr, 2),
                          "action": (f"Good to go — today's a planned rest day, but your load is light "
                                     f"(ACWR {acwr:.2f}) and you're green, so an easy run here is BONUS "
                                     f"aerobic base, not a breach — the weekly ACWR ceiling still caps you. "
                                     f"Recovery is also fine.")}
        else:
            assessment = {**assessment,
                          "action": "Good to go — and today's a planned rest day. Take the recovery."}
    # Already ran today's session? Acknowledge it instead of still nudging "run today's session".
    # Never overrides a red/halt — a logged run must not suppress a medical stop signal.
    if (session or {}).get("done") and assessment.get("verdict") != "red" and not assessment.get("halt"):
        act = session.get("actual") or {}
        ran = f"{act.get('km')}k" + (f" @ {act['pace']}/km" if act.get("pace") else "")
        assessment = {**assessment, "done": True,
                      "action": f"Today's session is done — {ran}. Recover; nothing else scheduled."}
    return {"date": today, "checkin": checkin,
            "assessment": assessment, "session": session}


# ── Flask app ───────────────────────────────────────────────────────────────
app = Flask(__name__)

FAVICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" role="img" aria-label="Sparing Horse"><rect width="100" height="100" rx="22" fill="#141210"/><path d="M50.0,16.0 L51.2,16.1 L52.3,16.4 L53.5,16.9 L54.5,17.6 L55.5,18.5 L56.5,19.6 L57.3,20.9 L57.9,22.3 L58.5,23.8 L58.9,25.5 L59.2,27.3 L59.3,29.2 L59.2,31.2 L58.9,33.2 L58.5,35.3 L57.9,37.4 L57.1,39.4 L56.2,41.5 L55.1,43.5 L53.8,45.5 L52.4,47.4 L50.8,49.1 L49.1,50.8 L47.4,52.4 L45.5,53.8 L43.5,55.1 L41.5,56.2 L39.4,57.1 L37.4,57.9 L35.3,58.5 L33.2,58.9 L31.2,59.2 L29.2,59.3 L27.3,59.2 L25.5,58.9 L23.8,58.5 L22.3,57.9 L20.9,57.3 L19.6,56.5 L18.5,55.5 L17.6,54.5 L16.9,53.5 L16.4,52.3 L16.1,51.2 L16.0,50.0 L16.1,48.8 L16.4,47.7 L16.9,46.5 L17.6,45.5 L18.5,44.5 L19.6,43.5 L20.9,42.7 L22.3,42.1 L23.8,41.5 L25.5,41.1 L27.3,40.8 L29.2,40.7 L31.2,40.8 L33.2,41.1 L35.3,41.5 L37.4,42.1 L39.4,42.9 L41.5,43.8 L43.5,44.9 L45.5,46.2 L47.4,47.6 L49.1,49.2 L50.8,50.9 L52.4,52.6 L53.8,54.5 L55.1,56.5 L56.2,58.5 L57.1,60.6 L57.9,62.6 L58.5,64.7 L58.9,66.8 L59.2,68.8 L59.3,70.8 L59.2,72.7 L58.9,74.5 L58.5,76.2 L57.9,77.7 L57.3,79.1 L56.5,80.4 L55.5,81.5 L54.5,82.4 L53.5,83.1 L52.3,83.6 L51.2,83.9 L50.0,84.0 L48.8,83.9 L47.7,83.6 L46.5,83.1 L45.5,82.4 L44.5,81.5 L43.5,80.4 L42.7,79.1 L42.1,77.7 L41.5,76.2 L41.1,74.5 L40.8,72.7 L40.7,70.8 L40.8,68.8 L41.1,66.8 L41.5,64.7 L42.1,62.6 L42.9,60.6 L43.8,58.5 L44.9,56.5 L46.2,54.5 L47.6,52.6 L49.2,50.9 L50.9,49.2 L52.6,47.6 L54.5,46.2 L56.5,44.9 L58.5,43.8 L60.6,42.9 L62.6,42.1 L64.7,41.5 L66.8,41.1 L68.8,40.8 L70.8,40.7 L72.7,40.8 L74.5,41.1 L76.2,41.5 L77.7,42.1 L79.1,42.7 L80.4,43.5 L81.5,44.5 L82.4,45.5 L83.1,46.5 L83.6,47.7 L83.9,48.8 L84.0,50.0 L83.9,51.2 L83.6,52.3 L83.1,53.5 L82.4,54.5 L81.5,55.5 L80.4,56.5 L79.1,57.3 L77.7,57.9 L76.2,58.5 L74.5,58.9 L72.7,59.2 L70.8,59.3 L68.8,59.2 L66.8,58.9 L64.7,58.5 L62.6,57.9 L60.6,57.1 L58.5,56.2 L56.5,55.1 L54.5,53.8 L52.6,52.4 L50.9,50.8 L49.2,49.1 L47.6,47.4 L46.2,45.5 L44.9,43.5 L43.8,41.5 L42.9,39.4 L42.1,37.4 L41.5,35.3 L41.1,33.2 L40.8,31.2 L40.7,29.2 L40.8,27.3 L41.1,25.5 L41.5,23.8 L42.1,22.3 L42.7,20.9 L43.5,19.6 L44.5,18.5 L45.5,17.6 L46.5,16.9 L47.7,16.4 L48.8,16.1 L50.0,16.0 Z" fill="none" stroke="#ece6db" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/><circle cx="50" cy="50" r="3.6" fill="#d4744e"/></svg>'

@app.get("/favicon.svg")
def favicon_svg():
    return FAVICON_SVG, 200, {"Content-Type": "image/svg+xml", "Cache-Control": "public, max-age=86400"}

# Runalyze wordmark for the footer attribution link. The brand icon keeps its green/teal palette;
# the wordmark (.st19) is set to currentColor so it adapts to every theme (dark on Daylight, light on
# Charcoal/Aurora) from a single inlined asset — no per-theme file. viewBox added (source had only a
# fixed width/height) so CSS can scale it.
RUNALYZE_LOGO_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 630 130" role="img" aria-label="Runalyze" xml:space="preserve"><style>.st1{fill:#3e9035}.st2{opacity:.9;fill:#2f9e37}.st3,.st4,.st5{opacity:.9;fill:#6bb54e}.st4,.st5{fill:#6fc4c8}.st5{fill:#479195}.st6,.st7,.st9{opacity:.9;fill:#2cb1ae}.st7,.st9{fill:#328492}.st9{fill:#1b646c}.st11,.st13{opacity:.9;fill:#147671}.st13{fill:#39b8c2}.st16,.st17{opacity:.9;fill:#59b044}.st17{fill:#3e9035}.st18{fill:#6bb54e}.st19{fill:currentColor}</style><path d="M97.2 54.7c-.5 2.5-2.9 4.2-5.4 3.7-2.5-.5-4.1-2.9-3.7-5.4.5-2.5 2.9-4.2 5.4-3.7 2.6.4 4.2 2.8 3.7 5.4z" fill="#59b044"/><path class="st1" d="M109.3 57.3c-.9 4.7-5.5 7.8-10.1 6.8-4.7-.9-7.7-5.5-6.8-10.2s5.5-7.8 10.1-6.8c4.7 1 7.7 5.5 6.8 10.2z"/><path class="st2" d="M117.4 44.7c-.8 3.9-4.5 6.4-8.3 5.6-3.8-.8-6.4-4.5-5.6-8.4s4.5-6.4 8.3-5.6c3.9.8 6.4 4.6 5.6 8.4z"/><circle transform="scale(.99997) rotate(-88.25 86.3 63.64)" class="st3" cx="86.3" cy="63.6" r="5.6"/><path class="st4" d="M296.2 54.7a6.5 6.5 0 0 1-8.4 3.6 6.6 6.6 0 0 1-3.6-8.5 6.5 6.5 0 1 1 12 4.9z"/><path class="st5" d="M244.6 48.1c-.9 4.8-5.5 7.9-10.3 7-4.8-.9-7.9-5.6-6.9-10.4s5.5-7.9 10.3-7c4.7 1 7.8 5.6 6.9 10.4z"/><path class="st6" d="M171 99.8c-.9 4.5-5.3 7.5-9.8 6.6-4.5-.9-7.5-5.3-6.6-9.8.9-4.5 5.3-7.5 9.8-6.6 4.5.9 7.5 5.2 6.6 9.8z"/><circle transform="rotate(-78.92 93.96 14.18)" class="st7" cx="94" cy="14.2" r="3.4"/><path class="st7" d="M106 43a8.97 8.97 0 0 1-17.6-3.4 9 9 0 0 1 10.5-7.1c4.8.9 8 5.7 7.1 10.5zM142.5 94.4a17.03 17.03 0 0 1-19.9 13.5c-9.2-1.8-15.2-10.7-13.4-20s10.7-15.3 19.9-13.5a17 17 0 0 1 13.4 20zM203.7 77c-.8 4.4-5.1 7.2-9.4 6.4-4.3-.9-7.2-5.1-6.3-9.4.8-4.4 5.1-7.2 9.4-6.4 4.3.8 7.2 5 6.3 9.4zM204.3 64.6c-1 2.4-3.7 3.5-6 2.6s-3.5-3.7-2.5-6.1c1-2.4 3.7-3.5 6-2.6 2.4 1 3.5 3.7 2.5 6.1z"/><path d="M153.7 89.3a7.4 7.4 0 0 1-9.6 4.1c-3.8-1.5-5.6-5.9-4.1-9.7s5.9-5.6 9.6-4.1 5.6 5.9 4.1 9.7z" opacity=".9" fill="#38b7be"/><path class="st7" d="M229.9 55.8c-1.5 3.6-5.6 5.4-9.2 3.9s-5.4-5.6-3.9-9.3c1.5-3.6 5.6-5.4 9.2-3.9 3.6 1.5 5.4 5.6 3.9 9.3zM290.6 45.7c-2.4 5.9-9 8.7-14.9 6.3a11.5 11.5 0 0 1-6.3-14.9c2.4-5.9 9-8.7 14.9-6.3s8.6 9.1 6.3 14.9z"/><path class="st9" d="M121.7 75.4c-1.4 7-8.2 11.6-15.2 10.3-7-1.4-11.6-8.2-10.2-15.3s8.2-11.6 15.2-10.3c7 1.5 11.5 8.3 10.2 15.3zM167.3 89.7c-.9 4.5-5.3 7.5-9.8 6.6-4.5-.9-7.5-5.3-6.6-9.8.9-4.5 5.3-7.5 9.8-6.6 4.5.9 7.5 5.3 6.6 9.8zM274.4 32.4c-.9 4.5-5.3 7.5-9.8 6.6-4.5-.9-7.5-5.3-6.6-9.8.9-4.5 5.3-7.5 9.8-6.6 4.5.9 7.5 5.3 6.6 9.8z"/><circle transform="rotate(-78.92 293.54 57.3)" class="st9" cx="293.5" cy="57.3" r="2.8"/><path class="st9" d="M215 64.6a8.06 8.06 0 1 1-4.4-10.5c4.1 1.7 6.1 6.4 4.4 10.5z"/><path class="st5" d="M155.2 100.3a8.97 8.97 0 1 1-7.1-10.5c4.9.9 8 5.6 7.1 10.5zM183.6 89.8a8.97 8.97 0 1 1-17.58-3.42 8.97 8.97 0 0 1 17.58 3.42zM108.9 63.5c-1.3 6.7-7.8 11.1-14.5 9.8-6.7-1.3-11.1-7.8-9.7-14.5C86 52 92.5 47.6 99.2 49c6.6 1.3 11 7.8 9.7 14.5zM250.1 42.1a5.56 5.56 0 0 1-7.2 3.1 5.73 5.73 0 0 1-3.1-7.3c1.2-2.9 4.4-4.2 7.2-3.1 2.9 1.2 4.2 4.5 3.1 7.3z"/><path class="st4" d="M210.6 67.6a5.56 5.56 0 0 1-10.9-2.1 5.56 5.56 0 0 1 10.9 2.1z"/><path d="M224.4 64.3c-.9 4.5-5.3 7.5-9.8 6.6-4.5-.9-7.5-5.3-6.6-9.8.9-4.5 5.3-7.5 9.8-6.6 4.6.9 7.5 5.3 6.6 9.8z" opacity=".9" fill="#44a2a3"/><path class="st4" d="M146.8 78.5c-.5 2.7-3.1 4.4-5.8 3.9-2.7-.5-4.4-3.1-3.9-5.8s3.1-4.4 5.8-3.9c2.7.5 4.5 3.1 3.9 5.8zM103.2 76.6c-.6 3.2-3.7 5.3-6.9 4.6-3.2-.6-5.3-3.7-4.6-6.9.6-3.2 3.7-5.3 6.9-4.6 3.2.6 5.2 3.7 4.6 6.9z"/><circle transform="rotate(-78.92 103.5 47.6)" class="st4" cx="103.5" cy="47.6" r="4"/><path class="st4" d="M237.2 58.6c-.8 3.9-4.5 6.4-8.3 5.6-3.8-.8-6.4-4.5-5.6-8.4.8-3.9 4.5-6.4 8.3-5.6s6.3 4.6 5.6 8.4z"/><path class="st6" d="M217.6 53.1c-1 2.5-3.9 3.8-6.4 2.7-2.5-1-3.7-3.9-2.7-6.5 1-2.5 3.9-3.8 6.4-2.7a5 5 0 0 1 2.7 6.5zM111.1 90a6.2 6.2 0 0 1-7.2 4.9c-3.3-.7-5.5-3.9-4.9-7.3a6.2 6.2 0 0 1 7.2-4.9c3.4.7 5.5 3.9 4.9 7.3zM193.3 81.9c-1.2 6.4-7.4 10.5-13.8 9.3s-10.5-7.4-9.3-13.8c1.2-6.4 7.4-10.5 13.8-9.3 6.4 1.3 10.6 7.4 9.3 13.8zM255.5 41.3c-1.3 3.1-4.8 4.7-8 3.4s-4.6-4.9-3.4-8a6.15 6.15 0 0 1 11.4 4.6z"/><path class="st6" d="M259.8 35.6a7.4 7.4 0 0 1-9.7 4.1c-3.8-1.6-5.6-5.9-4.1-9.7s5.9-5.7 9.7-4.1c3.9 1.5 5.7 5.8 4.1 9.7z"/><path class="st11" d="M100.9 26.9c-.6 3.2-3.7 5.3-6.9 4.6-3.2-.6-5.3-3.7-4.6-6.9s3.7-5.3 6.9-4.6c3.2.6 5.2 3.7 4.6 6.9z"/><circle transform="rotate(-78.92 107 95.15)" class="st11" cx="107" cy="95.2" r="3.4"/><path d="M301.6 68.6c-.7 1.7-2.7 2.6-4.4 1.9s-2.6-2.7-1.9-4.4c.7-1.7 2.7-2.6 4.4-1.9s2.6 2.7 1.9 4.4z" opacity=".9" fill="#32a29a"/><path class="st11" d="M232.1 45.6c-.5 2.6-3.1 4.4-5.7 3.9a4.8 4.8 0 0 1-3.8-5.7c.5-2.6 3.1-4.4 5.7-3.9 2.6.5 4.3 3.1 3.8 5.7zM246 33c-.5 1.3-2 2-3.4 1.4s-2-2-1.4-3.4c.5-1.3 2-2 3.4-1.4 1.3.6 2 2.1 1.4 3.4z"/><circle transform="rotate(-78.92 302.49 74.08)" class="st11" cx="302.5" cy="74.1" r="1.5"/><circle transform="rotate(-78.92 174.75 99.72)" class="st13" cx="174.8" cy="99.7" r="2.2"/><circle transform="rotate(-78.92 213.92 60.07)" cx="213.9" cy="60.1" opacity=".9" fill="#3ba7aa" r="1.9"/><path class="st13" d="M270.8 40.6c-.5 1.2-1.8 1.7-3 1.3s-1.7-1.8-1.3-3c.5-1.2 1.8-1.7 3-1.3 1.2.5 1.8 1.9 1.3 3z"/><path d="M243.1 39.8c-.5 1.3.1 2.8 1.4 3.4 1.3.5 2.8-.1 3.4-1.4.5-1.3-.1-2.8-1.4-3.4-1.3-.6-2.8.1-3.4 1.4z" opacity=".9" fill="#64c2d0"/><circle transform="rotate(-78.92 126 70.93)" class="st13" cx="126" cy="70.9" r="2.2"/><circle transform="rotate(-78.92 109.6 59.83)" class="st13" cx="109.6" cy="59.8" r="3.4"/><circle transform="rotate(-78.92 93.45 4.96)" class="st13" cx="93.5" cy="5" r="2"/><circle transform="rotate(-78.92 91.58 36.11)" class="st13" cx="91.6" cy="36.1" r="3.7"/><path class="st9" d="M197.6 69.5a6.83 6.83 0 1 1-5.4-8c3.8.7 6.2 4.3 5.4 8z"/><path class="st16" d="M124 52.6a6.2 6.2 0 0 1-7.2 4.9c-3.3-.7-5.5-3.9-4.9-7.3a6.2 6.2 0 0 1 7.2-4.9c3.4.7 5.6 4 4.9 7.3zM57.5 92.6c-.7 3.4-3.9 5.5-7.2 4.9s-5.5-3.9-4.9-7.3a6.2 6.2 0 0 1 7.2-4.9c3.4.7 5.5 4 4.9 7.3zM205.3 42.4a17.52 17.52 0 0 1-20.6 13.9c-9.5-1.9-15.8-11.1-13.9-20.7s11.1-15.8 20.6-13.9 15.7 11.1 13.9 20.7z"/><path class="st17" d="M223 54c-1.5 7.5-8.7 12.4-16.2 10.9s-12.3-8.7-10.9-16.2 8.7-12.4 16.2-10.9S224.5 46.5 223 54zM156.3 38.8c-1.6 8.1-9.3 13.3-17.4 11.7a14.81 14.81 0 1 1 5.7-29.1c8 1.5 13.3 9.3 11.7 17.4z"/><path class="st17" d="M136.2 46.7c-.8 3.9-4.5 6.4-8.3 5.6s-6.4-4.5-5.6-8.4c.8-3.9 4.5-6.4 8.3-5.6s6.4 4.5 5.6 8.4z"/><circle transform="rotate(-78.92 157.44 43.63)" class="st17" cx="157.4" cy="43.6" r="3.1"/><circle transform="rotate(-78.92 82.01 71.55)" class="st18" cx="82" cy="71.5" r="2.8"/><path class="st3" d="M133 38.9a8.97 8.97 0 1 1-7.1-10.5c4.9 1 8 5.7 7.1 10.5zM207.1 58.8c-.8 4.4-5.1 7.2-9.4 6.4-4.3-.9-7.2-5.1-6.3-9.4.8-4.4 5.1-7.2 9.4-6.4 4.3.8 7.1 5.1 6.3 9.4z"/><circle transform="rotate(-78.92 77.78 67.02)" class="st18" cx="77.8" cy="67" r="3.1"/><circle transform="scale(.99997) rotate(-88.25 303.1 78.2)" class="st2" cx="303.1" cy="78.2" r="1.6"/><path class="st2" d="M178.5 34.3c-1.4 7.4-8.6 12.2-15.9 10.8-7.4-1.4-12.2-8.6-10.7-16 1.4-7.4 8.6-12.2 15.9-10.8s12.2 8.7 10.7 16z"/><circle transform="rotate(-78.92 54.78 83.88)" class="st16" cx="54.8" cy="83.9" r="2.8"/><circle transform="rotate(-78.92 15.95 126.75)" class="st16" cx="16" cy="126.8" r="1.9"/><path class="st17" d="M79.9 76.2A5.56 5.56 0 0 1 69 74.1c.6-3 3.5-5 6.5-4.4 3 .5 4.9 3.5 4.4 6.5zM46.9 100a4.34 4.34 0 1 1-8.5-1.7 4.34 4.34 0 0 1 8.5 1.7z"/><circle transform="rotate(-78.92 214.86 38.25)" class="st3" cx="214.9" cy="38.3" r="3.7"/><circle transform="rotate(-78.92 33.41 108.09)" class="st3" cx="33.4" cy="108.1" r="3.7"/><circle transform="rotate(-78.92 64.04 84.75)" class="st2" cx="64" cy="84.7" r="3.4"/><circle transform="rotate(-78.92 24.38 117.36)" class="st2" cx="24.4" cy="117.4" r="2.8"/><circle transform="scale(.99997) rotate(-88.25 223.04 65.73)" class="st16" cx="223" cy="65.7" r="11.1"/><circle transform="scale(.99997) rotate(-88.25 216.19 75.4)" class="st3" cx="216.2" cy="75.4" r="5.6"/><path class="st3" d="M272.4 95.6a8.97 8.97 0 1 1-7.1-10.5c4.9 1 8.1 5.7 7.1 10.5z"/><circle transform="scale(.99997) rotate(-88.25 298.41 81.93)" class="st16" cx="298.4" cy="81.9" r="3.4"/><circle transform="rotate(-78.92 253 100.53)" class="st1" cx="253" cy="100.5" r="2.8"/><circle transform="scale(.99997) rotate(-88.25 249.5 86.63)" class="st17" cx="249.5" cy="86.6" r="10.5"/><circle transform="scale(.99997) rotate(-88.25 277.36 94)" class="st17" cx="277.4" cy="94" r="6.2"/><circle transform="scale(.99997) rotate(-88.25 259.4 85.7)" class="st3" cx="259.4" cy="85.7" r="3.4"/><circle transform="scale(.99997) rotate(-88.25 292.44 87)" class="st3" cx="292.4" cy="87" r="3.2"/><circle transform="scale(.99997) rotate(-88.25 285.8 90.84)" class="st3" cx="285.8" cy="90.8" r="4"/><circle transform="matrix(.03056 -.9995 .9995 .03056 142.8 339.5)" class="st3" cx="246.4" cy="96.1" r="5.6"/><circle transform="scale(.99997) rotate(-88.25 271.36 88.23)" class="st2" cx="271.4" cy="88.2" r="6.2"/><ellipse transform="matrix(.03056 -.9995 .9995 .03056 152.7 310.7)" class="st2" cx="236.5" cy="76.6" rx="12.4" ry="12.3"/><path class="st19" d="M328.1 75.7 325.9 94H315l6-48.9h14.8c3 0 5.5.3 7.6 1 2.1.6 3.8 1.5 5.2 2.6s2.3 2.4 2.9 4c.6 1.5.9 3.2.9 5a16.25 16.25 0 0 1-2.8 9.5c-.9 1.4-2 2.6-3.4 3.6-1.3 1-2.8 1.9-4.5 2.6.7.4 1.3.8 1.9 1.4.6.5 1.1 1.2 1.4 2l7.1 17.2h-9.8c-.9 0-1.7-.2-2.3-.5-.6-.4-1.1-.9-1.3-1.5l-5.3-14.5c-.2-.6-.6-1.1-1-1.3-.4-.3-1-.4-1.8-.4h-2.5zm2.7-22.5L329 68.1h4c1.6 0 2.9-.2 4-.7 1.1-.5 2-1.1 2.7-1.9.7-.8 1.2-1.7 1.5-2.8.3-1.1.5-2.2.5-3.4 0-.9-.1-1.8-.4-2.5-.3-.7-.7-1.4-1.3-1.9-.6-.5-1.3-.9-2.1-1.2-.9-.3-1.9-.4-3-.4h-4.1zM374 85.3c1.4 0 2.6-.3 3.7-.8s2.1-1.3 2.9-2.2c.8-1 1.5-2.2 2.1-3.5.6-1.4.9-2.9 1.1-4.7l3.5-29.1h10.9l-3.5 29.1c-.4 3-1.2 5.7-2.4 8.2a20.65 20.65 0 0 1-11.2 10.6c-2.5 1-5.3 1.5-8.3 1.5-2.7 0-5.1-.4-7.3-1.2-2.1-.8-3.9-2-5.4-3.5a13.6 13.6 0 0 1-3.3-5.4c-.8-2.1-1.1-4.4-1.1-6.9 0-1.1.1-2.1.2-3.3l3.5-29.1h10.9l-3.5 29.1c0 .4-.1.8-.1 1.2v1.2c0 2.8.6 4.9 1.9 6.5 1.2 1.5 3 2.3 5.4 2.3zM411.6 45.1c.3 0 .6.1.9.2.3.1.5.3.7.5.2.2.4.5.6.9l17.1 30.2c0-.8.1-1.5.2-2.2.1-.7.2-1.4.2-2l3.3-27.6h9.6l-6 48.9h-5.7c-.8 0-1.5-.1-2.1-.4-.6-.2-1-.7-1.4-1.4L412 62c0 .6-.1 1.2-.2 1.8-.1.6-.1 1.1-.2 1.6l-3.3 28.5h-9.6l6-48.9h5.8c.4 0 .8.1 1.1.1zM486.8 93.9h-8.4c-.9 0-1.7-.2-2.3-.7-.6-.4-.9-1-1-1.7l-1.5-8.8H457l-3.6 8.8c-.2.6-.7 1.2-1.4 1.7s-1.5.7-2.4.7H441L464.3 45h11.2l11.3 48.9zm-26.7-18.6h12.2l-2.4-14c-.2-1.2-.4-2.3-.7-3.5-.2-1.2-.4-2.3-.6-3.3-.2.5-.4 1.1-.7 1.8-.3.7-.6 1.3-.9 2-.3.6-.5 1.3-.8 1.8s-.4 1-.6 1.2l-5.5 14zM501.5 85.2h16.4l-1.1 8.7h-27.3l6-48.9h10.9l-4.9 40.2zM538.7 75.2l-2.3 18.7h-10.9l2.3-18.6L515.3 45h9.7c.9 0 1.7.2 2.2.7.5.4.9 1 1.2 1.7l4.7 14.8.9 2.9c.3.9.5 1.8.7 2.7.4-.9.9-1.7 1.4-2.6.5-.9 1.1-1.9 1.6-2.9l8.3-14.8c.3-.6.8-1.2 1.5-1.6.6-.5 1.4-.7 2.3-.7h9l-20.1 30zM594.5 45l-.5 3.3c-.1.4-.2.9-.4 1.3s-.5.8-.7 1.2l-24.6 34.7h20.6l-1.1 8.4H553l.4-3.2c.1-.4.2-.9.4-1.3s.5-.8.7-1.2l24.6-34.8h-19.5l1.1-8.4h33.8zM626.9 53.5h-18l-1.5 11.8h13.8l-1.1 8.1h-13.7l-1.5 12.1H623l-1.1 8.4h-29.1l6-48.9h29l-.9 8.5z"/></svg>'



@app.teardown_appcontext
def _close_db(exc):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


def _private_only_path(p):
    """Paths the public read-only container must never serve — location/medical/personal privacy.
    Centralised so the map-privacy self-test can assert this invariant can't silently regress:
    `/api/health` (blood markers), the workout `/map` (route geo reveals where the owner lives),
    `/api/effort-discipline` (§6m — per-run HR + a personal effort critique, more granular than the
    public 'training shape + plan only' posture), and `/api/settings` (athlete context is personal,
    and the panel is an owner-only control surface)."""
    return (p in ("/api/health", "/api/effort-discipline", "/api/settings", "/api/geocode")
            or (p.startswith("/api/activity/") and p.endswith("/map")))


@app.before_request
def _readonly_guard():
    """Public read-only mode: reject every mutation and withhold the medical + route-map endpoints,
    no matter what the UI does. Belt to the read-only DB mount + tokenless container's braces."""
    if not READONLY:
        return
    p = request.path
    if p == "/selftest" or p.startswith("/api/selftest"):   # diagnostics are private-only
        return jsonify(ok=False, error="diagnostics are private"), 403
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        return jsonify(ok=False, error="read-only public view"), 403
    if _private_only_path(p):   # blood markers + workout route geo stay fully private
        return jsonify(ok=False, error="not available on the public view"), 403
    # /api/readiness GET is allowed but redacted to a public-safe verdict in api_readiness();
    # its POST (a write check-in) is already rejected above by the mutating-method guard.


@app.before_request
def _csrf_origin_guard():
    """CSRF defence: refuse a state-changing request whose Origin is a different host. A browser
    always sends Origin on a cross-site POST and JS can't forge it, so this blocks cross-site
    forgery independently of the Cloudflare Access cookie's SameSite. Same-origin SPA calls match
    request.host; a missing Origin (curl, server-to-server) is allowed — a cross-site browser POST
    can't omit it. Covers the no-body POSTs that body()'s content-type check can't."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("Origin")
    if origin and urlparse(origin).hostname != request.host.split(":")[0]:
        return jsonify(ok=False, error="cross-origin request refused"), 403


def html_page(html):
    """Serve an HTML document with a per-request CSP nonce stamped onto every inline <script>.
    The nonce is handed to `_security_headers` (via g) so the Content-Security-Policy can lock
    script execution to these tags + the few trusted hosts — injected markup can't run."""
    nonce = secrets.token_urlsafe(16)
    g.csp_nonce = nonce
    return html.replace("<script>", f'<script nonce="{nonce}">')


@app.after_request
def _security_headers(resp):
    """Defence-in-depth headers on every response. CSP is the blanket XSS mitigation (it backstops
    the per-sink escaping); the rest block sniffing/clickjacking/referrer-leak. The CSP is only set
    on HTML pages (which carry a nonce) — JSON/asset responses don't need a script policy."""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    nonce = getattr(g, "csp_nonce", None)
    if nonce:
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            f"script-src 'nonce-{nonce}' https://unpkg.com; "  # inline SPA (nonce) + Leaflet (unpkg)
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://unpkg.com; "
            "font-src https://fonts.gstatic.com; "
            "img-src 'self' data: https://*.tile.openstreetmap.org https://unpkg.com; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; object-src 'none'"
        )
    return resp


def body():
    """Parsed JSON request body. Requires Content-Type: application/json (no force) so a cross-site
    HTML form — which can't set that header without tripping a CORS preflight — can't forge a write.
    Tolerant of a missing/blank/malformed payload otherwise: always a dict."""
    return request.get_json(silent=True) or {}


def replan(db, mutate):
    """Re-periodize around a write (§6b): snapshot the plan, apply `mutate`, commit, return the diff.
    Centralises the invariant that every objective/adjustment change re-anchors the road ahead."""
    base = plan_baseline(db)
    mutate()
    db.commit()
    return jsonify(regenerate(db, baseline=base))


@app.get("/healthz")
def healthz():
    return jsonify(ok=True, token_configured=bool(RUNALYZE_TOKEN), db=DB_PATH.exists(),
                   llm=llm_available(), readonly=READONLY)


@app.post("/api/sync")
def api_sync():
    backfill = request.args.get("backfill") in ("1", "true", "yes")
    auto = request.args.get("auto") in ("1", "true", "yes")
    # Opportunistic page-load sync: skip if we synced very recently, so reloads / multiple tabs
    # don't hammer Runalyze. The nightly job and the manual "Sync now" button stay unthrottled.
    if auto and not backfill:
        last = get_meta(get_db(), "last_sync")
        if last and _seconds_since(last) < AUTO_SYNC_THROTTLE:
            return jsonify(ok=True, skipped=True, last_sync=last)
    try:
        return jsonify(run_sync(backfill=backfill))
    except RunalyzeError as e:
        return jsonify(ok=False, error=str(e)), 502


@app.get("/api/shape")
def api_shape():
    db = get_db()
    latest = latest_snapshot(db)
    history = db.execute(
        "SELECT snapshot_date, effective_vo2max, fitness, fatigue, performance, acwr "
        "FROM shape_snapshots ORDER BY snapshot_date ASC"
    ).fetchall()
    dups = find_duplicates(db)
    # the dup ROWS (id + date), not just the count — so the banner can offer a direct 🗑 delete on
    # each leftover row (an OLD dup isn't reachable via the latest-activity tile otherwise).
    dup_rows = []
    if dups:
        qs = ",".join("?" * len(dups))
        dup_rows = [dict(r) for r in db.execute(
            f"SELECT id, date, distance FROM activities WHERE id IN ({qs}) ORDER BY date DESC",
            dups).fetchall()]
    ignored = db.execute(
        "SELECT i.id, a.date, a.distance, i.reason FROM ignored_activities i "
        "LEFT JOIN activities a ON a.id = i.id ORDER BY a.date DESC").fetchall()
    return jsonify(
        latest=dict(latest) if latest else None,
        history=[dict(r) for r in history],
        last_sync=get_meta(db, "last_sync"),
        duplicate_count=len(dups),
        duplicates=dup_rows,
        ignored=[dict(r) for r in ignored],
    )


@app.get("/api/hr-zones/derive")
def api_hr_zones_derive():
    """Private diagnostic: reconstruct the HR zone cutoffs (%HRmax) from Runalyze's per-activity
    zone distribution. Read-only, derives nothing into the DB — for eyeballing before the chart
    colours by them. Needs the MCP token, so it's private-only."""
    if READONLY:
        return jsonify(ok=False, error="diagnostics are private"), 403
    return jsonify(derive_hr_zones(get_db()))


@app.get("/api/effort-discipline")
def api_effort_discipline():
    """§6m — per-run effort vs prescription over the recent window (HR-led, Runalyze-native). Public-
    safe (effort/HR/pace, no medical); the readonly guard allows this GET. `?days=N` (default 21)."""
    days = int(request.args.get("days", str(EFFORT_WINDOW_DAYS)))
    return jsonify(effort_discipline(get_db(), window_days=days))


@app.get("/api/projector")
def api_projector():
    """The reconstructed fitness/fatigue curve + a validation of the model against
    Runalyze's reported values. `?days=N` trims the returned history (default 180)."""
    db = get_db()
    days = int(request.args.get("days", "180"))
    hist = reconstruct_history(db)
    modeled, snap = current_model(db)
    valid = None
    if modeled and snap:
        valid = {
            "modeled": {"ctl": modeled["ctl"], "atl": modeled["atl"], "tsb": modeled["tsb"]},
            "runalyze": {"ctl": snap["fitness"], "atl": snap["fatigue"], "tsb": snap["performance"]},
            "ctl_err": round(modeled["ctl"] - (snap["fitness"] or 0), 2),
            "atl_err": round(modeled["atl"] - (snap["fatigue"] or 0), 2),
            "tau_ctl": TAU_CTL, "tau_atl": TAU_ATL,
        }
    return jsonify(history=hist[-days:], validation=valid,
                   duplicate_count=len(find_duplicates(db)))


# ── Plan drift (§6b made visible — the initial road vs the road as it stands) ─
# The thesis says the plan MOVES, visibly, in both directions; the existing diff only shows the
# last step. These helpers reconstruct the cumulative shape of a saved plan so the *original* road
# can be drawn against where the plan stands now — slow-moving, weekly cadence.

def _plan_weeks(plan):
    """Every training week of a saved plan (across rebase + all phase blocks), sorted by start.
    Each carries {start, km, trimp_total, sessions}."""
    weeks = []
    for v in plan.values():
        if isinstance(v, dict) and isinstance(v.get("weeks"), list):
            weeks.extend(v["weeks"])
    return sorted((w for w in weeks if w.get("start")), key=lambda w: w["start"])


def _plan_daily_trimps(plan, since=None):
    """{date: TRIMP} from every planned session (optionally only on/after `since`) — the load
    schedule to roll the projector over, so a plan's CTL trajectory uses the SAME math as the
    fitness/fatigue chart rather than a parallel guess."""
    out = {}
    for w in _plan_weeks(plan):
        for s in w.get("sessions", []):
            d = s.get("date")
            if not d or (since and d < since):
                continue
            out[d] = out.get(d, 0.0) + (s.get("trimp") or 0.0)
    return out


def _monday(d):
    from datetime import timedelta
    return d - timedelta(days=d.weekday())


def _weekly_ctl(curve, since=None, upto=None):
    """Reduce a daily projector curve to ONE CTL point per ISO week (the week's settled, end-of-week
    value) — the slow-moving cadence the drift view wants. `since`/`upto` clip the window."""
    byweek = {}
    for p in curve:
        d = _date(p["date"])
        if (since and d < since) or (upto and d > upto):
            continue
        byweek[_monday(d)] = p["ctl"]   # later days in the week overwrite → end-of-week value
    return [{"date": m.isoformat(), "ctl": v} for m, v in sorted(byweek.items())]


def _actual_weekly_km():
    """{Monday(date): running km} for every ISO week we own — actuals for the cumulative line."""
    out = {}
    for r in db_weekly_running():
        y, w = (int(x) for x in r["week"].split("-W"))
        out[isoweek_monday(y, w)] = r["km"]
    return out


def _actual_weekly_trimp(db):
    """{Monday(date): summed TRIMP} per ISO week — the de-duplicated, whole-body load actually done
    (same series that drives CTL), the effort companion to actual km."""
    out = {}
    for d, t in daily_trimp_series(db).items():
        out[_monday(_date(d))] = out.get(_monday(_date(d)), 0.0) + t
    return out


def isoweek_monday(year, wk):
    from datetime import date, timedelta
    jan4 = date(year, 1, 4)
    return jan4 - timedelta(days=jan4.weekday()) + timedelta(weeks=wk - 1)


@app.get("/api/plandrift")
def api_plandrift():
    """The plan's drift from its founding statement (§6b, visible). Three slow-moving, weekly
    series comparing the FIRST saved plan (the original road) with where the plan stands now:
      • distance — cumulative planned km of the initial road vs actuals-to-date + the current
        plan's projection forward (so the gap reads as 'ahead of / behind your original road');
      • ctl     — the initial plan's projected fitness vs the de-duplicated actual curve continued
        by the current plan's forward projection;
      • outcome — projected race-day CTL as recorded by each plan version over time: is the goal
        getting more or less reachable?
    Actuals/projection seed from the de-duplicated model (like /api/projector), so a duplicate
    upload can't pollute them; the outcome series carries `duplicate_count` for the same caveat."""
    from datetime import timedelta
    db = get_db()
    rows = db.execute("SELECT id, created_at, for_date, plan FROM plans ORDER BY id").fetchall()
    if not rows:
        return jsonify(ok=False, error="no plan history yet — generate a plan first")
    current = json.loads(rows[-1]["plan"])
    cw = _plan_weeks(current)
    # Anchor = the EARLIEST plan BUILT FOR THE CURRENT GOAL that spans the full runway. Matching the
    # goal (objective.date) — not just runway span — keeps the founding road honest when the runner
    # swaps or drops the objective: a plan built for a different race can't be the road we measure
    # this race against. So a goal change resets the baseline (anchor falls back to `current` →
    # "just sealed, no drift yet") and self-heals as plans for the new goal accrue. (Older versions
    # persisted only the active block's weeks, so they can't anchor a cumulative road; the runway
    # span filters those out.) Race date bounds "full"; fall back to the current plan.
    obj = current.get("objective") or {}
    race_date = _date(obj["date"]) if obj.get("date") else None
    cur_goal = obj.get("date")                       # tie the founding road to THIS goal (None = no race)
    anchor_row, anchor = rows[-1], current
    for r in rows:
        p = json.loads(r["plan"])
        if ((p.get("objective") or {}).get("date")) != cur_goal:
            continue                                 # a plan for a different/no goal isn't this road
        w = _plan_weeks(p)
        if w and (race_date is None or _date(w[-1]["start"]) >= race_date - timedelta(days=21)):
            anchor_row, anchor = r, p
            break
    aw = _plan_weeks(anchor)
    if not aw:
        return jsonify(ok=False, error="no saved plan spans the runway to anchor against")
    today = datetime.now().date()
    today_mon = _monday(today)
    anchor_mon = _monday(_date(aw[0]["start"]))

    # — distance: initial road (cumulative planned km) —
    cum = 0.0
    init_dist = []
    for w in aw:
        cum += w.get("km") or 0.0
        init_dist.append({"date": w["start"], "cum": round(cum, 1)})

    # — distance: actuals-to-date, then the current plan's projection forward (running total) —
    actual_km = _actual_weekly_km()
    cum = 0.0
    cur_dist = []
    m = anchor_mon
    while m <= today_mon:
        cum += actual_km.get(m, 0.0)
        cur_dist.append({"date": m.isoformat(), "cum": round(cum, 1), "kind": "actual"})
        m += timedelta(days=7)
    for w in cw:
        if _date(w["start"]) > today_mon:
            cum += w.get("km") or 0.0
            cur_dist.append({"date": w["start"], "cum": round(cum, 1), "kind": "proj"})

    # — ctl: initial projection vs de-dup actual continued by the current plan's projection —
    ad = _plan_daily_trimps(anchor)
    ash = anchor.get("shape") or {}
    init_ctl = _weekly_ctl(
        roll(ad, _date(aw[0]["start"]), max(_date(d) for d in ad),
             ctl0=ash.get("ctl") or 0.0, atl0=ash.get("atl") or 0.0)
    ) if ad else []
    actual_ctl = _weekly_ctl(reconstruct_history(db), since=anchor_mon, upto=today)
    modeled, _snap = current_model(db)
    fwd = _plan_daily_trimps(current, since=(today_mon + timedelta(days=7)).isoformat())
    cur_ctl = []
    if modeled and fwd:
        cur_ctl = _weekly_ctl(project_forward(fwd, modeled["ctl"], modeled["atl"],
                                              (today_mon + timedelta(days=7)).isoformat()))
        if actual_ctl:                                  # stitch to today's actual so the lines meet
            cur_ctl = [actual_ctl[-1]] + cur_ctl

    # — effort: per-week training LOAD (TRIMP), the intensity dimension distance can't show. Initial
    #   plan's weekly load vs the de-dup actual load continued by the current plan's prescription —
    init_eff = [{"date": w["start"], "trimp": round(w.get("trimp_total") or 0.0, 1)} for w in aw]
    act_load = _actual_weekly_trimp(db)
    actual_eff, m = [], anchor_mon
    while m <= today_mon:                                 # include zero weeks (a missed week IS effort drift)
        actual_eff.append({"date": m.isoformat(), "trimp": round(act_load.get(m, 0.0), 1)})
        m += timedelta(days=7)
    cur_eff = [{"date": w["start"], "trimp": round(w.get("trimp_total") or 0.0, 1)}
               for w in cw if _date(w["start"]) > today_mon]
    if actual_eff:                                       # stitch so the prescription line meets actuals
        cur_eff = [actual_eff[-1]] + cur_eff

    # — outcome: projected race-day CTL recorded by each version, one per ISO week (last wins) —
    byweek = {}
    for r in rows:
        p = json.loads(r["plan"])
        pc = (p.get("feasibility") or {}).get("projected_ctl")
        if pc is None:
            continue
        fd = _date(r["for_date"])
        byweek[_monday(fd)] = {"date": _monday(fd).isoformat(), "ctl": pc,
                               "verdict": (p.get("feasibility") or {}).get("verdict")}
    outcome = [byweek[k] for k in sorted(byweek)]

    # — scorecard: synthesize the four series into one 'who's winning' verdict (§6b, settle the
    #   score). Deterministic numbers + templated language — the engine owns the score, no LLM
    #   drifting it. Three axes measured AT TODAY, all against the SAME founding road (the anchor):
    #   volume (cumulative km), fitness (CTL), and the race-day projection (anchor vs current plan).
    #   `open` is false on a just-sealed baseline (no drift yet); the race clause is suppressed when
    #   a duplicate upload is inflating the snapshot the current plan seeds from (§6i caveat). —
    dup_count = len(find_duplicates(db))
    is_current = anchor_row["id"] == rows[-1]["id"]

    def _at_today(series, key):
        v = None                                          # last weekly value with date <= today
        for p in series:
            if _date(p["date"]) <= today_mon:
                v = p[key]
            else:
                break
        return v

    def _gap(now, found):
        return None if now is None or found is None else round(now - found, 1)

    def _state(gap, band):
        if gap is None:
            return "unknown"
        return "ahead" if gap > band else "behind" if gap < -band else "level"

    cur_actual = [p for p in cur_dist if p.get("kind") == "actual"]
    vol_found, vol_now = _at_today(init_dist, "cum"), (cur_actual[-1]["cum"] if cur_actual else None)
    fit_found, fit_now = _at_today(init_ctl, "ctl"), (actual_ctl[-1]["ctl"] if actual_ctl else None)
    race_found = (anchor.get("feasibility") or {}).get("projected_ctl")    # same founding road
    race_now = (current.get("feasibility") or {}).get("projected_ctl")

    # CTL has a t0 seam the cumulative-km road doesn't: the plan's curve seeds from Runalyze's
    # snapshot (`shape.ctl`) while the actual curve is locally reconstructed — they start a few
    # points apart by construction, not by drift. Measure fitness as DIVERGENCE SINCE the shared
    # baseline (subtract that t0 offset), so a just-sealed baseline reads ~level, not fake-behind.
    fit_seam = (actual_ctl[0]["ctl"] - init_ctl[0]["ctl"]) if (actual_ctl and init_ctl) else 0.0
    vol_gap, race_gap = _gap(vol_now, vol_found), _gap(race_now, race_found)
    fit_gap = None if (fit_now is None or fit_found is None) else round((fit_now - fit_found) - fit_seam, 1)
    vol_state, fit_state = _state(vol_gap, 5.0), _state(fit_gap, 2.0)      # ±5 km, ±2 CTL: decisive
    race_trend = ("unknown" if dup_count or race_gap is None else
                  "gaining" if race_gap > 0.5 else "slipping" if race_gap < -0.5 else "steady")

    settled = race_date is not None and today >= race_date
    PHRASE = {                                            # completes "The rebuild is ___." — the two
        ("ahead", "ahead"):  "ahead of the founding road on both fitness and volume",   # thesis halves
        ("ahead", "behind"): "outrunning the founding road on fitness, trailing on volume",
        ("ahead", "level"):  "ahead on fitness, holding the planned volume",
        ("behind", "ahead"): "carrying the volume but behind on fitness",
        ("behind", "behind"):"behind the founding road on both fitness and volume",
        ("behind", "level"): "holding the planned volume but behind on fitness",
        ("level", "ahead"):  "tracking the founding road on fitness, running ahead on volume",
        ("level", "behind"): "tracking the founding road on fitness, behind on volume",
        ("level", "level"):  "tracking the founding road on both fitness and volume",
    }
    # The "settle the score" wager voice is a private in-joke (owner's bet); the public site gets
    # neutral copy. READONLY = the public read-only container.
    if is_current:
        headline = ("Baseline just sealed — the score isn't open yet; week one is the only live signal."
                    if not READONLY else
                    "Baseline just sealed — too early to call; week one is the only live signal.")
    elif fit_state == "unknown" or vol_state == "unknown":
        headline = ("Not enough reconstructed history yet to call the score." if not READONLY else
                    "Not enough reconstructed history yet to call it.")
    else:
        race_name = obj.get("label") or "Race-day"
        tail = "" if race_trend in ("unknown", "steady") else f" {race_name} projection {race_trend}."
        verdict = "Settled." if settled else ("Score open." if not READONLY else "Too early to call.")
        headline = f"The rebuild is {PHRASE[(fit_state, vol_state)]}.{tail} " + verdict

    scorecard = {
        "open": not is_current,
        "settled": settled,
        "weeks_to_go": obj.get("weeks_away"),
        "volume":  {"founding": vol_found, "now": vol_now, "gap": vol_gap, "state": vol_state},
        "fitness": {"founding": fit_found, "now": fit_now, "gap": fit_gap, "state": fit_state},
        "race":    {"founding": race_found, "now": race_now, "gap": race_gap, "trend": race_trend,
                    "caveat": bool(dup_count), "verdict": (current.get("feasibility") or {}).get("verdict")},
        "headline": headline,
    }

    return jsonify(
        ok=True,
        today=today.isoformat(),
        anchor={"for_date": anchor_row["for_date"], "created_at": anchor_row["created_at"],
                "versions": len(rows), "is_current": is_current},
        race={"label": obj.get("label"), "date": obj.get("date"),
              "weeks_away": obj.get("weeks_away")},
        distance={"initial": init_dist, "current": cur_dist},
        ctl={"initial": init_ctl, "actual": actual_ctl, "current": cur_ctl},
        effort={"initial": init_eff, "actual": actual_eff, "current": cur_eff},
        outcome=outcome,
        scorecard=scorecard,
        duplicate_count=dup_count,
    )


@app.get("/api/objectives")
def api_objectives():
    db = get_db()
    seed_objectives(db)
    rows = db.execute("SELECT * FROM objectives ORDER BY date").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/objectives")
def api_objectives_add():
    """Add an objective → re-periodize the road ahead and return the change (§6b)."""
    d = body()
    if not d.get("date"):
        return jsonify(ok=False, error="need a date"), 400
    db = get_db()
    return replan(db, lambda: db.execute(
        "INSERT INTO objectives (type,label,date,target,priority,status,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (d.get("type", "custom"), d.get("label", "Race"), d["date"],
         d.get("target", "finish"), d.get("priority", "A"), "upcoming", _now_iso()),
    ))


@app.post("/api/objectives/parse")
def api_objectives_parse():
    """§6c — parse a natural-language goal into structured fields for the owner to review.
    Advisory only: returns the proposal; the owner confirms via the normal add path."""
    d = body()
    text = (d.get("text") or "").strip()
    if not text:
        return jsonify(ok=False, error="say what the goal is"), 400
    out = parse_objective_nl(text)
    return jsonify(out), (200 if out.get("ok") else 502)


@app.post("/api/objectives/adjudicate")
def api_objectives_adjudicate():
    """§6c — advise which competing A-race should be the peak (advisory; not applied)."""
    out = adjudicate_objectives(get_db())
    return jsonify(out), (200 if out.get("ok") or out.get("error") == "no A-race conflict to adjudicate" else 502)


@app.post("/api/objectives/<int:oid>/priority")
def api_objectives_priority(oid):
    """Apply a priority (A/B/C) to an objective and re-periodize — the deterministic follow-through
    to the LLM's adjudication advice."""
    d = body()
    if d.get("priority") not in ("A", "B", "C"):
        return jsonify(ok=False, error="priority must be A, B or C"), 400
    db = get_db()
    return replan(db, lambda: db.execute(
        "UPDATE objectives SET priority=? WHERE id=?", (d["priority"], oid)))


@app.post("/api/objectives/<int:oid>/remove")
def api_objectives_remove(oid):
    """Explicit removal (§6b) — drop the race and re-anchor the plan to what remains
    (or fall back to a maintenance block), returning the change."""
    db = get_db()
    return replan(db, lambda: db.execute(
        "UPDATE objectives SET status='removed' WHERE id=?", (oid,)))


@app.post("/api/adjustment/propose")
def api_adjustment_propose():
    """§6c — read free text and classify it (see propose_adjustment): a reflection comes back
    kind='log' with a reply (the front-end journals it, no plan change); a real signal comes
    back kind='adjust' with an engine-clamped directive to confirm. Advisory; not saved."""
    d = body()
    text = (d.get("text") or "").strip()
    if not text:
        return jsonify(ok=False, error="tell me how it's going"), 400
    out = propose_adjustment(text, easy_pace=latest_easy_pace(get_db()))
    return jsonify(out), (200 if out.get("ok") else 502)


@app.post("/api/adjustment/apply")
def api_adjustment_apply():
    """Confirm a proposal: re-clamp server-side, save as the active adjustment, regenerate.
    Guards against a no-op (multiplier ≥ 1, no easy-only, no medical) ever being stored as an
    'active adjustment' — a reflection isn't a load change; it belongs in the session log."""
    d = body()
    directive = d.get("directive") or {}
    note = (d.get("note") or "").strip()
    if not directive:
        return jsonify(ok=False, error="nothing to apply"), 400
    today = datetime.now().date().isoformat()
    directive, clamp = clamp_adjustment(directive, today)   # never trust the client's numbers
    if is_noop_adjustment(directive):
        return jsonify(ok=False, kind="log", error="nothing to adjust — that's a reflection, "
                       "log it against today's run instead"), 400
    directive["clamp"] = clamp
    db = get_db()

    def mutate():
        db.execute("UPDATE adjustments SET active=0 WHERE active=1")  # one active at a time
        db.execute(
            "INSERT INTO adjustments (created_at, note, directive, applies_from, applies_until, active) "
            "VALUES (?,?,?,?,?,1)",
            (_now_iso(), note, json.dumps(directive),
             directive["applies_from"], directive["applies_until"]),
        )
    return replan(db, mutate)


@app.post("/api/adjustment/clear")
def api_adjustment_clear():
    """Drop the active adjustment and re-plan back to the unadjusted road."""
    db = get_db()
    return replan(db, lambda: db.execute("UPDATE adjustments SET active=0 WHERE active=1"))


@app.get("/api/log")
def api_log():
    """The training log for the live block — planned sessions with done/actual/reflection.
    Done + actual-vs-planned are training-side (public-safe); the free-text reflections are
    withheld on the public view, like the readiness note."""
    log = block_log(get_db())
    if log and READONLY:
        for w in log["weeks"]:
            for s in w["sessions"]:
                s.pop("reflection", None)
    return jsonify(log)


@app.post("/api/log/note")
def api_log_note():
    """Journal a reflection against a day (defaults to today). This is where 'how it felt'
    lands — it never touches the plan's forward load (that's /api/adjustment)."""
    d = body()
    note = (d.get("note") or "").strip()
    date = (d.get("date") or datetime.now().strftime("%Y-%m-%d")).strip()
    db = get_db()
    if not note:
        db.execute("DELETE FROM session_log WHERE date=?", (date,))
    else:
        db.execute("INSERT OR REPLACE INTO session_log (date, note, created_at) VALUES (?,?,?)",
                   (date, note, _now_iso()))
    db.commit()
    return jsonify(ok=True, date=date, note=note)


@app.post("/api/plan/generate")
def api_plan_generate():
    db = get_db()
    seed_objectives(db)
    plan = regenerate(db)
    if not plan.get("ok"):
        return jsonify(plan), 400
    return jsonify(plan)


@app.post("/api/plan/explain")
def api_plan_explain():
    """§6c — plain-language explanation of the latest plan + the most recent change (advisory)."""
    d = body()
    out = explain_plan(get_db(), d.get("diff"))
    return jsonify(out), (200 if out.get("ok") else 502)


@app.get("/api/plan")
def api_plan():
    """The latest generated plan (or null if none yet)."""
    db = get_db()
    row = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    plan = json.loads(row["plan"]) if row else None
    if plan and READONLY:
        plan.pop("adjustment", None)   # the adjustment carries free-text/medical context — withhold
    return jsonify(plan)


@app.get("/api/readiness")
def api_readiness():
    data = today_readiness(get_db())
    if READONLY:
        # public-safe projection: the traffic-light verdict + today's planned session only.
        # Withhold the check-in inputs, the free-text note, the raw HRV signal, the detailed
        # reasons, and any halt/medical guidance — those stay on the private (Access) side.
        a = data.get("assessment") or {}
        v = a.get("verdict", "green")
        is_rest = (data.get("session") or {}).get("kind") == "rest"
        # A logged run flips the tile to "done" — but never softens a red (parity with the
        # private path; a completed run must not mask a medical stop signal).
        done = bool((data.get("session") or {}).get("done")) and v != "red"
        generic = {"green": ("All clear — today's a planned rest day." if is_rest
                             else "Good to go — today's session is on."),
                   "amber": "Easy day — holding back a little.",
                   "red":   "Rest day — not training today."}
        action = "Today's session is done." if done else generic.get(v, generic["green"])
        assess = {"verdict": v, "action": action, "public": True}
        if done:
            assess["done"] = True
        data = {"date": data.get("date"), "assessment": assess,
                "session": data.get("session")}
    return jsonify(data)


@app.post("/api/readiness")
def api_readiness_post():
    """Submit today's check-in: {energy, sleep, stop_symptom, note}."""
    d = body()
    db = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    db.execute(
        "INSERT OR REPLACE INTO readiness (date,energy,sleep,stop_symptom,note,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (today, d.get("energy", "ok"), d.get("sleep", "ok"),
         1 if d.get("stop_symptom") else 0, d.get("note", ""), _now_iso()),
    )
    db.commit()
    return jsonify(today_readiness(db))


def _activity_payload(db, a):
    """The 'activity tile' view of one activity (raw REST JSON → derived pace/cadence). Shared by the
    latest-activity default and the by-id view (a completed planned session's run)."""
    dist, dur = a.get("distance") or 0, a.get("duration") or 0
    pace = (dur / 60) / dist if dist else None  # min/km
    cad = a.get("cadence")
    if cad and cadence_is_halved(a.get("source")):  # Suunto logs one-leg cadence → ×2 for spm
        cad *= 2
    return {
        "id": a.get("id"), "sport": (a.get("sport") or {}).get("name"),
        "date_time": a.get("date_time"), "date": (a.get("date_time") or "")[:10],
        "title": a.get("title") or "",
        "distance": dist, "duration": dur, "elapsed": a.get("elapsed_time"),
        "pace_min_km": pace, "hr_avg": a.get("hr_avg"), "hr_max": a.get("hr_max"),
        "trimp": a.get("trimp"), "elevation_up": a.get("elevation_up"),
        "cadence": cad,
        "ignored": bool(db.execute("SELECT 1 FROM ignored_activities WHERE id=?",
                                   (a.get("id"),)).fetchone()),
    }


def latest_running_activity(db):
    """For the 'latest running activity' tile: the most-recent RUNNING-FAMILY activity (any sport
    whose name contains 'run' — Running, Trail Running, Treadmill Running, …), plus a note when the
    OVERALL most-recent activity is a non-run (e.g. a tennis match). The non-run still reaches the
    plan via Runalyze's all-sport CTL/ATL snapshot — it just isn't a run to show here. Returns
    (run_row_or_None, cross_note_or_None)."""
    run = db.execute("SELECT raw FROM activities WHERE LOWER(sport) LIKE '%run%' "
                     "ORDER BY date_time DESC LIMIT 1").fetchone()
    top = db.execute("SELECT sport, date FROM activities ORDER BY date_time DESC LIMIT 1").fetchone()
    cross = ({"sport": top["sport"], "date": top["date"]}
             if top and "run" not in (top["sport"] or "").lower() else None)
    return run, cross


@app.get("/api/activity/latest")
def api_activity_latest():
    """Latest RUNNING activity for the tile, with derived pace. Running-family (so trail/treadmill
    runs count); attaches a cross_training note when the most-recent activity isn't a run."""
    db = get_db()
    run, cross = latest_running_activity(db)
    payload = (_activity_payload(db, json.loads(run["raw"])) if run
               else ({"empty_run": True} if cross else None))
    # the cross-training note (latest non-run sport + date) is personal — withhold it server-side on
    # the public read-only container, not just in the UI, so the endpoint itself can't leak it.
    if payload is not None and cross and not READONLY:
        payload["cross_training"] = cross
    return jsonify(payload)


@app.get("/api/activity/<int:aid>")
def api_activity_one(aid):
    """A specific activity by id — for viewing a completed planned session's run in the tile + map."""
    db = get_db()
    row = db.execute("SELECT raw FROM activities WHERE id=?", (aid,)).fetchone()
    if not row:
        return jsonify(None), 404
    return jsonify(_activity_payload(db, json.loads(row["raw"])))


@app.post("/api/activity/<int:aid>/ignore")
def api_activity_ignore(aid):
    """One-click data-quality override: exclude this activity from the reconstruction
    (a near-duplicate or mis-tag the exact-match heuristic can't catch). Writable only
    — the public read-only container 403s this via the before_request guard."""
    db = get_db()
    if not db.execute("SELECT 1 FROM activities WHERE id=?", (aid,)).fetchone():
        return jsonify(ok=False, error="no such activity"), 404
    reason = (request.get_json(silent=True) or {}).get("reason") or "manual"
    db.execute("INSERT OR REPLACE INTO ignored_activities(id, reason, created_at) VALUES (?,?,?)",
               (aid, reason, _now_iso()))
    db.commit()
    return jsonify(ok=True, ignored=aid)


@app.post("/api/activity/<int:aid>/unignore")
def api_activity_unignore(aid):
    """Undo a manual ignore — the activity rejoins the reconstruction."""
    db = get_db()
    db.execute("DELETE FROM ignored_activities WHERE id=?", (aid,))
    db.commit()
    return jsonify(ok=True, unignored=aid)


@app.post("/api/activity/<int:aid>/delete")
def api_activity_delete(aid):
    """Hard-delete an activity from the owned local copy (see `delete_activity_local`) — for one
    already removed on Runalyze that insert-only sync left behind, so the leftover row stops
    inflating the structural duplicate count + banner. Writable only — the public read-only
    container 403s this via the before_request guard."""
    db = get_db()
    if not delete_activity_local(db, aid):
        return jsonify(ok=False, error="no such activity"), 404
    return jsonify(ok=True, deleted=aid)


@app.post("/api/earned")
def api_earned_toggle():
    """§6e/§6f — owner opt-in for earned upward responsiveness (the bounded volume lift on the
    building phases). This toggle is the owner's CONSENT, not a switch that forces load: the lift
    still only acts when the banked-week streak + readiness gates are met, and the ACWR governor
    still caps every week. Writable only — the public container 403s every POST via the guard.
    Returns the freshly-recomputed gate state so the UI reflects it without a full re-plan."""
    db = get_db()
    on = bool((request.get_json(silent=True) or {}).get("on"))
    set_meta(db, EARNED_KEY, "1" if on else "0")
    db.commit()
    prior = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    prior_plan = json.loads(prior["plan"]) if prior else None
    return jsonify(ok=True, earned=earned_state(db, datetime.now().date(), prior_plan))


@app.post("/api/freq")
def api_freq_toggle():
    """§6e — owner opt-in for the earned FREQUENCY advance (the 6th weekly run on non-down Base/Build
    weeks). Like `/api/earned`, this is CONSENT, not a forced change: the 6th run still only appears
    when the (stricter) banked-week streak + readiness gates are met, at constant governed volume,
    and the ACWR governor still caps every week. Writable only — the public container 403s every POST
    via the guard. Returns the freshly-recomputed gate state so the UI reflects it without a re-plan."""
    db = get_db()
    on = bool((request.get_json(silent=True) or {}).get("on"))
    set_meta(db, FREQ_KEY, "1" if on else "0")
    db.commit()
    prior = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    prior_plan = json.loads(prior["plan"]) if prior else None
    return jsonify(ok=True, freq=freq_state(db, datetime.now().date(), prior_plan))


def _profile_cached(db, aid):
    """The current-version downsampled profile for an activity: from trackcache, else fetched + stored.
    Returns (profile|None, error|None). Re-fetches on a VERSION mismatch (not just a cache miss), so a
    post-deploy bump never serves stale shapeless data. On a fetch failure with a stale cache present,
    returns (stale, err) so callers can still serve something (e.g. the tokenless public container);
    on a hard miss returns (None, err)."""
    row = db.execute("SELECT profile FROM trackcache WHERE activity_id=?", (aid,)).fetchone()
    cached = json.loads(row["profile"]) if row else None
    if cached and cached.get("v") == PROFILE_VERSION:
        return cached, None
    try:
        prof = activity_profile(aid)
    except (RunalyzeError, requests.RequestException, KeyError, ValueError) as e:
        return cached, e
    db.execute("INSERT OR REPLACE INTO trackcache (activity_id, profile, cached_at) VALUES (?,?,?)",
               (aid, json.dumps(prof), _now_iso()))
    db.commit()
    return prof, None


def _strip_geo(prof):
    """Route geo (the lat/long `path`) must only ever leave via the private /map endpoint — never the
    shared, public-served /profile. Returns the profile without the path."""
    return {k: v for k, v in prof.items() if k != "path"}


@app.get("/api/activity/<int:aid>/profile")
def api_activity_profile(aid):
    """Downsampled pace/HR/cadence/elevation profile for the hover backgrounds. Cached locally so we
    hit the MCP at most once per activity. Geo is stripped — the route map is private, served by /map."""
    db = get_db()
    prof, err = _profile_cached(db, aid)
    if prof is None:
        return jsonify(error=str(err), dist=[], pace=[], hr=[]), 502
    out = _strip_geo(prof)
    out["hrmax"] = _robust_hrmax(db)   # anchors the HR-line zone colouring on the frontend
    return jsonify(out)


@app.get("/api/activity/<int:aid>/map")
def api_activity_map(aid):
    """Route polyline (lat/long) + bounds for the private workout map. PRIVATE-ONLY: the public
    read-only container 403s this in _readonly_guard — the routes reveal where the owner lives."""
    db = get_db()
    prof, err = _profile_cached(db, aid)
    if prof is None:
        return jsonify(error=str(err), has_gps=False, path=[]), 502
    path = prof.get("path") or []
    if not prof.get("has_gps") or len(path) < 2:
        return jsonify(has_gps=False, path=[])
    lats = [p[0] for p in path]
    lons = [p[1] for p in path]
    return jsonify(has_gps=True, path=path,
                   bounds=[[min(lats), min(lons)], [max(lats), max(lons)]])


@app.get("/api/weekly")
def api_weekly():
    """Running km per ISO week. `weeks>0` trims to the most recent N; `weeks<=0` returns the
    FULL history so the volume chart can pan back/forth over everything we own (it's tiny —
    a few hundred {week,km} rows)."""
    weeks = int(request.args.get("weeks", "26"))
    rows = db_weekly_running()
    return jsonify(rows[-weeks:] if weeks > 0 else rows)


@app.get("/api/vo2max")
def api_vo2max():
    """Per-activity VO₂max trend over the last `months` (default 6) — feeds the VO₂max tile's
    background sparkline. shape_snapshots only holds today's value, so the trend comes from
    each run's own vo2max estimate (in the raw activity JSON), lightly smoothed."""
    db = get_db()
    months = int(request.args.get("months", "6"))
    return jsonify(vo2max_trend(db, months))


def vo2max_trend(db, months=6):
    """Build a smoothed VO₂max series from runs Runalyze counts toward fitness
    (`use_vo2max`), within the window. Per-run vo2max is noisy, so we EWMA-smooth it to
    mirror the 'effective' value the tile shows; we return both raw and smoothed."""
    from datetime import timedelta
    cutoff = (datetime.now().date() - timedelta(days=round(months * 30.4))).isoformat()
    rows = db.execute(
        "SELECT date, raw FROM activities WHERE sport = ? AND date >= ? ORDER BY date ASC",
        (RUNNING_SPORT, cutoff),
    ).fetchall()
    sm, out = None, []
    for r in rows:
        try:
            d = json.loads(r["raw"])
        except (ValueError, TypeError):
            continue
        if not d.get("use_vo2max"):
            continue
        v = d.get("vo2max")
        if not v:
            continue
        v = float(v)
        sm = v if sm is None else sm + 0.25 * (v - sm)
        out.append({"date": r["date"], "raw": round(v, 2), "vo2max": round(sm, 2)})
    return {"months": months, "n": len(out), "points": out}


def db_weekly_running():
    db = get_db()
    rows = db.execute(
        "SELECT date, distance FROM activities WHERE sport = ? AND date IS NOT ''",
        (RUNNING_SPORT,),
    ).fetchall()
    buckets = {}
    for r in rows:
        try:
            iso = datetime.strptime(r["date"], "%Y-%m-%d").isocalendar()
            key = f"{iso[0]}-W{iso[1]:02d}"
        except (ValueError, TypeError):
            continue
        buckets[key] = buckets.get(key, 0.0) + (r["distance"] or 0.0)
    return [{"week": k, "km": round(v, 1)} for k, v in sorted(buckets.items())]


# ── Weather (house chrome widget) ────────────────────────────────────────────
# A small forecast icon for the configured cities. Source is Open-Meteo: keyless, no token,
# CC-BY — fits the project's "no extra secrets" rule (so it works on the public container too).
# We cache the whole bundle in-process for WEATHER_TTL so a page load never hammers the API and
# a transient outage falls back to the last good fetch.
def _parse_weather_cities(spec):
    """Parse SH_WEATHER_CITIES ('Name,lat,lon[,CODE];…') into the widget's city list. The optional
    4th field is the short display code (e.g. Tokyo→TYO); without it the code defaults to the name's
    first 3 letters. Empty/bad spec → [] (the widget hides itself). Lets a self-hoster pick their own
    cities, or none."""
    out = []
    for part in (p for p in (spec or "").split(";") if p.strip()):
        bits = [b.strip() for b in part.split(",")]
        if len(bits) >= 3:
            try:
                lat, lon = float(bits[1]), float(bits[2])
            except ValueError:
                continue
            name = bits[0]
            code = (bits[3] if len(bits) >= 4 and bits[3] else name[:3]).upper()
            out.append({"key": code, "name": name, "lat": lat, "lon": lon})
    return out


WEATHER_CITIES = _parse_weather_cities(os.environ.get("SH_WEATHER_CITIES", ""))
WEATHER_TTL = 1800          # 30 min — weather doesn't move faster than the cache is worth
_weather_cache = {"at": 0.0, "data": None}
_weather_lock = threading.Lock()

# WMO weather-interpretation codes → (emoji, label). Open-Meteo's `weathercode` follows WMO 4677.
WMO_ICONS = {
    0: ("☀️", "Clear"), 1: ("🌤️", "Mainly clear"), 2: ("⛅", "Partly cloudy"),
    3: ("☁️", "Overcast"), 45: ("🌫️", "Fog"), 48: ("🌫️", "Rime fog"),
    51: ("🌦️", "Light drizzle"), 53: ("🌦️", "Drizzle"), 55: ("🌧️", "Dense drizzle"),
    56: ("🌧️", "Freezing drizzle"), 57: ("🌧️", "Freezing drizzle"),
    61: ("🌦️", "Light rain"), 63: ("🌧️", "Rain"), 65: ("🌧️", "Heavy rain"),
    66: ("🌧️", "Freezing rain"), 67: ("🌧️", "Freezing rain"),
    71: ("🌨️", "Light snow"), 73: ("🌨️", "Snow"), 75: ("❄️", "Heavy snow"),
    77: ("🌨️", "Snow grains"), 80: ("🌦️", "Rain showers"), 81: ("🌧️", "Rain showers"),
    82: ("⛈️", "Violent showers"), 85: ("🌨️", "Snow showers"), 86: ("❄️", "Snow showers"),
    95: ("⛈️", "Thunderstorm"), 96: ("⛈️", "Thunderstorm + hail"), 99: ("⛈️", "Thunderstorm + hail"),
}


def _fetch_city_weather(city):
    """One city: current conditions + today's high/low from Open-Meteo. Raises on failure."""
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": city["lat"], "longitude": city["lon"],
            "current_weather": "true",
            "daily": "temperature_2m_max,temperature_2m_min",
            "timezone": "auto", "forecast_days": 1,
        },
        timeout=8,
    )
    r.raise_for_status()
    d = r.json()
    cur = d.get("current_weather") or {}
    daily = d.get("daily") or {}
    code = int(cur.get("weathercode", -1))
    icon, label = WMO_ICONS.get(code, ("🌡️", "—"))
    hi = (daily.get("temperature_2m_max") or [None])[0]
    lo = (daily.get("temperature_2m_min") or [None])[0]
    return {
        "key": city["key"], "name": city["name"],
        "temp": round(cur["temperature"]) if cur.get("temperature") is not None else None,
        "code": code, "icon": icon, "label": label,
        "hi": round(hi) if hi is not None else None,
        "lo": round(lo) if lo is not None else None,
        # local reading time (timezone=auto ⇒ already the city's local clock), e.g. "2026-06-16T14:00"
        "time": cur.get("time"),
    }


def get_weather():
    """Cached three-city bundle. Refreshes at most every WEATHER_TTL; on a failed refresh it
    keeps serving the last good bundle (with stale=True) rather than blanking the widget."""
    now = time.time()
    with _weather_lock:
        cached = _weather_cache["data"]
        if cached and now - _weather_cache["at"] < WEATHER_TTL:
            return cached
    cities = []
    for c in WEATHER_CITIES:
        try:
            cities.append(_fetch_city_weather(c))
        except Exception as e:  # one city failing shouldn't drop the others
            print(f"[weather] {c['name']} fetch failed: {e}")
    if not cities:
        with _weather_lock:
            if _weather_cache["data"]:
                stale = dict(_weather_cache["data"], stale=True)
                return stale
        return {"cities": [], "stale": True}
    bundle = {"cities": cities, "stale": False, "source": "open-meteo"}
    with _weather_lock:
        _weather_cache.update(at=now, data=bundle)
    return bundle


@app.get("/api/weather")
def api_weather():
    """Forecast icon for the configured cities (SH_WEATHER_CITIES). Cached + public-safe."""
    return jsonify(get_weather())


@app.get("/api/health")
def api_health():
    """All tracked health markers as time-series, plus the marker registry (labels,
    units, reference bands) so the UI can render reference lines and trend direction."""
    db = get_db()
    rows = db.execute(
        "SELECT marker, date, value, source, note FROM health_markers ORDER BY date ASC"
    ).fetchall()
    series = {}
    for r in rows:
        series.setdefault(r["marker"], []).append(
            {"date": r["date"], "value": r["value"], "source": r["source"], "note": r["note"]}
        )
    return jsonify(markers=MARKERS, series=series)


@app.post("/api/health")
def api_health_add():
    """Add or update one marker reading: {marker, date, value, [source], [note]}."""
    d = body()
    marker, date, value = d.get("marker"), d.get("date"), d.get("value")
    if marker not in MARKERS:
        return jsonify(ok=False, error=f"unknown marker {marker!r}"), 400
    try:
        value = float(value)
        datetime.strptime(date, "%Y-%m-%d")
    except (TypeError, ValueError):
        return jsonify(ok=False, error="need a numeric value and a YYYY-MM-DD date"), 400
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO health_markers (marker, date, value, source, note) "
        "VALUES (?,?,?,?,?)",
        (marker, date, value, d.get("source", "manual"), d.get("note", "")),
    )
    db.commit()
    return jsonify(ok=True)


@app.get("/api/settings")
def api_settings():
    """The settable non-secret personalization + provenance. Private-only via `_private_only_path`
    (the _readonly_guard 403s it on the public container, where the JS also drops the card)."""
    return jsonify(ok=True, settings=current_settings(get_db()))


@app.get("/api/geocode")
def api_geocode():
    """Resolve a city name → candidates with lat/lon, via Open-Meteo's keyless geocoding API (same
    provider as the weather widget). Server-side proxy so the browser never calls a third party
    directly (keeps CSP `connect-src 'self'` + the user's typing private). Private-only via
    `_private_only_path`. Returns a trimmed list the Settings city-picker turns into the stored
    `Name,lat,lon,CODE` format."""
    q = (request.args.get("q") or "").strip()[:80]
    if len(q) < 2:
        return jsonify(ok=True, results=[])
    try:
        r = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                         params={"name": q, "count": 6, "language": "en", "format": "json"},
                         timeout=8)
        r.raise_for_status()
        rows = r.json().get("results") or []
        results = [{
            "name": c.get("name"),
            "admin1": c.get("admin1") or "",
            "country": c.get("country") or "",
            "country_code": (c.get("country_code") or "").upper(),
            "lat": round(c["latitude"], 4), "lon": round(c["longitude"], 4),
        } for c in rows if isinstance(c.get("latitude"), (int, float))
                        and isinstance(c.get("longitude"), (int, float))]
    except Exception as e:
        print(f"[geocode] {q!r} failed: {e}")   # detail to logs, generic message to the client
        return jsonify(ok=False, error="geocoding unavailable"), 502
    return jsonify(ok=True, results=results)


@app.post("/api/settings")
def api_settings_save():
    """Persist edited settings (meta-override of the SH_* env) and re-apply live. The _readonly_guard
    already rejects this on the public container; secrets are unsettable (not in SETTINGS_SPEC)."""
    ok, result = save_settings(get_db(), body())
    if not ok:
        return jsonify(ok=False, errors=result), 400
    return jsonify(ok=True, settings=current_settings(get_db()))


@app.get("/")
def index():
    # inject the mode flag + private-console URL synchronously so the UI gates with no round-trip
    # HOUSE_URL/NAME can now be set via the Settings panel (validated) OR raw env (unvalidated), and
    # are injected into header HTML — so escape at the render site regardless of source (defence in
    # depth, not relying on the save-time char check alone).
    hublink = (f'<a class="hublink" href="{html.escape(HOUSE_URL, quote=True)}">'
               f'← {html.escape(HOUSE_NAME or HOUSE_URL)}</a>'
               if HOUSE_URL else "")
    page = html_page(INDEX_HTML
            .replace("__SH_READONLY__", "true" if READONLY else "false")
            # json.dumps escapes quotes/backslashes but NOT "/", so neutralise "</" → a value with
            # "</script>" (e.g. a raw env SH_PRIVATE_URL that bypassed validate_setting) can't close
            # the inline <script> and inject markup into the (public) page.
            .replace("__SH_PRIVATE_URL__", json.dumps(PRIVATE_URL).replace("</", "<\\/"))
            .replace("__RUNALYZE_LOGO__", RUNALYZE_LOGO_SVG)
            .replace("__SH_HUBLINK__", hublink))
    # The whole SPA — markup + inline JS — is this one document. Tell the browser to revalidate it
    # every load so a deploy takes effect on an ordinary reload (no hard-refresh needed): browsers
    # otherwise heuristically cache an un-headered HTML doc and serve stale JS after a release.
    return (page, 200, {"Cache-Control": "no-cache"})


# ── The SPA (house terracotta theme + daylight light mode) ───────────────────
INDEX_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sparing Horse — running</title>
<meta name="theme-color" content="#f4f1ea">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600;9..144,900&family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<script>try{var t=localStorage.getItem("sh-theme");document.documentElement.dataset.theme=(t==="dark"||t==="aurora")?t:"light"}catch(e){document.documentElement.dataset.theme="light"}</script>
<style>
  /* House design system tokens (DESIGN.md §2.3) — apps reference TOKEN NAMES only, never raw hex,
     so one data-theme switch re-skins the whole app. Light/Daylight is the CSS base (:root) so a
     fresh load (JS off / pre-script) is light; Dark/Charcoal + Aurora/Electric are overrides. The
     inline <head> script restores the saved preference per origin before first paint. Legacy aliases
     (--surface-2/--terra/--ok-bright) map onto the canonical tokens so existing rules keep working. */
  :root{   /* Daylight — warm paper, terracotta */
    --bg:#f4f1ea; --surface:#fbf9f4; --surface2:#ece7db; --line:#ddd6c7;
    --text:#2a2620; --muted:#6f6857; --accent:#b9542c;
    --ok:#4f8c5f; --warn:#a9781f; --danger:#b5563f;
    --readybg:#4d8a5c; --readybg2:#3c6a48; --onacc:#fff;
    --surface-2:var(--surface2); --terra:var(--accent); --ok-bright:var(--ok);   /* legacy aliases */
    --serif:'Fraunces',Georgia,serif; --sans:'Inter',system-ui,sans-serif;
    --mono:'IBM Plex Mono',ui-monospace,Menlo,monospace;
  }
  [data-theme="dark"]{   /* Charcoal — neutral graphite, brighter terracotta so it reads as a glow */
    --bg:#191a1d; --surface:#222327; --surface2:#2a2b30; --line:#3a3c43;
    --text:#edeef1; --muted:#9b9da5; --accent:#fa7d42;
    --ok:#33d98a; --warn:#f7b32b; --danger:#fc6a55;
    --readybg:#2f9760; --readybg2:#1d6240; --onacc:#fff;
    --surface-2:var(--surface2); --terra:var(--accent); --ok-bright:var(--ok);
  }
  [data-theme="aurora"]{   /* Electric — deep indigo, violet→cyan accents, neon signals */
    --bg:#121226; --surface:#1c1d3e; --surface2:#262752; --line:#3a3c74;
    --text:#eef0ff; --muted:#a2a6dc; --accent:#7b61ff; --accent2:#28d6ee;
    --ok:#22e3a6; --warn:#ffc24d; --danger:#ff5d8a;
    --readybg:#12b39a; --readybg2:#0a7d6e; --onacc:#fff;
    --surface-2:var(--surface2); --terra:var(--accent); --ok-bright:var(--ok);
  }
  *{box-sizing:border-box}
  html,body{margin:0}
  body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;
    font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased;
    transition:background .25s ease,color .25s ease}
  body::before{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;
    background:radial-gradient(60% 45% at 50% 0%, color-mix(in oklab,var(--terra),transparent 88%), transparent 70%);}
  .wrap{position:relative;z-index:1;max-width:1080px;margin:0 auto;padding:34px 26px 90px}

  header{display:flex;align-items:center;gap:14px;border-bottom:1px solid var(--line);
    padding-bottom:18px;margin-bottom:26px}
  .brand{display:grid;grid-template-columns:auto 1fr;grid-template-areas:". eyebrow" "logo title";align-items:center;column-gap:14px}
  .dotmark{grid-area:logo;width:38px;height:38px;flex:none;display:grid;place-items:center}
  .dotmark svg{width:100%;height:100%;display:block}
  h1{font-family:var(--serif);font-weight:600;font-size:30px;letter-spacing:-.01em;margin:0}
  .eyebrow{font-family:var(--mono);font-size:10.5px;letter-spacing:.22em;text-transform:uppercase;
    color:var(--muted);margin:0 0 3px;grid-area:eyebrow}
  .titlerow{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;grid-area:title}
  .motto{font-family:var(--mono);font-size:11px;color:var(--accent);opacity:.9}
  .motto small{color:var(--muted)}
  .bar{display:flex;align-items:center;gap:12px;margin-left:auto}
  /* configured-cities forecast icon — pinned to the top-right of the readiness tile: "you're good to
     go physiologically; here's roughly what's outside" (shows only the cities you've chosen) */
  #sec-readiness{position:relative}
  .weather{position:absolute;top:1px;right:0;z-index:2;display:inline-flex;align-items:center;gap:13px}
  .weather:empty{display:none}
  .weather .wx{display:inline-flex;align-items:center;gap:5px;cursor:help}
  .weather .wx .c{font-family:var(--mono);font-size:9px;letter-spacing:.1em;color:var(--muted)}
  .weather .wx .ico{font-size:15px;line-height:1}
  .weather .wx .t{font-family:var(--mono);font-size:12px;font-weight:600;color:var(--text)}
  .weather.stale{opacity:.55}
  @media(max-width:620px){.weather .wx .c{display:none}.weather{gap:10px}}
  .ro-badge{font-family:var(--mono);font-size:10px;letter-spacing:.1em;text-transform:uppercase;
    color:var(--muted);border:1px solid var(--line);border-radius:20px;padding:3px 9px}
  .adminlink{font-family:var(--sans);font-size:12px;font-weight:500;color:var(--accent);text-decoration:none;
    border:1px solid color-mix(in oklab,var(--accent),transparent 50%);border-radius:9px;padding:6px 11px;
    transition:border-color .15s,background .15s}
  .adminlink:hover{border-color:var(--accent);background:color-mix(in oklab,var(--accent),transparent 92%)}
  button{font-family:var(--sans);font-size:13px;font-weight:500;color:var(--text);cursor:pointer;
    background:var(--surface-2);border:1px solid var(--line);border-radius:9px;padding:8px 14px;
    transition:border-color .15s,transform .1s}
  button:hover{border-color:var(--accent)} button:active{transform:translateY(1px)}
  button:disabled,input:disabled{opacity:.5;cursor:not-allowed}
  button.primary{background:var(--accent);border-color:var(--accent);color:var(--onacc)}
  button.ghost{background:transparent;color:var(--muted);font-size:12px;padding:8px 12px}
  button.ghost:hover{color:var(--text)}
  /* shared house chrome: a fixed top-right control cluster (login + swatches) and a
     top-left hub link. Swatches mirror bookworm's component (30px + accent underline);
     the login pill sits to their left (the sparinghorse arrangement). */
  .topctl{position:fixed;top:8px;right:16px;z-index:50;display:inline-flex;align-items:center;gap:10px}
  .hublink{position:fixed;top:8px;left:16px;z-index:50;font-family:var(--mono);font-size:10.5px;
    letter-spacing:.14em;text-transform:uppercase;color:var(--muted);text-decoration:none}
  .hublink:hover{color:var(--accent)}
  .themes{display:inline-flex;gap:8px}
  .swatch{width:30px;height:9px;border-radius:2px;border:1px solid var(--line);padding:0;
    cursor:pointer;opacity:.55;position:relative;background:var(--sw);transition:opacity .12s}
  .swatch:hover,.swatch[aria-pressed="true"]{opacity:1}
  .swatch[aria-pressed="true"]::after{content:"";position:absolute;left:0;right:0;bottom:-5px;
    height:2px;background:var(--accent);border-radius:2px}

  .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}
  @media(max-width:880px){.grid{grid-template-columns:repeat(2,1fr)}}
  @media(max-width:520px){.grid{grid-template-columns:1fr}}
  .tile{background:linear-gradient(180deg,var(--surface),var(--surface-2));
    border:1px solid var(--line);border-radius:14px;padding:18px 18px 16px;position:relative;overflow:hidden}
  .tile::before{content:"";position:absolute;left:0;top:0;height:3px;width:100%;background:linear-gradient(90deg,var(--accent),var(--accent2,var(--accent)));opacity:.9}
  .tile .k{font-family:var(--mono);font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
  .tile .v{font-family:var(--serif);font-weight:600;font-size:34px;line-height:1.05;margin-top:8px}
  .tile .v small{font-size:15px;color:var(--muted);font-family:var(--sans);font-weight:400}
  .tile .sub{font-size:12px;color:var(--muted);margin-top:2px}
  /* caption tiles: lift the title/value/subtitle up and reserve a clear band at the bottom
     for the timeframe caption, so a long subtitle can never crowd or overlap it */
  .tile.hascap{padding-top:14px;padding-bottom:30px}
  .tile{cursor:help}
  .tile .k,.tile .v,.tile .sub{position:relative;z-index:1}
  /* VO₂max tile background sparkline (per-activity trend) */
  .tilebg{position:absolute;inset:0;z-index:0;opacity:0;transition:opacity .5s ease;pointer-events:none}
  .tilebg.on{opacity:1}
  .tilebg svg{position:absolute;inset:0;width:100%;height:100%}
  .tilebg .proffill{fill:color-mix(in oklab,var(--accent),transparent 92%);
    stroke:color-mix(in oklab,var(--accent),transparent 70%);stroke-width:1}
  .tilecap{position:absolute;right:12px;bottom:9px;z-index:1;font-family:var(--mono);
    font-size:8.5px;letter-spacing:.04em;color:var(--muted);opacity:.7;cursor:help}
  .info{opacity:.5;font-size:9px;cursor:help}
  .dqwarn{border:1px solid var(--warn);background:color-mix(in oklab,var(--warn),transparent 90%);
    border-radius:10px;padding:10px 14px;margin-bottom:16px;font-size:13px;color:var(--text);line-height:1.5}
  .dqwarn b{color:var(--warn)}
  .dqnote{border:1px solid var(--line);border-radius:10px;padding:8px 14px;margin-bottom:16px;
    font-size:12px;color:var(--muted);line-height:1.5}
  .dqnote a{color:var(--accent);text-decoration:none;border-bottom:1px dotted var(--accent)}
  .dqnote a:hover{opacity:.8}
  /* recent activity + planned session metric rows */
  .mrow{display:flex;flex-wrap:wrap;gap:8px 26px;align-items:baseline}
  .mrow .ttl{font-family:var(--serif);font-weight:600;font-size:17px;margin-right:6px}
  .metric{display:flex;flex-direction:column;gap:1px}
  .metric .ml{font-family:var(--mono);font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
  .metric .mv{font-size:16px;font-weight:600}
  .metric .mv small{font-size:11px;color:var(--muted);font-weight:400}
  .rkick{font-family:var(--mono);font-size:10px;letter-spacing:.12em;text-transform:uppercase;
    color:var(--accent);margin-bottom:8px}
  .planned{margin-top:16px}
  /* latest-activity hover profile background */
  #recent{position:relative;overflow:hidden}
  /* the chart wrapper bounds the profile background to the metrics area (height), while its negative
     margins let the chart still bleed to the tile edges — so the route map below it isn't overlapped */
  .actwrap{position:relative;margin:-20px -20px 0;padding:20px 20px 0}
  /* public tile: no route map follows, so the chart would otherwise leave a bare strip of panel
     padding below it. When .actwrap is the panel's last child, bleed to the bottom edge too. */
  .actwrap:last-child{margin-bottom:-20px;padding-bottom:20px}
  .actbg{position:absolute;inset:0;z-index:0;opacity:0;transition:opacity .4s ease;pointer-events:none}
  .actbg.on{opacity:1}
  .actbg svg{position:absolute;inset:0;width:100%;height:100%}
  /* low-contrast so the tile text stays readable */
  .actbg .proffill{fill:color-mix(in oklab,var(--accent),transparent 94%);
    stroke:color-mix(in oklab,var(--accent),transparent 78%);stroke-width:1}
  /* Aurora's deep-indigo surface nearly swallows the faint accent area-fill — lift it toward white
     so the shade under the trace reads a touch lighter than the background (not just more violet) */
  [data-theme="aurora"] .actbg .proffill{fill:color-mix(in oklab,color-mix(in oklab,var(--accent),#fff 30%),transparent 86%)}
  [data-theme="aurora"] .tilebg .proffill{fill:color-mix(in oklab,color-mix(in oklab,var(--accent),#fff 30%),transparent 84%)}
  .actbg .avgline{stroke:var(--muted);stroke-width:1;stroke-dasharray:5 4;opacity:.4}
  /* hovered metric traced on top of the locked area — value-coloured, no fill; non-scaling so the
     stretched viewBox doesn't make the stroke uneven */
  .actbg .profline{fill:none;stroke-width:1;vector-effect:non-scaling-stroke;
    stroke-linejoin:round;stroke-linecap:round}
  /* the chart hint (left) and the locked-variable label + HR-zone legend (right) share ONE baseline
     row. profmeta is ABSOLUTE inside the relative .profbar (anchored bottom-right), so the legend
     appearing on HR hover grows it sideways WITHOUT reflowing the row → the tile height never jumps.
     The row's height is set by .profhint alone (constant); padding-right keeps the hint clear of it. */
  .profbar{position:relative;margin-top:12px;min-height:15px}
  .profmeta{position:absolute;right:0;bottom:0;display:inline-flex;align-items:center;gap:12px;
    white-space:nowrap;text-align:right;color:var(--muted);font-family:var(--mono);font-size:9.5px;letter-spacing:.04em}
  .hrlegend{display:inline-flex;gap:9px}
  /* the between-zone gap (14px) must exceed the square↔its-own-label gap (4px), else each square
     reads as belonging to the previous zone's label */
  .hrlegend .hrleg{display:inline-flex;gap:14px}
  .hrlegend .hrz{display:inline-flex;align-items:center;gap:4px;color:var(--muted)}
  .hrlegend .hrz i{width:9px;height:9px;border-radius:2px}
  .actfg{position:relative;z-index:1}   /* profmeta now lives in-flow in .profbar — no reserved strip (was the public tile's empty gap) */
  .metric.hovx{cursor:pointer;border-radius:7px;transition:background .15s,box-shadow .15s;padding:2px 6px;margin:-2px -6px}
  .metric.hovx:hover{background:color-mix(in oklab,var(--accent),transparent 90%)}
  /* lock = the same shade as hover (an underline collided with the value-coloured trace line);
     the 🔒 marker is what tells the persistent lock apart from a transient hover */
  .metric.hovx.locked{background:color-mix(in oklab,var(--accent),transparent 90%)}
  .metric.hovx.locked .ml::after{content:" 🔒";font-size:8px}
  .proflbl{font-family:var(--mono);font-size:9.5px;color:var(--muted);text-transform:none;
    letter-spacing:0;margin-left:8px}
  .profhint{font-size:11px;opacity:.75;padding-right:320px}
  .profhint b{color:var(--accent)}
  /* top row: the LATEST ACTIVITY kicker (left) + data-quality utilities (right corner). The two
     actions are muted at rest, hover reveals intent (ignore→accent, delete→danger). */
  .rtop{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
  .dqtools{display:inline-flex;gap:14px;flex-shrink:0;font-size:11px;color:var(--muted);white-space:nowrap}
  .dqtools a{color:var(--muted);text-decoration:none;border-bottom:1px dotted color-mix(in oklab,var(--muted),transparent 40%)}
  .dqtools a:hover{color:var(--accent);border-bottom-color:var(--accent)}
  .dqtools a.delact:hover{color:var(--danger);border-bottom-color:var(--danger)}
  /* workout route map (private only) — Leaflet renders into .actmap; needs an explicit height */
  .actmap{position:relative;z-index:1;height:240px;margin-top:14px;border-radius:10px;
    overflow:hidden;border:1px solid var(--line)}
  .actmap .leaflet-container{height:100%;background:var(--surface-2);font-family:var(--sans)}
  /* cross-training note — shown when the most-recent activity isn't a run */
  .crossnote{color:var(--muted);font-size:11.5px;line-height:1.5;margin-top:12px;
    padding-top:9px;border-top:1px dashed var(--line)}
  .mapempty{height:100%;display:flex;align-items:center;justify-content:center;color:var(--muted);
    font-size:13px;background:color-mix(in oklab,var(--accent),var(--surface-2) 82%)}
  .up{color:var(--ok)} .down{color:var(--danger)}

  .section{margin-top:30px}
  .section h2{font-family:var(--serif);font-weight:600;font-size:19px;margin:0 0 14px}
  /* Collapsible sections (Plan drift, Fitness & fatigue, Weekly volume) load collapsed — the runner
     deliberately opens them; the chevron rotates on [open] */
  details.section > summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:10px}
  details.section > summary::-webkit-details-marker{display:none}
  details.section > summary h2{margin:0}
  details.section > summary::before{content:"";flex:none;width:7px;height:7px;margin-top:-3px;
    border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);
    transform:rotate(-45deg);transition:transform .15s}
  details.section[open] > summary::before{transform:rotate(45deg)}
  details.section > summary:hover::before{border-color:var(--accent)}
  details.section > .panel{margin-top:14px}
  .panel{background:linear-gradient(180deg,var(--surface),var(--surface-2));
    border:1px solid var(--line);border-radius:14px;padding:20px}

  /* ACWR gauge */
  .gauge{position:relative;height:34px;border-radius:8px;border:1px solid var(--line);
    background:var(--surface-2);margin-top:26px}
  .gauge .band{position:absolute;top:0;bottom:0;background:color-mix(in oklab,var(--ok),transparent 72%);
    border-radius:8px}
  .gauge .mark{position:absolute;top:-4px;bottom:-4px;width:3px;background:var(--accent);border-radius:2px;
    box-shadow:0 0 0 2px var(--bg)}
  .gyou{position:absolute;top:-24px;transform:translateX(-50%);white-space:nowrap}
  .gyou span{font-family:var(--mono);font-size:11px;font-weight:500;padding:2px 7px;border-radius:6px}
  .gyou .inb{color:var(--ok);border:1px solid color-mix(in oklab,var(--ok),transparent 55%)}
  .gyou .out{color:var(--danger);border:1px solid color-mix(in oklab,var(--danger),transparent 50%)}
  .gauge-scale{display:flex;justify-content:space-between;font-family:var(--mono);font-size:10px;
    color:var(--muted);margin-top:6px}
  .gauge-scale b{color:var(--ok)}

  /* weekly bars */
  .chart{cursor:grab}
  .chart.grabbing{cursor:grabbing}
  .chart .bars,.chart .ruler{user-select:none}
  .chart .bars{display:flex;align-items:flex-end;gap:4px;height:150px}
  .chart .col{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:3px;min-width:0}
  .chart .col .barb{width:100%;background:var(--accent);border-radius:3px 3px 0 0;opacity:.82;
    transition:height .4s ease,opacity .15s}
  .chart .col:hover .barb{opacity:1}
  .chart .vlbl{font-family:var(--mono);font-size:8.5px;color:var(--muted);height:11px;line-height:11px}
  .chart .ruler{position:relative;height:16px;margin-top:7px;border-top:1px solid var(--line)}
  .chart .tick{position:absolute;top:5px;font-family:var(--mono);font-size:9.5px;color:var(--muted);
    transform:translateX(-1px)}
  .chart .tick::before{content:"";position:absolute;top:-6px;left:0;width:1px;height:5px;background:var(--line)}
  /* readiness gate */
  .ready{display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}
  .light{width:54px;height:54px;border-radius:50%;flex:none;position:relative;
    box-shadow:0 0 0 4px color-mix(in oklab,var(--rc),transparent 80%)}
  .light.green{--rc:var(--ok-bright)} .light.amber{--rc:var(--warn)} .light.red{--rc:var(--danger)}
  .light{background:var(--rc)}
  .ready .rbody{flex:1;min-width:240px}
  .ready .rv{font-family:var(--serif);font-weight:600;font-size:18px;text-transform:capitalize}
  .ready .raction{font-size:14px;color:var(--text);margin-top:3px;line-height:1.5}
  .ready .rwhy{font-family:var(--mono);font-size:10.5px;color:var(--muted);margin-top:6px}
  .ready .rhrv{font-family:var(--mono);font-size:10.5px;color:var(--muted);margin-top:4px}
  .ready .raisrc{font-family:var(--mono);font-size:10px;color:var(--accent);margin-top:5px;letter-spacing:.04em}
  /* §3 status card — "lead with the verdict" (DESIGN.md Almanac). The bg swaps with the state. */
  .statuscard{position:relative;overflow:hidden;border-radius:16px;padding:20px 22px;color:#fff;
    background:linear-gradient(155deg,var(--readybg),var(--readybg2));
    box-shadow:0 8px 22px color-mix(in oklab,var(--readybg),transparent 60%)}
  .statuscard.amber{background:linear-gradient(155deg,var(--warn),color-mix(in oklab,var(--warn),#000 30%));
    box-shadow:0 8px 22px color-mix(in oklab,var(--warn),transparent 60%)}
  .statuscard.red{background:linear-gradient(155deg,var(--danger),color-mix(in oklab,var(--danger),#000 32%));
    box-shadow:0 8px 22px color-mix(in oklab,var(--danger),transparent 55%)}
  .statuscard .sc-orb{position:absolute;border-radius:50%;background:rgba(255,255,255,.09);pointer-events:none}
  .statuscard .sc-top{display:flex;align-items:center;justify-content:space-between;gap:12px;position:relative}
  .statuscard .sc-eyebrow{font-family:var(--mono);font-size:9.5px;letter-spacing:.18em;text-transform:uppercase;color:rgba(255,255,255,.78)}
  .statuscard .sc-pill{display:inline-flex;align-items:center;gap:7px;background:rgba(255,255,255,.16);
    border:1px solid rgba(255,255,255,.28);border-radius:999px;padding:4px 11px;
    font-family:var(--mono);font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:#fff}
  .statuscard .sc-pill .dot{width:8px;height:8px;border-radius:50%;background:#fff;box-shadow:0 0 8px rgba(255,255,255,.9)}
  .statuscard .sc-verdict{font-family:var(--serif);font-weight:600;font-size:23px;margin-top:14px;line-height:1.18;position:relative}
  .statuscard .halt{font-weight:600;margin-top:10px;position:relative}
  .statuscard .sc-foot{font-family:var(--mono);font-size:10.5px;color:rgba(255,255,255,.82);
    margin-top:14px;padding-top:12px;border-top:1px solid rgba(255,255,255,.2);position:relative;line-height:1.6;
    display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px}
  .statuscard .sc-wx{display:inline-flex;gap:12px;margin-left:auto}
  .statuscard .sc-wx .wxc{display:inline-flex;align-items:center;gap:4px;color:rgba(255,255,255,.92)}
  .statuscard .sc-wx .wxk{opacity:.6;font-size:9px;letter-spacing:.06em}
  /* readiness ⨉ chronic-load, side by side (mockup Almanac) */
  .rgrid{display:grid;grid-template-columns:1.45fr 1fr;gap:16px;align-items:stretch}
  @media(max-width:760px){.rgrid{grid-template-columns:1fr}}
  /* readiness + today's session share one surface tile (same bg as the load tile); the green
     status card bleeds to the tile's top edges — top corners curved, bottom corners squared so
     it reads as the tile's header, with the session in the body below. */
  #readiness{background:linear-gradient(180deg,var(--surface),var(--surface-2));
    border:1px solid var(--line);border-radius:14px;padding:20px;overflow:hidden}
  #readiness .statuscard{margin:-20px -20px 0;border-radius:14px 14px 0 0;box-shadow:none}
  .acwrcard{display:flex;flex-direction:column}
  .acwrcard .acwr-title{font-family:var(--serif);font-weight:600;font-size:15px;margin-bottom:34px}
  /* CTL ramp readout — the divider sits just under the ACWR explanation, then the ramp FILLS the
     rest of the tile (flex:1) and vertically centres its readout in that lower half (so it isn't
     crammed against the divider on a stretched tile) */
  .acwrcard .acwr-foot{position:relative;overflow:hidden;margin-top:16px;flex:1;display:flex;
    flex-direction:column;justify-content:center;min-height:84px;padding-top:16px;border-top:1px solid var(--line)}
  .acwrcard .acwr-foot .acwr-foot-txt{position:relative;z-index:1}
  .acwrcard .acwr-foot .k{font-family:var(--mono);font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
  .acwrcard .acwr-foot .v{display:block;font-family:var(--serif);font-weight:600;font-size:22px;line-height:1;margin-top:5px}
  .acwrcard .acwr-foot .v small{font-family:var(--mono);font-size:11px;font-weight:400;color:var(--muted)}
  .acwrcard .acwr-foot .cap{display:block;font-family:var(--mono);font-size:10px;color:var(--muted);margin-top:6px}
  /* A-race pill bar */
  .objbar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;border:1px solid var(--line);
    border-radius:12px;padding:12px 16px;margin-bottom:22px;background:var(--surface)}
  .objbar:empty{display:none}
  .objbar .arace{font-family:var(--mono);font-size:9px;letter-spacing:.16em;text-transform:uppercase;
    color:var(--onacc);background:linear-gradient(135deg,var(--accent),var(--accent2,var(--accent)));
    padding:3px 8px;border-radius:5px;white-space:nowrap}
  .objbar .oname{font-family:var(--serif);font-weight:600;font-size:19px}
  .objbar .owhen{font-family:var(--mono);font-size:11px;color:var(--accent)}
  .objbar .overdict{margin-left:auto;font-size:12px;color:var(--muted)}
  .checkin .cinote{flex:1;min-width:200px;font-family:var(--sans);font-size:13px;color:var(--text);
    background:var(--surface-2);border:1px solid var(--line);border-radius:8px;padding:7px 10px}
  .halt{border:1px solid var(--danger);background:color-mix(in oklab,var(--danger),transparent 90%);
    border-radius:10px;padding:10px 14px;margin-top:10px;color:var(--danger);font-size:13px;font-weight:500}
  .checkin{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:14px;
    border-top:1px solid var(--line);padding-top:14px}
  .checkin label{font-family:var(--mono);font-size:10px;text-transform:uppercase;
    letter-spacing:.1em;color:var(--muted)}
  .checkin select{font-family:var(--sans);font-size:13px;color:var(--text);
    background:var(--surface-2);border:1px solid var(--line);border-radius:8px;padding:7px 10px}
  .checkin .stop{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--danger)}

  /* training plan */
  .objline{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px 16px;margin-bottom:14px}
  .objline .race{font-family:var(--serif);font-weight:600;font-size:20px}
  .objline .away{font-family:var(--mono);font-size:11px;color:var(--accent)}
  .feas{font-size:13px;color:var(--muted);line-height:1.5;margin:0 0 16px;
    border-left:2px solid var(--accent);padding-left:12px}
  .feas b{color:var(--text);font-weight:600}
  .phases{display:flex;gap:3px;margin:6px 0 4px;height:34px;border-radius:8px;overflow:hidden}
  .phaseseg{display:flex;flex-direction:column;justify-content:center;align-items:center;
    color:var(--text);font-family:var(--mono);font-size:9px;text-align:center;padding:0 4px;
    background:color-mix(in oklab,var(--accent),var(--surface-2) var(--mix))}
  .phaseseg b{font-size:10px}
  /* the phase bar is a selector: each segment reveals its own phase's weeks below */
  .phaseseg{cursor:pointer;opacity:.5;transition:opacity .15s,box-shadow .15s}
  .phaseseg:hover{opacity:.8}
  .phaseseg.active{opacity:1;box-shadow:inset 0 0 0 2px var(--accent)}
  /* only the active phase's weeks are shown; the rest live behind their bar segment */
  .phasepanel{display:none}
  .phasepanel.active{display:block}
  /* week strip — a second-level selector INSIDE a phase (echoes the phase bar): one segment per
     week, only the selected week's detail is shown below, keeping the tile glanceable on load */
  .weekstrip{display:flex;gap:3px;margin:10px 0 8px;border-radius:8px;overflow:hidden}
  .weekseg{flex:1;display:flex;align-items:center;justify-content:center;gap:3px;min-width:30px;
    font-family:var(--mono);font-size:10px;line-height:1;padding:6px 3px;cursor:pointer;
    color:var(--text);background:color-mix(in oklab,var(--accent),var(--surface-2) 30%);
    opacity:.5;transition:opacity .15s,box-shadow .15s}
  .weekseg:hover{opacity:.8}
  .weekseg.active{opacity:1;box-shadow:inset 0 0 0 2px var(--accent)}
  .weekseg.wsdown{background:color-mix(in oklab,var(--muted),var(--surface-2) 35%)}  /* recovery (down) week */
  .weekseg .wlock{font-size:8px;opacity:.8}
  .weekdetail{display:none}
  .weekdetail.active{display:block}
  .zones{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}
  .zone{font-family:var(--mono);font-size:11px;border:1px solid var(--line);border-radius:8px;
    padding:5px 10px;color:var(--muted)}
  .zone b{color:var(--accent);font-weight:500}
  .zone.hl{border-color:var(--accent)}
  .wk{display:grid;grid-template-columns:30px 1fr auto;gap:12px;align-items:center;
    padding:10px 0;border-top:1px solid var(--line)}
  .wk .wn{font-family:var(--serif);font-weight:600;font-size:17px;color:var(--accent)}
  .wk .wbody{min-width:0}
  .wk .wkm{font-weight:600}
  .wk .wintent{font-size:12px;color:var(--muted);margin-top:2px}
  .wk .wsess{font-family:var(--mono);font-size:10px;color:var(--muted);margin-top:4px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  /* session log — planned vs actual + reflections (the daily-workflow journal) */
  .wsesslog{margin-top:5px;display:flex;flex-direction:column;gap:2px}
  .sline{font-family:var(--mono);font-size:10.5px;color:var(--muted);display:flex;
    align-items:baseline;gap:5px;flex-wrap:wrap}
  .sline.stoday{color:var(--text)}
  /* a completed session is clickable → opens that run's tile + route map */
  .sline.sclick{cursor:pointer;border-radius:5px;padding:1px 4px;margin:0 -4px}
  .sline.sclick:hover{background:color-mix(in oklab,var(--accent),transparent 88%);color:var(--text)}
  .sline.sclick::after{content:"🗺";font-size:9px;opacity:0;margin-left:2px;transition:opacity .12s}
  .sline.sclick:hover::after{opacity:.7}
  .backlatest{font-family:var(--sans);text-transform:none;letter-spacing:0;font-size:11px;
    margin-left:4px;color:var(--accent)}
  .smk{width:1em;display:inline-block;text-align:center;color:var(--line)}
  .smk.done{color:var(--ok)} .smk.missed{color:var(--danger)} .smk.today{color:var(--accent)}
  .smk.extra{color:var(--accent)}
  .sline .splan.exu{font-style:italic;opacity:.8}
  .sline .splan{color:var(--muted)} .sline.stoday .splan{color:var(--text)}
  .sline .sact{color:var(--accent)}
  .sline .sdate{color:var(--muted);opacity:.85;min-width:104px;display:inline-block}
  .srefl{flex-basis:100%;margin-left:1.4em;color:var(--muted);font-family:var(--sans);
    font-size:11px;font-style:italic}
  /* a double's per-run breakdown (the combined actual split into AM/PM, each map-linkable) */
  .srun{flex-basis:100%;margin-left:1.4em;color:var(--muted);font-size:9.5px}
  .srun .brkrun[data-act-id]{cursor:pointer;text-decoration:underline dotted;text-underline-offset:2px}
  .srun .brkrun[data-act-id]:hover{color:var(--text)}
  .adjreply{margin-bottom:5px;color:var(--text);line-height:1.45}
  .acbadge{font-family:var(--mono);font-size:10px;padding:3px 8px;border-radius:20px;white-space:nowrap}
  .acbadge.lo{color:var(--ok);border:1px solid color-mix(in oklab,var(--ok),transparent 60%)}
  .acbadge.mid{color:var(--warn);border:1px solid color-mix(in oklab,var(--warn),transparent 55%)}
  /* Reusable click-to-open help affordance — a small "?" that pops an explanation bubble */
  .qhint{display:inline-flex;align-items:center;justify-content:center;width:15px;height:15px;flex:none;
    margin-left:6px;border:1px solid var(--line);border-radius:50%;font-family:var(--mono);font-size:9px;
    font-weight:700;color:var(--muted);cursor:pointer;vertical-align:middle;user-select:none}
  .qhint:hover,.qhint:focus,.qhint.open{color:var(--text);border-color:var(--muted);outline:none}
  .qtip{display:none;position:fixed;z-index:200;padding:9px 11px;background:var(--surface);color:var(--text);
    border:1px solid var(--line);border-radius:10px;box-shadow:0 12px 34px rgba(0,0,0,.22);font-family:var(--sans);
    font-size:11.5px;font-weight:400;line-height:1.45;letter-spacing:0;text-align:left;white-space:normal}
  .qhint.open .qtip{display:block}
  .wk.wdown{opacity:.82}
  .wk.wfrozen{opacity:.5}
  .wk.wcur{border-top-color:var(--accent)}
  .wk.wcur .wn{color:var(--accent)}
  .wk .wlock{font-size:11px;margin-left:1px;opacity:.8}
  .wsi.qs{color:var(--accent)}
  .wfz{color:var(--ok);font-size:11px}
  .phasehdr{font-family:var(--serif);font-weight:600;font-size:16px;margin:22px 0 2px;
    display:flex;justify-content:space-between;align-items:baseline;gap:12px}
  /* objectives manager + re-plan diff */
  .objs{display:flex;flex-direction:column;gap:7px;margin:6px 0 12px}
  .obj{display:flex;align-items:center;gap:10px;font-size:13px;
    border:1px solid var(--line);border-radius:9px;padding:8px 11px}
  .obj .pr{font-family:var(--mono);font-size:10px;font-weight:600;width:18px;height:18px;
    display:flex;align-items:center;justify-content:center;border-radius:5px;flex:none;
    background:var(--accent);color:var(--onacc)}
  .obj .pr.B,.obj .pr.C{background:var(--surface-2);color:var(--muted);border:1px solid var(--line)}
  /* inline A|B|C priority selector (private console) — the selected letter is accented, the rest dim */
  .prsel{display:inline-flex;flex:none;border:1px solid var(--line);border-radius:6px;overflow:hidden}
  .prseg{font-family:var(--mono);font-size:10px;font-weight:600;width:18px;height:18px;line-height:1;
    display:flex;align-items:center;justify-content:center;cursor:pointer;border:none;
    border-left:1px solid var(--line);background:var(--surface-2);color:var(--muted)}
  .prseg:first-child{border-left:none}
  .prseg:hover:not(.on){background:var(--surface);color:var(--text)}
  .prseg.on{background:var(--accent);color:var(--onacc);cursor:default}
  .obj .od{font-family:var(--mono);font-size:11px;color:var(--muted);margin-left:auto}
  .obj .x{cursor:pointer;color:var(--muted);border:1px solid var(--line);border-radius:6px;
    padding:2px 8px;font-size:11px;background:none}
  .obj .x:hover{color:var(--danger);border-color:var(--danger)}
  .obj.anchor{border-color:var(--accent)}
  .addobj{display:flex;flex-wrap:wrap;gap:7px;align-items:center;margin-bottom:6px}
  .addobj input,.addobj select{font-family:var(--sans);font-size:12px;color:var(--text);
    background:var(--surface-2);border:1px solid var(--line);border-radius:7px;padding:6px 8px}
  /* natural-language objective parse (LLM, §6c) */
  .nlobj{display:flex;flex-wrap:wrap;gap:7px;align-items:center;margin:10px 0 8px}
  .nlobj input{font-family:var(--sans);font-size:12px;color:var(--text);min-width:240px;
    background:var(--surface-2);border:1px solid var(--line);border-radius:7px;padding:6px 8px}
  #ao_parse{font-size:12px;padding:6px 11px;border:1px solid color-mix(in oklab,var(--accent),transparent 40%);
    color:var(--accent);background:color-mix(in oklab,var(--accent),transparent 92%)}
  #ao_parse:hover{border-color:var(--accent)}
  .nlinterp{font-size:11.5px;color:var(--muted);flex-basis:100%}
  .nlinterp.guess{color:var(--warn)} .nlinterp.err{color:var(--danger)}
  /* multi-objective conflict adjudication (LLM advises, engine periodizes, §6c) */
  .conflictrow{margin:10px 0}
  #adjBtn{font-size:12px;padding:6px 11px;border:1px solid color-mix(in oklab,var(--warn),transparent 40%);
    color:var(--warn);background:color-mix(in oklab,var(--warn),transparent 90%)}
  #adjBtn:hover{border-color:var(--warn)}
  .adjudbox{border:1px solid var(--line);border-radius:12px;padding:13px 16px;margin-top:10px;
    background:color-mix(in oklab,var(--warn),transparent 95%)}
  .adjudbox .exh{font-family:var(--serif);font-weight:600;font-size:14px;margin-bottom:8px}
  .adjudbox .expts{margin:0;padding-left:18px;font-size:13px;line-height:1.5}
  .adjudbox .expts li{margin-bottom:8px}
  .adjudbox .exfoot{font-family:var(--mono);font-size:9.5px;color:var(--muted);margin-top:8px;opacity:.7}
  /* qualitative adjustment (LLM proposes, engine clamps, §6c) */
  .adjbox,.adjmed{border-radius:10px;padding:11px 14px;margin:12px 0;font-size:13px;line-height:1.5}
  .adjbox{border:1px solid var(--accent);background:color-mix(in oklab,var(--accent),transparent 92%)}
  .adjmed{border:1px solid var(--danger);background:color-mix(in oklab,var(--danger),transparent 88%)}
  .adjh{font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--accent)}
  .adjmed .adjh{color:var(--danger)}
  .adjmeta{color:var(--muted);font-size:12px;margin-top:4px}
  .adjhint{color:var(--muted);font-size:11px;line-height:1.4;margin-top:5px}
  .adjhint b{color:var(--text);font-weight:600}
  .gradnote{margin:6px 0 2px;padding:8px 11px;border-radius:9px;font-size:12px;line-height:1.45;
    color:var(--text);border:1px solid color-mix(in oklab,var(--ok),transparent 60%);
    background:color-mix(in oklab,var(--ok),transparent 90%)}
  .adjclamp{font-family:var(--mono);font-size:10.5px;color:var(--muted);margin-top:5px}
  .adjclamp.err{color:var(--danger)}
  .adjask{display:flex;flex-wrap:wrap;gap:7px;align-items:center;margin:12px 0 4px}
  .adjask input{flex:1;min-width:240px;font-family:var(--sans);font-size:12px;color:var(--text);
    background:var(--surface-2);border:1px solid var(--line);border-radius:7px;padding:6px 8px}
  #adj_propose{font-size:12px;padding:6px 11px;border:1px solid color-mix(in oklab,var(--accent),transparent 40%);
    color:var(--accent);background:color-mix(in oklab,var(--accent),transparent 92%)}
  #adj_propose:hover{border-color:var(--accent)}
  .adjpreview{flex-basis:100%}
  .adjprop{border:1px dashed var(--line);border-radius:9px;padding:9px 12px;font-size:12.5px;margin-top:4px}
  .eased{color:var(--accent);font-size:11px}
  /* plan explanation (LLM narrates the engine's numbers, §6c) */
  .explainrow{margin:12px 0 0}
  #explainBtn{font-size:12px;padding:6px 11px;border:1px solid color-mix(in oklab,var(--accent),transparent 40%);
    color:var(--accent);background:color-mix(in oklab,var(--accent),transparent 92%)}
  #explainBtn:hover{border-color:var(--accent)}
  .explainbox{border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-top:10px;
    background:color-mix(in oklab,var(--accent),transparent 95%)}
  .explainbox .exh{font-family:var(--serif);font-weight:600;font-size:15px;margin-bottom:8px}
  .explainbox .expts{margin:0;padding-left:18px;font-size:13px;line-height:1.6}
  .explainbox .exchange{margin-top:10px;padding-top:9px;border-top:1px solid var(--line);font-size:12.5px}
  .explainbox .exfoot{font-family:var(--mono);font-size:9.5px;color:var(--muted);margin-top:9px;opacity:.7}
  .diff{border:1px solid var(--accent);border-radius:10px;padding:11px 14px;margin:12px 0;
    background:color-mix(in oklab,var(--accent),transparent 92%)}
  .diff .dh{font-family:var(--mono);font-size:11px;color:var(--accent);text-transform:uppercase;letter-spacing:.1em}
  .diff ul{margin:6px 0 0;padding-left:18px;font-size:13px}
  .diff li{margin:2px 0}

  /* fitness/fatigue trend (reconstructed by the projector) */
  .ff{width:100%;height:200px;display:block;overflow:visible}
  .ff .ctl{fill:none;stroke:var(--accent);stroke-width:2}
  .ff .atl{fill:none;stroke:var(--muted);stroke-width:1.5;stroke-dasharray:4 3}
  .ff .zero{stroke:var(--line);stroke-width:1}
  .ff .axis{font-family:var(--mono);font-size:9px;fill:var(--muted)}
  .legend{display:flex;gap:18px;margin-bottom:10px;font-family:var(--mono);font-size:11px;color:var(--muted)}
  .legend i{display:inline-block;width:14px;height:0;border-top:2px solid currentColor;vertical-align:middle;margin-right:6px}
  .legend .ctl{color:var(--accent)} .legend .atl{color:var(--muted)}
  .valid{font-family:var(--mono);font-size:10px;color:var(--muted);margin-top:10px}
  .valid b{color:var(--ok)}
  .ff{cursor:crosshair}
  .ff .cross{stroke:var(--muted);stroke-width:1;stroke-dasharray:3 3;opacity:.7}
  .ffdot{stroke:var(--bg);stroke-width:1.5}
  .ffdot.ctl{fill:var(--accent)} .ffdot.atl{fill:var(--muted)}
  #ffchart{position:relative}
  .fftip{position:absolute;top:0;pointer-events:none;background:var(--surface-2);
    border:1px solid var(--line);border-radius:8px;padding:7px 10px;font-size:11px;
    display:flex;flex-direction:column;gap:1px;z-index:3;box-shadow:0 6px 18px rgba(0,0,0,.35)}
  .fftip b{font-family:var(--mono);font-size:10px;color:var(--muted);margin-bottom:2px}
  .fftip .t-ctl{color:var(--accent);font-weight:600}
  .fftip .t-atl{color:var(--text)}

  /* effort discipline (§6m) — are your easy days actually easy? */
  .effort-head{display:flex;gap:18px;align-items:center;margin-bottom:14px}
  .effort-score{font-family:var(--serif);font-weight:600;font-size:46px;line-height:1;flex:none}
  .effort-score .pct{font-size:20px;opacity:.7}
  .effort-cap .big{font-family:var(--serif);font-size:16px;margin-bottom:3px}
  .effort-cap .muted{font-size:12px;line-height:1.5}
  table.efftbl{border-collapse:collapse;width:100%;font-size:12.5px}
  table.efftbl th{text-align:left;font-family:var(--mono);font-size:9px;letter-spacing:.1em;
    text-transform:uppercase;color:var(--muted);padding:4px 8px;border-bottom:1px solid var(--line)}
  table.efftbl td{padding:5px 8px;border-bottom:1px solid var(--line)}
  /* plan drift — the original road vs the road as it stands (§6b made visible) */
  /* scorecard — the four series synthesized into one 'who's winning' verdict (§6j) */
  .scorecard{border:1px solid var(--line);border-radius:12px;padding:15px 18px;margin-bottom:22px;
    background:linear-gradient(180deg,var(--surface),var(--surface-2))}
  .scorecard .sc-head{font-family:var(--mono);font-size:9.5px;letter-spacing:.18em;text-transform:uppercase;
    color:var(--muted);margin-bottom:12px}
  .scorecard .sc-rows{display:flex;flex-wrap:wrap;gap:8px 26px;margin-bottom:13px}
  .scorecard .sc-row{display:flex;flex-direction:column;gap:2px;min-width:150px}
  .scorecard .sc-k{font-family:var(--mono);font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
  .scorecard .sc-v{font-family:var(--serif);font-weight:600;font-size:16px;line-height:1.15}
  .scorecard .sc-v.ahead{color:var(--ok)} .scorecard .sc-v.behind{color:var(--danger)}
  .scorecard .sc-v.level,.scorecard .sc-v.unknown{color:var(--muted)}
  .scorecard .sc-v .sub{font-family:var(--mono);font-size:9.5px;font-weight:400;color:var(--muted);letter-spacing:0}
  .scorecard .sc-verdict{font-family:var(--serif);font-size:14.5px;line-height:1.45;color:var(--text);
    padding-top:11px;border-top:1px solid var(--line)}
  .scorecard .sc-verdict .wks{font-family:var(--mono);font-size:10px;color:var(--muted)}
  .driftcap{font-family:var(--mono);font-size:10px;color:var(--muted);margin-bottom:20px;line-height:1.5}
  .driftcap .warn{color:var(--warn)}
  .driftblock{margin-bottom:26px} .driftblock:last-child{margin-bottom:0}
  .driftblock h3{font-family:var(--serif);font-size:15px;font-weight:600;margin:0 0 2px}
  .driftblock .note{font-size:11.5px;color:var(--muted);margin:0 0 9px;max-width:62ch}
  .driftwrap{position:relative}
  .drift{width:100%;height:170px;display:block;overflow:visible;cursor:crosshair}
  .drift .dl{fill:none;stroke-width:2}
  .drift .dl.init{stroke:var(--muted);stroke-width:1.5}
  .drift .dl.actual{stroke:var(--accent)}
  .drift .dl.proj{stroke:var(--accent)}
  .drift .grid{stroke:var(--line);stroke-width:1;opacity:.45}
  .drift .now{stroke:var(--muted);stroke-width:1;stroke-dasharray:2 3;opacity:.65}
  .drift .axis{font-family:var(--mono);font-size:9px;fill:var(--muted)}
  .drift .cross{stroke:var(--muted);stroke-width:1;stroke-dasharray:3 3;opacity:.7}
  .drift .ddot{stroke:var(--bg);stroke-width:1.5;fill:var(--accent)}
  .drifttip{position:absolute;top:0;pointer-events:none;background:var(--surface-2);
    border:1px solid var(--line);border-radius:8px;padding:7px 10px;font-size:11px;
    display:none;flex-direction:column;gap:1px;z-index:3;box-shadow:0 6px 18px rgba(0,0,0,.35)}
  .drifttip b{font-family:var(--mono);font-size:10px;color:var(--muted);margin-bottom:2px}

  /* health markers — small multiples */
  .hgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
  @media(max-width:760px){.hgrid{grid-template-columns:1fr}}
  .hcard{background:linear-gradient(180deg,var(--surface),var(--surface-2));
    border:1px solid var(--line);border-radius:12px;padding:14px 16px}
  .hcard .hk{font-family:var(--mono);font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
  .hcard .hv{font-family:var(--serif);font-weight:600;font-size:24px;margin-top:3px}
  .hcard .hv small{font-size:12px;color:var(--muted);font-family:var(--sans);font-weight:400}
  .hcard .hv .flag{font-family:var(--mono);font-size:10px;padding:2px 7px;border-radius:20px;margin-left:6px;vertical-align:middle}
  .flag.ok{color:var(--ok);border:1px solid color-mix(in oklab,var(--ok),transparent 60%)}
  .flag.bad{color:var(--danger);border:1px solid color-mix(in oklab,var(--danger),transparent 55%)}
  .hcard svg{display:block;width:100%;height:54px;margin-top:8px;overflow:visible}
  .spark{fill:none;stroke:var(--accent);stroke-width:2}
  .sparkdot{fill:var(--accent)}
  .refband{fill:color-mix(in oklab,var(--ok),transparent 86%)}
  .refline{stroke:color-mix(in oklab,var(--ok),transparent 45%);stroke-width:1;stroke-dasharray:3 3}
  .hform{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:14px}
  .hform select,.hform input{font-family:var(--sans);font-size:13px;color:var(--text);
    background:var(--surface-2);border:1px solid var(--line);border-radius:8px;padding:7px 10px}
  .hform input{width:110px}
  /* Settings panel — one labelled row per setting, full-width inputs/textarea */
  .setform{display:flex;flex-direction:column;gap:16px;margin-top:6px}
  .setrow label{display:block;font-size:13px;font-weight:600;margin-bottom:4px}
  .setrow .src{font-family:var(--mono);font-size:9px;letter-spacing:.1em;text-transform:uppercase;
    color:var(--muted);font-weight:400;margin-left:8px}
  .setrow input,.setrow textarea{width:100%;box-sizing:border-box;font-family:var(--sans);
    font-size:13px;color:var(--text);background:var(--surface-2);border:1px solid var(--line);
    border-radius:8px;padding:8px 10px}
  .setrow textarea{min-height:64px;resize:vertical;line-height:1.5}
  .setrow .help{font-size:11px;color:var(--muted);margin-top:4px;line-height:1.5}
  .setrow .err{color:#c0392b;font-size:11px;margin-top:4px}
  .setbar{display:flex;align-items:center;gap:12px;margin-top:4px}
  .setbar .ok{color:var(--accent);font-size:12px}
  /* Settings modal — native <dialog>, centered, backdrop-dimmed */
  dialog.modal{border:none;border-radius:16px;padding:0;width:min(560px,92vw);max-height:86vh;
    background:var(--surface);color:var(--text);box-shadow:0 24px 64px rgba(0,0,0,.4);overflow:hidden;
    display:flex;flex-direction:column}
  dialog.modal::backdrop{background:rgba(0,0,0,.55);backdrop-filter:blur(2px)}
  .modal-head{flex:none;display:flex;align-items:center;justify-content:space-between;gap:12px;
    padding:18px 22px;border-bottom:1px solid var(--line);background:var(--surface)}
  .modal-head h2{margin:0}
  .modal-x{background:none;border:none;color:var(--muted);font-size:18px;cursor:pointer;
    line-height:1;padding:4px 8px;border-radius:8px}
  .modal-x:hover{color:var(--text);background:var(--surface-2)}
  #settings{padding:20px 22px;overflow:auto;flex:1 1 auto;min-height:0}
  /* Weather city picker — chips + typeahead, replaces the raw lat/lon string */
  .wxchips{display:flex;flex-wrap:wrap;gap:8px;margin:2px 0 8px}
  .wxchip{display:inline-flex;align-items:center;gap:6px;background:var(--surface-2);
    border:1px solid var(--line);border-radius:999px;padding:4px 6px 4px 11px;font-size:13px}
  .wxchip b{font-family:var(--mono);font-size:10px;letter-spacing:.08em;color:var(--muted)}
  .wxchip button{background:none;border:none;color:var(--muted);cursor:pointer;font-size:14px;
    line-height:1;padding:0 2px;border-radius:6px}
  .wxchip button:hover{color:#c0392b}
  .wxsearchrow{display:flex;gap:8px}
  .wxsearchrow input{flex:1}
  .wxresults{list-style:none;margin:6px 0 0;padding:0;border:1px solid var(--line);border-radius:8px;
    overflow:hidden}
  .wxresults:empty{display:none}
  .wxresults li{padding:8px 11px;cursor:pointer;font-size:13px;border-top:1px solid var(--line)}
  .wxresults li:first-child{border-top:none}
  .wxresults li:hover{background:var(--surface-2)}
  .wxresults .sub{color:var(--muted);font-size:11px}
  .muted{color:var(--muted)} .mono{font-family:var(--mono)}
  .empty{color:var(--muted);font-style:italic;padding:8px 0}
  footer{margin-top:48px;font-family:var(--mono);font-size:10px;letter-spacing:.12em;
    text-transform:uppercase;color:var(--muted);text-align:center;
    display:flex;flex-direction:column;align-items:center;gap:13px}
  /* Runalyze attribution — the wordmark inherits currentColor (var(--text)) so it adapts per theme;
     the brand icon keeps its colours. Subtle by default, full-strength on hover. */
  .ralink{display:inline-flex;align-items:center;gap:8px;color:var(--text);text-decoration:none;
    opacity:.65;transition:opacity .15s}
  .ralink:hover{opacity:1}
  .ralink svg{height:15px;width:auto;display:block}
</style></head>
<body>
  __SH_HUBLINK__
  <div class="topctl">
    <span class="themes" id="themes">
      <button class="swatch" data-theme="light" title="Daylight" style="--sw:linear-gradient(90deg,#f4f1ea 50%,#b9542c 50%)"></button>
      <button class="swatch" data-theme="dark"  title="Charcoal" style="--sw:linear-gradient(90deg,#191a1d 50%,#fa7d42 50%)"></button>
      <button class="swatch" data-theme="aurora" title="Aurora" style="--sw:linear-gradient(90deg,#121226 50%,#7b61ff 50%)"></button>
    </span>
  </div>
  <div class="wrap">
    <header>
      <div class="brand">
        <span class="dotmark"><svg class="" viewBox="0 0 100 100" aria-hidden="true"><circle cx="50" cy="50" r="46" fill="none" stroke="var(--text)" stroke-width="1.1" opacity=".22"/><path d="M50,50 L47.5,30 L50,11 L52.5,30 Z" fill="var(--text)" opacity=".28" transform="rotate(45 50 50)"/><path d="M50,50 L47.5,30 L50,11 L52.5,30 Z" fill="var(--text)" opacity=".28" transform="rotate(135 50 50)"/><path d="M50,50 L47.5,30 L50,11 L52.5,30 Z" fill="var(--text)" opacity=".28" transform="rotate(225 50 50)"/><path d="M50,50 L47.5,30 L50,11 L52.5,30 Z" fill="var(--text)" opacity=".28" transform="rotate(315 50 50)"/><path d="M50.0,16.0 L51.2,16.1 L52.3,16.4 L53.5,16.9 L54.5,17.6 L55.5,18.5 L56.5,19.6 L57.3,20.9 L57.9,22.3 L58.5,23.8 L58.9,25.5 L59.2,27.3 L59.3,29.2 L59.2,31.2 L58.9,33.2 L58.5,35.3 L57.9,37.4 L57.1,39.4 L56.2,41.5 L55.1,43.5 L53.8,45.5 L52.4,47.4 L50.8,49.1 L49.1,50.8 L47.4,52.4 L45.5,53.8 L43.5,55.1 L41.5,56.2 L39.4,57.1 L37.4,57.9 L35.3,58.5 L33.2,58.9 L31.2,59.2 L29.2,59.3 L27.3,59.2 L25.5,58.9 L23.8,58.5 L22.3,57.9 L20.9,57.3 L19.6,56.5 L18.5,55.5 L17.6,54.5 L16.9,53.5 L16.4,52.3 L16.1,51.2 L16.0,50.0 L16.1,48.8 L16.4,47.7 L16.9,46.5 L17.6,45.5 L18.5,44.5 L19.6,43.5 L20.9,42.7 L22.3,42.1 L23.8,41.5 L25.5,41.1 L27.3,40.8 L29.2,40.7 L31.2,40.8 L33.2,41.1 L35.3,41.5 L37.4,42.1 L39.4,42.9 L41.5,43.8 L43.5,44.9 L45.5,46.2 L47.4,47.6 L49.1,49.2 L50.8,50.9 L52.4,52.6 L53.8,54.5 L55.1,56.5 L56.2,58.5 L57.1,60.6 L57.9,62.6 L58.5,64.7 L58.9,66.8 L59.2,68.8 L59.3,70.8 L59.2,72.7 L58.9,74.5 L58.5,76.2 L57.9,77.7 L57.3,79.1 L56.5,80.4 L55.5,81.5 L54.5,82.4 L53.5,83.1 L52.3,83.6 L51.2,83.9 L50.0,84.0 L48.8,83.9 L47.7,83.6 L46.5,83.1 L45.5,82.4 L44.5,81.5 L43.5,80.4 L42.7,79.1 L42.1,77.7 L41.5,76.2 L41.1,74.5 L40.8,72.7 L40.7,70.8 L40.8,68.8 L41.1,66.8 L41.5,64.7 L42.1,62.6 L42.9,60.6 L43.8,58.5 L44.9,56.5 L46.2,54.5 L47.6,52.6 L49.2,50.9 L50.9,49.2 L52.6,47.6 L54.5,46.2 L56.5,44.9 L58.5,43.8 L60.6,42.9 L62.6,42.1 L64.7,41.5 L66.8,41.1 L68.8,40.8 L70.8,40.7 L72.7,40.8 L74.5,41.1 L76.2,41.5 L77.7,42.1 L79.1,42.7 L80.4,43.5 L81.5,44.5 L82.4,45.5 L83.1,46.5 L83.6,47.7 L83.9,48.8 L84.0,50.0 L83.9,51.2 L83.6,52.3 L83.1,53.5 L82.4,54.5 L81.5,55.5 L80.4,56.5 L79.1,57.3 L77.7,57.9 L76.2,58.5 L74.5,58.9 L72.7,59.2 L70.8,59.3 L68.8,59.2 L66.8,58.9 L64.7,58.5 L62.6,57.9 L60.6,57.1 L58.5,56.2 L56.5,55.1 L54.5,53.8 L52.6,52.4 L50.9,50.8 L49.2,49.1 L47.6,47.4 L46.2,45.5 L44.9,43.5 L43.8,41.5 L42.9,39.4 L42.1,37.4 L41.5,35.3 L41.1,33.2 L40.8,31.2 L40.7,29.2 L40.8,27.3 L41.1,25.5 L41.5,23.8 L42.1,22.3 L42.7,20.9 L43.5,19.6 L44.5,18.5 L45.5,17.6 L46.5,16.9 L47.7,16.4 L48.8,16.1 L50.0,16.0 Z" fill="color-mix(in oklab,var(--accent),transparent 86%)" stroke="color-mix(in oklab,var(--accent),transparent 50%)" stroke-width="2.6" stroke-linejoin="round" stroke-linecap="round"/><path d="M50,4 L52.3,9.6 L50,7.7 L47.7,9.6 Z" fill="var(--accent)"/><circle cx="50" cy="50" r="3.2" fill="var(--accent)"/></svg></span>
        <p class="eyebrow">Running</p>
        <div class="titlerow">
          <h1>Sparing Horse</h1>
          <span class="motto">Νενικήκαμεν&nbsp;<small>· we have won</small></span>
        </div>
      </div>
      <div class="bar">
        <button class="primary" id="syncBtn">Sync now</button>
        <button class="primary" id="settingsBtn" title="Personalization — athlete context, weather cities, links, sync timezone">⚙ Settings</button>
        <button class="ghost" id="backfillBtn" title="One-time full-history pull — walks every page back to your first activity. Use on a fresh machine or if old history is missing.">Backfill all</button>
      </div>
    </header>

    <div id="dqbanner"></div>
    <div id="objbar" class="objbar"></div>
    <div class="grid" id="tiles"><div class="empty">Loading current shape…</div></div>

    <div class="section">
      <div class="panel" id="recent"><div class="empty">Loading latest activity…</div></div>
    </div>

    <div class="section" id="sec-readiness">
      <h2>Today's readiness <span class="muted mono" style="font-size:12px">— should you run today's session?</span></h2>
      <div class="rgrid">
        <div id="readiness"><div class="empty">Loading…</div></div>
        <div class="panel acwrcard">
          <div class="acwr-title">Acute : chronic load <span class="muted mono" style="font-size:10px;font-weight:400">— stay in the green band</span></div>
          <div class="gauge" id="gauge"><div class="band" id="gband"></div><div class="mark" id="gmark"></div><div class="gyou" id="gyou"></div></div>
          <div class="gauge-scale"><span>0.0</span><span><b>0.8</b></span><span><b>1.3</b></span><span>2.0</span></div>
          <p class="muted" style="font-size:11px;margin:10px 0 0;line-height:1.5">Fatigue ÷ fitness. The shaded band (0.8–1.3) is the sweet spot — below it you're detraining, above it injury risk rises.</p>
          <div class="acwr-foot" id="acwrFoot"><div class="tilebg" id="acwrRampBg"></div><div class="acwr-foot-txt" id="acwrFootTxt"></div><div class="tilecap" id="acwrRampBgcap"></div></div>
        </div>
      </div>
    </div>

    <div class="section">
      <h2>Training plan <span class="muted mono" style="font-size:12px">— objective-driven, bounded by your fitness</span>
        <button class="primary" id="planBtn" style="float:right;font-size:12px;padding:6px 12px">Generate plan</button></h2>
      <div class="panel" id="plan"><div class="empty">No plan yet — hit <b>Generate plan</b>.</div></div>
    </div>

    <div class="section" id="sec-effort">
      <h2>Effort discipline <span class="muted mono" style="font-size:12px">— are your easy days actually easy?</span></h2>
      <div class="panel" id="effort"><div class="empty">Loading…</div></div>
    </div>

    <details class="section" id="sec-drift">
      <summary><h2>Plan drift <span class="muted mono" style="font-size:12px">— how far the road has moved from its founding statement</span></h2></summary>
      <div class="panel" id="drift"><div class="empty">Loading…</div></div>
    </details>

    <details class="section" id="sec-ff">
      <summary><h2>Fitness &amp; fatigue <span class="muted mono" style="font-size:12px">— reconstructed from your training load (CTL/ATL model)</span></h2></summary>
      <div class="panel">
        <div class="legend">
          <span class="ctl"><i></i>Fitness (CTL)</span>
          <span class="atl"><i></i>Fatigue (ATL)</span>
        </div>
        <div id="ffchart"><div class="empty">No activities synced yet.</div></div>
        <div class="valid" id="ffvalid"></div>
      </div>
    </details>

    <details class="section" id="sec-vol">
      <summary><h2>Weekly running volume <span class="muted mono" id="wkrange" style="font-size:12px">— last 26 weeks</span></h2></summary>
      <div class="panel"><div class="chart" id="chart"><div class="empty">No activities synced yet.</div></div></div>
    </details>

    <div class="section" id="sec-health">
      <h2>Health markers <span class="muted mono" style="font-size:12px">— metabolism &amp; the body behind the engine</span></h2>
      <div class="hgrid" id="health"><div class="empty">No markers yet — add one below.</div></div>
      <form class="hform" id="hform">
        <select id="hmarker" required></select>
        <input id="hdate" type="date" required>
        <input id="hvalue" type="number" step="any" placeholder="value" required>
        <button class="primary" type="submit">Add reading</button>
      </form>
    </div>

    <dialog id="settingsDialog" class="modal">
      <div class="modal-head">
        <h2>Settings <span class="muted mono" style="font-size:12px">— personalization, stored in your database (overrides env)</span></h2>
        <button class="modal-x" id="settingsClose" aria-label="Close settings">✕</button>
      </div>
      <div id="settings"><div class="empty">Loading…</div></div>
    </dialog>

    <footer>
      <span id="foot">Spares the horse by being the horse · not synced yet</span>
      <a class="ralink" href="https://runalyze.com" target="_blank" rel="noopener noreferrer"
         title="Sparing Horse runs on your Runalyze training data">powered by __RUNALYZE_LOGO__</a>
    </footer>
  </div>

<script>
const SH_READONLY = __SH_READONLY__;   // public read-only mode (server-injected)
const SH_PRIVATE_URL = __SH_PRIVATE_URL__;   // public→private console link (optional, JSON-injected)
const $ = s => document.querySelector(s);
const fmt = (n, d=1) => (n==null ? "—" : Number(n).toFixed(d));
// Per-workout calendar date, e.g. "Jun 23 - Tue" — so the runner can schedule life around the plan.
const sessDate = iso => { if(!iso) return ""; const d=new Date(iso+"T00:00:00");
  return d.toLocaleDateString("en-US",{month:"short",day:"numeric"})+" - "+d.toLocaleDateString("en-US",{weekday:"short"}); };
const getJSON = (url, opts) => fetch(url, opts).then(r => r.json());   // fetch + parse; callers keep their own try/catch
const esc = s => String(s==null?"":s).replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));  // HTML-escape before innerHTML

// theme switcher
const themes = $("#themes");
function paintTheme(){ const t=document.documentElement.dataset.theme;
  themes.querySelectorAll("button").forEach(b=>b.setAttribute("aria-pressed", b.dataset.theme===t)); }
themes.addEventListener("click", e=>{ const b=e.target.closest("button"); if(!b)return;
  document.documentElement.dataset.theme=b.dataset.theme;
  try{localStorage.setItem("sh-theme",b.dataset.theme)}catch(e){} paintTheme(); });
paintTheme();

function tile(k, v, unit, sub, cls, desc, bg){
  return `<div class="tile${bg?' hascap':''}" ${desc?`title="${desc}"`:""}>
    ${bg?`<div class="tilebg" id="${bg}"></div>`:""}
    <div class="k">${k}${desc?' <span class="info">ⓘ</span>':''}</div>
    <div class="v">${v}${unit?`<small> ${unit}</small>`:""}</div>
    ${sub?`<div class="sub ${cls||""}">${sub}</div>`:""}
    ${bg?`<div class="tilecap" id="${bg}cap"></div>`:""}</div>`;
}
function monYr(iso){ const d=new Date(iso+"T00:00:00");
  return d.toLocaleDateString(undefined,{month:"short",year:"2-digit"}); }
// paint a metric's trend as a tile background + set its timeframe caption (higher = taller)
function paintTrend(id, vals, dates, captionTitle){
  const bg=document.getElementById(id); if(!bg || vals.length<2) return;
  const built=buildProfilePath(vals,{W:1000,H:120}); if(!built) return;
  bg.innerHTML=`<svg viewBox="0 0 1000 120" preserveAspectRatio="none"><path d="${built.path}" class="proffill"/></svg>`;
  bg.classList.add("on");
  const cap=document.getElementById(id+"cap");
  if(cap){ cap.textContent=`6-mo trend · ${monYr(dates[0])}–${monYr(dates[dates.length-1])}`; cap.title=captionTitle; }
}
async function drawVo2maxTrend(){
  if(!document.getElementById("vo2bg")) return;
  try{
    const d=await getJSON("/api/vo2max?months=6");
    const pts=d.points||[];
    paintTrend("vo2bg", pts.map(p=>p.vo2max), pts.map(p=>p.date),
      "Background: your per-run VO₂max estimates over the last 6 months, lightly smoothed.");
  }catch(e){}
}
async function drawShapeTrends(){   // Fitness/Fatigue/Form from the reconstructed CTL/ATL/TSB curve
  if(!document.getElementById("ctlbg")) return;
  try{
    const d=await getJSON("/api/projector?days=180");
    const h=d.history||[], dts=h.map(p=>p.date);
    paintTrend("ctlbg", h.map(p=>p.ctl), dts, "Background: reconstructed Fitness (CTL) over the last 6 months.");
    paintTrend("atlbg", h.map(p=>p.atl), dts, "Background: reconstructed Fatigue (ATL) over the last 6 months.");
    paintTrend("tsbbg", h.map(p=>p.tsb), dts, "Background: reconstructed Form (TSB) over the last 6 months.");
    // CTL ramp — weekly Δ fitness from the reconstructed daily curve (chronic-load tile footer).
    // Build a daily ramp series (ctl[i] − ctl[i−7]) for a background sparkline, like the other tiles.
    const af=document.getElementById("acwrFoot"), aft=document.getElementById("acwrFootTxt");
    if(af && aft){
      const c=h.filter(p=>p.ctl!=null);
      const ramps=[], rdts=[];
      for(let k=7;k<c.length;k++){ ramps.push(c[k].ctl - c[k-7].ctl); rdts.push(c[k].date); }
      if(ramps.length){
        const ramp=ramps[ramps.length-1];
        const cap = ramp>1 ? "fitness building" : ramp<-1 ? "easing — recovering" : "holding steady";
        aft.innerHTML = `<span class="k">CTL ramp · 7-day</span>`+
          `<span class="v">${ramp>=0?"+":""}${ramp.toFixed(1)} <small>/ wk</small></span>`+
          `<span class="cap">${cap}</span>`;
        paintTrend("acwrRampBg", ramps, rdts, "Background: 7-day CTL ramp (weekly Δ fitness) over the last 6 months.");
        af.style.display="";
      } else af.style.display="none";
    }
  }catch(e){}
}

async function loadShape(){
  const r = await fetch("/api/shape"); const d = await r.json();
  const s = d.latest;
  const tiles = $("#tiles");
  if(!s){ tiles.innerHTML = `<div class="empty">No shape snapshot yet — hit <b>Sync now</b>.</div>`; return; }
  const prog = s.effective_vo2max_progress;
  const progTxt = prog==null ? "" : `${prog>=0?"▲":"▼"} ${fmt(Math.abs(prog),2)} trend`;
  tiles.innerHTML =
    tile("Effective VO₂max", fmt(s.effective_vo2max,1), "ml/kg/min", progTxt, prog>=0?"up":"down",
      "Maximal oxygen uptake Runalyze estimates from your HR–pace relationship. The single best correlate of endurance performance. Higher is fitter.", "vo2bg") +
    tile("Fitness", fmt(s.fitness,0), "CTL", s.fitness_pct!=null?`${fmt(s.fitness_pct,0)}% of your all-time max`:"",null,
      "CTL (Chronic Training Load): a 42-day weighted average of daily training load (TRIMP). Your built-up aerobic fitness — slow to gain, slow to lose.", "ctlbg") +
    tile("Fatigue", fmt(s.fatigue,0), "ATL", "acute load",null,
      "ATL (Acute Training Load): a 7-day weighted average of training load. Your recent fatigue — rises and falls fast.", "atlbg") +
    tile("Form", fmt(s.performance,0), "TSB", "fitness − fatigue",null,
      "TSB (Training Stress Balance) = Fitness − Fatigue. Positive = fresh/tapered; negative = loaded/building. Near a race you want it positive.", "tsbbg");
  // ACWR gauge: value is a RATIO; band 0.8–1.3 on a 0–2 scale
  const acwr = s.acwr==null ? null : Number(s.acwr);
  const pct = x => Math.max(0,Math.min(100, x/2*100));
  $("#gband").style.left = pct(0.8)+"%"; $("#gband").style.width = (pct(1.3)-pct(0.8))+"%";
  if(acwr!=null){
    const L = pct(acwr);
    $("#gmark").style.left = L+"%";
    const inb = acwr>=0.8 && acwr<=1.3;
    $("#gyou").style.left = L+"%";
    $("#gyou").innerHTML = `<span class="${inb?'inb':'out'}">you: ${acwr.toFixed(2)}</span>`;
  }
  if(d.last_sync){ const dt=new Date(d.last_sync);
    $("#foot").textContent = `Spares the horse by being the horse · synced ${dt.toLocaleString()}`; }
  // data-quality: flag likely-duplicate activities (inflate Runalyze's fitness/fatigue too)
  const dq=$("#dqbanner");
  let dqhtml="";
  if(d.duplicate_count>0){
    const dl=d.duplicates||[];
    // direct 🗑 per leftover row, so an OLD dup (not the latest activity) is still reachable
    const del=(SH_READONLY||!dl.length)?"":` Already deleted on Runalyze but this persists? `+
      `Sync never removes the local copy — drop the leftover row: `+
      dl.map(r=>`<a href="#" class="dupdel delact" data-id="${r.id}">🗑 ${r.date||("#"+r.id)}</a>`).join(", ")+`.`;
    dqhtml+=`<div class="dqwarn">⚠ ${d.duplicate_count} likely-duplicate ${d.duplicate_count>1?"activities":"activity"} detected `+
      `(same time, distance &amp; sport — e.g. a watch/Strava double-upload). These inflate Fitness/Fatigue/ACWR `+
      `<b>on Runalyze too</b> — delete the duplicate in Runalyze and re-sync to correct the tiles above. `+
      `(The fitness/fatigue chart already ignores them.)${del}</div>`;
  }
  const ign=d.ignored||[];
  if(ign.length){
    const undo=SH_READONLY?"":ign.map(r=>`<a href="#" class="ignundo" data-id="${r.id}">${r.date||("#"+r.id)}</a>`).join(", ");
    dqhtml+=`<div class="dqnote">⊘ ${ign.length} ${ign.length>1?"activities":"activity"} manually excluded from your stats`+
      (undo?` — restore: ${undo}`:"")+`.</div>`;
  }
  dq.innerHTML=dqhtml;
  dq.querySelectorAll(".dupdel").forEach(el=>el.addEventListener("click", async ev=>{
    ev.preventDefault();
    if(!confirm("Delete this leftover duplicate from your LOCAL copy?\n\nFor a dup you already removed on Runalyze that insert-only sync left behind. If it still exists on Runalyze it returns on the next sync.")) return;
    await fetch(`/api/activity/${el.dataset.id}/delete`,
      {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
    await Promise.all([loadShape(), loadActivity(CURACT), loadProjector()]);
  }));
  dq.querySelectorAll(".ignundo").forEach(el=>el.addEventListener("click", async ev=>{
    ev.preventDefault(); await toggleIgnore(el.dataset.id, false);
  }));
  drawVo2maxTrend();
  drawShapeTrends();
}

function isoWeekMonday(year, wk){
  const simple=new Date(Date.UTC(year,0,1+(wk-1)*7));
  const dow=simple.getUTCDay(); const monday=new Date(simple);
  monday.setUTCDate(simple.getUTCDate()-((dow+6)%7));
  return monday;
}
// Weekly volume: we hold the FULL history and show a fixed window you can grab-drag
// horizontally to pan back/forth through time (bar heights stay on a global scale so weeks
// are comparable across the whole span).
const WEEKLY_WIN=26;
let WEEKLY_ALL=[], WEEKLY_END=0, WEEKLY_MAX=1, WEEKLY_WIRED=false;
function weekLabel(wk){ const [y,w]=wk.split("-W").map(Number); const mon=isoWeekMonday(y,w);
  return mon.toLocaleDateString(undefined,{month:"short",year:"2-digit"}); }
function renderWeekly(){
  const chart=$("#chart");
  if(!WEEKLY_ALL.length){ chart.innerHTML=`<div class="empty">No activities synced yet.</div>`; return; }
  const win=Math.min(WEEKLY_WIN, WEEKLY_ALL.length);
  const start=Math.max(0, WEEKLY_END-win);
  const rows=WEEKLY_ALL.slice(start, WEEKLY_END);
  const bars=rows.map(x=>`<div class="col" title="${x.week}: ${x.km} km">
    <div class="vlbl">${x.km>=1?Math.round(x.km):""}</div>
    <div class="barb" style="height:${Math.round(x.km/WEEKLY_MAX*120)}px"></div>
  </div>`).join("");
  // month/year ruler below: mark where the month changes
  let lastMonth="", ticks="";
  rows.forEach((x,i)=>{
    const [y,w]=x.week.split("-W").map(Number);
    const mon=isoWeekMonday(y,w);
    const key=mon.getUTCFullYear()+"-"+mon.getUTCMonth();
    if(key!==lastMonth){ lastMonth=key;
      const lbl=mon.toLocaleDateString(undefined,{month:"short"})+
        (mon.getUTCMonth()===0?` '${String(mon.getUTCFullYear()).slice(2)}`:"");
      ticks+=`<span class="tick" style="left:${(i/rows.length*100).toFixed(2)}%">${lbl}</span>`;
    }
  });
  chart.innerHTML=`<div class="bars">${bars}</div><div class="ruler">${ticks}</div>`;
  const rng=$("#wkrange");
  if(rng && rows.length){
    const atEnd = WEEKLY_END>=WEEKLY_ALL.length;
    rng.textContent = `— ${weekLabel(rows[0].week)} → ${weekLabel(rows[rows.length-1].week)}`+
      (WEEKLY_ALL.length>win ? (atEnd?" · drag to pan back ‹" : " · ‹ drag ›") : "");
  }
}
function wireWeeklyDrag(){
  if(WEEKLY_WIRED) return; WEEKLY_WIRED=true;
  const chart=$("#chart");
  let startX=0, startEnd=0, dragging=false;
  chart.addEventListener("pointerdown", e=>{
    if(WEEKLY_ALL.length<=WEEKLY_WIN) return;
    dragging=true; startX=e.clientX; startEnd=WEEKLY_END;
    chart.classList.add("grabbing"); chart.setPointerCapture?.(e.pointerId); e.preventDefault();
  });
  chart.addEventListener("pointermove", e=>{
    if(!dragging) return;
    const pxPerWeek=chart.clientWidth/Math.min(WEEKLY_WIN,WEEKLY_ALL.length);
    const dw=Math.round((e.clientX-startX)/pxPerWeek);   // drag right → older weeks
    const lo=Math.min(WEEKLY_WIN, WEEKLY_ALL.length);
    let ne=Math.max(lo, Math.min(WEEKLY_ALL.length, startEnd-dw));
    if(ne!==WEEKLY_END){ WEEKLY_END=ne; renderWeekly(); }
  });
  const end=()=>{ dragging=false; chart.classList.remove("grabbing"); };
  chart.addEventListener("pointerup", end);
  chart.addEventListener("pointercancel", end);
  // mouse wheel / trackpad: scroll down or right → back in time, up or left → forward
  let wheelAcc=0;
  chart.addEventListener("wheel", e=>{
    if(WEEKLY_ALL.length<=WEEKLY_WIN) return;   // nothing to pan; let the page scroll
    e.preventDefault();
    wheelAcc += (Math.abs(e.deltaY)>=Math.abs(e.deltaX) ? e.deltaY : e.deltaX);
    const step=Math.trunc(wheelAcc/40);          // ~one week per notch, accumulate sub-steps
    if(!step) return;
    wheelAcc -= step*40;
    const lo=Math.min(WEEKLY_WIN, WEEKLY_ALL.length);
    const ne=Math.max(lo, Math.min(WEEKLY_ALL.length, WEEKLY_END-step));
    if(ne!==WEEKLY_END){ WEEKLY_END=ne; renderWeekly(); }
  }, {passive:false});
}
async function loadWeekly(){
  WEEKLY_ALL = await getJSON("/api/weekly?weeks=0");   // full history (tiny)
  WEEKLY_END = WEEKLY_ALL.length;
  WEEKLY_MAX = Math.max(...WEEKLY_ALL.map(x=>x.km), 1);
  renderWeekly();
  wireWeeklyDrag();
}

async function loadWeather(){
  try{ WX = await getJSON("/api/weather"); }catch(e){ WX=null; }
  if(RDY) renderReadiness(RDY);   // fold the three-city forecast into the readiness card footer
}

$("#syncBtn").addEventListener("click", async ()=>{
  const b=$("#syncBtn"); b.disabled=true; const t=b.textContent; b.textContent="Syncing…";
  try{
    const r=await fetch("/api/sync",{method:"POST"}); const d=await r.json();
    if(!d.ok){ alert("Sync failed: "+(d.error||"unknown")); }
    await loadShape(); await loadRecent(); await loadProjector(); await loadWeekly(); loadDrift(); loadEffort();
  }catch(e){ alert("Sync error: "+e); }
  finally{ b.disabled=false; b.textContent=t; }
});
$("#backfillBtn").addEventListener("click", async ()=>{
  if(!confirm("Full-history backfill: walks every page back to your first activity (a minute or two). Use on a fresh machine or if old history is missing. Proceed?")) return;
  const b=$("#backfillBtn"); b.disabled=true; const t=b.textContent; b.textContent="Backfilling…";
  try{
    const r=await fetch("/api/sync?backfill=1",{method:"POST"}); const d=await r.json();
    if(!d.ok){ alert("Backfill failed: "+(d.error||"unknown")); }
    else { alert(`Backfill done — added ${d.activities.added} activities across ${d.activities.pages_fetched} pages.`); }
    await loadShape(); await loadRecent(); await loadProjector(); await loadWeekly(); loadDrift(); loadEffort();
  }catch(e){ alert("Backfill error: "+e); }
  finally{ b.disabled=false; b.textContent=t; }
});

// ── Latest activity ─────────────────────────────────────────────────────────
function metric(label, val, unit){
  return `<div class="metric"><div class="ml">${label}</div>
    <div class="mv">${val}${unit?`<small> ${unit}</small>`:""}</div></div>`;
}
function paceStr(minkm){ if(minkm==null) return "—"; const m=Math.floor(minkm), s=Math.round((minkm-m)*60);
  return `${m}:${String(s).padStart(2,"0")}`; }
function durStr(sec){ if(!sec) return "—"; const h=Math.floor(sec/3600), m=Math.round(sec%3600/60);
  return h?`${h}h${String(m).padStart(2,"0")}`:`${m} min`; }
// profile hover: draw an activity's pace/HR/cadence trace as a subtle tile background
let ACTPROFILE=null;
function buildProfilePath(vals, {invert=false, W=1000, H=120}={}){
  const pts=vals.map((v,i)=>[i,v]).filter(p=>p[1]!=null);
  if(pts.length<2) return null;
  const xs=pts.map(p=>p[0]), ys=pts.map(p=>p[1]);
  const hi=Math.max(...ys), lo=Math.min(...ys);
  const x=i=>i/(vals.length-1)*W;
  const y=v=>{ let t=(v-lo)/((hi-lo)||1); if(invert) t=1-t; return H-6-t*(H-18); };
  const poly=pts.map(p=>`${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`);
  const d=`M0,${H} L`+poly.join(" L")+` L${W},${H} Z`;   // filled area (baseline-closed)
  const line="M"+poly.join(" L");                         // stroke-only polyline (no fill/baseline)
  return {path:d, line, hi, lo, y, x, pts, W, H};
}
let LOCKED="pace";  // the profile shown by default / when not hovering
// the channel + chart orientation for a metric (pace inverts: faster = taller)
function profileVals(kind){
  const p=ACTPROFILE; if(!p) return null;
  if(kind==="pace" && p.has_pace) return {vals:p.pace, invert:true};
  if(kind==="hr" && p.has_hr) return {vals:p.hr, invert:false};
  if(kind==="cadence" && p.has_cadence) return {vals:p.cadence, invert:false};
  if(kind==="elevation" && p.has_elevation) return {vals:p.elevation, invert:false};
  return null;
}
function profileLabel(kind){
  const p=ACTPROFILE||{};
  return kind==="pace"?"pace profile (faster = higher)"
    : kind==="hr"?`heart-rate profile · avg ${p.hr_avg||"—"} bpm`
    : kind==="elevation"?"elevation profile (climb)"
    : "cadence profile (higher = quicker)";
}
// red→green by t (0=red, 1=green) for the hover line
function rg(t){ t=t<0?0:t>1?1:t; return `hsl(${Math.round(120*t)} 60% 42%)`; }
// 5 HR zones at 60/70/80/90 %HRmax — reconstructed from Runalyze's per-activity zone distribution
// (see runalyze-hr-zones-api). [label, colour, upper %HRmax bound]; single source for line + legend.
const HRZONES=[["Z1","#7c8597",0.60],["Z2","#3f7fd0",0.70],["Z3","var(--ok)",0.80],
               ["Z4","var(--warn)",0.90],["Z5","var(--danger)",Infinity]];
function hrLegendHtml(){
  return `<span class="hrleg">`+HRZONES.map(z=>`<span class="hrz"><i style="background:${z[1]}"></i>${z[0]}</span>`).join("")+`</span>`;
}
// per-sample colour for the hover line — meaning differs by metric
function metricColor(kind, v, ctx){
  if(v==null) return "transparent";
  if(kind==="hr"){ const hm=ctx.hrmax||ctx.hi||0, f=hm?v/hm:0;       // robust HRmax anchors the zones
    return (HRZONES.find(z=>f<z[2])||HRZONES[4])[1]; }
  if(kind==="pace")    return rg((ctx.hi-v)/((ctx.hi-ctx.lo)||1));   // faster (smaller sec/km) = green
  if(kind==="cadence"){ const d=Math.abs(v-180);                    // 170–190 stays green, then ramps
    return rg(d<=10 ? 1 : 1-(d-10)/15); }                           // to red by ±25 (155 / 205)
  return "var(--accent)";                                            // elevation: neutral single hue
}
// draw the LOCKED metric as a shaded area; if hovering a DIFFERENT metric, trace it as a
// value-coloured line on top (no fill). hoverKind=null → locked layer only.
function renderProfile(hoverKind){
  const p=ACTPROFILE; if(!p) return;
  const bg=$("#actbg"); const W=1000,H=120;
  let svg="";
  const lb=profileVals(LOCKED);
  if(lb){
    const bl=buildProfilePath(lb.vals,{W,H,invert:lb.invert});
    if(bl){
      let avg="";
      if(LOCKED==="hr" && p.hr_avg){ const yv=bl.y(p.hr_avg);
        avg=`<line x1="0" y1="${yv.toFixed(1)}" x2="${W}" y2="${yv.toFixed(1)}" class="avgline"/>`; }
      svg += `<path d="${bl.path}" class="proffill"/>${avg}`;
    }
  }
  const showHover = hoverKind && hoverKind!==LOCKED;
  const hb = showHover ? profileVals(hoverKind) : null;
  if(hb){
    const bh=buildProfilePath(hb.vals,{W,H,invert:hb.invert});
    if(bh){
      const ctx={hi:bh.hi, lo:bh.lo, hrmax:p.hrmax};
      const stops=bh.pts.map(pt=>`<stop offset="${(bh.x(pt[0])/W*100).toFixed(2)}%" style="stop-color:${metricColor(hoverKind,pt[1],ctx)}"/>`).join("");
      svg += `<defs><linearGradient id="aclg" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="${W}" y2="0">${stops}</linearGradient></defs>`+
             `<path d="${bh.line}" class="profline" stroke="url(#aclg)"/>`;
    }
  }
  bg.innerHTML=`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${svg}</svg>`;
  bg.classList.toggle("on", !!svg);
  const lblKind = (showHover && hb) ? hoverKind : LOCKED;
  $("#proflbl").textContent = profileLabel(lblKind) + ((showHover && hb) ? " · click to lock" : " · 🔒 locked");
  const wideHR = showHover && hb && hoverKind==="hr";   // only the 5-zone HR legend is wide enough to reach the hint
  const leg=$("#hrlegend"); if(leg) leg.innerHTML = wideHR ? hrLegendHtml() : "";
  // yield the hint to the wide HR legend (visibility, not display, so the row height never shifts)
  const hint=document.querySelector("#recent .profhint"); if(hint) hint.style.visibility = wideHR ? "hidden" : "visible";
  document.querySelectorAll("#recent .metric.hovx").forEach(el=>
    el.classList.toggle("locked", el.dataset.prof===LOCKED));
}
let CURACT=null;   // the activity currently shown in the tile (null = the latest)
function loadRecent(){ return loadActivity(); }   // default tile = latest activity
async function loadActivity(aid){
  CURACT = aid || null;
  const a = await getJSON(aid?`/api/activity/${aid}`:"/api/activity/latest");
  const host=$("#recent");
  // a non-run is the most-recent activity → note it (private view only). Its load still counts toward
  // the plan via Runalyze's all-sport fitness/fatigue, so this just explains why an older run shows.
  const cx = (!SH_READONLY && a && a.cross_training) ? a.cross_training : null;
  const cxDate = cx && cx.date ? new Date(cx.date+"T00:00:00").toLocaleDateString(undefined,{day:"numeric",month:"short"}) : "";  // local midnight (no UTC off-by-one)
  const crossNote = cx
    ? `<div class="crossnote">Your latest activity was <b>${esc(cx.sport||"a non-run")}</b>${cxDate?` on ${cxDate}`:""} — not a run, so it isn't shown here. Its training load still counts toward your plan, reflected in your fitness &amp; fatigue markers (Runalyze tracks all sports).</div>`
    : "";
  if(!a || a.empty_run){
    host.innerHTML = (a&&a.empty_run ? `<div class="empty">No running activity logged yet.</div>`
      : `<div class="empty">${aid?"Activity not found.":"No activities synced yet."}</div>`) + crossNote;
    return;
  }
  const dt=new Date(a.date_time);
  const when=dt.toLocaleDateString(undefined,{weekday:"short",day:"numeric",month:"short"})+
    " · "+dt.toLocaleTimeString(undefined,{hour:"2-digit",minute:"2-digit"});
  const m=(label,val,unit,hover)=>`<div class="metric ${hover?'hovx':''}" ${hover?`data-prof="${hover}"`:""}>
    <div class="ml">${label}</div><div class="mv">${val}${unit?`<small> ${unit}</small>`:""}</div></div>`;
  const kick = aid
    ? `Activity of ${dt.toLocaleDateString(undefined,{day:"numeric",month:"short",year:"2-digit"})} <a href="#" id="backlatest" class="backlatest">← latest</a>`
    : "Latest running activity";
  host.innerHTML=`<div class="actwrap"><div class="actbg" id="actbg"></div>
    <div class="actfg">
      <div class="rtop">
        <div class="rkick">${kick}</div>
        ${SH_READONLY||!a.id?"":`<div class="dqtools">`+
          (a.ignored
            ? `<span class="muted">⊘ ignored</span> <a href="#" id="igntog" data-id="${a.id}" data-on="0">undo</a>`
            : `<a href="#" id="igntog" data-id="${a.id}" data-on="1" title="Exclude this activity from the fitness/fatigue reconstruction — for a duplicate or mis-tagged upload the auto-detector missed">⊘ ignore</a>`)+
          `<a href="#" id="delact" data-id="${a.id}" class="delact" title="Hard-remove this activity from your local copy — for one you ALREADY deleted on Runalyze (insert-only sync leaves the row behind). Still on Runalyze? It returns next sync — use ⊘ ignore instead.">🗑 delete</a></div>`}
      </div>
      <div class="mrow">
        <span class="ttl">${esc(a.sport||"Activity")}${a.title?` — ${esc(a.title)}`:""}</span>
        ${m("When", when, "")}
        ${m("Distance", fmt(a.distance,2), "km")}
        ${m("Duration", durStr(a.duration), "")}
        ${m("Pace", paceStr(a.pace_min_km), "/km", "pace")}
        ${m("Avg HR", a.hr_avg||"—", "bpm", "hr")}
        ${m("Max HR", a.hr_max||"—", "bpm", "hr")}
        ${a.cadence?m("Cadence", a.cadence, "spm", "cadence"):""}
        ${m("TRIMP", a.trimp!=null?Math.round(a.trimp):"—", "")}
        ${a.elevation_up?m("Climb", a.elevation_up, "m", "elevation"):""}
      </div>
      <div class="profbar">
        <span class="profhint muted">Background shades the locked trace · hover <b>Pace/HR/Cadence/Climb</b> to overlay it (colour = value), click to lock.</span>
        <span class="profmeta" id="profmeta"><span class="proflbl" id="proflbl"></span><span class="hrlegend" id="hrlegend"></span></span>
      </div>
    </div></div>
    ${SH_READONLY?"":'<div id="actmap" class="actmap"></div>'}` + crossNote;
  // load the profile once, show the default (locked) one, then wire hover-preview + click-lock
  if(a.id){
    try{ ACTPROFILE = await getJSON(`/api/activity/${a.id}/profile`); }
    catch(e){ ACTPROFILE={}; }
  }
  if(!ACTPROFILE || !ACTPROFILE.has_pace) LOCKED = (ACTPROFILE&&ACTPROFILE.has_hr)?"hr":(ACTPROFILE&&ACTPROFILE.has_cadence)?"cadence":(ACTPROFILE&&ACTPROFILE.has_elevation)?"elevation":"pace";
  // drop the hover affordance from any metric whose channel didn't come through, so the cursor never
  // promises a trace that isn't there (e.g. Climb on a run Runalyze couldn't elevation-correct).
  host.querySelectorAll(".metric.hovx").forEach(el=>{
    const k=el.dataset.prof, P=ACTPROFILE||{};
    const ok=(k==="pace"&&P.has_pace)||(k==="hr"&&P.has_hr)||(k==="cadence"&&P.has_cadence)||(k==="elevation"&&P.has_elevation);
    if(!ok){ el.classList.remove("hovx"); el.removeAttribute("data-prof"); }
  });
  renderProfile(null);
  host.querySelectorAll(".hovx").forEach(el=>{
    el.addEventListener("mouseenter", ()=>renderProfile(el.dataset.prof));
    el.addEventListener("mouseleave", ()=>renderProfile(null));
    el.addEventListener("click", ()=>{ LOCKED=el.dataset.prof; renderProfile(null); });
  });
  const tog=$("#igntog");
  if(tog) tog.addEventListener("click", async ev=>{
    ev.preventDefault();
    await toggleIgnore(tog.dataset.id, tog.dataset.on==="1");
  });
  const del=$("#delact");
  if(del) del.addEventListener("click", async ev=>{
    ev.preventDefault();
    if(!confirm("Delete this activity from your LOCAL copy?\n\nUse this only for an activity you already deleted on Runalyze — insert-only sync left the row behind. If it still exists on Runalyze it will return on the next sync (use ⊘ Ignore instead). An accidental delete recovers with a full backfill.")) return;
    await fetch(`/api/activity/${del.dataset.id}/delete`,
      {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
    CURACT=null;   // the row is gone → fall back to the latest activity
    await Promise.all([loadShape(), loadActivity(), loadProjector()]);
  });
  const bl=$("#backlatest");
  if(bl) bl.addEventListener("click", ev=>{ ev.preventDefault(); loadActivity(); });
  if(!SH_READONLY && a.id) showActivityMap(a.id);
}
// ── Workout route map (private only) ────────────────────────────────────────
// Leaflet is loaded lazily from a CDN and ONLY on the private instance — the public read-only
// container never fetches it, and its /map endpoint 403s, because the routes reveal where the owner
// lives (the whole reason this feature is private). Falls back to a clean empty state with no GPS.
let LEAFLET_READY=null;
function ensureLeaflet(){
  if(window.L) return Promise.resolve();
  if(LEAFLET_READY) return LEAFLET_READY;
  LEAFLET_READY=new Promise((res,rej)=>{
    // Subresource Integrity: this runs in the PRIVATE instance (token + blood markers), so pin the
    // exact 1.9.4 bytes — a compromised/MITM'd CDN can't substitute code. Hashes are version-locked.
    const css=document.createElement("link");
    css.rel="stylesheet"; css.href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css";
    css.integrity="sha384-sHL9NAb7lN7rfvG5lfHpm643Xkcjzp4jFvuavGOndn6pjVqS6ny56CAt3nsEVT4H";
    css.crossOrigin="anonymous"; document.head.appendChild(css);
    const js=document.createElement("script");
    js.src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js";
    js.integrity="sha384-cxOPjt7s7Iz04uaHJceBmS+qpjv2JkIHNVcuOrM+YHwZOmJGBXI00mdUXEq65HTH";
    js.crossOrigin="anonymous";
    js.onload=()=>res(); js.onerror=()=>rej(new Error("leaflet failed to load"));
    document.head.appendChild(js);
  });
  return LEAFLET_READY;
}
let ACTMAP=null;
async function showActivityMap(aid){
  const host=$("#actmap"); if(!host) return;
  let d=null; try{ d=await getJSON(`/api/activity/${aid}/map`); }catch(e){}
  if(ACTMAP){ ACTMAP.remove(); ACTMAP=null; }   // tear down a prior map before re-init
  if(!d || !d.has_gps || !(d.path&&d.path.length>1)){
    host.innerHTML=`<div class="mapempty">No route recorded for this activity.</div>`; return;
  }
  host.innerHTML="";
  try{ await ensureLeaflet(); }catch(e){ host.innerHTML=`<div class="mapempty">Map unavailable (offline?).</div>`; return; }
  const cs=getComputedStyle(document.documentElement);
  const accent=cs.getPropertyValue("--accent").trim()||"#b9542c";   // route line follows the theme
  const okc=cs.getPropertyValue("--ok").trim()||"#4f8c5f";          // start marker = good (semantic)
  const dangerc=cs.getPropertyValue("--danger").trim()||"#b5563f";  // end marker = stop (semantic)
  ACTMAP=L.map(host,{zoomControl:true, scrollWheelZoom:false});
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    {maxZoom:19, attribution:"© OpenStreetMap"}).addTo(ACTMAP);
  L.polyline(d.path,{color:accent, weight:4, opacity:.9}).addTo(ACTMAP);
  L.circleMarker(d.path[0],{radius:5, color:okc, weight:2, fillColor:okc, fillOpacity:1}).addTo(ACTMAP);
  L.circleMarker(d.path[d.path.length-1],{radius:5, color:dangerc, weight:2, fillColor:dangerc, fillOpacity:1}).addTo(ACTMAP);
  ACTMAP.fitBounds(d.bounds,{padding:[18,18]});
}

// Data-quality: exclude (or restore) an activity from the owned reconstruction, then
// refresh the dashboard so the shape tiles + projector reflect it.
async function toggleIgnore(id, on){
  if(SH_READONLY) return;
  await fetch(`/api/activity/${id}/${on?"ignore":"unignore"}`,
    {method:"POST", headers:{"Content-Type":"application/json"},
     body: JSON.stringify({reason:"manual"})});
  // keep the tile on whatever activity is displayed (CURACT) — not always the latest
  await Promise.all([loadShape(), loadActivity(CURACT), loadProjector()]);
}

// ── Readiness gate ──────────────────────────────────────────────────────────
function plannedSession(s, easyPace){
  if(!s) return `<div class="planned muted" style="font-size:13px">No active plan — generate one below in Training plan.</div>`;
  if(s.kind==="pre"){
    const d=new Date(s.start+"T00:00:00");
    const fmt=d.toLocaleDateString(undefined,{weekday:"short",month:"short",day:"numeric"});
    return `<div class="planned"><div class="rkick">Training plan active</div>
      <div class="mrow"><span class="ttl">Re-base starts ${fmt}</span><span class="muted" style="font-size:13px">Keep it easy until then — readiness check-ins still count.</span></div></div>`;
  }
  if(s.kind==="post") return `<div class="planned"><div class="rkick">Today's session</div>
    <div class="mrow"><span class="ttl">Re-base complete</span><span class="muted" style="font-size:13px">Regenerate to periodize the next phase.</span></div></div>`;
  if(s.kind==="rest") return `<div class="planned"><div class="rkick">Today's session</div>
    <div class="mrow"><span class="ttl">Rest day</span><span class="muted" style="font-size:13px">${esc(s.note)}</span></div></div>`;
  const act=s.actual||{};
  const kick=`Today's session · re-base week ${s.week}`+
    (s.done?` · <span style="color:var(--ok);font-weight:600">done ✓</span>`:"");
  const actLine=s.done
    ? `<div style="font-size:12px;margin-top:6px;color:var(--ok)">✓ Ran ${act.km}k${act.pace?` @ ${act.pace}/km`:""} today — session complete.</div>`
    : "";
  return `<div class="planned"${s.done?' style="opacity:.85"':''}><div class="rkick">${kick}</div>
    <div class="mrow">
      <span class="ttl" style="text-transform:capitalize">${s.kind} run</span>
      ${metric("Distance", s.km, "km")}
      ${metric("Duration", `~${s.minutes}`, "min")}
      ${metric("Pace", (s.easy_pace||easyPace||"").replace("/km",""), "/km easy")}
      ${metric("Target load", s.trimp!=null?Math.round(s.trimp):"—", "TRIMP")}
    </div>
    ${actLine}
    <div class="muted" style="font-size:12px;margin-top:6px">${esc(s.note)}</div></div>`;
}
// the configured cities' forecast, folded into the readiness card footer (white on the gradient)
function wxFootHtml(){
  if(!WX||!WX.cities||!WX.cities.length) return "";
  return `<span class="sc-wx" title="Current conditions in the cities you've configured">`+
    WX.cities.map(c=>`<span class="wxc"><span class="wxk">${esc((c.key||"").toUpperCase())}</span> ${c.icon||""} ${c.temp==null?"–":c.temp+"°"}</span>`).join("")+
    `</span>`;
}
// §3 status card — "lead with the verdict": gradient panel (state-coloured), glass pill, big verdict.
function statusCard(a, foot, wx){
  const v=a.verdict||"green";
  const orbs=`<span class="sc-orb" style="top:-50px;right:-40px;width:180px;height:180px"></span>`+
             `<span class="sc-orb" style="bottom:-60px;left:-30px;width:150px;height:150px;background:rgba(255,255,255,.05)"></span>`;
  return `<div class="statuscard ${v}">${orbs}
      <div class="sc-top">
        <span class="sc-eyebrow">Today's readiness</span>
        <span class="sc-pill"><span class="dot"></span>${v}${a.halt?" · halted":""}</span>
      </div>
      <div class="sc-verdict">${esc(a.action||v)}</div>
      ${a.halt?`<div class="halt">⚠ Plan halted — clear it with your doctor before resuming.</div>`:""}
      ${(foot||wx)?`<div class="sc-foot">${foot?`<span>${esc(foot)}</span>`:""}${wx||""}</div>`:""}
    </div>`;
}
function renderReadiness(d){
  RDY=d;                                   // cache so loadWeather can re-render with the forecast folded in
  const a=d.assessment||{};
  if(SH_READONLY || a.public){   // public view: verdict card + planned session only
    $("#readiness").innerHTML = statusCard(a, "", wxFootHtml()) + plannedSession(d.session);
    return;
  }
  const c=d.checkin||{};
  const hrv=a.hrv||{};
  const hrvTxt = hrv.state==null ? "HRV: no data"
    : `HRV ${hrv.baseline} vs ${hrv.band[0]}–${hrv.band[1]} — ${hrv.state}`;
  const sel=(name,val,opts)=>`<label>${name}</label><select id="ci_${name}">`+
    opts.map(([v,t])=>`<option value="${v}" ${v===val?"selected":""}>${t}</option>`).join("")+`</select>`;
  let aiLine="";
  if(a.source && a.source.startsWith("llm"))
    aiLine=`🩺 AI judgment${a.engine_floor&&a.engine_floor!==a.verdict?` · engine floor was ${a.engine_floor}`:""}`;
  else if(a.source && a.source.startsWith("engine ("))
    aiLine=`engine floor held — AI suggested ${a.ai_verdict}`;
  const notePh = LLM_OK ? "Anything else? e.g. “slight cold coming on”, “legs heavy but slept great” — the AI reads this"
                        : "Note (optional)";
  const foot=[(a.reasons||[]).join(" · "), hrvTxt, aiLine].filter(Boolean).join("  ·  ");
  $("#readiness").innerHTML=
    statusCard(a, foot, wxFootHtml()) +
    plannedSession(d.session) +
    `<div class="checkin">
      ${sel("energy", c.energy||"ok", [["good","Legs: fresh"],["ok","Legs: ok"],["heavy","Legs: heavy"]])}
      ${sel("sleep", c.sleep||"ok", [["good","Slept: well"],["ok","Slept: ok"],["poor","Slept: poorly"]])}
      <input id="ci_note" class="cinote" placeholder="${notePh}" value="${esc(c.note)}">
      <button class="primary" id="ciBtn" style="font-size:13px;padding:7px 12px">Save check-in</button>
    </div>`;
  $("#ciBtn").addEventListener("click", async ()=>{
    const body={energy:$("#ci_energy").value, sleep:$("#ci_sleep").value,
      note:$("#ci_note").value};
    const r=await fetch("/api/readiness",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    renderReadiness(await r.json());
  });
}
async function loadReadiness(){ renderReadiness(await getJSON("/api/readiness")); }
// Opportunistic page-load sync (private only): pull any activities synced to Runalyze since the
// last sync — throttled server-side — so a run finished earlier today lands without the manual
// button, and the readiness tile flips to "done ✓". Silent + non-blocking; only the refresh
// fires when something actually arrived, so it never flickers on a quiet load.
async function touchSync(){
  try{
    const d = await getJSON("/api/sync?auto=1",{method:"POST"});
    if(d && d.ok && d.activities && d.activities.added>0){
      loadReadiness(); loadPlan(); loadShape(); loadRecent(); loadProjector(); loadWeekly(); loadEffort();
    }
  }catch(e){ /* offline / token issue — the tile just keeps its last-known state */ }
}

// ── Training plan ───────────────────────────────────────────────────────────
function acBadge(a){
  if(a==null) return "";
  const cls = a<=1.18 ? "lo" : "mid";
  return `<span class="acbadge ${cls}">ACWR →${a.toFixed(2)}</span>`+
    qhint("Projected acute:chronic load at this week's end — rolled forward from today's fitness if you run the plan. Stay in the green band (≤ ~1.3); the engine trims volume to keep it there.");
}
// Reusable click-to-open help bubble (Runalyze-style "?"). Delegated, so it works on dynamic content;
// positioned as a fixed bubble so an overflow:hidden ancestor can't clip it.
function qhint(text){
  return `<span class="qhint" tabindex="0" role="button" aria-label="Explanation">?<span class="qtip" role="tooltip">${esc(text)}</span></span>`;
}
document.addEventListener("click", e=>{
  if(e.target.closest(".qtip")) return;                       // clicks inside the bubble: leave it open
  const h = e.target.closest(".qhint");
  document.querySelectorAll(".qhint.open").forEach(n=>{ if(n!==h) n.classList.remove("open"); });
  if(!h) return;
  e.stopPropagation(); e.preventDefault();                    // don't let the week row's click fire
  const opening = !h.classList.contains("open");
  h.classList.toggle("open");
  if(opening){
    const t=h.querySelector(".qtip"), r=h.getBoundingClientRect(), w=Math.min(240, window.innerWidth-16);
    t.style.width=w+"px"; t.style.top=(r.bottom+6)+"px";
    t.style.left=Math.max(8, Math.min(r.right-w, window.innerWidth-w-8))+"px";
  }
});
document.addEventListener("keydown", e=>{
  if(e.key==="Escape"){ document.querySelectorAll(".qhint.open").forEach(n=>n.classList.remove("open")); return; }
  if((e.key==="Enter"||e.key===" ") && e.target.classList && e.target.classList.contains("qhint")){ e.preventDefault(); e.target.click(); }
});
let OBJECTIVES=[], LASTDIFF=null, LLM_OK=false, LOG=null, WX=null, RDY=null;
function objManager(p){
  // Priority chip: static on the public view; an inline A|B|C selector on the private console
  // (clicking a letter POSTs /priority and re-periodizes — same path as the adjudication "Set B").
  const priBadge = o => SH_READONLY
    ? `<span class="pr ${o.priority}">${o.priority}</span>`
    : `<span class="prsel" role="group" aria-label="priority for ${esc(o.label)}">${['A','B','C'].map(x=>
        `<button type="button" class="prseg ${x===o.priority?'on':''}" data-oid="${o.id}" data-pri="${x}" title="Set priority ${x}">${x}</button>`).join("")}</span>`;
  const rows = OBJECTIVES.filter(o=>o.status==='upcoming').map(o=>{
    const isAnchor = p.objective && o.label===p.objective.label && o.date===p.objective.date;
    return `<div class="obj ${isAnchor?'anchor':''}">
      ${priBadge(o)}
      <span>${esc(o.label)}${isAnchor?' <span class="muted mono" style="font-size:10px">· anchor</span>':''}</span>
      <span class="od">${o.date} · ${o.type} · ${o.target}</span>
      <button class="x" data-oid="${o.id}">remove</button>
    </div>`;}).join("") || `<div class="muted" style="font-size:13px">No objectives — maintenance mode.</div>`;
  if(SH_READONLY) return `<div class="objs">${rows}</div>`;   // public: list only, no controls
  const aCount = OBJECTIVES.filter(o=>o.status==='upcoming' && o.priority==='A').length;
  const conflictRow = aCount>=2 ? `
    <div class="conflictrow">
      <button id="adjBtn" ${LLM_OK?'':'disabled'}>⚖ ${aCount} A-races compete — get advice${LLM_OK?'':' (set ANTHROPIC_API_KEY)'}</button>
      <div id="objAdjudicate"></div>
    </div>` : "";
  const nlRow = `
    <div class="nlobj">
      <input id="ao_nl" ${LLM_OK?'':'disabled'} placeholder="Describe a goal — e.g. &quot;sub-45 10k in October&quot;, &quot;spring marathon, want to BQ&quot;" style="flex:1">
      <button id="ao_parse" ${LLM_OK?'':'disabled'} style="font-size:12px;padding:6px 11px">✨ Parse</button>
      <span id="ao_interp" class="nlinterp${LLM_OK?'':' guess'}">${LLM_OK?'':'⚙ Set ANTHROPIC_API_KEY in .env to enable AI parsing'}</span>
    </div>`;
  return `<div class="objs">${rows}</div>
    ${conflictRow}
    ${nlRow}
    <div class="addobj">
      <input id="ao_label" placeholder="race name" style="width:130px">
      <select id="ao_type"><option>5k</option><option>10k</option><option>half</option><option selected>marathon</option><option>custom</option></select>
      <input id="ao_date" type="date">
      <select id="ao_pri"><option value="A">A</option><option value="B">B</option><option value="C">C</option></select>
      <input id="ao_target" placeholder="goal (finish / 3:55)" style="width:120px">
      <button class="primary" id="ao_add" style="font-size:12px;padding:6px 11px">Add objective</button>
    </div>`;
}
function diffBanner(diff){
  if(!diff || diff.first) return "";
  return `<div class="diff"><div class="dh">Re-planned — ${diff.summary}</div>
    <ul>${(diff.changes||[]).map(c=>`<li>${c}</li>`).join("")}</ul></div>`;
}
function renderAdjudicate(d){
  const host=$("#objAdjudicate"); if(!host) return;
  if(!d.ok){ host.innerHTML=`<div class="adjclamp err">⚠ ${esc(d.error||'could not adjudicate')}</div>`; return; }
  const recs=(d.recommendations||[]).map(r=>{
    const cur=OBJECTIVES.find(o=>o.id===r.id);
    const changed=cur && cur.priority!==r.suggested_priority;
    return `<li><b>${esc(r.label)}</b> → <span class="pr ${r.suggested_priority}">${r.suggested_priority}</span>${r.id===d.primary_id?' <span class="muted mono" style="font-size:10px">· peak</span>':''}
      <div class="adjmeta">${esc(r.reason)}</div>
      ${changed?`<button class="applyrec" data-id="${r.id}" data-pri="${r.suggested_priority}" style="font-size:11px;padding:3px 9px;margin-top:4px">Set ${r.suggested_priority}</button>`:''}</li>`;
  }).join("");
  host.innerHTML=`<div class="adjudbox">
    <div class="exh">${d.summary||''}</div>
    <ul class="expts">${recs}</ul>
    <div class="exfoot">Claude advises; the engine periodizes from the priorities you keep.</div>
  </div>`;
  host.querySelectorAll(".applyrec").forEach(b=>b.addEventListener("click", async ()=>{
    const r=await fetch(`/api/objectives/${b.dataset.id}/priority`,{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({priority:b.dataset.pri})});
    const p=await r.json(); LASTDIFF=p.diff; await refreshPlan(p);
  }));
}
function wireObjActions(){
  const adj=$("#adjBtn");
  if(adj) adj.addEventListener("click", async ()=>{
    const t=adj.textContent; adj.disabled=true; adj.textContent="Weighing…";
    const host=$("#objAdjudicate"); if(host) host.innerHTML=`<div class="adjclamp">thinking…</div>`;
    try{ renderAdjudicate(await getJSON("/api/objectives/adjudicate",{method:"POST"})); }
    catch(e){ if(host) host.innerHTML=`<div class="adjclamp err">⚠ ${e}</div>`; }
    finally{ adj.disabled=false; adj.textContent=t; }
  });
  const parse=$("#ao_parse");
  if(parse) parse.addEventListener("click", async ()=>{
    const text=$("#ao_nl").value.trim(); if(!text){ $("#ao_nl").focus(); return; }
    const interp=$("#ao_interp"); const t=parse.textContent;
    parse.disabled=true; parse.textContent="Parsing…"; interp.textContent="";
    try{
      const r=await fetch("/api/objectives/parse",{method:"POST",
        headers:{"Content-Type":"application/json"},body:JSON.stringify({text})});
      const d=await r.json();
      if(!d.ok){ interp.textContent="⚠ "+(d.error||"couldn't parse"); interp.className="nlinterp err"; return; }
      // fill the structured form — the owner reviews, then clicks Add objective
      $("#ao_label").value=d.label||""; $("#ao_type").value=d.type||"marathon";
      $("#ao_date").value=d.date||""; $("#ao_pri").value=d.priority||"A";
      $("#ao_target").value=d.target||"finish";
      interp.className="nlinterp"+(d.confident?"":" guess");
      interp.textContent=(d.confident?"":"⚠ guessed — check the date · ")+(d.interpretation||"Review and click Add.");
    }catch(e){ interp.textContent="⚠ "+e; interp.className="nlinterp err"; }
    finally{ parse.disabled=false; parse.textContent=t; }
  });
  const add=$("#ao_add");
  if(add) add.addEventListener("click", async ()=>{
    const body={label:$("#ao_label").value||"Race", type:$("#ao_type").value,
      date:$("#ao_date").value, priority:$("#ao_pri").value, target:$("#ao_target").value||"finish"};
    if(!body.date){ alert("Pick a date"); return; }
    const r=await fetch("/api/objectives",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const p=await r.json(); LASTDIFF=p.diff; await refreshPlan(p);
  });
  document.querySelectorAll(".obj .x").forEach(btn=>btn.addEventListener("click", async ()=>{
    const r=await fetch(`/api/objectives/${btn.dataset.oid}/remove`,{method:"POST"});
    const p=await r.json(); LASTDIFF=p.diff; await refreshPlan(p);
  }));
  // inline A|B|C priority selector — set a priority and re-periodize (no-op if already that letter)
  document.querySelectorAll(".obj .prseg").forEach(b=>b.addEventListener("click", async ()=>{
    if(b.classList.contains("on")) return;
    const r=await fetch(`/api/objectives/${b.dataset.oid}/priority`,{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({priority:b.dataset.pri})});
    const p=await r.json();
    if(!p.ok){ alert("Could not set priority: "+(p.error||"unknown")); return; }
    LASTDIFF=p.diff; await refreshPlan(p);
  }));
}
// ── Qualitative adjustment (§6c) — LLM proposes, engine clamps ───────────────
function adjEffect(a){
  const med = a.medical || a.medical_flag;
  if(med) return "Plan halted — full rest until you've seen your doctor";
  if(a.volume_multiplier===0) return "Full rest over the window";
  if(a.volume_multiplier>=1) return a.easy_only ? "Easy effort only over the window" : "On plan — no load change";
  return `Load eased to ${Math.round(a.volume_multiplier*100)}% of plan${a.easy_only?", easy effort only":""}`;
}
function adjustmentUI(p){
  if(SH_READONLY) return "";   // public: no qualitative-adjustment controls or (medical) banner
  const a=p.adjustment;
  const banner = a ? `<div class="${(a.medical||a.medical_flag)?'adjmed':'adjbox'}">
      <div class="adjh">${(a.medical||a.medical_flag)?'⚠ Medical flag':'Active adjustment'} — ${adjEffect(a)}</div>
      <div class="adjmeta">“${esc(a.note)}” · ${a.applies_from}→${a.applies_until}${a.summary?` · ${esc(a.summary)}`:''}</div>
      ${a.clamp?`<div class="adjclamp">engine clamp: ${a.clamp}</div>`:''}
      ${(a.medical||a.medical_flag)?`<div class="adjclamp">Sparing Horse tracks &amp; flags — it never diagnoses. Clear this once your doctor signs off.</div>`:''}
      <button id="adj_clear" style="font-size:11px;padding:4px 9px;margin-top:8px">Clear adjustment</button>
    </div>` : "";
  const ask = `<div class="adjask">
      <input id="adj_text" ${LLM_OK?'':'disabled'} placeholder="How'd it go / how are you? e.g. “felt great today”, “knee’s a bit sore”, “travelling Mon–Fri”">
      <button id="adj_propose" ${LLM_OK?'':'disabled'}>💬 Tell the horse</button>
      <div class="adjhint">A run that's done → I'll <b>log it</b> · something ahead (a niggle, travel, a cold) → I'll <b>propose easing</b> the plan. I only ever ease or hold — never push harder.</div>
      <div id="adj_preview" class="adjpreview">${LLM_OK?'':'<div class="adjclamp">⚙ Set ANTHROPIC_API_KEY in .env to enable — the engine still clamps every suggestion.</div>'}</div>
    </div>`;
  return banner + ask;
}
async function renderAdjPreview(d){
  const host=$("#adj_preview"); if(!host) return;
  if(!d.ok){ host.innerHTML=`<div class="adjclamp err">⚠ ${esc(d.error||'could not read that')}</div>`; return; }
  // A reflection ('felt great') changes nothing about the plan — journal it against today and
  // show the reply. Only a real ease/hold/medical signal becomes a confirmable adjustment.
  if(d.kind==="log"){
    await fetch("/api/log/note",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({note:d.note})});
    const t=$("#adj_text"); if(t) t.value="";
    await refreshPlan();   // rebuilds #plan (and a fresh, empty #adj_preview) — do this FIRST
    const h2=$("#adj_preview");   // then write the reply into the NEW node, or it gets clobbered
    if(h2) h2.innerHTML=`<div class="adjprop">
      <div class="adjreply">${esc(d.reply||"Noted — keeping you on plan.")}</div>
      <div class="adjmeta">📓 logged to today's run · plan unchanged</div></div>`;
    return;
  }
  const a=d.directive;
  host.dataset.note=d.note||""; host.dataset.directive=JSON.stringify(a);
  host.innerHTML=`<div class="adjprop">
    ${d.reply?`<div class="adjreply">${esc(d.reply)}</div>`:''}
    <div>${(a.medical_flag)?'⚠ ':''}${adjEffect(a)} · ${a.applies_from}→${a.applies_until}</div>
    ${a.summary?`<div class="adjmeta">${esc(a.summary)}</div>`:''}
    ${d.clamp?`<div class="adjclamp">engine clamp: ${d.clamp}</div>`:''}
    <div style="margin-top:6px">
      <button id="adj_apply" class="primary" style="font-size:11px;padding:4px 10px">Apply</button>
      <button id="adj_dismiss" style="font-size:11px;padding:4px 10px">Dismiss</button></div>
  </div>`;
  $("#adj_apply").addEventListener("click", async ()=>{
    const r=await fetch("/api/adjustment/apply",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({note:host.dataset.note, directive:JSON.parse(host.dataset.directive)})});
    const p=await r.json(); LASTDIFF=p.diff; await refreshPlan(p);
  });
  $("#adj_dismiss").addEventListener("click", ()=>{ host.innerHTML=""; });
}
function renderExplain(d){
  const host=$("#planExplain"); if(!host) return;
  if(!d.ok){ host.innerHTML=`<div class="adjclamp err">⚠ ${esc(d.error||'could not explain')}</div>`; return; }
  host.innerHTML=`<div class="explainbox">
    <div class="exh">${d.headline||''}</div>
    <ul class="expts">${(d.points||[]).map(p=>`<li>${p}</li>`).join("")}</ul>
    ${d.change_note?`<div class="exchange"><b>Latest change:</b> ${esc(d.change_note)}</div>`:""}
    <div class="exfoot">Claude explains the engine's numbers — it doesn't set them.</div>
  </div>`;
}
function wireAdjust(){
  const exp=$("#explainBtn");
  if(exp) exp.addEventListener("click", async ()=>{
    const t=exp.textContent; exp.disabled=true; exp.textContent="Explaining…";
    const host=$("#planExplain"); if(host) host.innerHTML=`<div class="adjclamp">thinking…</div>`;
    try{
      const r=await fetch("/api/plan/explain",{method:"POST",
        headers:{"Content-Type":"application/json"},body:JSON.stringify({diff:LASTDIFF})});
      renderExplain(await r.json());
    }catch(e){ if(host) host.innerHTML=`<div class="adjclamp err">⚠ ${e}</div>`; }
    finally{ exp.disabled=false; exp.textContent=t; }
  });
  const clr=$("#adj_clear");
  if(clr) clr.addEventListener("click", async ()=>{
    const r=await fetch("/api/adjustment/clear",{method:"POST"});
    const p=await r.json(); LASTDIFF=p.diff; await refreshPlan(p);
  });
  const prop=$("#adj_propose");
  if(prop) prop.addEventListener("click", async ()=>{
    const text=$("#adj_text").value.trim(); if(!text){ $("#adj_text").focus(); return; }
    const t=prop.textContent; prop.disabled=true; prop.textContent="Thinking…";
    try{
      const r=await fetch("/api/adjustment/propose",{method:"POST",
        headers:{"Content-Type":"application/json"},body:JSON.stringify({text})});
      renderAdjPreview(await r.json());
    }catch(e){ const h=$("#adj_preview"); if(h) h.innerHTML=`<div class="adjclamp err">⚠ ${e}</div>`; }
    finally{ prop.disabled=false; prop.textContent=t; }
  });
}
// §6f Step F — compact label for a planned session. Structured quality sessions (intervals / MP
// long run / tempo, carried as `reps`) read their structure; plain runs show distance.
function sessSummary(s){
  if(s.reps&&s.reps.length){
    const work=s.reps.filter(r=>r.effort==='work');
    if(s.kind==='interval'&&work.length) return `${work.length}×${work[0].minutes}′ ${work[0].zone}`;
    if(s.kind==='long_mp'){ const mp=work.find(r=>r.zone==='marathon'); return `long ${s.km}k +${mp?mp.minutes:0}′ MP`; }
    if(s.kind==='tempo'&&work.length) return `${s.km}k · ${work.reduce((a,r)=>a+r.minutes,0)}′ ${work[0].zone}`;
  }
  return `${s.km}k`;
}
// One plan week (used for every phase but the re-base, which carries the journal/actuals overlay).
// Tags down weeks (3:1 mesocycle), frozen weeks (§6f Step E — completed, carried verbatim), and the
// week containing today. Quality sessions are accented; the ACWR badge rides the right rail.
function weekHtml(w,p,today){
  const down=/down/i.test(w.intent||'');
  let cur=false;
  if(w.start){ const we=new Date(w.start); we.setDate(we.getDate()+6);
    cur=!w.frozen && w.start<=today && today<=we.toISOString().slice(0,10); }
  const sess=w.sessions.map(s=>`<div class="sline"><span class="sdate">${sessDate(s.date)}</span><span class="wsi${(s.reps&&s.reps.length)?' qs':''}">${sessSummary(s)}</span></div>`).join("");
  const flags=[w.clipped?'<span class="down">clipped to fit ACWR</span>':'',
               w.adjusted?'<span class="eased">eased</span>':'',
               w.frozen?'<span class="wfz">✓ done</span>':''].filter(Boolean).join(" · ");
  return `<div class="wk ${down?'wdown':''} ${w.frozen?'wfrozen':''} ${cur?'wcur':''}">
      <div class="wn">${w.wk}${w.frozen?'<span class="wlock" title="completed — carried verbatim">🔒</span>':''}</div>
      <div class="wbody">
        <div><span class="wkm">${w.km} km</span> · ${w.runs} runs${flags?' · '+flags:''}</div>
        <div class="wintent">${w.intent}</div>
        <div class="wsesslog">${sess}${w.strides?`<div class="muted mono" style="margin-top:2px">strides×${w.strides}</div>`:''}</div>
      </div>
      <div>${acBadge(w.proj_acwr)}</div>
    </div>`;
}
// Map a phase's display name → the stable key shared by its bar segment and its weeks panel.
function phaseKey(name){
  if(/^re-?base/i.test(name)) return "rebase";
  if(/^base/i.test(name))     return "base";
  if(/^build/i.test(name))    return "build";
  if(/^peak/i.test(name))     return "peak";
  if(/^taper/i.test(name))    return "taper";
  return "";
}
// True when `today` falls inside the week starting at w.start (Mon–Sun).
function weekHoldsToday(w,today){
  if(!w||!w.start) return false;
  const we=new Date(w.start); we.setDate(we.getDate()+6);
  return w.start<=today && today<=we.toISOString().slice(0,10);
}
// Which week is open by default in a phase's strip: the one holding `today`, else the first.
function defaultWeek(weeks,today){ return ((weeks.find(w=>weekHoldsToday(w,today))||weeks[0]||{}).wk); }
// A second-level selector that echoes the phase bar: one segment per week, the selected one active.
// Shared by every phase (re-base + future) — only the *detail* below it differs per phase, so the
// LOG-enriched vs plain week renderers stay untouched (we wrap their output, never merge them).
function weekStrip(weeks,pk,sel){
  return `<div class="weekstrip" data-pk="${pk}">`+weeks.map(w=>{
    const down=/down/i.test(w.intent||'')||w.wk===4;
    return `<div class="weekseg${w.wk===sel?' active':''}${down?' wsdown':''}" data-pk="${pk}" data-wk="${w.wk}"
       title="Week ${w.wk} · ${w.km} km · ${w.runs} runs${w.frozen?' · done':''}">W${w.wk}${w.frozen?'<span class="wlock">🔒</span>':''}</div>`;
  }).join("")+`</div>`;
}
// Wrap a week's already-rendered detail so the strip can show/hide it (scoped by phase key + week).
function weekDetail(inner,pk,wk,sel){ return `<div class="weekdetail${wk===sel?' active':''}" data-pk="${pk}" data-wk="${wk}">${inner}</div>`; }
// A collapsible-free phase section: header (weeks, calendar, projected end CTL/ATL, Σ km, frozen
// count) + a week strip; only the selected week's detail shows. Renders nothing if the phase
// wasn't generated (short runways drop late phases).
function phaseSection(title,block,p,today,pk){
  if(!block||!block.weeks||!block.weeks.length) return "";
  pk = pk || phaseKey(title);   // §6q — explicit key (chain segments share base/peak/taper names)
  const km=block.weeks.reduce((s,w)=>s+(w.km||0),0);
  const froz=block.weeks.filter(w=>w.frozen).length;
  const fz=froz?` · <span class="wfz">${froz} done</span>`:"";
  const sel=defaultWeek(block.weeks,today);
  return `<h3 class="phasehdr">
      <span>${title} <span class="muted mono" style="font-size:11px">(${block.weeks.length}w · start ${block.start} · ends CTL ${block.end_ctl}/ATL ${block.end_atl})${fz}</span>${qhint("CTL = chronic load (your fitness), a slow ~42-day average of training; ATL = acute load (recent fatigue), a fast ~7-day average. Shown here is each value projected to this phase's end.")}</span>
      <span class="muted mono" style="font-size:12px;font-weight:600;white-space:nowrap" title="Total planned distance across the phase">Σ ${km.toFixed(0)} km</span>
    </h3>
    ${weekStrip(block.weeks,pk,sel)}
    <div class="weekdetails">${block.weeks.map(w=>weekDetail(weekHtml(w,p,today),pk,w.wk,sel)).join("")}</div>`;
}
function renderPlan(p){
  const host=$("#plan");
  if(!p){ host.innerHTML=`<div class="empty">No plan yet — hit <b>Generate plan</b>.</div>`; return; }
  const o=p.objective, rb=p.rebase;
  // A-race pill bar at the top of the page (mockup Almanac) — driven by the plan's objective
  const ob=$("#objbar");
  if(ob) ob.innerHTML = o ? `<span class="arace">${esc(o.priority||'A')}-race</span>`+
      `<span class="oname">${esc(o.label)}</span>`+
      `<span class="owhen">${esc(o.date)} · ${o.weeks_away} weeks out</span>`+
      ((p.feasibility&&(p.feasibility.verdict||o.target))?`<span class="overdict">Verdict — <b style="color:var(--text)">${esc(p.feasibility.verdict||o.target)}</b></span>`:"")
    : "";
  const totalw=p.phases.reduce((s,x)=>s+x.weeks,0);
  const zoneChips=Object.entries(p.pace_zones).map(([k,v])=>
    `<span class="zone ${k==='easy_top'?'hl':''}">${k.replace('_',' ')} <b>${v}</b></span>`).join("");
  // Prefer the log's weeks (sessions enriched with done/actual/reflection); fall back to the
  // raw plan weeks when the log isn't loaded yet. The log weeks are a superset of plan weeks.
  const today = (LOG&&LOG.today) || new Date().toISOString().slice(0,10);
  const planWeeks = (LOG&&LOG.weeks) || rb.weeks;
  // Which phase owns "today" — the one whose weeks bracket it. Default selection for the Plan tile:
  // the bar shows the whole road, but only the live phase's weeks are open underneath. Fallbacks:
  // before the plan starts → the first phase; after it ends → the last.
  // §6q — phase groups drive "which phase owns today". Re-base weeks come from the LOG-enriched
  // planWeeks; every other segment (base/build/peak/taper + any chain bridge/peak/taper) comes from
  // its own block keyed by p.phases[].key, so a multi-A chain opens the right segment under the bar.
  const phaseGroups=[{key:"rebase",weeks:planWeeks}]
    .concat((p.phases||[]).filter(x=>x.key&&x.key!=="rebase")
            .map(x=>({key:x.key,weeks:(p[x.key]&&p[x.key].weeks)||[]})))
    .filter(g=>g.weeks.length);
  let curPhase=(phaseGroups.find(g=>g.weeks.some(w=>weekHoldsToday(w,today)))||{}).key;
  if(!curPhase && phaseGroups.length){
    const first=phaseGroups[0];
    curPhase=(first.weeks[0].start && today<first.weeks[0].start)
      ? first.key : phaseGroups[phaseGroups.length-1].key;
  }
  curPhase=curPhase||"rebase";
  const phaseBar=p.phases.filter(x=>x.weeks>0).map((x,i)=>{
    const mix=20+i*16, k=x.key||phaseKey(x.phase);   // §6q — explicit per-segment key
    return `<div class="phaseseg${k===curPhase?' active':''}" data-pk="${k}" title="${esc(x.phase)}" style="flex:${x.weeks};--mix:${mix}%">
      <b>${x.phase.split(" ")[0]}</b>${x.weeks}w</div>`;}).join("");
  // Glanceable block total — Phase 0 is all easy running, so planned time = distance × easy pace.
  const phaseKm = planWeeks.reduce((s,w)=>s+(w.km||0),0);
  const easySec = (m=>m?(+m[1]*60+ +m[2]):0)(/(\d+):(\d+)/.exec(p.pace_zones.easy_top||""));
  const fmtDur = m => m>=60 ? `${Math.floor(m/60)}h${String(m%60).padStart(2,"0")}m` : `${m}m`;
  const phaseMin = easySec ? Math.round(phaseKm*easySec/60) : 0;
  const phaseTot = `Σ ${phaseKm.toFixed(0)} km${phaseMin?` · ~${fmtDur(phaseMin)} easy`:""}`;
  const ran = LOG&&LOG.ran;   // actually run across the block so far (dups excluded)
  const ranTot = ran&&ran.km>0 ? `▸ ran ${ran.km.toFixed(0)} km${ran.min?` · ${fmtDur(ran.min)}`:""}` : "";
  // Last refreshed = when the plan was regenerated (auto-replan runs nightly; paces & totals are
  // recomputed from that snapshot's VO₂max, so this is how fresh the pills below are).
  const refreshed = p.generated_at
    ? `<div class="legend" style="margin-top:6px;opacity:.85">↻ Last refreshed ${new Date(p.generated_at).toLocaleString()}</div>` : "";
  const sessHtml=s=>{
    const mark = s.unplanned ? '<span class="smk extra" title="unplanned — bonus volume">+</span>'
      : s.done ? '<span class="smk done">✓</span>'
      : s.missed ? '<span class="smk missed">✕</span>'
      : (s.date===today ? '<span class="smk today">•</span>' : '<span class="smk">○</span>');
    const act = s.actual ? `<span class="sact">${s.actual.km}k${s.actual.pace?` @ ${s.actual.pace}`:''}</span>` : "";
    const refl = s.reflection ? `<div class="srefl">📓 ${esc(s.reflection)}</div>` : "";
    const clk = !SH_READONLY && (s.done||s.unplanned) && s.activity_id;   // a completed/extra run → view its run + map
    const plan = s.unplanned ? '<span class="splan exu">unplanned</span>' : `<span class="splan">${s.km}k</span>`;
    // a DOUBLE (≥2 runs that day): break the combined actual into its per-run halves, each map-linkable
    const brk = (s.runs && s.runs.length>1)
      ? `<div class="srun" title="${s.runs.length} runs this day">↳ ${s.runs.map(r=>{
            const rc = !SH_READONLY && r.activity_id;
            return `<span class="brkrun"${rc?` data-act-id="${r.activity_id}" title="View this run on the map"`:''}>${r.km}k${r.pace?` @ ${r.pace}`:''}</span>`;
          }).join(" · ")}</div>`
      : "";
    return `<div class="sline ${s.date===today?'stoday':''}${s.unplanned?' unplanned':''}${clk?' sclick':''}"${clk?` data-act-id="${s.activity_id}" title="View this run on the map"`:""}>${mark}<span class="sdate">${sessDate(s.date)}</span>${plan}${act?' → '+act:''}${refl}${brk}</div>`;
  };
  // Re-base weeks are LOG-enriched (done/missed/unplanned/doubles via sessHtml) — kept as their own
  // renderer; we only wrap each in a week-detail and front it with the shared strip (selector below).
  const rbSel=defaultWeek(planWeeks,today);
  const weeks=weekStrip(planWeeks,'rebase',rbSel)+`<div class="weekdetails">`+planWeeks.map(w=>{
    const hasLog = w.sessions.some(s=>'done' in s);
    const sess = hasLog ? `<div class="wsesslog">${w.sessions.map(sessHtml).join("")}</div>`
      : `<div class="wsesslog">${w.sessions.map(s=>`<div class="sline"><span class="sdate">${sessDate(s.date)}</span><span class="splan">${s.km}k</span></div>`).join("")}<div class="muted mono" style="margin-top:3px">${w.strides?`strides×${w.strides} · `:''}@ easy ${p.pace_zones.easy_top}</div></div>`;
    const inner = `<div class="wk ${w.wk===4?'wdown':''}">
      <div class="wn">${w.wk}</div>
      <div class="wbody">
        <div><span class="wkm">${w.km} km</span> · ${w.runs} runs${w.clipped?' · <span class="down">clipped to fit ACWR</span>':''}${w.adjusted?' · <span class="eased">eased</span>':''}</div>
        <div class="wintent">${w.intent}</div>
        ${sess}
      </div>
      <div>${acBadge(w.proj_acwr)}</div>
    </div>`;
    return weekDetail(inner,'rebase',w.wk,rbSel);}).join("")+`</div>`;
  const adh = (LOG&&LOG.adherence&&LOG.adherence.scheduled)
    ? `<span class="muted mono" style="font-size:11px"> · done ${LOG.adherence.done}/${LOG.adherence.scheduled} so far</span>` : "";
  // §6e — earned faster exit: the block can graduate a week early when recent weeks are banked.
  const grad = rb.graduated
    ? `<div class="gradnote">▲ Earned a faster exit — you've banked ${rb.banked_streak} solid week${rb.banked_streak===1?'':'s'}, so Phase 0 graduates a week early (${rb.full_len-rb.graduated} of ${rb.full_len} weeks) and base-build starts sooner. Volumes and the ACWR ceiling are unchanged — the reward is time.</div>`
    : (rb.banked_streak>0
        ? `<div class="legend" style="margin-top:6px">▲ ${rb.banked_streak} week${rb.banked_streak===1?'':'s'} banked — ${rb.grad_at-rb.banked_streak>0?`${rb.grad_at-rb.banked_streak} more to graduate Phase 0 a week early`:'on track to graduate early'}.</div>`
        : "");
  // §6e/§6f — earned upward responsiveness (volume). The opt-in sibling of the graduation above:
  // banked weeks earn a small, ACWR-capped volume bump on the HARD weeks (recovery weeks protected).
  const E = p.earned || {};
  const pct = Math.round((( E.factor||1)-1)*100);
  const earnedNote = SH_READONLY ? "" : (
    E.active
      ? `<div class="gradnote">▲ Earned faster build is <b>on</b> — you've banked ${E.banked_streak} solid week${E.banked_streak===1?'':'s'}, so Base/Build volume is nudged up ~${pct}% on the hard weeks. Recovery (down) weeks and the ACWR ≤1.25 ceiling are untouched — the governor still caps every week. <a href="#" id="earnedToggle" data-on="0">turn off</a></div>`
    : E.opted_in
      ? `<div class="legend" style="margin-top:6px">▲ Earned faster build is on — ${E.banked_streak>=E.bank_at?(E.ready_ok?'applying now':'paused until readiness is green'):`${E.bank_at-E.banked_streak} more banked week${E.bank_at-E.banked_streak===1?'':'s'} to unlock`}. <a href="#" id="earnedToggle" data-on="0">turn off</a></div>`
    : E.banked_streak>=E.bank_at
      ? `<div class="gradnote">▲ You've banked ${E.banked_streak} solid weeks — you can opt into an <b>earned faster build</b>: a small (~${E.step?Math.round(E.step*100):8}–${E.step&&E.max_tiers?Math.round(E.step*E.max_tiers*100):16}%) volume bump on hard weeks as the build progresses, ACWR-capped, recovery weeks protected. <a href="#" id="earnedToggle" data-on="1">opt in</a></div>`
    : "");
  // §6h — CTL-responsive volume floor: only surfaced when it's actually lifting volume (it's dormant
  // until measured fitness outruns the conservative ramp — no action needed, it's automatic).
  const CF = p.ctl_floor || {};
  const ctlFloorNote = (CF.active)
    ? `<div class="gradnote">▲ Your volume is tracking your <b>fitness</b> — measured CTL ${CF.anchor_ctl} has outrun the default ramp, so the engine raised Base/Build volume to match (~${CF.floor_km} km/wk floor). The ACWR ≤1.25 ceiling and recovery weeks are unchanged. This is your faster-than-expected rebuild being rewarded automatically.</div>`
    : "";
  // §6e — earned FREQUENCY advance (the 6th run). Sibling of the volume lift above, its own opt-in:
  // banked weeks earn a 6th weekly run on the hard Base/Build weeks at the SAME volume (shorter runs,
  // more frequency for durability — honestly a tradeoff: more loading cycles, not "easier").
  const Q = p.freq || {};
  const freqNote = SH_READONLY ? "" : (
    Q.active
      ? `<div class="gradnote">▲ Earned 6th run is <b>on</b> — you've banked ${Q.banked_streak} solid week${Q.banked_streak===1?'':'s'}, so the hard Base/Build weeks run <b>6×</b> instead of 5× at the same weekly volume (shorter, more frequent runs — durability, not a heavier week). Recovery (down) weeks keep fewer runs and the ACWR ≤1.25 ceiling is untouched. <a href="#" id="freqToggle" data-on="0">turn off</a></div>`
    : Q.opted_in
      ? `<div class="legend" style="margin-top:6px">▲ Earned 6th run is on — ${Q.banked_streak>=Q.bank_at?(Q.ready_ok?'applying now':'paused until readiness is green'):`${Q.bank_at-Q.banked_streak} more banked week${Q.bank_at-Q.banked_streak===1?'':'s'} to unlock`}. <a href="#" id="freqToggle" data-on="0">turn off</a></div>`
    : Q.banked_streak>=Q.bank_at
      ? `<div class="gradnote">▲ You've banked ${Q.banked_streak} solid weeks — you can opt into an <b>earned 6th weekly run</b> on the hard Base/Build weeks: same weekly volume, spread over one more day (shorter, more frequent runs for durability), recovery weeks and the ACWR ceiling protected. It stays at 5 runs until your volume is high enough that the 6th run is real training (~4 km+), so it's quiet for now. <a href="#" id="freqToggle" data-on="1">opt in</a></div>`
    : "");
  const tuneTxt = (p.tune_ups&&p.tune_ups.length)
    ? `<div class="legend" style="margin-top:8px">Tune-ups before the peak: ${p.tune_ups.map(t=>`${esc(t.label)} (${t.date}, ${t.priority})`).join(" · ")}</div>` : "";
  const header = o
    ? `<div class="objline">
        <span class="race">${esc(o.label)}</span>
        <span class="away">${o.weeks_away} weeks away · ${o.date}</span>
        <span class="away" style="color:var(--muted)">goal: ${o.target} · priority ${o.priority||'A'}</span>
      </div>`
    : `<div class="objline"><span class="race">Maintenance</span>
        <span class="away" style="color:var(--muted)">no objective — holding fitness</span></div>`;
  // Per-phase week lists, each behind its bar segment. Only the active phase's panel is shown;
  // clicking a segment swaps which one is open (wired below). Empty phases render no panel.
  // §6q — one panel per non-rebase phase in p.phases (single-A = base/build/peak/taper; a chain adds
  // its bridge/peak/taper segments), keyed by the phase's own key so segments never collide.
  const panel=(k,inner)=>inner?`<div class="phasepanel${k===curPhase?' active':''}" data-pk="${k}">${inner}</div>`:'';
  const phasePanels = o ? (p.phases||[]).filter(x=>x.key&&x.key!=="rebase")
      .map(x=>panel(x.key, phaseSection(x.phase, p[x.key], p, today, x.key))).join("") : '';
  const rebaseInner=`
    <h3 style="font-family:var(--serif);font-weight:600;font-size:16px;margin:18px 0 2px;display:flex;justify-content:space-between;align-items:baseline;gap:12px">
      <span>Phase 0 — the re-base block <span class="muted mono" style="font-size:11px">(start ${rb.start}, ends CTL ${rb.end_ctl}/ATL ${rb.end_atl})${adh}</span>${qhint("CTL = chronic load (your fitness), a slow ~42-day average of training; ATL = acute load (recent fatigue), a fast ~7-day average. Shown here is each value projected to this phase's end.")}</span>
      <span style="text-align:right;line-height:1.4">
        <div class="mono" style="font-size:12px;font-weight:600;color:var(--muted);white-space:nowrap" title="Total planned distance and easy-run time across the re-base block">${phaseTot}</div>
        ${ranTot?`<div class="mono" style="font-size:11px;font-weight:600;color:var(--ok);white-space:nowrap" title="Distance and time you've actually run during this block so far (duplicates excluded)">${ranTot}</div>`:""}
      </span>
    </h3>
    ${weeks}`;
  host.innerHTML=`
    ${diffBanner(LASTDIFF)}
    ${objManager(p)}
    ${header}
    ${adjustmentUI(p)}
    ${SH_READONLY?'':`<div class="explainrow">
      <button id="explainBtn" ${LLM_OK?'':'disabled'}>📖 Explain this plan${LLM_OK?'':' — set ANTHROPIC_API_KEY'}</button>
    </div>
    <div id="planExplain"></div>`}
    <p class="feas">${esc(p.feasibility.note).replace(/\*\*(.+?)\*\*/g,'<b>$1</b>')}</p>
    <div class="phases">${phaseBar}</div>
    <div class="legend" style="margin-top:4px">${o?`Periodization to race day (${totalw} weeks) · tap a phase to open its weeks`:'Re-base, then hold'}</div>
    ${tuneTxt}
    <div class="zones">${zoneChips}</div>
    ${refreshed}
    <p class="feas" style="border-color:var(--warn)">${esc(p.note).replace(/THRESHOLD/,'<b>THRESHOLD</b>')}</p>
    ${grad}${earnedNote}${ctlFloorNote}${freqNote}
    <div class="phasepanels">
      ${panel('rebase',rebaseInner)}
      ${phasePanels}
    </div>`;
  // The phase bar acts as a selector: clicking a segment opens that phase's weeks and closes the rest.
  host.querySelectorAll(".phaseseg").forEach(seg=>seg.addEventListener("click",()=>{
    const pk=seg.dataset.pk;
    if(!host.querySelector(`.phasepanel[data-pk="${pk}"]`)) return;  // no weeks for this phase
    host.querySelectorAll(".phaseseg").forEach(s=>s.classList.toggle("active",s===seg));
    host.querySelectorAll(".phasepanel").forEach(pn=>pn.classList.toggle("active",pn.dataset.pk===pk));
  }));
  // The week strip is the second-level selector: clicking a week shows only its detail, scoped to
  // its own phase (data-pk + data-wk) so e.g. W1 of Base doesn't toggle W1 of Build.
  host.querySelectorAll(".weekseg").forEach(seg=>seg.addEventListener("click",()=>{
    const pk=seg.dataset.pk, wk=seg.dataset.wk;
    host.querySelectorAll(`.weekstrip[data-pk="${pk}"] .weekseg`).forEach(s=>s.classList.toggle("active",s===seg));
    host.querySelectorAll(`.weekdetail[data-pk="${pk}"]`).forEach(d=>d.classList.toggle("active",d.dataset.wk===wk));
  }));
  // a completed session → load that day's run into the activity tile + its route map
  host.querySelectorAll(".sline.sclick").forEach(el=>el.addEventListener("click",()=>{
    loadActivity(+el.dataset.actId);
    const r=document.getElementById("recent"); if(r) r.scrollIntoView({behavior:"smooth", block:"start"});
  }));
  // a double's per-run links → that specific run's map (stop bubbling so the line's own click doesn't fire)
  host.querySelectorAll(".brkrun[data-act-id]").forEach(el=>el.addEventListener("click",ev=>{
    ev.stopPropagation();
    loadActivity(+el.dataset.actId);
    const r=document.getElementById("recent"); if(r) r.scrollIntoView({behavior:"smooth", block:"start"});
  }));
  wireObjActions();
  wireAdjust();
  const et = document.getElementById("earnedToggle");
  if(et) et.addEventListener("click", async ev=>{
    ev.preventDefault(); et.style.pointerEvents="none";
    try{
      await fetch("/api/earned",{method:"POST",headers:{"Content-Type":"application/json"},
                                 body:JSON.stringify({on: et.dataset.on==="1"})});
      // regenerate so the lift takes effect and the change stays versioned/diff-able (§6b)
      const r=await fetch("/api/plan/generate",{method:"POST"}); const p=await r.json();
      if(p.ok){ LASTDIFF=p.diff; await refreshPlan(p); } else { await refreshPlan(); }
    }catch(e){ alert("Could not update earned setting: "+e); }
  });
  const ft = document.getElementById("freqToggle");
  if(ft) ft.addEventListener("click", async ev=>{
    ev.preventDefault(); ft.style.pointerEvents="none";
    try{
      await fetch("/api/freq",{method:"POST",headers:{"Content-Type":"application/json"},
                               body:JSON.stringify({on: ft.dataset.on==="1"})});
      // regenerate so the 6th run takes effect and the change stays versioned/diff-able (§6b)
      const r=await fetch("/api/plan/generate",{method:"POST"}); const p=await r.json();
      if(p.ok){ LASTDIFF=p.diff; await refreshPlan(p); } else { await refreshPlan(); }
    }catch(e){ alert("Could not update 6th-run setting: "+e); }
  });
}
async function refreshPlan(p){
  OBJECTIVES = await getJSON("/api/objectives");
  try{ LOG = await getJSON("/api/log"); }catch(e){ LOG=null; }
  renderPlan(p || await getJSON("/api/plan"));
  loadDrift();   // the plan (or its history) may have moved — refresh the drift view
}
async function loadPlan(){ await refreshPlan(); }
$("#planBtn").addEventListener("click", async ()=>{
  const b=$("#planBtn"); b.disabled=true; const t=b.textContent; b.textContent="Generating…";
  try{
    const r=await fetch("/api/plan/generate",{method:"POST"}); const p=await r.json();
    if(!p.ok){ alert("Plan failed: "+(p.error||"unknown")); } else { LASTDIFF=p.diff; await refreshPlan(p); }
  }catch(e){ alert("Plan error: "+e); }
  finally{ b.disabled=false; b.textContent=t; }
});

// ── Fitness & fatigue trend (projector) ─────────────────────────────────────
async function loadProjector(){
  const d = await getJSON("/api/projector?days=180");
  const h = d.history||[];
  const host = $("#ffchart");
  if(!h.length){ host.innerHTML = `<div class="empty">No activities synced yet.</div>`; return; }
  const W=1000, H=200, pad=24;
  const ctls=h.map(p=>p.ctl), atls=h.map(p=>p.atl);
  const hi=Math.max(...ctls,...atls), lo=Math.min(0,...ctls,...atls);
  const x=i=>pad+i*(W-2*pad)/(h.length-1);
  const y=v=>H-pad-(v-lo)/(hi-lo)*(H-2*pad);
  const path=key=>h.map((p,i)=>`${i?"L":"M"}${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join(" ");
  let ticks="", lastMonth="";
  h.forEach((p,i)=>{ const m=p.date.slice(0,7); if(m!==lastMonth){ lastMonth=m;
    ticks+=`<text class="axis" x="${x(i).toFixed(1)}" y="${H-6}">${p.date.slice(2,7)}</text>`; }});
  host.innerHTML = `<svg class="ff" id="ffsvg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <line class="zero" x1="${pad}" y1="${y(0).toFixed(1)}" x2="${W-pad}" y2="${y(0).toFixed(1)}"/>
    <path class="atl" d="${path('atl')}"/>
    <path class="ctl" d="${path('ctl')}"/>
    <text class="axis" x="2" y="${y(hi).toFixed(1)}">${hi.toFixed(0)}</text>
    ${ticks}
    <g id="ffcross" style="display:none">
      <line class="cross" id="ffline" y1="0" y2="${H-14}"/>
      <circle class="ffdot ctl" id="ffdc" r="3.5"/><circle class="ffdot atl" id="ffda" r="3.5"/>
    </g>
  </svg>
  <div class="fftip" id="fftip" style="display:none"></div>`;
  const v=d.validation;
  if(v){
    const gap=Math.abs(v.modeled.atl-v.runalyze.atl);
    if(d.duplicate_count>0 && gap>8){
      $("#ffvalid").innerHTML =
        `<b style="color:var(--warn)">Heads-up:</b> your training history models to CTL <b>${v.modeled.ctl}</b> / `+
        `ATL <b>${v.modeled.atl}</b>, but Runalyze currently reports ${v.runalyze.ctl}/${v.runalyze.atl}. `+
        `That gap lines up with ${d.duplicate_count} detected duplicate(s) — Runalyze's figure looks inflated by them. `+
        `This chart uses the de-duplicated, history-consistent values; fix the duplicate on Runalyze to reconcile `+
        `(or 🗑 Delete from local copy if you already removed it on Runalyze).`;
    } else {
      $("#ffvalid").innerHTML =
        `Model validated against Runalyze — today: CTL <b>${v.modeled.ctl}</b> vs ${v.runalyze.ctl}, `+
        `ATL <b>${v.modeled.atl}</b> vs ${v.runalyze.atl} (τ ${v.tau_ctl}/${v.tau_atl}d). `+
        `Reconstructed so you have a full curve from one daily snapshot.`;
    }
  }
  // hover crosshair: map cursor x → nearest day, draw line + dots + tooltip
  const svg=$("#ffsvg"), g=$("#ffcross"), tip=$("#fftip");
  function onMove(e){
    const rect=svg.getBoundingClientRect();
    const px=(e.clientX-rect.left)/rect.width*W;
    let i=Math.round((px-pad)/((W-2*pad)/(h.length-1)));
    i=Math.max(0,Math.min(h.length-1,i)); const p=h[i];
    $("#ffline").setAttribute("x1",x(i)); $("#ffline").setAttribute("x2",x(i));
    $("#ffdc").setAttribute("cx",x(i)); $("#ffdc").setAttribute("cy",y(p.ctl));
    $("#ffda").setAttribute("cx",x(i)); $("#ffda").setAttribute("cy",y(p.atl));
    g.style.display="block";
    const dt=new Date(p.date+"T00:00");
    tip.style.display="block";
    tip.style.left=Math.min(rect.width-150,(e.clientX-rect.left)+12)+"px";
    tip.innerHTML=`<b>${dt.toLocaleDateString(undefined,{day:"numeric",month:"short",year:"2-digit"})}</b>`+
      `<span class="t-ctl">Fitness ${p.ctl.toFixed(1)}</span>`+
      `<span class="t-atl">Fatigue ${p.atl.toFixed(1)}</span>`+
      `<span class="muted">Form ${p.tsb.toFixed(1)} · ACWR ${p.acwr??"—"}</span>`;
  }
  svg.addEventListener("mousemove",onMove);
  svg.addEventListener("mouseleave",()=>{g.style.display="none";tip.style.display="none";});
}

// ── Plan drift — the original road vs the road as it stands (§6b, visible) ───
const ISO2T = iso => new Date(iso+"T00:00:00").getTime();
function driftLegend(lines){
  return `<div class="legend">`+lines.filter(l=>l.pts&&l.pts.length).map(l=>
    `<span style="color:${l.color}"><i style="${l.dash?'border-top-style:dashed':''}"></i>${l.label}</span>`
  ).join("")+`</div>`;
}
function mkChart(host, lines, {fmt=v=>v.toFixed(0), zeroBase=false, nowT=null, dots=false}={}){
  const all=lines.flatMap(l=>l.pts||[]);
  if(all.length<2){ host.innerHTML=`<div class="empty">Not enough history yet — the road will visibly move as results come in.</div>`; return; }
  const W=1000, H=170, padL=34, padR=14, padT=10, padB=18;
  const xs=all.map(p=>ISO2T(p.date)), x0=Math.min(...xs), x1=Math.max(...xs);
  let lo=Math.min(...all.map(p=>p.val)), hi=Math.max(...all.map(p=>p.val));
  if(zeroBase) lo=0;
  if(hi===lo) hi=lo+1;
  const m=(hi-lo)*0.08; hi+=m; if(!zeroBase) lo-=m;
  const X=t=>padL+(x1===x0?0.5:(t-x0)/(x1-x0))*(W-padL-padR);
  const Y=v=>H-padB-(v-lo)/(hi-lo)*(H-padT-padB);
  const dpath=pts=>pts.map((p,i)=>`${i?"L":"M"}${X(ISO2T(p.date)).toFixed(1)},${Y(p.val).toFixed(1)}`).join(" ");
  // month gridlines + labels
  let grid="", gd=new Date(x0); gd.setDate(1); gd.setHours(0,0,0,0);
  if(gd.getTime()<x0) gd.setMonth(gd.getMonth()+1);
  for(let i=0; gd.getTime()<=x1 && i<40; i++){
    const gx=X(gd.getTime()).toFixed(1);
    grid+=`<line class="grid" x1="${gx}" y1="2" x2="${gx}" y2="${H-padB}"/>`+
      `<text class="axis" x="${gx}" y="${H-5}" text-anchor="middle">${gd.toLocaleDateString(undefined,{month:"short"})}</text>`;
    gd.setMonth(gd.getMonth()+1);
  }
  const nowln = (nowT!=null && nowT>=x0 && nowT<=x1)
    ? `<line class="now" x1="${X(nowT).toFixed(1)}" y1="2" x2="${X(nowT).toFixed(1)}" y2="${H-padB}"/>` : "";
  const paths=lines.map(l=>(l.pts&&l.pts.length)
    ? `<path class="dl ${l.cls}" ${l.dash?'stroke-dasharray="5 4"':''} d="${dpath(l.pts)}"/>` : "").join("");
  // dots: everywhere when asked, plus any single-point series (an invisible zero-length path)
  const dotsm=lines.flatMap(l=>(dots||(l.pts||[]).length===1?(l.pts||[]):[]).map(p=>
    `<circle class="ddot" cx="${X(ISO2T(p.date)).toFixed(1)}" cy="${Y(p.val).toFixed(1)}" r="3" style="fill:${l.color}"/>`)).join("");
  host.innerHTML=`<div class="driftwrap"><svg class="drift" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    ${grid}
    <text class="axis" x="2" y="${(Y(hi)+9).toFixed(1)}">${fmt(hi)}</text>
    <text class="axis" x="2" y="${Y(lo).toFixed(1)}">${fmt(lo)}</text>
    ${nowln}${paths}${dotsm}
    <g class="dcross" style="display:none"><line class="cross" y1="2" y2="${H-padB}"/></g>
  </svg><div class="drifttip"></div></div>`;
  const svg=host.querySelector("svg"), g=host.querySelector(".dcross"),
        cl=g.querySelector("line"), tip=host.querySelector(".drifttip");
  const dates=[...new Set(all.map(p=>p.date))].sort();
  svg.addEventListener("mousemove",e=>{
    const rect=svg.getBoundingClientRect(), px=(e.clientX-rect.left)/rect.width*W;
    let best=dates[0], bd=1e18;
    for(const dt of dates){ const dx=Math.abs(X(ISO2T(dt))-px); if(dx<bd){bd=dx;best=dt;} }
    const gx=X(ISO2T(best)); cl.setAttribute("x1",gx); cl.setAttribute("x2",gx); g.style.display="block";
    const rows=lines.map(l=>{ const p=(l.pts||[]).find(q=>q.date===best);
      return p?`<span style="color:${l.color}">${l.label}: ${fmt(p.val)}</span>`:""; }).filter(Boolean).join("");
    const dt=new Date(best+"T00:00:00");
    tip.style.display="flex"; tip.style.left=Math.min(rect.width-160,(e.clientX-rect.left)+12)+"px";
    tip.innerHTML=`<b>${dt.toLocaleDateString(undefined,{day:"numeric",month:"short",year:"2-digit"})}</b>${rows}`;
  });
  svg.addEventListener("mouseleave",()=>{g.style.display="none";tip.style.display="none";});
}
// scorecard — the four series synthesized into one 'who's winning' read (§6j). Engine owns the
// numbers AND the templated headline; this only formats them. ahead→ok, behind→danger, level→muted.
function scoreRow(k, main, sub, cls){
  return `<div class="sc-row"><span class="sc-k">${esc(k)}</span>`+
    `<span class="sc-v ${cls}">${main}${sub?` <span class="sub">${sub}</span>`:""}</span></div>`;
}
function scorecardHTML(sc, r){
  if(!sc) return "";
  const sign=g=>(g>0?"+":"")+g;
  const v=sc.volume, f=sc.fitness, rc=sc.race;
  const volMain = v.state==="unknown"?"—" : v.state==="level"?"on the road" : `${sign(v.gap)} km ${v.state}`;
  const fitMain = f.state==="unknown"?"—" : f.state==="level"?"CTL on track" : `CTL ${sign(f.gap)} ${f.state}`;
  let raceMain, raceCls, raceSub="";
  if(rc.now==null){ raceMain="—"; raceCls="unknown"; }
  else if(rc.caveat){ raceMain=`proj ${Math.round(rc.now)}`; raceCls="unknown"; raceSub="duplicate caveat"; }
  else { raceCls = rc.trend==="gaining"?"ahead":rc.trend==="slipping"?"behind":"level";
         raceMain = `${Math.round(rc.founding)} → ${Math.round(rc.now)}`; raceSub = rc.trend; }
  const sub = s => s.state==="unknown"?"":"vs founding road";
  const wks = sc.weeks_to_go!=null?`<span class="wks"> — ${sc.weeks_to_go}w to go</span>`:"";
  return `<div class="scorecard"><div class="sc-head">The road vs the road as it stands</div>`+
    `<div class="sc-rows">`+
      scoreRow("Volume", volMain, sub(v), v.state)+
      scoreRow("Fitness", fitMain, sub(f), f.state)+
      scoreRow(r&&r.label?r.label:"Race day", raceMain, raceSub, raceCls)+
    `</div><div class="sc-verdict">${esc(sc.headline)}${wks}</div></div>`;
}
// §6m — effort discipline: HR-led "are your easy days actually easy?" Judged by heart rate (terrain
// & heat already live in HR), TE corroborates, GAP shown as terrain-fair pace.
const EFFP = s => s ? `${Math.floor(s/60)}:${String(s%60).padStart(2,"0")}` : "—";
const EFFV = {on:["var(--ok)","on target"], hot:["var(--warn)","hot"],
              too_hard:["var(--danger)","too hard"], too_easy:["var(--muted)","too easy"],
              unknown:["var(--muted)","—"]};
async function loadEffort(){
  const host=$("#effort"); if(!host) return;
  if(SH_READONLY){ const s=$("#sec-effort"); if(s) s.style.display="none"; return; }  // private-only (per-run HR + critique)
  let d; try{ d=await getJSON("/api/effort-discipline"); }catch(e){ return; }
  if(!d || d.easy_score==null){
    host.innerHTML=`<div class="empty">Not enough heart-rate runs in the last ${d&&d.window_days||21} days yet — this reads your synced runs' HR.</div>`; return; }
  const c=d.easy_counts, score=d.easy_score;
  const tone = score>=80?"var(--ok)":score>=50?"var(--warn)":"var(--danger)";
  const verdict = score>=80?"dialed in":score>=50?"drifting hard":"easy days are threshold days";
  const rows=(d.runs||[]).map(r=>{
    const [col,lbl]=EFFV[r.verdict]||EFFV.unknown;
    const tag = r.confidence==="high"?"":r.confidence==="low"?` <span class="muted" style="font-weight:400">·low conf</span>`:"";
    return `<tr><td class="mono">${esc(r.date.slice(5))}</td><td>${esc(r.kind)}</td>`+
      `<td class="mono">${r.km}k</td><td class="mono">${EFFP(r.gap_pace)}</td>`+
      `<td class="mono">${r.hr_avg} <span class="muted">${r.hr_pct}%</span></td>`+
      `<td class="mono">${r.te==null?"—":r.te}</td>`+
      `<td class="mono">${r.feeling==null?"—":r.feeling+"/5"}</td>`+
      `<td style="color:${col};font-weight:600">${lbl}${tag}</td></tr>`;
  }).join("");
  host.innerHTML=`
    <div class="effort-head">
      <div class="effort-score" style="color:${tone}">${score}<span class="pct">%</span></div>
      <div class="effort-cap">
        <div class="big">Easy discipline — <b style="color:${tone}">${verdict}</b></div>
        <div class="muted">Last ${d.window_days} days · <b>${c.on}/${c.judged}</b> easy runs stayed aerobic · easy-HR ceiling ≈ <b>${d.easy_hr_ceiling}</b> bpm (78% of HRmax ${d.hrmax}).${c.too_hard?` <b style="color:var(--danger)">${c.too_hard}</b> ran at threshold effort.`:""}</div>
        <div class="muted" style="font-size:11px;margin-top:4px">Judged by heart rate, not pace — terrain &amp; heat already live in your HR. Pace shown is grade-adjusted (GAP). Runs with no matching plan session (incl. before the plan existed) are judged against the easy default; one mismatch is an observation, not a verdict.</div>
      </div>
    </div>
    <table class="efftbl"><thead><tr><th>date</th><th>session</th><th>dist</th>
      <th>GAP ${qhint("Grade Adjusted Pace — your pace corrected for hills so efforts compare fairly across terrain (from Runalyze). Runs here are judged on heart rate, not this.")}</th>
      <th>avg HR</th>
      <th>TE ${qhint("Training Effect — Runalyze/Firstbeat's 1–5 aerobic-stress rating (intensity × duration). It only corroborates the heart-rate read here, it never overrides it.")}</th>
      <th>feel</th>
      <th>verdict ${qhint("How this run's effort compared to its prescription — graded by heart rate (terrain and heat already live in your HR), not pace.")}</th></tr></thead><tbody>${rows}</tbody></table>`;
}
async function loadDrift(){
  const host=$("#drift"); if(!host) return;
  let d; try{ d=await getJSON("/api/plandrift"); }catch(e){ return; }
  if(!d || !d.ok){ host.innerHTML=`<div class="empty">${(d&&d.error)||"No plan history yet."}</div>`; return; }
  const MUTED="var(--muted)", ACC="var(--accent)";
  // 1 — cumulative distance
  const dc=d.distance.current||[];
  const act=dc.filter(p=>p.kind==="actual").map(p=>({date:p.date,val:p.cum}));
  const prj=dc.filter(p=>p.kind==="proj").map(p=>({date:p.date,val:p.cum}));
  const distLines=[
    {pts:(d.distance.initial||[]).map(p=>({date:p.date,val:p.cum})), cls:"init", color:MUTED, label:"Original road"},
    {pts:act, cls:"actual", color:ACC, label:"Run so far"},
    {pts:act.length?[act[act.length-1],...prj]:prj, cls:"proj", dash:true, color:ACC, label:"Projected"},
  ];
  // 2 — weekly training load (effort / intensity)
  const effLines=[
    {pts:(d.effort.initial||[]).map(p=>({date:p.date,val:p.trimp})), cls:"init", color:MUTED, label:"Original load"},
    {pts:(d.effort.actual||[]).map(p=>({date:p.date,val:p.trimp})), cls:"actual", color:ACC, label:"Done so far"},
    {pts:(d.effort.current||[]).map(p=>({date:p.date,val:p.trimp})), cls:"proj", dash:true, color:ACC, label:"Prescribed now"},
  ];
  // 3 — fitness trajectory (CTL)
  const ctlLines=[
    {pts:(d.ctl.initial||[]).map(p=>({date:p.date,val:p.ctl})), cls:"init", color:MUTED, label:"Original projection"},
    {pts:(d.ctl.actual||[]).map(p=>({date:p.date,val:p.ctl})), cls:"actual", color:ACC, label:"Actual fitness"},
    {pts:(d.ctl.current||[]).map(p=>({date:p.date,val:p.ctl})), cls:"proj", dash:true, color:ACC, label:"Projected now"},
  ];
  // 4 — projected race-day fitness, version over version
  const outLines=[{pts:(d.outcome||[]).map(p=>({date:p.date,val:p.ctl})), cls:"actual", color:ACC, label:"Projected race CTL"}];
  const a=d.anchor, r=d.race;
  let cap=`Baseline: plan of <b style="color:var(--text)">${a.for_date}</b>`+
    (a.is_current?` — just sealed (the first complete road); drift accrues from here`:"")+
    ` · ${a.versions} version${a.versions>1?"s":""} on record`+
    (r.label?` · ${esc(r.label)}${r.weeks_away!=null?` (${r.weeks_away}w out)`:""}`:"");
  if(d.duplicate_count>0) cap+=`<br><span class="warn">⚠ ${d.duplicate_count} duplicate activity is inflating the snapshot the plan seeds from — fix it in Runalyze to clean the projection (actuals here already ignore it; if you already removed it on Runalyze, use 🗑 Delete from local copy to drop the leftover row).</span>`;
  host.innerHTML=`${scorecardHTML(d.scorecard, r)}<div class="driftcap">${cap}</div>
    <div class="driftblock"><h3>Cumulative distance</h3>
      <p class="note">The founding road versus what you've actually run plus the current plan's projection forward. The widening gap is volume drift — ahead of, or behind, the original plan.</p>
      ${driftLegend(distLines)}<div id="drift-dist"></div></div>
    <div class="driftblock"><h3>Weekly training load · effort</h3>
      <p class="note">The intensity half of the road: each week's prescribed load (TRIMP), original versus now, against what you actually did. When the plan eases — sickness, missed weeks, low readiness — this line drops below the founding plan; when results earn it, it rises.</p>
      ${driftLegend(effLines)}<div id="drift-eff"></div></div>
    <div class="driftblock"><h3>Fitness trajectory · CTL</h3>
      <p class="note">What the original plan projected your fitness would do, against your real reconstructed CTL continued by today's projection. The engine's true currency — distance is the volume view, this is the fitness view.</p>
      ${driftLegend(ctlLines)}<div id="drift-ctl"></div></div>
    <div class="driftblock"><h3>Projected race-day fitness</h3>
      <p class="note">The race-day CTL the engine projected at each re-plan. Is your goal race getting more or less reachable as your results come in?</p>
      ${driftLegend(outLines)}<div id="drift-out"></div></div>`;
  mkChart($("#drift-dist"), distLines, {zeroBase:true, fmt:v=>v.toFixed(0)+" km", nowT:ISO2T(d.today)});
  mkChart($("#drift-eff"), effLines, {zeroBase:true, fmt:v=>v.toFixed(0), nowT:ISO2T(d.today)});
  mkChart($("#drift-ctl"), ctlLines, {fmt:v=>v.toFixed(0), nowT:ISO2T(d.today)});
  mkChart($("#drift-out"), outLines, {fmt:v=>v.toFixed(0), dots:true});
}

// ── Health markers ──────────────────────────────────────────────────────────
let MARKERS = {};
function sparkline(points, ref){
  // points: [{date, value}] ascending. ref:[low,high] (either may be null).
  const W=240, H=54, pad=6;
  const vals = points.map(p=>p.value);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  [ref[0], ref[1]].forEach(v=>{ if(v!=null){ lo=Math.min(lo,v); hi=Math.max(hi,v); }});
  if(hi===lo) hi=lo+1;
  const x = i => pad + (points.length<2 ? (W-2*pad)/2 : i*(W-2*pad)/(points.length-1));
  const y = v => H-pad - (v-lo)/(hi-lo)*(H-2*pad);
  let band="";
  if(ref[0]!=null || ref[1]!=null){
    const top = ref[1]!=null ? y(ref[1]) : pad;
    const bot = ref[0]!=null ? y(ref[0]) : H-pad;
    band = `<rect class="refband" x="0" y="${Math.min(top,bot).toFixed(1)}" width="${W}" height="${Math.abs(bot-top).toFixed(1)}"/>`;
    if(ref[1]!=null) band += `<line class="refline" x1="0" y1="${y(ref[1]).toFixed(1)}" x2="${W}" y2="${y(ref[1]).toFixed(1)}"/>`;
    if(ref[0]!=null) band += `<line class="refline" x1="0" y1="${y(ref[0]).toFixed(1)}" x2="${W}" y2="${y(ref[0]).toFixed(1)}"/>`;
  }
  const d = points.map((p,i)=>`${i?"L":"M"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(" ");
  const last = points[points.length-1];
  const dot = `<circle class="sparkdot" cx="${x(points.length-1).toFixed(1)}" cy="${y(last.value).toFixed(1)}" r="3"/>`;
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${band}<path class="spark" d="${d}"/>${dot}</svg>`;
}
function inRange(v, ref){ return (ref[0]==null||v>=ref[0]) && (ref[1]==null||v<=ref[1]); }

async function loadHealth(){
  const d = await getJSON("/api/health");
  MARKERS = d.markers;
  // populate the add-form marker dropdown once
  const sel = $("#hmarker");
  if(!sel.options.length){
    sel.innerHTML = Object.entries(MARKERS).map(([k,m])=>`<option value="${k}">${m.label} (${m.unit})</option>`).join("");
  }
  const host = $("#health");
  const present = Object.keys(d.series||{});
  if(!present.length){ host.innerHTML = `<div class="empty">No markers yet — add one below.</div>`; return; }
  host.innerHTML = present.map(k=>{
    const m = MARKERS[k]||{label:k,unit:"",ref:[null,null],good:"band"};
    const pts = d.series[k];
    const last = pts[pts.length-1];
    const ok = m.good==="band" ? inRange(last.value,m.ref)
             : m.good==="low"  ? (m.ref[1]==null||last.value<=m.ref[1])
             :                    (m.ref[0]==null||last.value>=m.ref[0]);
    const refTxt = m.ref[0]!=null&&m.ref[1]!=null ? `${m.ref[0]}–${m.ref[1]}`
                 : m.ref[1]!=null ? `&lt; ${m.ref[1]}` : m.ref[0]!=null ? `&gt; ${m.ref[0]}` : "";
    return `<div class="hcard">
      <div class="hk">${m.label}</div>
      <div class="hv">${fmt(last.value, last.value%1?1:0)}<small> ${m.unit}</small>
        ${refTxt?`<span class="flag ${ok?"ok":"bad"}">${ok?"ok":"watch"} ${refTxt}</span>`:""}</div>
      ${sparkline(pts, m.ref)}
      <div class="hk" style="margin-top:6px;text-transform:none;letter-spacing:0">
        ${pts.length} readings · ${pts[0].date} → ${last.date}</div>
    </div>`;
  }).join("");
}

const _hform=$("#hform");
if(_hform) _hform.addEventListener("submit", async e=>{
  e.preventDefault();
  const body = {marker:$("#hmarker").value, date:$("#hdate").value, value:$("#hvalue").value, source:"manual"};
  const r = await fetch("/api/health",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const d = await r.json();
  if(!d.ok){ alert("Could not add: "+(d.error||"unknown")); return; }
  $("#hvalue").value=""; await loadHealth();
});

// ── Settings (private-only, in a modal) — edit the non-secret personalization that otherwise comes
// from SH_* env. Values are stored in the DB (meta) and override env; secrets are never shown/settable.
// "Name,lat,lon[,CODE];…" ⇄ [{name,lat,lon,code}] for the weather city picker (backend format unchanged).
function parseCities(str){
  return (str||"").split(";").map(s=>s.trim()).filter(Boolean).map(p=>{
    const b=p.split(",").map(x=>x.trim());
    return {name:b[0], lat:b[1], lon:b[2], code:((b[3]||(b[0]||"").slice(0,3))||"").toUpperCase()};
  }).filter(c=>c.name && c.lat && c.lon);
}
function serializeCities(arr){ return arr.map(c=>`${c.name},${c.lat},${c.lon},${c.code}`).join(";"); }

async function loadSettings(){
  const host=$("#settings"); if(!host) return;
  let d; try{ d=await getJSON("/api/settings"); }catch(e){ host.innerHTML=`<div class="empty">Could not load settings.</div>`; return; }
  if(!d.ok){ host.innerHTML=`<div class="empty">${esc(d.error||"unavailable")}</div>`; return; }
  // data-orig = the value as loaded, so saveSettings posts ONLY the fields the user changed —
  // posting an untouched env/default-sourced value would persist it to meta and shadow the env.
  const cityField=s=>`<div class="setrow">
      <label>${esc(s.label)}<span class="src">${esc(s.source)}</span></label>
      <input type="hidden" id="set_weather_cities" data-key="weather_cities" data-orig="${esc(s.value||"")}" value="${esc(s.value||"")}">
      <div class="wxchips" id="wxchips"></div>
      <div class="wxsearchrow">
        <input id="wxsearch" type="text" placeholder="Search a city — e.g. Lisbon" autocomplete="off">
        <button type="button" class="ghost" id="wxsearchbtn">Search</button>
      </div>
      <div class="err" id="wxcap"></div>
      <ul class="wxresults" id="wxresults"></ul>
      <div class="help">Type a city and pick it — the coordinates are resolved for you. Up to 5; no cities = the widget is hidden.</div>
      <div class="err" id="err_weather_cities"></div>
    </div>`;
  const field=s=>{
    if(s.key==="weather_cities") return cityField(s);
    const id="set_"+s.key;
    const ctl = s.kind==="text"
      ? `<textarea id="${id}" data-key="${s.key}" data-orig="${esc(s.value||"")}">${esc(s.value||"")}</textarea>`
      : `<input id="${id}" data-key="${s.key}" data-orig="${esc(s.value||"")}" type="text" value="${esc(s.value||"")}">`;
    return `<div class="setrow">
      <label for="${id}">${esc(s.label)}<span class="src">${esc(s.source)}</span></label>
      ${ctl}
      <div class="help">${esc(s.help)}</div>
      <div class="err" id="err_${s.key}"></div>
    </div>`;
  };
  host.innerHTML=`<form class="setform" id="setform">
    ${d.settings.map(field).join("")}
    <div class="setbar"><button class="primary" type="submit">Save settings</button>
      <span class="ok" id="setok"></span></div>
  </form>`;
  $("#setform").addEventListener("submit", saveSettings);
  wireCityPicker();
}

const MAX_CITIES=5;   // mirrors the server's MAX_WEATHER_CITIES (validated there too)
function wireCityPicker(){
  const hidden=$("#set_weather_cities"); if(!hidden) return;
  let cities=parseCities(hidden.value);
  const chips=$("#wxchips"), results=$("#wxresults"), search=$("#wxsearch"),
        searchBtn=$("#wxsearchbtn"), cap=$("#wxcap");
  const sync=()=>{ hidden.value=serializeCities(cities); renderChips(); };
  function renderChips(){
    chips.innerHTML = cities.length
      ? cities.map((c,i)=>`<span class="wxchip"><b>${esc(c.code)}</b> ${esc(c.name)} <button type="button" data-i="${i}" aria-label="Remove ${esc(c.name)}">✕</button></span>`).join("")
      : `<span class="muted" style="font-size:12px">No cities — the widget is hidden.</span>`;
    chips.querySelectorAll("button[data-i]").forEach(b=>b.addEventListener("click",()=>{ cities.splice(+b.dataset.i,1); sync(); }));
    const full = cities.length>=MAX_CITIES;   // at the cap → block adding, prompt a removal
    search.disabled=searchBtn.disabled=full;
    cap.textContent = full ? `Maximum ${MAX_CITIES} cities — remove one to add another.` : "";
    if(full) results.innerHTML="";
  }
  async function doSearch(){
    const q=search.value.trim(); if(q.length<2){ results.innerHTML=""; return; }
    results.innerHTML=`<li class="sub">searching…</li>`;
    let out; try{ out=await getJSON("/api/geocode?q="+encodeURIComponent(q)); }catch(e){ results.innerHTML=`<li class="sub">search failed</li>`; return; }
    if(!out.ok){ results.innerHTML=`<li class="sub">search unavailable</li>`; return; }
    const rs=out.results||[];
    results.innerHTML = rs.length
      ? rs.map((r,i)=>`<li data-i="${i}">${esc(r.name)} <span class="sub">${esc([r.admin1,r.country].filter(Boolean).join(", "))}</span></li>`).join("")
      : `<li class="sub">no matches</li>`;
    results.querySelectorAll("li[data-i]").forEach(li=>li.addEventListener("click",()=>{
      const r=rs[+li.dataset.i];
      const name=(r.name||"").replace(/[,;]/g," ").trim();   // ',' and ';' are delimiters in the stored format
      if(name && cities.length<MAX_CITIES && !cities.some(c=>c.lat==r.lat && c.lon==r.lon)){   // cap + skip dup
        cities.push({name, lat:r.lat, lon:r.lon, code:name.slice(0,3).toUpperCase()});
      }
      search.value=""; results.innerHTML=""; sync();
    }));
  }
  $("#wxsearchbtn").addEventListener("click", doSearch);
  search.addEventListener("keydown", e=>{ if(e.key==="Enter"){ e.preventDefault(); doSearch(); } });
  renderChips();
}
async function saveSettings(e){
  e.preventDefault();
  document.querySelectorAll("#setform .err").forEach(n=>n.textContent="");
  $("#setok").textContent="";
  const payload={};
  document.querySelectorAll("#setform [data-key]").forEach(n=>{
    if(n.value !== n.dataset.orig) payload[n.dataset.key]=n.value;   // changed fields only
  });
  if(Object.keys(payload).length===0){ $("#setok").textContent="No changes"; return; }
  const r=await fetch("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
  const d=await r.json();
  if(!d.ok){
    const errs=d.errors||{};
    Object.keys(errs).forEach(k=>{ const n=$("#err_"+k); if(n) n.textContent="⚠ "+errs[k]; });
    return;
  }
  $("#setok").textContent="Saved ✓";
  loadSettings();          // refresh provenance badges (env → saved)
  if(typeof loadWeather==="function") loadWeather();   // weather cities take effect live
}

// learn whether the LLM layer is configured (§6c) before the plan/objectives render
fetch("/healthz").then(r=>r.json()).then(d=>{ LLM_OK=!!d.llm; loadPlan(); }).catch(()=>loadPlan());
loadShape(); loadRecent(); loadProjector(); loadWeekly(); loadWeather(); loadEffort();
// Settings modal open/close (private only — the button is removed on the public view below).
const _setBtn=$("#settingsBtn"), _setDlg=$("#settingsDialog");
if(_setBtn && _setDlg){
  _setBtn.addEventListener("click", ()=>{ if(!$("#setform")) loadSettings(); _setDlg.showModal(); });  // (re)load if the initial fetch failed
  const _x=$("#settingsClose"); if(_x) _x.addEventListener("click", ()=>_setDlg.close());
  _setDlg.addEventListener("click", e=>{ if(e.target===_setDlg) _setDlg.close(); });  // backdrop click
}
if(SH_READONLY){
  // public view: health markers stay private; the readiness VERDICT tile stays (the server
  // redacts its inputs/HRV/note). Drop the write controls; surface read-only + the Log-in link.
  ["sec-health","settingsDialog"].forEach(id=>{const e=$("#"+id); if(e) e.remove();});
  ["syncBtn","backfillBtn","planBtn","settingsBtn"].forEach(id=>{const e=$("#"+id); if(e) e.remove();});
  loadReadiness();
  const cluster=document.querySelector(".topctl");
  if(cluster){
    let extra='<span class="ro-badge" title="Read-only public view">read-only</span>';
    if(SH_PRIVATE_URL) extra+=`<a class="adminlink" href="${esc(SH_PRIVATE_URL)}" title="Private console">🔒 Log in</a>`;
    cluster.insertAdjacentHTML("afterbegin", extra);
  }
}else{
  loadReadiness(); loadHealth(); loadSettings();
  touchSync();   // pull today's run if it's already on Runalyze, then refresh "done ✓"
}
</script>
</body></html>"""


# ── Scheduled daily sync ─────────────────────────────────────────────────────
# The private (writable, tokened) side pulls the day's activities once a night so the shared DB —
# and so the public page — stays current without a manual "Sync now". Runalyze has no push we can
# rely on, so it's a tiny in-process daily timer: no extra deps, no host cron, runs the same locally
# and on the NAS (under waitress too, since it starts at import). Default 22:00 Luxembourg time
# (late enough to catch the day's runs). Inert on the read-only/tokenless public container.
_scheduler_started = False
# Fire the nightly sync at your wall-clock hour, not the container's. Set SH_TZ to your IANA zone
# (e.g. "Europe/Lisbon", "America/New_York"); defaults to UTC. Falls back to UTC on a bad name.
try:
    SYNC_TZ = ZoneInfo(os.environ.get("SH_TZ", "UTC"))
except Exception:
    SYNC_TZ = ZoneInfo("UTC")


def _seconds_until(hhmm):
    """Seconds until the next HH:MM in Luxembourg local time (DST-aware), so the job fires at the
    same wall-clock hour whatever timezone the container runs in (the NAS containers run UTC)."""
    h, m = (int(x) for x in hhmm.split(":"))
    now = datetime.now(SYNC_TZ)
    nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


def _daily_replan():
    """§6b daily refresh: after the nightly sync, recompute the existing plan so today's date,
    the freshly-synced actuals, the updated shape, and §6e banking are all current without a
    manual 'Generate plan'. This is what makes 'the plan is a function recomputed forward from
    today' true day-to-day. No-ops when no plan exists yet (we refresh one, never auto-create
    one) — and the frozen rebase_start keeps the block from sliding. The stored diff is just
    versioning metadata; it is never surfaced as a 'you changed something' banner in the UI."""
    db = connect_db()
    try:
        if not db.execute("SELECT 1 FROM plans LIMIT 1").fetchone():
            return
        out = regenerate(db)   # saves a new version (save_plan commits)
        print("[scheduler] plan refreshed (daily re-plan)" if out.get("ok")
              else f"[scheduler] plan refresh skipped: {out.get('error')}")
    finally:
        db.close()


def _scheduler_loop(hhmm):
    while True:
        time.sleep(_seconds_until(hhmm))
        try:
            res = run_sync()
            print(f"[scheduler] daily sync ok: {res.get('activities')}")
        except Exception as e:
            print(f"[scheduler] daily sync failed: {e}")
        # §6b — recompute against today even if the sync failed: advancing the plan's date and
        # §6e banking needs no fresh pull, so a flaky Runalyze night must not also freeze the plan.
        try:
            _daily_replan()
        except Exception as e:
            print(f"[scheduler] daily re-plan failed: {e}")
        time.sleep(61)  # step past the trigger minute before recomputing the next wait


def start_scheduler():
    """Start the nightly sync thread — only on a writable, tokened instance, and only once."""
    global _scheduler_started
    if _scheduler_started or READONLY or not RUNALYZE_TOKEN:
        return
    if os.environ.get("SH_SCHEDULE", "1").lower() not in ("1", "true", "yes"):
        return
    hhmm = os.environ.get("SH_SYNC_AT", "22:00")
    threading.Thread(target=_scheduler_loop, args=(hhmm,), daemon=True).start()
    _scheduler_started = True
    print(f"Sparing Horse → scheduled daily sync at {hhmm} {SYNC_TZ.key}")


# ── Self-test harness (§ diagnostics) ───────────────────────────────────────
# A scriptable battery that runs IN-PROCESS on the tokened private instance and records
# structured, verbatim results to `selftest_runs`. The point: the key-gated §6c paths run
# where the key actually lives, so the LLM's real output is captured and correctness judged
# from the report — not relayed by hand. Safety-critical invariants are auto-asserted
# (passed True/False); subjective LLM-quality outputs carry needs_human=True with the output
# captured. Uses only NON-persisting entry points, so a run never mutates training data.
# Private only (gated off the public read-only container).
import time as _time


def _st(cat, sid, desc, *, passed=None, expect=None, got=None, inp=None, output=None,
        note=None, needs_human=False, skipped=False, error=None):
    """One scenario result. passed: True/False, or None for informational/needs-human-only."""
    return {"category": cat, "id": sid, "desc": desc, "expect": expect, "got": got,
            "input": inp, "output": output, "note": note, "needs_human": needs_human,
            "skipped": skipped, "error": error, "ms": None,
            "passed": None if (skipped or passed is None) else bool(passed)}


def _run_one(fn):
    """Run a scenario fn()→result dict; time it, trap any exception into the result's error."""
    t = _time.perf_counter()
    try:
        r = fn()
    except Exception as e:
        r = _st("error", getattr(fn, "__name__", "?"), "scenario raised", passed=False,
                error=f"{type(e).__name__}: {e}")
    r["ms"] = round((_time.perf_counter() - t) * 1000, 1)
    return r


def _max_streak(days):
    days = sorted(days); best = cur = 1
    for a, b in zip(days, days[1:]):
        cur = cur + 1 if b == a + 1 else 1
        best = max(best, cur)
    return best


# — deterministic scenarios (run with or without a key) —
def _stc_clamp():
    cases = [({"volume_multiplier": 1.5, "scope_days": 7}, "tries to ADD load"),
             ({"volume_multiplier": -0.3, "scope_days": 3}, "negative multiplier"),
             ({"volume_multiplier": 0.6, "scope_days": 90}, "90-day window"),
             ({"volume_multiplier": 0.8, "scope_days": 5, "medical_flag": True}, "medical w/ load"),
             ({"volume_multiplier": "x", "scope_days": "soon"}, "garbage values")]
    detail, bad = [], []
    for d, lbl in cases:
        dv, _n = clamp_adjustment(d, "2026-06-19")
        m, days, med = dv["volume_multiplier"], dv["scope_days"], dv["medical_flag"]
        ok = 0.0 <= m <= 1.0 and 1 <= days <= 28 and (not med or m == 0.0)
        detail.append({"case": lbl, "mult": m, "days": days, "medical": med, "ok": ok})
        if not ok:
            bad.append(lbl)
    return _st("det", "clamp-invariants",
               "clamp_adjustment forces multiplier∈[0,1], window∈[1,28]d, medical⇒full rest",
               passed=not bad, expect="all bounded",
               got="all bounded" if not bad else f"violations: {bad}", output=detail)


def _stc_map_privacy(db):
    """The workout route map is private-only — the routes reveal where the owner lives. Assert the
    WIRING, not just the predicate: drive the real endpoints via a test client so a future refactor
    that drops the guard or the geo-strip is caught (a predicate-only check would miss that). (a) On
    a read-only instance, GET /map → 403. (b) /profile (served on the public container) carries NO
    route geo. Seeds a throwaway trackcache row so neither GET needs the MCP/token."""
    global READONLY
    fail = []
    client = app.test_client()
    db.execute("INSERT OR REPLACE INTO trackcache (activity_id, profile, cached_at) VALUES (?,?,?)",
               (-1, json.dumps({"v": PROFILE_VERSION, "pace": [1], "has_pace": True,
                                "path": [[49.5, 6.0]], "has_gps": True}), _now_iso()))
    db.commit()
    try:
        saved = READONLY
        try:                              # the guard reads the module global at request time
            READONLY = True
            code = client.get("/api/activity/-1/map").status_code
            del_code = client.post("/api/activity/-1/delete").status_code  # destructive POST must 403 on public
        finally:
            READONLY = saved
        if code != 403:
            fail.append(f"read-only /map returned {code}, expected 403")
        if del_code != 403:
            fail.append(f"read-only POST /delete returned {del_code}, expected 403")
        body = client.get("/api/activity/-1/profile").get_data(as_text=True)
        if any(tok in body for tok in ("latitude", "longitude", '"path"')):
            fail.append("/profile leaks route geo")
        # the by-id activity payload (public-served) must also stay geo-free — a future "start
        # location" field added to _activity_payload must not slip onto the public container.
        payload = _activity_payload(db, {"id": -1, "distance": 5, "duration": 1500,
                                         "date_time": "2026-06-16T08:00:00"})
        if any(k in payload for k in ("latitude", "longitude", "lat", "lon", "path")):
            fail.append("/api/activity payload carries geo")
        # /api/activity/latest must NOT leak the cross-training note (latest non-run sport + date) on
        # the public container — withheld server-side, not just hidden in the UI. Seed a future-dated
        # non-run so it's the global latest → a cross note would be produced if not gated.
        db.execute("INSERT OR REPLACE INTO activities (id, date_time, date, sport, raw) VALUES (?,?,?,?,?)",
                   (-2, "2099-01-01T12:00:00", "2099-01-01", "Tennis", json.dumps({"sport": "Tennis"})))
        db.commit()
        try:
            READONLY = True
            pub_latest = client.get("/api/activity/latest").get_data(as_text=True)
        finally:
            READONLY = saved
        if "cross_training" in pub_latest:
            fail.append("/api/activity/latest leaks the cross-training note on the public view")
    finally:
        db.execute("DELETE FROM trackcache WHERE activity_id=?", (-1,))
        db.execute("DELETE FROM activities WHERE id=?", (-2,))
        db.commit()
    return _st("det", "map-privacy",
               "read-only /map + destructive POST /delete 403; /profile + by-id payload carry no route geo (wiring, via test client)",
               passed=not fail, expect="/map + /delete 403 on read-only · /profile & /activity geo-free",
               got={"violations": fail or "none"})


def _stc_day_spacing():
    from datetime import date
    detail, bad = [], []
    for n in (3, 4, 5):
        d = _run_days(n); s = _max_streak(d); weekend = d[-1] >= 5
        detail.append({"runs": n, "days": d, "max_consecutive": s, "long_on_weekend": weekend})
        if not (s <= 2 and weekend):
            bad.append(n)
    # n=6 (the §6e earned frequency advance): 6 runs / 1 rest CAN'T avoid a 3-run streak, so the
    # ≤2-consecutive invariant is infeasible. The invariant that actually protects a masters/post-
    # illness body is weaker but the one that matters: NO TWO HARD sessions (quality / long-MP) on
    # consecutive days, and the long run on the weekend. Check it on a REAL generated 6-run Base
    # (one light tempo) and Build (interval + long-MP) week, not just the day grid.
    easy = 430
    zones = {"easy_top": easy, "easy": 460, "marathon": 360, "threshold": 330, "interval": 300}
    mon, HARD = date(2026, 8, 3), {"tempo", "interval", "long_mp"}     # 2026-08-03 is a Monday
    six = {}
    # high start volume so the §6e min-distance floor opens (the 6th run only appears once non-long
    # runs clear FREQ_MIN_EASY_KM) — here we're testing the LAYOUT of an advanced week, not the floor.
    for ph, shp in (("base", base_shape(4, 55)), ("build", build_shape(4, 60))):
        adv = _apply_freq_advance(shp, True)
        wk = next(w for w in adv if w["runs"] == 6 and w.get("quality"))   # an advanced week WITH quality
        sess, _ = _distribute_week(wk, mon, 320.0, easy, zones=zones)
        dk = sorted((_date(s["date"]).weekday(), s["kind"]) for s in sess)
        hard = [dw for dw, k in dk if k in HARD]
        long_dow = next((dw for dw, k in dk if k in ("long", "long_mp")), None)
        six[ph] = {"days": [dw for dw, _ in dk], "hard_days": hard, "long_dow": long_dow}
        if any(b - a == 1 for a, b in zip(hard, hard[1:])):
            bad.append(f"{ph}6:hard-adjacent")
        if long_dow is None or long_dow < 5:
            bad.append(f"{ph}6:long-off-weekend")
    detail.append({"six_run": six})
    # cross-week BOUNDARY spacing (2026-06-22 fix): a week must never end AND the next begin on a
    # rest — the double-rest seam the owner hit (a 3-run week ending Sat, the next week resting Mon).
    # Every layout ends on the Sunday slot, so the gap from one week's last run to the next week's
    # first run stays ≤2 calendar days (≤1 rest) for every frequency transition the plan uses. (The
    # OLD 3→4 layout gapped 3 days = 2 rests — this guard would have caught it.)
    seq = [w["runs"] for w in REBASE_SHAPE]
    for a, b in zip(seq, seq[1:]):
        da, db_ = _run_days(a), _run_days(b)
        gap = (7 + db_[0]) - da[-1]
        detail.append({"boundary": f"{a}->{b}", "gap_days": gap})
        if gap > 2:
            bad.append(f"boundary {a}->{b} gap {gap}d (double rest)")
    # long run on the TRUE calendar weekend, asserted on REAL generated dates off a Monday anchor
    # (production Monday-anchors via _rebase_start). Every "long" session must land Sat/Sun.
    lw, _ = generate_block(base_shape(4, 30), mon, 30.0, 28.0, easy)
    long_wkdays = sorted({_date(s["date"]).weekday()
                          for w in lw for s in w["sessions"] if "long" in (s.get("kind") or "")})
    if any(wd < 5 for wd in long_wkdays):
        bad.append(f"long off weekend (weekdays {long_wkdays})")
    detail.append({"long_run_weekdays": long_wkdays})
    return _st("det", "day-spacing",
               "≤2 consecutive in a 3/4/5-run week; 6-run week no two hard sessions adjacent + long on "
               "weekend; no double-rest at any week BOUNDARY; long run on the true calendar weekend",
               passed=not bad, expect="≤2 consec · 6: no hard adjacent · boundary gap ≤2d · long Sat/Sun",
               got="ok" if not bad else f"fails: {bad}", output=detail)


def _stc_rebase_anchor():
    """§6d/§6f (2026-06-22) — the block anchors to a Monday so weeks are calendar Mon–Sun (weekend
    long runs), and a legacy non-Monday anchor migrates to its CONTAINING Monday: back-only, never
    forward (so the runner is never pushed to a pre-start tile and a banked week can't be un-elapsed).
    Drives `_rebase_start` directly on a throwaway in-memory DB — the production path the day-spacing
    test only assumes."""
    import sqlite3 as _sq
    from datetime import date as _d, timedelta as _td
    fails = []

    def db(seed=None):
        m = _sq.connect(":memory:"); m.row_factory = _sq.Row
        m.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        if seed:
            m.execute("INSERT INTO meta VALUES('rebase_start', ?)", (seed,))
        m.commit()
        return m

    today = _d(2026, 6, 24)                      # a Wednesday
    if _rebase_start(db(), today) != _monday(today):
        fails.append("fresh anchor not this week's Monday")
    base = _monday(today) - _td(weeks=1)         # an in-flight start ~1 week ago
    for wd in range(7):                          # every weekday a legacy anchor could carry
        s = base + _td(days=wd)
        out = _rebase_start(db(s.isoformat()), today)
        if out.weekday() != 0:
            fails.append(f"migrated anchor not Monday (wd={wd}): {out}")
        if out > s:                              # BACK-ONLY — never forward into the future
            fails.append(f"migration shifted forward (wd={wd}): {s}->{out}")
        if (s - out).days != wd:                 # containing Monday is exactly `wd` days back
            fails.append(f"not the containing Monday (wd={wd}): {s}->{out}")
    monday = _monday(today)
    if _rebase_start(db(monday.isoformat()), today) != monday:
        fails.append("an already-Monday anchor was disturbed")
    elapsed = _monday(today) - _td(weeks=len(REBASE_SHAPE) + 1)
    if _rebase_start(db(elapsed.isoformat()), today) != _monday(today):
        fails.append("fully-elapsed anchor did not reset to this Monday")
    return _st("det", "rebase-anchor",
               "block Monday-anchored (calendar weeks → weekend long run); legacy anchor migrates to "
               "its containing Monday — back-only (never forward → no pre-start tile / un-bank)",
               passed=not fails, expect="Monday-aligned · back-only migration · elapsed resets",
               got={"violations": fails or "none"})


def _stc_unplanned_log():
    """§ out-of-schedule (2026-06-22) — block_log surfaces an UNPLANNED run (an activity on a day with
    no planned session) as a flagged bonus entry on its own day, WITHOUT inflating adherence (it was
    never scheduled). Throwaway in-memory DB; dates in the past so adherence counters engage."""
    import sqlite3 as _sq
    m = _sq.connect(":memory:"); m.row_factory = _sq.Row
    m.executescript(
        "CREATE TABLE activities(id INTEGER PRIMARY KEY, date TEXT, date_time TEXT, sport TEXT,"
        " distance REAL, duration REAL);"
        "CREATE TABLE ignored_activities(id INTEGER PRIMARY KEY);"
        "CREATE TABLE session_log(date TEXT PRIMARY KEY, note TEXT);"
        "CREATE TABLE plans(id INTEGER PRIMARY KEY, created_at TEXT, for_date TEXT, inputs TEXT, plan TEXT);")
    plan = {"rebase": {"weeks": [
        {"wk": 1, "start": "2026-06-08", "km": 16, "runs": 3, "intent": "x",
         "sessions": [{"date": "2026-06-09", "km": 5, "kind": "easy"},   # Tue planned → done
                      {"date": "2026-06-11", "km": 5, "kind": "easy"},   # Thu planned → missed
                      {"date": "2026-06-14", "km": 6, "kind": "long"}]}]}}   # Sun planned → missed
    m.execute("INSERT INTO plans(created_at,for_date,inputs,plan) VALUES('now','2026-06-08','{}',?)",
              (json.dumps(plan),))
    for i, (d, dist) in enumerate([("2026-06-09", 5.0), ("2026-06-10", 6.0)]):  # 06-10 = Wed rest day
        m.execute("INSERT INTO activities VALUES(?,?,?,?,?,?)",
                  (i + 1, d, d + "T18:00:00", RUNNING_SPORT, dist, 1800))
    log = block_log(m)
    sess = log["weeks"][0]["sessions"]
    by = {s["date"]: s for s in sess}
    fails = []
    up = by.get("2026-06-10")
    if not (up and up.get("unplanned") and up.get("done") and (up.get("actual") or {}).get("km") == 6.0):
        fails.append(f"unplanned rest-day run not surfaced: {up}")
    if by.get("2026-06-09", {}).get("unplanned") or not by.get("2026-06-09", {}).get("done"):
        fails.append("planned-day run mis-tagged (should be done, not unplanned)")
    if [s["date"] for s in sess] != sorted(s["date"] for s in sess):
        fails.append(f"sessions not date-sorted: {[s['date'] for s in sess]}")
    if log["adherence"] != {"done": 1, "scheduled": 3}:        # unplanned must NOT touch the ratio
        fails.append(f"adherence polluted by unplanned run: {log['adherence']}")
    m.close()
    return _st("det", "unplanned-log",
               "block_log surfaces an out-of-schedule run on its day (flagged unplanned) without "
               "inflating adherence; sessions stay in calendar order",
               passed=not fails, expect="unplanned shown · adherence {done:1,scheduled:3} unchanged",
               got={"violations": fails or "none"})


def _stc_within_week():
    """§6o within-week awareness — for the week straddling `today`, generate_block keeps the elapsed
    days and governs ONLY today-onward volume from today's seed (model A): EOW ACWR still holds ≤ cap,
    load already done this week (a higher seed ATL) SHRINKS the remaining allowance, remaining sessions
    fall only on today-onward days, and today=None stays the full week. Pure/deterministic."""
    from datetime import date, timedelta
    easy = 425
    shape = [{"wk": 1, "km": 80, "runs": 5, "long": 20, "strides": 0, "intent": "x"}]  # big ⇒ governor binds
    mon = date(2026, 8, 3)                    # Monday
    today = mon + timedelta(days=3)           # Thursday — Mon/Tue already elapsed
    fails = []
    lo, _ = generate_block(shape, mon, 30.0, 28.0, easy, today=today)   # little done this week (ATL 28)
    hi, _ = generate_block(shape, mon, 30.0, 40.0, easy, today=today)   # lots done this week (ATL 40)
    for tag, wks in (("lo", lo), ("hi", hi)):
        w = wks[0]
        if not w.get("partial"):
            fails.append(f"{tag}: straddle week not flagged partial")
        if (w.get("proj_acwr") or 0) > ACWR_SOFT + 0.02:
            fails.append(f"{tag}: EOW ACWR {w.get('proj_acwr')} > cap")          # the safety invariant
        elapsed = [x for x in w["sessions"] if x["date"] < today.isoformat()]
        rem = [x for x in w["sessions"] if x["date"] >= today.isoformat()]
        if not elapsed:
            fails.append(f"{tag}: elapsed days not kept (block_log matching would break)")
        if not rem:
            fails.append(f"{tag}: no today-onward sessions generated")
    if not (hi[0]["trimp_total"] < lo[0]["trimp_total"]):                          # absorption
        fails.append(f"more done this week didn't shrink the remaining allowance: "
                     f"lo={lo[0]['trimp_total']} hi={hi[0]['trimp_total']}")
    full, _ = generate_block(shape, mon, 30.0, 28.0, easy)                          # today=None
    if full[0].get("partial") or len(full[0]["sessions"]) != 5:
        fails.append(f"today=None not the full week: partial={full[0].get('partial')} n={len(full[0]['sessions'])}")
    # INDEPENDENT no-double-count check (model A): projecting the remaining days from TODAY's seed must
    # equal a single full-week roll (elapsed actuals + remaining) from the week-start seed. A regression
    # that rolled the remainder from week-start (double-counting elapsed) would diverge here.
    c0, a0 = 25.0, 24.0                                  # week-start (Mon) seed
    elapsed_t = {"2026-08-03": 60.0, "2026-08-04": 40.0}            # Mon/Tue actuals
    remaining_t = {"2026-08-07": 50.0, "2026-08-09": 70.0}          # Fri/Sun remaining
    to_today = roll(elapsed_t, mon, today - timedelta(days=1), c0, a0)   # roll Mon..Wed → today's seed
    ct, at = to_today[-1]["ctl"], to_today[-1]["atl"]
    _, _, eow_a, _ = _project_week(ct, at, mon.isoformat(), remaining_t, roll_from=today.isoformat())
    truth = roll({**elapsed_t, **remaining_t}, mon, mon + timedelta(days=6), c0, a0)[-1]["acwr"]
    if abs(eow_a - truth) > 0.02:
        fails.append(f"model A double-counts: today-seed EOW {eow_a} != full-week roll {truth}")
    return _st("det", "within-week",
               "partial-week governor keeps elapsed days + governs only today-onward from today's seed; "
               "EOW ACWR ≤ cap; load already done shrinks the remaining allowance; today=None = full week",
               passed=not fails, expect="partial · EOW≤cap · absorption · full week when today=None",
               got={"violations": fails or "none",
                    "rem_trimp_lo": lo[0]["trimp_total"], "rem_trimp_hi": hi[0]["trimp_total"]})


def _stc_doubles_log():
    """§ doubles v1 — block_log keeps a day's runs INDIVIDUAL: a double surfaces both halves as a
    per-run breakdown (each map-linkable) while plan-vs-actual + 'ran so far' use the daily SUM; a
    single-run day has no breakdown; adherence counts the day once (not per run). In-memory DB."""
    import sqlite3 as _sq
    m = _sq.connect(":memory:"); m.row_factory = _sq.Row
    m.executescript(
        "CREATE TABLE activities(id INTEGER PRIMARY KEY, date TEXT, date_time TEXT, sport TEXT,"
        " distance REAL, duration REAL);"
        "CREATE TABLE ignored_activities(id INTEGER PRIMARY KEY);"
        "CREATE TABLE session_log(date TEXT PRIMARY KEY, note TEXT);"
        "CREATE TABLE plans(id INTEGER PRIMARY KEY, created_at TEXT, for_date TEXT, inputs TEXT, plan TEXT);")
    plan = {"rebase": {"weeks": [
        {"wk": 1, "start": "2026-06-08", "km": 16, "runs": 2, "intent": "x",
         "sessions": [{"date": "2026-06-09", "km": 10, "kind": "long"},     # planned day → ran as a DOUBLE
                      {"date": "2026-06-11", "km": 5, "kind": "easy"}]}]}}   # planned day → single run
    m.execute("INSERT INTO plans(created_at,for_date,inputs,plan) VALUES('now','2026-06-08','{}',?)",
              (json.dumps(plan),))
    rows = [("2026-06-09", "2026-06-09T07:00:00", 6.0, 1800),   # AM
            ("2026-06-09", "2026-06-09T18:00:00", 7.0, 2100),   # PM → 06-09 is a double (13k)
            ("2026-06-11", "2026-06-11T07:00:00", 5.0, 1500),   # single
            ("2026-06-10", "2026-06-10T07:00:00", 4.0, 1200),   # rest-day double…
            ("2026-06-10", "2026-06-10T18:00:00", 3.0, 900)]    # …(unplanned, 7k)
    for i, (d, dtm, dist, dur) in enumerate(rows):
        m.execute("INSERT INTO activities VALUES(?,?,?,?,?,?)", (i + 1, d, dtm, RUNNING_SPORT, dist, dur))
    log = block_log(m)
    by = {s["date"]: s for s in log["weeks"][0]["sessions"]}
    fails = []
    d09 = by.get("2026-06-09")
    if not (d09 and d09.get("runs") and len(d09["runs"]) == 2):
        fails.append(f"planned double: missing 2-run breakdown: {d09 and d09.get('runs')}")
    if not (d09 and (d09.get("actual") or {}).get("km") == 13.0):
        fails.append(f"planned double: combined actual not summed: {d09 and d09.get('actual')}")
    if {r["km"] for r in (d09.get("runs") or [])} != {6.0, 7.0}:
        fails.append("breakdown km mismatch")
    if (by.get("2026-06-11") or {}).get("runs"):
        fails.append("single-run day should have NO breakdown")
    d10 = by.get("2026-06-10")
    if not (d10 and d10.get("unplanned") and d10.get("runs") and len(d10["runs"]) == 2):
        fails.append(f"rest-day double not surfaced with breakdown: {d10}")
    if log["ran"]["km"] != 25.0:
        fails.append(f"'ran so far' must sum ALL runs (6+7+5+4+3): {log['ran']}")
    if log["adherence"] != {"done": 2, "scheduled": 2}:
        fails.append(f"adherence must count a double's day ONCE: {log['adherence']}")
    m.close()
    return _st("det", "doubles-log",
               "a double surfaces both runs (per-run breakdown, each map-linkable); plan-vs-actual + "
               "'ran so far' use the daily sum; single-run day has no breakdown; adherence counts day once",
               passed=not fails, expect="2-run breakdown · combined actual · ran sums all · adherence/day",
               got={"violations": fails or "none"})


def _stc_bonus_affordance():
    """§6o — the low-ACWR bonus-run note offers ONLY on a green + rest-day + clearly-low-ACWR day; never
    on amber/red, a non-rest day, high ACWR, or missing ACWR. Pure (a note, not a prescription)."""
    fails = []
    if not _bonus_run_ok("green", "rest", 0.85):
        fails.append("low-ACWR green rest day should offer the bonus note")
    if _bonus_run_ok("green", "rest", 1.20):
        fails.append("high ACWR must NOT offer (no headroom)")
    if _bonus_run_ok("amber", "rest", 0.80) or _bonus_run_ok("red", "rest", 0.80):
        fails.append("amber/red must NOT offer")
    if _bonus_run_ok("green", "easy", 0.80):
        fails.append("a non-rest (training) day must NOT offer")
    if _bonus_run_ok("green", "rest", None):
        fails.append("missing ACWR must NOT offer")
    return _st("det", "bonus-run",
               f"low-ACWR bonus-run note offers iff green + rest day + ACWR < {BONUS_ACWR_MAX} "
               "(note only — the ACWR governor still caps the week)",
               passed=not fails, expect=f"offer iff green·rest·ACWR<{BONUS_ACWR_MAX}",
               got={"violations": fails or "none"})


def _stc_dedup(db):
    auto, manual, dropped = set(find_duplicates(db)), manual_ignores(db), dropped_ids(db)
    ok = isinstance(dropped, set) and dropped == (auto | manual)
    return _st("det", "dedup-union",
               "dropped_ids = auto exact-dups ∪ manual ignores (single de-dup source of truth)",
               passed=ok, expect="union holds",
               got={"auto": len(auto), "manual": len(manual), "dropped": len(dropped)},
               output={"manual_ids": sorted(manual)[:10]})


def _stc_local_delete():
    """Hard local-delete — the sync-no-delete gap fix. Insert-only sync never removes a row a
    Runalyze deletion left behind, so it keeps inflating the structural duplicate count + banner;
    `delete_activity_local` is the only way to drop it. Verifies the row + its derived rows
    (ignore-list, trackcache) are removed, the structural dup clears, the keeper survives, and a
    missing id no-ops. In-memory so it never touches the real DB."""
    import sqlite3 as _sq
    mem = _sq.connect(":memory:"); mem.row_factory = _sq.Row
    mem.executescript(
        "CREATE TABLE activities(id INTEGER PRIMARY KEY, date_time TEXT, distance REAL, sport TEXT, raw TEXT);"
        "CREATE TABLE ignored_activities(id INTEGER PRIMARY KEY, reason TEXT, created_at TEXT);"
        "CREATE TABLE trackcache(activity_id INTEGER PRIMARY KEY, profile TEXT, cached_at TEXT);")
    for i in (1, 2):   # two exact-dups → find_duplicates keeps id 1, flags id 2
        mem.execute("INSERT INTO activities VALUES(?,?,?,?,?)",
                    (i, "2026-06-14T19:00:00", 5.02, RUNNING_SPORT, "{}"))
    mem.execute("INSERT INTO ignored_activities VALUES(2,'manual','now')")
    mem.execute("INSERT INTO trackcache VALUES(2,'{}','now')")
    mem.commit()
    fails = []
    if find_duplicates(mem) != [2]:
        fails.append(f"setup: find_duplicates={find_duplicates(mem)} (expected [2])")
    if not delete_activity_local(mem, 2):
        fails.append("delete returned False for an existing id")
    if mem.execute("SELECT 1 FROM activities WHERE id=2").fetchone():
        fails.append("activity row survived delete")
    if mem.execute("SELECT 1 FROM ignored_activities WHERE id=2").fetchone():
        fails.append("ignore-list row not cleaned")
    if mem.execute("SELECT 1 FROM trackcache WHERE activity_id=2").fetchone():
        fails.append("trackcache row not cleaned")
    if find_duplicates(mem) != [] or dropped_ids(mem) != set():
        fails.append(f"dup not cleared: find_dups={find_duplicates(mem)} dropped={dropped_ids(mem)}")
    if mem.execute("SELECT COUNT(*) c FROM activities").fetchone()["c"] != 1:
        fails.append("kept activity (id 1) not preserved")
    if delete_activity_local(mem, 999):
        fails.append("delete returned True for a missing id")
    mem.close()
    return _st("det", "local-delete",
               "hard local-delete drops the row + its ignore/trackcache rows and clears the structural "
               "duplicate (closes the insert-only sync no-delete gap); no-ops on a missing id",
               passed=not fails, expect="row gone · derived rows cleaned · dup count→0 · keeper survives",
               got={"violations": fails or "none"})


def _stc_settings():
    """Settings panel — the meta→env→default resolution and the save-time validation guard. Pure:
    uses an in-memory meta table + a SYNTHETIC env var, so it never touches a real SH_* var, the
    real DB, or the live process globals (it does NOT call apply_settings_overrides)."""
    import sqlite3 as _sq, os as _os
    mem = _sq.connect(":memory:"); mem.row_factory = _sq.Row
    mem.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    fails = []
    T = {"key": "_t", "env": "SH__SELFTEST_ONLY_", "default": "D"}   # synthetic — safe to mutate
    _os.environ.pop(T["env"], None)
    if _resolve_setting(mem, T) != ("D", "default"):  fails.append("unset → built-in default")
    _os.environ[T["env"]] = "E"
    try:
        if _resolve_setting(mem, T) != ("E", "env"):    fails.append("absent meta row → env fallback")
        _os.environ[T["env"]] = ""                      # set-but-empty env still counts as 'env'
        if _resolve_setting(mem, T) != ("", "env"):     fails.append("set-but-empty env → ('', 'env')")
        mem.execute("INSERT INTO meta VALUES('set:_t','')"); mem.commit()
        if _resolve_setting(mem, T) != ("", "saved"):   fails.append("stored '' is a clear, NOT env fallback")
    finally:
        _os.environ.pop(T["env"], None)
    mem.close()
    # validation guard: markup-break + url-scheme (XSS) + city-format + IANA-tz; athlete_context is free
    checks = [("house_url", "https://ok.com", True), ("house_url", "ftp://x", False),
              ("house_url", "javascript:alert(1)", False), ("house_name", 'a"b', False),
              ("house_name", "My Site", True), ("weather_cities", "nonsense", False),
              ("weather_cities", "Lisbon,38.72,-9.14,LIS", True), ("weather_cities", "", True),
              ("weather_cities", "A,1,1;B,2,2;C,3,3;D,4,4;E,5,5", True),          # exactly 5 = ok
              ("weather_cities", "A,1,1;B,2,2;C,3,3;D,4,4;E,5,5;F,6,6", False),   # 6 > cap
              ("tz", "Europe/Luxembourg", True), ("tz", "Not/AZone", False),
              ("private_url", "https://pvt.example.com", True), ("private_url", "javascript:1", False),
              ("private_url", 'https://x"y', False),
              ("athlete_context", "masters runner <returning>", True)]
    for key, val, want in checks:
        if validate_setting(key, val)[0] != want:
            fails.append(f"validate({key},{val!r}) ≠ {want}")
    # Assert the WIRING (like _stc_map_privacy): the settings + geocode endpoints must stay private —
    # the public read-only container relies on _private_only_path to 403 them. A refactor that drops
    # one (typo / tuple reorder) must fail here, not leak the owner's settings + an open geocode proxy.
    for p in ("/api/settings", "/api/geocode"):
        if not _private_only_path(p):
            fails.append(f"{p} not gated private")
    return _st("det", "settings",
               "meta→env→default resolution (stored ''=clear) + save-time guard (incl. 5-city cap) + settings/geocode private",
               passed=not fails, expect="resolution + validation + private-only wiring hold",
               got={"violations": fails or "none"})


def _stc_multi_a_chain():
    """§6q select_chain — role assignment by race-type-scaled separation + the no-A fallback + B→tune-ups.
    Pure function of (objectives, today)."""
    today = _date("2026-06-01")
    def race(i, wks, typ, prio):
        return {"id": i, "date": (today + timedelta(weeks=wks)).isoformat(), "type": typ, "priority": prio}
    roles = lambda objs: [c["role"] for c in select_chain(objs, today)[0]]
    fails = []
    cases = [
        ("marathon +4wk → earlier subordinate (4<6)", [race(1,12,"marathon","A"), race(2,16,"marathon","A")], ["subordinate","goal"]),
        ("marathon +8wk → earlier co-equal (8≥6)",     [race(1,8,"marathon","A"),  race(2,16,"marathon","A")], ["coequal","goal"]),
        ("10k +3wk → earlier co-equal (3≥3)",          [race(1,9,"10k","A"),       race(2,12,"10k","A")],      ["coequal","goal"]),
        ("marathon→10k +5wk → subordinate (earlier=marathon, 5<6)", [race(1,10,"marathon","A"), race(2,15,"10k","A")], ["subordinate","goal"]),
        ("single A → goal",                            [race(1,14,"marathon","A")], ["goal"]),
    ]
    for label, objs, want in cases:
        got = roles(objs)
        if got != want:
            fails.append(f"{label}: {got} (want {want})")
    # no A flagged → nearest race is the lone goal, no chain past it
    chain, tune = select_chain([race(1,6,"10k","B"), race(2,10,"half","C")], today)
    if [c["id"] for c in chain] != [1] or chain[0]["role"] != "goal" or tune != []:
        fails.append(f"no-A fallback: chain={[(c['id'],c['role']) for c in chain]} tune={[t['id'] for t in tune]}")
    # a B race before the final A → tune-up, NOT in the chain
    chain, tune = select_chain([race(1,5,"10k","B"), race(2,14,"marathon","A")], today)
    if [c["id"] for c in chain] != [2] or [t["id"] for t in tune] != [1]:
        fails.append(f"B-before-A: chain={[c['id'] for c in chain]} tune={[t['id'] for t in tune]}")
    return _st("det", "multi-a-chain",
               "select_chain: role by race-type-scaled separation (marathon 6wk vs 10k 3wk), no-A fallback, B→tune-ups",
               passed=not fails, expect="roles + chain/tune split correct",
               got={"violations": fails or "none"})


def _stc_periodize_chain():
    """§6q periodize_chain — REDUCES to periodize() for a single goal race; multi-A adds a bridge/peak/
    taper segment per later race; a subordinate race gets a 1-wk sharpen + no full peak. Pure."""
    today = _date("2026-06-01")
    def race(i, wks, typ, label):
        return {"id": i, "date": (today + timedelta(weeks=wks)).isoformat(), "type": typ, "label": label}
    fails = []
    # (a) single goal race ≡ periodize() — same leading-word + weeks per phase, same total
    goal = {**race(1, 24, "marathon", "Goal Marathon"), "role": "goal"}
    ch, tw = periodize_chain(today, [goal], rebase_weeks=6)
    pz, _ = periodize(today, goal["date"], rebase_weeks=6)
    red = lambda ps: [(p["phase"].split()[0], p["weeks"]) for p in ps]
    if red(ch) != red(pz):
        fails.append(f"single-A not reducing to periodize: {red(ch)} vs {red(pz)}")
    if tw != weeks_until(goal["date"], today):
        fails.append("single-A total-weeks mismatch")
    # (b) two co-equal A's → a Bridge→Peak→Taper segment for the 2nd race; rebase first, goal-taper last
    co = [{**race(1, 12, "10k", "Spring 10k"), "role": "coequal"},
          {**race(2, 24, "marathon", "Goal Marathon"), "role": "goal"}]
    ch2, _ = periodize_chain(today, co, rebase_weeks=6)
    keys2, kinds2 = [p["key"] for p in ch2], [p["kind"] for p in ch2]
    if "bridge1" not in keys2 or "bridge" not in kinds2:
        fails.append(f"co-equal chain missing bridge: {keys2}")
    if kinds2[0] != "rebase" or ch2[-1]["kind"] != "taper" or ch2[-1]["race"] != "Goal Marathon":
        fails.append(f"chain endpoints wrong: first={kinds2[0]} last={ch2[-1].get('key')}/{ch2[-1].get('race')}")
    # (c) subordinate first race → taper=1 (mini), no peak phase (peak weeks 0 → filtered)
    sub = [{**race(1, 12, "marathon", "Tune-up Mara"), "role": "subordinate"},
           {**race(2, 16, "marathon", "Goal Mara"), "role": "goal"}]
    ch3, _ = periodize_chain(today, sub, rebase_weeks=6)
    seg0_taper = next((p for p in ch3 if p["key"] == "taper"), None)
    if not seg0_taper or seg0_taper["weeks"] != 1:
        fails.append(f"subordinate taper not 1wk: {seg0_taper}")
    if next((p for p in ch3 if p["key"] == "peak"), None) is not None:
        fails.append("subordinate race should have no full peak phase")
    # (d) a SHORT inter-race gap (1 wk) must be clamped — phase weeks can't overrun the final race date
    tight = [{**race(1, 11, "10k", "R1"), "role": "coequal"},
             {**race(2, 12, "marathon", "R2"), "role": "goal"}]
    ch4, tw4 = periodize_chain(today, tight, rebase_weeks=6)
    seg_sum = sum(ph["weeks"] for ph in ch4)
    if seg_sum > tw4:
        fails.append(f"short-gap overrun: phase weeks {seg_sum} > runway {tw4}")
    return _st("det", "periodize-chain",
               "periodize_chain ≡ periodize for single-A; multi-A adds bridge/peak/taper per race; subordinate → 1wk sharpen, no peak",
               passed=not fails, expect="reduction + chain structure + subordinate sizing",
               got={"violations": fails or "none"})


def _stc_multi_a_plan():
    """§6q INTEGRATION — generate_plan over a real 2-A chain (in-memory DB): produces the chain + a
    bridge segment, and the ACWR ceiling holds on EVERY week across ALL segments (the safety invariant
    that must survive the multi-segment rewrite). Self-contained: never touches the real DB."""
    import sqlite3 as _sq
    mem = _sq.connect(":memory:"); mem.row_factory = _sq.Row
    mem.executescript(SCHEMA)
    today = datetime.now().date()
    mem.execute("INSERT INTO shape_snapshots(snapshot_date,effective_vo2max,fitness,fatigue) VALUES(?,?,?,?)",
                (today.isoformat(), 50.0, 30.0, 28.0))
    def add(label, wks, typ):
        mem.execute("INSERT INTO objectives(type,label,date,target,priority,status,created_at) VALUES(?,?,?,?,?,?,?)",
                    (typ, label, (today + timedelta(weeks=wks)).isoformat(), "finish", "A", "upcoming", _now_iso()))
    add("Tune 10k", 12, "10k")          # co-equal (gap to marathon 12wk ≫ 10k recovery 3wk)
    add("Goal Marathon", 24, "marathon")
    mem.commit()
    p = generate_plan(mem)
    fails = []
    if not p.get("ok") or p.get("mode") != "race":
        fails.append(f"plan not ok/race: ok={p.get('ok')} mode={p.get('mode')} err={p.get('error')}")
    chain = p.get("chain", [])
    if [c.get("role") for c in chain] != ["coequal", "goal"]:
        fails.append(f"chain roles: {[(c.get('label'), c.get('role')) for c in chain]}")
    if not any(ph["kind"] == "bridge" for ph in p.get("phases", [])):
        fails.append("no bridge segment in multi-A phases")
    overs = []
    for ph in p.get("phases", []):
        for w in (p.get(ph.get("key")) or {}).get("weeks", []):
            a = w.get("proj_acwr")
            if a is not None and a > 1.25 + 1e-6:
                overs.append((ph["key"], w.get("wk"), round(a, 3)))
    if overs:
        fails.append(f"ACWR ceiling breached: {overs[:5]}")
    mem.close()
    return _st("det", "multi-a-plan",
               "generate_plan over a 2-A chain: chain roles + bridge segment + ACWR ≤1.25 on every week of every segment",
               passed=not fails, expect="chain + bridge + ceiling held across all segments",
               got={"violations": fails or "none"})


def _stc_latest_running():
    """latest_running_activity — the tile filters to RUNNING-family (trail/treadmill count) and notes a
    non-run only when it's the most-recent activity. Pure/in-memory."""
    import sqlite3 as _sq
    mem = _sq.connect(":memory:"); mem.row_factory = _sq.Row
    mem.executescript(SCHEMA)
    def add(i, dt, sport):
        mem.execute("INSERT INTO activities(id,date_time,date,sport,raw) VALUES(?,?,?,?,?)",
                    (i, dt, dt[:10], sport, json.dumps({"sport": sport})))
    fails = []
    add(1, "2026-06-20T18:00:00", "Running")
    add(2, "2026-06-22T18:00:00", "Tennis")    # a more-recent non-run
    mem.commit()
    run, cross = latest_running_activity(mem)
    if not run or json.loads(run["raw"])["sport"] != "Running":
        fails.append(f"should pick the Running activity (got {run and json.loads(run['raw'])['sport']})")
    if not cross or cross["sport"] != "Tennis":
        fails.append(f"should note the Tennis cross-train (got {cross})")
    add(3, "2026-06-23T18:00:00", "Trail Running")   # newer, running-family
    mem.commit()
    run, cross = latest_running_activity(mem)
    if not run or json.loads(run["raw"])["sport"] != "Trail Running":
        fails.append(f"trail run should count as running (got {run and json.loads(run['raw'])['sport']})")
    if cross is not None:
        fails.append(f"latest is a run → no cross note (got {cross})")
    mem.close()
    return _st("det", "latest-running",
               "latest tile filters to running-family (trail counts) + notes a non-run iff it's the most recent",
               passed=not fails, expect="running picked · cross note only when latest is a non-run",
               got={"violations": fails or "none"})


def _stc_projector(db):
    # Validate the reconstruction only where it's LIKE-FOR-LIKE with Runalyze's snapshot. A
    # snapshot is comparable only when both hold:
    #   (a) it sits STRICTLY BEHIND our activity frontier (latest activity day) — so every activity
    #       it reflects is actually ingested. The frontier snapshot can legitimately LEAD the
    #       activity feed by a day (sync captures /activity and /statistics/current separately, and
    #       Runalyze can surface a session in "current" before our paginated pull sees it), so it
    #       reflects load the reconstruction structurally cannot — a malformed comparison, not a
    #       model error. (Proven: on the lead day a single TRIMP impulse reconciles BOTH CTL and
    #       ATL at once — impossible if τ/the EWMA were wrong; the model is correct.)
    #   (b) it falls on a REST day (no TRIMP that day) — Runalyze's value is then pure decay, so the
    #       snapshot's intra-day capture time can't diverge from our whole-day roll (a snapshot taken
    #       mid-activity-day mismatches a full-day impulse — that's the other-signed error we see on
    #       the active frontier day itself).
    # When only such non-settled snapshots exist we SKIP with a diagnostic — never loosen the
    # tolerance (that would mask future real model drift). Validation resumes for real as settled
    # rest-day snapshots accrue (exactly the CTL/ATL-divergent data §6/τ-validation wants).
    daily = daily_trimp_series(db)
    if not daily:
        return _st("det", "projector-validation", "reconstructed CTL/ATL vs Runalyze",
                   skipped=True, note="no activity history")
    snaps = db.execute("SELECT snapshot_date, fitness, fatigue FROM shape_snapshots "
                       "ORDER BY snapshot_date DESC").fetchall()
    if not snaps:
        return _st("det", "projector-validation",
                   "reconstructed CTL/ATL reproduces Runalyze's reported values",
                   skipped=True, note="no shape snapshot yet")
    frontier = max(_date(d) for d in daily)
    settled = next((s for s in snaps if _date(s["snapshot_date"]) < frontier
                    and daily.get(s["snapshot_date"], 0.0) == 0.0), None)
    if settled is None:
        latest = snaps[0]["snapshot_date"]
        lead = (_date(latest) - frontier).days
        return _st("det", "projector-validation",
                   "the projector reproduces Runalyze's CTL/ATL at the latest settled snapshot",
                   skipped=True,
                   note=(f"no settled snapshot to validate against yet: the latest ({latest}) "
                         f"leads the activity frontier ({frontier.isoformat()}) by {lead}d, so it "
                         f"reflects activities the reconstruction can't see, and no earlier rest-day "
                         f"snapshot sits behind the frontier. Not a model error — one impulse on the "
                         f"lead day reconciles both CTL and ATL. Validates as settled snapshots accrue."),
                   output={"latest_snapshot": latest, "activity_frontier": frontier.isoformat()})
    hist = roll(daily, min(_date(d) for d in daily), _date(settled["snapshot_date"]))
    modeled = hist[-1]
    ce = round(modeled["ctl"] - (settled["fitness"] or 0), 2)
    ae = round(modeled["atl"] - (settled["fatigue"] or 0), 2)
    tol = 5.0
    return _st("det", "projector-validation",
               "the projector reproduces Runalyze's CTL/ATL at the latest settled snapshot (within tol)",
               passed=abs(ce) <= tol and abs(ae) <= tol, expect=f"|err|≤{tol}",
               got={"ctl_err": ce, "atl_err": ae, "at": settled["snapshot_date"]},
               output={"modeled": {"ctl": modeled["ctl"], "atl": modeled["atl"]},
                       "runalyze": {"ctl": settled["fitness"], "atl": settled["fatigue"]},
                       "activity_frontier": frontier.isoformat()})


def _stc_acwr_ceiling(db):
    p = generate_plan(db)
    if not (p.get("rebase") or {}).get("weeks"):
        return _st("det", "plan-acwr-ceiling", "every planned week's projected ACWR ≤ soft cap",
                   skipped=True, note="no rebase weeks (maintenance mode / no plan inputs)")
    # §6f Step D / §6q — across EVERY phase block the plan actually generated, keyed off p["phases"]
    # so chain segments (bridge/peak1/taper1…) are covered, not just the single-A base/build/peak/taper.
    keys = ["rebase"] + [ph["key"] for ph in (p.get("phases") or [])
                         if ph.get("key") and ph["key"] != "rebase"]
    tagged = [(k, w) for k in keys for w in (p.get(k) or {}).get("weeks", [])]
    over = [{"phase": k, "wk": w["wk"], "acwr": w.get("proj_acwr")} for k, w in tagged
            if (w.get("proj_acwr") or 0) > ACWR_SOFT + 0.02]
    counts = {k: len((p.get(k) or {}).get("weeks", [])) for k in keys}
    return _st("det", "plan-acwr-ceiling",
               f"every week's projected end-ACWR ≤ soft cap {ACWR_SOFT} across ALL phases",
               passed=not over, expect=f"≤{ACWR_SOFT}", got="all within" if not over else over,
               output={"phase_weeks": counts,
                       "max_acwr": max((w.get("proj_acwr") or 0 for _ph, w in tagged), default=None)})


def _stc_base_phase():
    """§6f Step B: the parametric Base shape ramps volume off the re-base end, runs a 3:1 down-week
    cadence, and (through generate_block) holds the ACWR ceiling every week."""
    from datetime import date
    bs, easy = date(2026, 8, 1), 425
    n = 10
    shape = base_shape(n, 19)
    weeks, bound = generate_block(shape, bs, 30.0, 28.0, easy)   # chained off a plausible re-base end
    over = [w["wk"] for w in weeks if (w.get("proj_acwr") or 0) > ACWR_SOFT + 0.02]
    downs = [s["wk"] for s in shape if s["intent"].startswith("Down")]
    rises = max(w["intent_km"] for w in weeks) > weeks[0]["intent_km"]
    cadence_ok = downs == list(range(BASE_DOWN_EVERY, n + 1, BASE_DOWN_EVERY))
    return _st("det", "base-phase",
               "Base shape: rising volume + 3:1 down-week cadence, ACWR ceiling held every week",
               passed=not over and rises and cadence_ok,
               expect="≤cap, volume rises, down weeks at 4/8",
               got={"acwr_over": over or "none", "down_weeks": downs, "volume_rises": rises},
               output={"intent_km": [w["intent_km"] for w in weeks],
                       "actual_km": [w["km"] for w in weeks], "end_ctl": bound.get("end_ctl")})


def _stc_polarized():
    """§6f Step C/D: every quality phase, generated WITH zones, keeps the POLARIZED invariant — each
    week is easy-dominant (work share ≤ the phase's POLARIZED cap, i.e. easy ≥ POLARIZED_EASY_MIN)
    and the threshold/interval slice alone stays ≤ PHASE_HARD_CAP — while every quality session is
    STRUCTURED (a work rep at its zone, easy wu/cd) and the load stays ACWR-governed. Also exercises
    the Step D structures: multi-rep intervals (≥2 work reps + recovery jogs) and a marathon-pace
    long-run finish. And the re-base carries NO quality even with zones (polarized is opt-in)."""
    from datetime import date, timedelta
    easy = 425
    zones = {"easy_top": easy, "easy": 460, "marathon": 360, "threshold": 330, "interval": 300}
    phases = [("base", base_shape(8, 19)), ("build", build_shape(6, 24)),
              ("peak", peak_shape(2, 26)), ("taper", taper_shape(3, 26))]
    bad, detail, saw_interval, saw_mp = [], [], False, False
    ctl, atl, bs = 30.0, 28.0, date(2026, 8, 1)
    for name, shape in phases:
        weeks, bound = generate_block(shape, bs, ctl, atl, easy, zones=zones)
        cap = PHASE_HARD_CAP[name]
        for w in weeks:
            total = w["trimp_total"] or 0.0
            reps = [r for sess in w["sessions"] for r in (sess.get("reps") or [])]
            work = sum(r["trimp"] for r in reps if r["effort"] == "work")
            hard = sum(r["trimp"] for r in reps if r["effort"] == "work" and r["zone"] in HARD_ZONES)
            easy_frac = round(1 - work / total, 3) if total else 1.0
            hard_frac = round(hard / total, 3) if total else 0.0
            for sess in w["sessions"]:
                wr = [r for r in (sess.get("reps") or []) if r["effort"] == "work"]
                rc = [r for r in (sess.get("reps") or []) if r["effort"] == "recovery"]
                if sess.get("kind") == "interval" and len(wr) >= 2 and rc:
                    saw_interval = True
                if sess.get("kind") == "long_mp" and any(r["zone"] == "marathon" for r in wr):
                    saw_mp = True
            structured = all(any(r["effort"] == "work" for r in s["reps"])
                             for s in w["sessions"] if s.get("reps"))
            acwr_ok = (w.get("proj_acwr") or 0) <= ACWR_SOFT + 0.02
            ok = (hard_frac <= cap + 0.001 and easy_frac >= POLARIZED_EASY_MIN - 0.005
                  and acwr_ok and structured)
            detail.append({"phase": name, "wk": w["wk"], "easy_frac": easy_frac,
                           "hard_frac": hard_frac, "acwr": w.get("proj_acwr")})
            if not ok:
                bad.append(f"{name}#{w['wk']}")
        ctl, atl, bs = bound["end_ctl"], bound["end_atl"], bs + timedelta(weeks=len(shape))
    rb, _ = generate_block(REBASE_SHAPE, date(2026, 6, 19), 24.0, 25.0, 430, zones=zones)
    rebase_clean = not any(s.get("reps") for w in rb for s in w["sessions"])
    return _st("det", "polarized-distribution",
               f"every phase easy-dominant (easy ≥{POLARIZED_EASY_MIN}, hard ≤ PHASE_HARD_CAP), "
               "structured intervals + MP long run, ACWR-governed; re-base stays pure easy",
               passed=not bad and saw_interval and saw_mp and rebase_clean,
               expect=f"easy≥{POLARIZED_EASY_MIN}, hard≤cap, intervals+MP present, re-base clean",
               got={"weeks_bad": bad or "none", "saw_interval": saw_interval,
                    "saw_mp": saw_mp, "rebase_clean": rebase_clean},
               output=detail)


def _stc_taper():
    """§6f Step D: the taper curve drops volume monotonically to ~40–60% below the peak-end volume,
    and the race week carries no structured quality (just freshening) — while still ACWR-governed."""
    from datetime import date
    easy = 425
    zones = {"easy_top": easy, "marathon": 360, "threshold": 330, "interval": 300}
    peak_end_km = 26
    weeks, _ = generate_block(taper_shape(3, peak_end_km), date(2026, 11, 1), 35.0, 33.0,
                              easy, zones=zones)
    kms = [w["intent_km"] for w in weeks]
    descends = all(b <= a for a, b in zip(kms, kms[1:]))
    race_drop = round(1 - kms[-1] / peak_end_km, 2) if peak_end_km else 0.0
    race_clean = not any(s.get("reps") for s in weeks[-1]["sessions"])
    acwr_ok = all((w.get("proj_acwr") or 0) <= ACWR_SOFT + 0.02 for w in weeks)
    ok = descends and 0.35 <= race_drop <= 0.70 and race_clean and acwr_ok
    return _st("det", "taper-volume-drop",
               "taper volume falls monotonically to ~40–60% below peak end; race week unstructured",
               passed=ok, expect="monotonic drop, race-week 35–70% down + no quality",
               got={"intent_km": kms, "race_week_drop": race_drop, "race_week_clean": race_clean},
               output={"acwr": [w.get("proj_acwr") for w in weeks]})


def _stc_freeze_continuity():
    """§6f Step E: a mid-block regeneration FREEZES fully-elapsed weeks verbatim from the prior plan
    and generates today-onward fresh from the live seed. Time-travels `_split_freeze` deterministically:
    weeks whose window ended before `today` are carried byte-for-byte (incl. a sentinel only the prior
    plan has); the week containing today and later are regenerated (real sessions, not frozen)."""
    from datetime import date
    easy = 425
    zones = {"easy_top": easy, "easy": 460, "marathon": 360, "threshold": 330, "interval": 300}
    ps, shape = date(2026, 1, 5), base_shape(4, 19)            # weeks start 01-05/12/19/26
    prior = {"2026-01-05": {"start": "2026-01-05", "wk": 1, "_sentinel": True},
             "2026-01-12": {"start": "2026-01-12", "wk": 2, "_sentinel": True}}
    weeks, _ec, _ea, gen = _split_freeze(shape, ps, (30.0, 28.0), easy, None, zones, prior,
                                         date(2026, 1, 20))   # wk1,2 elapsed; wk3 holds today; wk4 future
    by_start = {w["start"]: w for w in weeks}
    frozen = [w for w in weeks if w.get("frozen")]
    fresh = [w for w in weeks if not w.get("frozen")]
    verbatim = all({k: v for k, v in by_start[s].items() if k not in ("frozen", "elapsed")} == prior[s]
                   for s in prior)
    froze_past = {w["start"] for w in frozen} == set(prior)
    fresh_future = all(w.get("sessions") and not w.get("elapsed") for w in fresh)
    # edges: nothing elapsed ⇒ all fresh; everything elapsed w/o history ⇒ best-effort backfill
    allf, _, _, ga = _split_freeze(shape, ps, (30.0, 28.0), easy, None, zones, {}, date(2025, 12, 1))
    all_future = ga and not any(w.get("frozen") for w in allf)
    bk, _, _, _ = _split_freeze(shape, ps, (30.0, 28.0), easy, None, zones, prior, date(2027, 1, 1))
    backfilled_ok = sum(w.get("frozen") for w in bk) == 2 and \
        sum(bool(w.get("elapsed")) and not w.get("frozen") for w in bk) == 2
    ok = verbatim and froze_past and fresh_future and gen and all_future and backfilled_ok
    return _st("det", "freeze-continuity",
               "mid-block regen freezes elapsed weeks verbatim from the prior plan; today-onward "
               "regenerates from the live seed (history is never rewritten)",
               passed=ok, expect="past carried byte-for-byte, future fresh, edges hold",
               got={"verbatim": verbatim, "froze_past": froze_past, "fresh_future": fresh_future,
                    "all_future": all_future, "backfilled_ok": backfilled_ok},
               output={"frozen_starts": [w["start"] for w in frozen],
                       "fresh_starts": [w["start"] for w in fresh]})


def _stc_down_weeks():
    """§6f Step D/F: the 3:1 mesocycle — every 4th Base/Build week is a DOWN week with reduced
    volume (vs the prior week) and NO quality, so the block absorbs load before building again."""
    from datetime import date
    easy = 425
    zones = {"easy_top": easy, "easy": 460, "marathon": 360, "threshold": 330, "interval": 300}
    bad, detail = [], []
    for name, shape in (("base", base_shape(8, 19)), ("build", build_shape(8, 24))):
        weeks, _ = generate_block(shape, date(2026, 8, 1), 30.0, 28.0, easy, zones=zones)
        downs = [w["wk"] for w in weeks if w["wk"] % 4 == 0]
        for w in weeks:
            if w["wk"] % 4 != 0:
                continue                                   # down weeks are the 4th of each block
            prev = weeks[w["wk"] - 2]                       # the preceding (build) week
            has_q = any(s.get("reps") for s in w["sessions"])
            lower = w["intent_km"] < prev["intent_km"]
            detail.append({"phase": name, "wk": w["wk"], "intent_km": w["intent_km"],
                           "prev_km": prev["intent_km"], "quality": has_q})
            if has_q or not lower:
                bad.append(f"{name}#{w['wk']}")
        if downs != [4, 8]:
            bad.append(f"{name}-cadence:{downs}")
    return _st("det", "down-weeks",
               "3:1 mesocycle: every 4th Base/Build week drops volume + carries no quality (absorb)",
               passed=not bad, expect="down weeks at 4/8, reduced volume, no quality",
               got={"violations": bad or "none"}, output=detail)


def _stc_long_run():
    """§ long-run recalibration (2026-06-20): the long run is the marathon cornerstone and must reach a
    REAL fraction of the week — LONG_RUN_MAX_FRAC raised 0.35→0.50 after the owner's OWN history showed
    his real long runs ran ~0.40–0.50 of the week. This guards two things so a future tightening can't
    silently revert: (a) base-build long runs clear the OLD ~0.35 ceiling; (b) the pure-easy re-base
    keeps its conservative REBASE_LONG_CAP (the post-illness restart stays byte-identical). The size is
    still CTL-gated by the unchanged EOW ACWR governor (peak long run ~12km off this base, not 30) —
    that honest ceiling is covered by det/plan-acwr-ceiling; here we only assert the fraction. Pure."""
    from datetime import date
    easy = 425
    zones = {"easy_top": easy, "easy": 460, "marathon": 360, "threshold": 330, "interval": 300}
    longfrac = lambda w: (max((s.get("km", 0) for s in w["sessions"] if "long" in (s.get("kind") or "")),
                              default=0) / w["km"]) if w["km"] else 0.0
    bb, _ = generate_block(build_shape(8, 24), date(2026, 8, 1), 30.0, 28.0, easy, zones=zones)
    bb_max = max(longfrac(w) for w in bb if not _is_down(w.get("intent")))
    rb, _ = generate_block(REBASE_SHAPE, date(2026, 8, 1), 24.0, 25.0, easy)   # zones=None ⇒ re-base cap
    rb_max = max(longfrac(w) for w in rb)
    fail = []
    if bb_max < 0.37:                              # recalibration active — clears the old ~0.35 ceiling
        fail.append(f"base-build long fraction {round(bb_max, 2)} — recalibration reverted?")
    if rb_max > REBASE_LONG_CAP + 0.02:            # re-base cautious cap preserved (restart untouched)
        fail.append(f"re-base long fraction {round(rb_max, 2)} > cap {REBASE_LONG_CAP}")
    return _st("det", "long-run",
               "marathon long run reaches its recalibrated fraction (base-build clears the old 0.35 "
               "ceiling) while the pure-easy re-base keeps the conservative REBASE_LONG_CAP",
               passed=not fail, expect=f"base-build≥0.37 · re-base≤{REBASE_LONG_CAP}",
               got={"violations": fail or "none", "base_build_max_longfrac": round(bb_max, 2),
                    "rebase_max_longfrac": round(rb_max, 2)})


def _stc_ctl_floor():
    """§6h CTL-responsive volume FLOOR — the safety + correctness assertions:
      (1) DORMANT at low CTL — a pure no-op (underpins the byte-identical-to-main guarantee);
      (2) ACTIVATES at high CTL as a uniform SCALE that preserves the ramp progression AND the 3:1
          down-week ratio (down weeks stay proportional troughs, never flattened or stranded deep);
      (3) end-to-end the ACWR governor still caps every week and the down-week ACWR trough survives;
      (4) reduce-only (§6c) still WINS over the floor (a readiness/medical ease dominates);
      (5) composes with the earned lift (floor scales, earned multiplies non-down) without blowup.
    Pure/deterministic — no DB, no snapshot injection."""
    from datetime import date
    easy = 425
    zones = {"easy_top": easy, "easy": 460, "marathon": 360, "threshold": 330, "interval": 300}
    base = build_shape(8, 24)
    fail = []
    # (1) dormant at low CTL — pure no-op (0.55×20≈11 < the smallest build week)
    if _apply_ctl_floor(base, 20) != base:
        fail.append("not dormant at low CTL")
    # (2) activates + preserves structure at high CTL (0.55×70≈38.5 floor)
    hi = _apply_ctl_floor(base, 70)
    nd0 = [w["km"] for w in base if not _is_down(w.get("intent"))]
    nd1 = [w["km"] for w in hi if not _is_down(w.get("intent"))]
    if not all(b > a for a, b in zip(nd0, nd1)):
        fail.append("floor didn't lift building weeks")
    if not (nd1 == sorted(nd1)):                                  # ramp progression preserved (monotonic)
        fail.append("floor flattened the ramp")
    for i, w in enumerate(hi):                                    # down weeks stay proportional troughs
        if _is_down(w.get("intent")):
            nb = [hi[j]["km"] for j in (i - 1, i + 1) if 0 <= j < len(hi)]
            if nb and w["km"] >= min(nb):
                fail.append(f"down#{w['wk']} not a trough under floor")
    # (3) end-to-end: ACWR ceiling held + the down-week ACWR trough survives at high CTL
    wks, _ = generate_block(hi, date(2026, 8, 1), 70.0, 70.0, easy, zones=zones)
    for i, w in enumerate(wks):
        if (w.get("proj_acwr") or 0) > ACWR_SOFT + 0.02:
            fail.append(f"acwr#{w['wk']}={w.get('proj_acwr')}")
        if _is_down(w.get("intent")):
            nb = [wks[j].get("proj_acwr") for j in (i - 1, i + 1) if 0 <= j < len(wks)]
            if nb and (w.get("proj_acwr") or 0) > min(nb) - 0.04:
                fail.append(f"acwr-trough-collapsed#{w['wk']}")
    # (4) reduce-only wins: a §6c ease over week 1 lowers realized load despite the floor
    ease = {"applies_from": "2026-08-01", "applies_until": "2026-08-07", "volume_multiplier": 0.5}
    plain, _ = generate_block(hi, date(2026, 8, 1), 70.0, 70.0, easy, zones=zones)
    eased, _ = generate_block(hi, date(2026, 8, 1), 70.0, 70.0, easy, adjust=ease, zones=zones)
    if not (eased[0]["km"] < plain[0]["km"]):
        fail.append("reduce-only did NOT win over the floor")
    # (5) composes with the earned lift: floor scales all weeks, earned then multiplies non-down only
    both = _apply_earned_lift(hi, 1.16)
    if not all(_is_down(b.get("intent")) or b["km"] > h["km"] for h, b in zip(hi, both)):
        fail.append("earned×floor composition broken")
    return _st("det", "ctl-floor",
               "CTL volume floor: dormant at low CTL (no-op), scales+preserves structure at high CTL, "
               "holds ≤1.25 with trough intact, reduce-only wins, composes with earned lift",
               passed=not fail, expect="dormant · structure-preserving · safe · reduce-only wins",
               got={"violations": fail or "none", "floor_km@CTL70": round(K_CTL_VOLUME * 70, 1),
                    "lifted_weeks": nd1})


def _stc_earned_lift():
    """§6e/§6f earned upward responsiveness — the SAFETY assertion. When a banked streak unlocks the
    bounded volume lift, it must raise non-down BUILDING weeks WITHOUT flattening the 3:1 recovery
    trough: a uniform lift pushes the down week's realized ACWR up to its neighbours — the one
    masters/post-illness risk the ≤1.25 cap alone does NOT catch (every week still passes ≤1.25 while
    the recovery is gone). So this asserts the down-week trough SURVIVES, not just that the cap holds.
    Also CO-FIRES with the re-base graduation: a graduated (shortened) re-base chains into the lifted
    block, both mechanisms active at once. Pure/deterministic."""
    from datetime import date, timedelta
    easy = 425
    zones = {"easy_top": easy, "easy": 460, "marathon": 360, "threshold": 330, "interval": 300}
    factor = round(1.0 + EARNED_VOLUME_STEP * EARNED_MAX_TIERS, 4)      # full earned lift (~+16%)
    grad = REBASE_SHAPE[:len(REBASE_SHAPE) - REBASE_MAX_GRADUATE]       # graduated (shortened) re-base
    rb, rbm = generate_block(grad, date(2026, 8, 1), 24.0, 25.0, easy)  # → seeds the lifted block
    bstart = date(2026, 8, 1) + timedelta(weeks=len(grad))
    shape = build_shape(8, rb[-1]["intent_km"])
    base0, _ = generate_block(shape, bstart, rbm["end_ctl"], rbm["end_atl"], easy, zones=zones)
    base1, _ = generate_block(_apply_earned_lift(shape, factor), bstart,
                              rbm["end_ctl"], rbm["end_atl"], easy, zones=zones)
    fail, troughs = [], []
    for w0, w1 in zip(base0, base1):
        if _is_down(w1.get("intent")) and w1["intent_km"] != w0["intent_km"]:
            fail.append(f"down#{w1['wk']} intent moved")            # lift must skip down weeks
        if (w1.get("proj_acwr") or 0) > ACWR_SOFT + 0.02:
            fail.append(f"acwr#{w1['wk']}={w1['proj_acwr']}")       # governor still caps every week
    for i, w in enumerate(base1):
        if not _is_down(w.get("intent")):
            continue
        nb = [base1[j]["proj_acwr"] for j in (i - 1, i + 1) if 0 <= j < len(base1)]
        a = w.get("proj_acwr") or 0
        troughs.append({"wk": w["wk"], "down_acwr": a, "neighbours": nb})
        if nb and a > min(nb) - 0.04:                              # the recovery dip must stay a dip
            fail.append(f"trough-collapsed#{w['wk']} {a} vs {nb}")
    moved = sum(1 for w0, w1 in zip(base0, base1)
                if not _is_down(w1.get("intent")) and w1["km"] > w0["km"] + 0.5)
    if moved == 0:
        fail.append("lift inert — no non-down week rose")          # the lever must actually move output
    return _st("det", "earned-lift",
               "earned lift raises non-down building weeks, PRESERVES the 3:1 trough (down-week ACWR "
               "below neighbours), holds ≤1.25; co-fires with re-base graduation",
               passed=not fail, expect="troughs survive · ceiling held · lift moves output",
               got={"violations": fail or "none", "weeks_moved": moved,
                    "graduated_rebase_weeks": len(grad)},
               output={"troughs": troughs})


def _stc_earned_gate(db):
    """§6e/§6f earned-progression GATE: it's opt-in and bounded. Default-off is a pure no-op
    (factor 1.0 ⇒ shape untouched), the lift never scales a down week, the tier math is capped at
    EARNED_MAX_TIERS, and the LIVE plan exposes the gate state OFF ⇒ factor 1.0 (never automatic).
    Non-persisting (read-only on the DB)."""
    fails = []
    sh = build_shape(8, 24)
    if _apply_earned_lift(sh, 1.0) != sh:
        fails.append("factor-1.0 not a no-op")                     # the default-off guarantee
    lifted = _apply_earned_lift(sh, 1.2)
    for a, b in zip(sh, lifted):
        if _is_down(a.get("intent")) and b["km"] != a["km"]:
            fails.append(f"down scaled wk{a['wk']}")
        elif not _is_down(a.get("intent")) and b["km"] <= a["km"]:
            fails.append(f"non-down not scaled wk{a['wk']}")
    tiers = lambda s: min(EARNED_MAX_TIERS, s - EARNED_BANK_AT + 1) if s >= EARNED_BANK_AT else 0
    if (tiers(EARNED_BANK_AT - 1), tiers(EARNED_BANK_AT), tiers(EARNED_BANK_AT + 50)) \
            != (0, 1, EARNED_MAX_TIERS):
        fails.append("tier math not bounded as expected")
    e = generate_plan(db).get("earned") or {}
    if not e:
        fails.append("plan missing earned state")
    elif not e.get("opted_in") and e.get("factor") != 1.0:
        fails.append(f"off-but-not-no-op: {e}")                    # off must always mean factor 1.0
    # End-to-end through generate_plan (force the gate active): the lift must be a SINGLE level-lift
    # (build ≈ ×F vs off, NOT ×F² compounded phase-over-phase) and the down-week troughs must survive
    # the real frozen/chained path — the regression `det/earned-lift` (one generate_block) can't see.
    _orig, F = earned_state, round(1 + EARNED_VOLUME_STEP * EARNED_MAX_TIERS, 4)
    g = globals()
    try:
        off = generate_plan(db)
        g["earned_state"] = lambda d, t, pr: {**_orig(d, t, pr), "opted_in": True, "unlocked": True,
            "ready_ok": True, "active": True, "tiers": EARNED_MAX_TIERS, "factor": F}
        on = generate_plan(db)
    finally:
        g["earned_state"] = _orig
    nd = lambda pl, k: next((w["intent_km"] for w in (pl.get(k) or {}).get("weeks", [])
                             if not _is_down(w.get("intent"))), None)
    bo, bn = nd(off, "build"), nd(on, "build")
    ratio = round(bn / bo, 3) if bo and bn else None
    if ratio is not None and ratio > F + 0.06:                     # F²≈1.35 would land well above this
        fails.append(f"build lift compounded: ×{ratio} (expected ~{F})")
    moved_e2e = 0
    for key in ("base", "build"):
        ws = (on.get(key) or {}).get("weeks", [])
        offw = {w["wk"]: w for w in (off.get(key) or {}).get("weeks", [])}
        for i, w in enumerate(ws):
            if (w.get("proj_acwr") or 0) > ACWR_SOFT + 0.02:
                fails.append(f"{key} acwr#{w['wk']}={w.get('proj_acwr')}")
            if _is_down(w.get("intent")):
                nb = [ws[j].get("proj_acwr") for j in (i - 1, i + 1) if 0 <= j < len(ws)]
                if nb and (w.get("proj_acwr") or 0) > min(nb) - 0.04:
                    fails.append(f"{key} trough-collapsed#{w['wk']}")
            elif offw.get(w["wk"]) and w["km"] > offw[w["wk"]]["km"] + 0.5:
                moved_e2e += 1
    if moved_e2e == 0:
        fails.append("e2e lift inert (no building week rose through generate_plan)")
    return _st("det", "earned-gate",
               "earned lift is opt-in & bounded: factor-1.0 no-op, down weeks never scaled, tiers "
               "capped; end-to-end it's a single level-lift (no phase compounding), troughs survive, "
               "ceiling held, live plan off ⇒ no-op",
               passed=not fails, expect="opt-in no-op · single lift · troughs survive",
               got={"violations": fails or "none", "build_lift_ratio": ratio,
                    "weeks_moved_e2e": moved_e2e}, output={"earned_live": e})


def _stc_freq_advance(db):
    """§6e earned FREQUENCY advance GATE: opt-in, bounded, AND volume-floored. Inactive is a pure
    no-op; active advances a NON-DOWN Base/Build week to the 6th run ONLY when its non-long runs would
    still clear FREQ_MIN_EASY_KM — so it's dormant at low volume (no ~2 km junk), wakes as volume
    grows, never touches down weeks / Peak/Taper, and holds volume + the ACWR ceiling. Live off ⇒
    inactive / 5 runs. Non-persisting (read-only)."""
    fails = []
    # the floor's two sides, at the shape level —
    lo = _apply_freq_advance(build_shape(8, 24), True)             # ~24 km/wk: non-long ≈2.6 km < floor
    if any(w["runs"] != BASE_RUNS for w in lo):
        fails.append("low-volume week advanced (floor not enforced)")
    if _apply_freq_advance(build_shape(8, 24), False) != build_shape(8, 24):
        fails.append("inactive not a no-op")                       # the default-off guarantee
    hi_in = build_shape(8, 60)                                     # ~60 km/wk: non-long ≫ floor → advance
    hi = _apply_freq_advance(hi_in, True)
    advanced_hi = 0
    for a, b in zip(hi_in, hi):
        if _is_down(a.get("intent")):
            if b["runs"] != a["runs"]:
                fails.append(f"down advanced wk{a['wk']}")         # recovery trough keeps fewer runs
        elif b["runs"] != BASE_RUNS + 1:
            fails.append(f"high-volume non-down not advanced wk{a['wk']}")
        else:
            advanced_hi += 1
        if b["km"] != a["km"]:
            fails.append(f"volume changed wk{a['wk']}")            # constant volume — only runs change
    if advanced_hi == 0:
        fails.append("floor never opens (no high-volume week advanced)")
    f = generate_plan(db).get("freq") or {}
    if not f:
        fails.append("plan missing freq state")
    elif not f.get("opted_in") and (f.get("active") or f.get("runs") != BASE_RUNS):
        fails.append(f"off-but-not-no-op: {f}")                    # off must always mean 5 runs / inactive
    # End-to-end through generate_plan (force the gate active): structural invariants hold whatever the
    # live volume is — down weeks + Peak/Taper stay 5, any non-down Base/Build week is 5-or-6, volume
    # is unchanged vs off, ACWR held. At today's detrained volume the floor keeps it DORMANT (0
    # advanced) — that's correct, not a failure; advancement itself is covered by the high-volume case.
    _orig = freq_state
    g = globals()
    try:
        off = generate_plan(db)
        g["freq_state"] = lambda d, t, pr: {**_orig(d, t, pr), "opted_in": True, "unlocked": True,
            "ready_ok": True, "active": True, "runs": BASE_RUNS + 1}
        on = generate_plan(db)
    finally:
        g["freq_state"] = _orig
    moved = 0
    for key in ("base", "build", "peak", "taper"):
        offw = {w["wk"]: w for w in (off.get(key) or {}).get("weeks", [])}
        for w in (on.get(key) or {}).get("weeks", []):
            if (w.get("proj_acwr") or 0) > ACWR_SOFT + 0.02:
                fails.append(f"{key} acwr#{w['wk']}={w.get('proj_acwr')}")
            allowed = {BASE_RUNS} if (key in ("peak", "taper") or _is_down(w.get("intent"))) \
                else {BASE_RUNS, BASE_RUNS + 1}
            if w.get("runs") not in allowed:
                fails.append(f"{key} runs#{w['wk']}={w.get('runs')} not in {allowed}")
            ow = offw.get(w["wk"])
            if ow and abs((w.get("intent_km") or 0) - (ow.get("intent_km") or 0)) > 0.6:
                fails.append(f"{key} volume moved#{w['wk']}")      # frequency must not change km
            if w.get("runs") == BASE_RUNS + 1:
                moved += 1
    return _st("det", "freq-advance",
               "earned 6th run is opt-in, volume-floored & bounded: inactive no-op, floor suppresses "
               "low-volume weeks + opens high-volume ones, down + Peak/Taper stay 5, volume constant, "
               "ACWR held, live off ⇒ 5",
               passed=not fails, expect="opt-in · floored · non-down Base/Build only · constant volume",
               got={"violations": fails or "none", "advanced_hi_vol": advanced_hi,
                    "advanced_e2e_live": moved}, output={"freq_live": f})


def _stc_effort_discipline(db):
    """§6m effort monitor — HR-LED, not TE-led (the load-bearing design choice). The DISCRIMINATING
    case: a long easy run with LOW HR but a duration-lifted high Training Effect must read ON, never
    'too hard' (TE-gating would false-flag his cleanest easy run). Plus: a threshold-paced 'easy' run
    flags too_hard, TE only sets confidence, quality sandbagging reads too_easy/low, and the live read
    is structurally sound with a spike-resistant HRmax (his raw max is a 210 strap artifact)."""
    fails = []
    HM = 189
    def v(kind, hr, te):
        return _effort_verdict(kind, hr / HM, te)
    if v("long", 138, 3.0) != ("on", "moderate"):                  # ← the case that decides the design
        fails.append(f"genuinely-easy long mis-judged {v('long',138,3.0)} (TE-gating leak)")
    if v("easy", 168, 4.5) != ("too_hard", "high"):
        fails.append(f"threshold easy not high-conf too_hard: {v('easy',168,4.5)}")
    if v("easy", 165, 2.0) != ("too_hard", "moderate"):            # too_hard w/o TE = moderate, not high
        fails.append(f"too_hard-no-TE conf wrong: {v('easy',165,2.0)}")
    if v("easy", 150, 2.5)[0] != "hot":
        fails.append("mid-Z3 easy not 'hot'")
    if v("tempo", 130, 2.0) != ("too_easy", "low"):
        fails.append("sandbagged quality not too_easy/low")
    if v("interval", 175, 4.5) != ("on", "low"):
        fails.append("hit quality not on/low")
    if _effort_verdict("easy", None, None)[0] != "unknown":
        fails.append("no-HR not 'unknown'")
    d = effort_discipline(db)
    if not isinstance(d.get("runs"), list) or "easy_score" not in d:
        fails.append("live read malformed")
    if d.get("hrmax") and d["hrmax"] > 200:
        fails.append(f"HRmax not spike-resistant: {d['hrmax']}")
    # the date→prescribed-kind MATCH (the live window is all pre-plan defaults, so cover it on a
    # synthetic in-memory plan): a run on a prescribed QUALITY date must be classified quality and
    # EXCLUDED from the easy score — if the date match silently breaks, everything defaults to easy.
    import sqlite3 as _sq
    from datetime import timedelta as _td
    mem = _sq.connect(":memory:"); mem.row_factory = _sq.Row
    mem.executescript(
        "CREATE TABLE activities(id INTEGER PRIMARY KEY, date_time TEXT, date TEXT, sport TEXT, "
        "distance REAL, duration REAL, hr_avg INTEGER, hr_max INTEGER, raw TEXT);"
        "CREATE TABLE ignored_activities(id INTEGER PRIMARY KEY);"
        "CREATE TABLE plans(id INTEGER PRIMARY KEY, created_at TEXT, for_date TEXT, inputs TEXT, plan TEXT);")
    tdy = datetime.now().date()
    qd, ed = (tdy - _td(days=3)).isoformat(), (tdy - _td(days=5)).isoformat()
    mem.execute("INSERT INTO plans(created_at,for_date,inputs,plan) VALUES(?,?,?,?)",
                ("now", tdy.isoformat(), "{}", json.dumps(
                    {"build": {"weeks": [{"sessions": [{"date": qd, "kind": "interval"},
                                                       {"date": ed, "kind": "easy"}]}]}})))
    for i, (dt, hr) in enumerate([(qd, 170), (ed, 168)]):
        mem.execute("INSERT INTO activities VALUES(?,?,?,?,?,?,?,?,?)",
                    (i + 1, dt + "T19:00:00", dt, RUNNING_SPORT, 6.0, 2160, hr, hr + 20,
                     json.dumps({"fit_training_effect": 4.5, "gap": 10.0})))
    md = effort_discipline(mem)
    kinds = {r["date"]: r["kind"] for r in md["runs"]}
    if kinds.get(qd) != "interval":
        fails.append(f"quality date not matched: {kinds.get(qd)} (default leak)")
    if kinds.get(ed) != "easy":
        fails.append(f"easy date not matched: {kinds.get(ed)}")
    if md["easy_counts"]["judged"] != 1:          # only the easy-prescribed run is in the easy bucket
        fails.append(f"quality run leaked into easy score: judged={md['easy_counts']['judged']}")
    mem.close()
    if not _private_only_path("/api/effort-discipline"):   # per-run HR must not reach the public view
        fails.append("effort endpoint not gated private")
    return _st("det", "effort-discipline",
               "effort monitor is HR-LED (a low-HR long run w/ duration-lifted TE reads ON not too-hard); "
               "prescribed quality dates are matched + excluded from the easy score; HRmax spike-resistant; "
               "endpoint gated private",
               passed=not fails, expect="HR gates · TE corroborates · quality excluded · private",
               got={"violations": fails or "none"},
               output={"easy_score": d.get("easy_score"), "hrmax": d.get("hrmax"),
                       "easy_counts": d.get("easy_counts")})


def _stc_plan_structure(db):
    p = generate_plan(db)
    mode = p.get("mode")
    phases = p.get("phases") or []
    ok = isinstance(mode, str) and mode != "" and isinstance(phases, list) and len(phases) >= 1
    return _st("det", "plan-structure",
               "generate_plan returns a coherent plan (non-empty mode + ≥1 phase; anchor captured)",
               passed=ok, expect="mode set, phases≥1",
               got={"mode": mode, "n_phases": len(phases)},
               output={"objective": p.get("objective"), "feasibility": p.get("feasibility")})


def _stc_block_generator():
    """§6f Step A regression: the phase-agnostic generate_block reproduces the re-base byte-for-byte
    (generate_rebase is now a thin wrapper), AND it generalizes — an arbitrary longer/heavier shape
    still respects the ACWR ceiling every week (the property base-build relies on)."""
    from datetime import date
    bs, ctl0, atl0, easy = date(2026, 6, 19), 24.0, 25.0, 430  # ~7:10/km easy
    identical = generate_rebase(bs, ctl0, atl0, easy) == generate_block(REBASE_SHAPE, bs, ctl0, atl0, easy)
    shape = [{"wk": i + 1, "km": 18 + 2 * i, "runs": 4 if i % 4 != 3 else 3,
              "long": 8 + i, "strides": 0} for i in range(8)]          # a base-build-like ramp
    weeks, bound = generate_block(shape, bs, ctl0, atl0, easy)
    over = [{"wk": w["wk"], "acwr": w.get("proj_acwr")} for w in weeks
            if (w.get("proj_acwr") or 0) > ACWR_SOFT + 0.02]
    return _st("det", "block-generator",
               "generate_block reproduces the re-base exactly + holds the ACWR ceiling for any shape",
               passed=identical and not over, expect="re-base identical + ≤cap every week",
               got={"rebase_identical": identical, "acwr_over": over or "none"},
               output={"arbitrary_shape_weeks": len(weeks), "end_ctl": bound.get("end_ctl"),
                       "end_atl": bound.get("end_atl")})


def _stc_readiness_floor(db):
    out, ok = [], True
    r1 = assess_readiness(db, {"stop_symptom": True})
    p1 = r1["verdict"] == "red" and r1.get("halt") is True
    out.append({"case": "checkbox stop-symptom ⇒ red+HALT", "verdict": r1["verdict"],
                "halt": r1.get("halt"), "passed": p1})
    r2 = assess_readiness(db, {"energy": "heavy", "sleep": "poor", "note": "tired but okay"})
    p2 = r2["verdict"] == "red"
    out.append({"case": "two poor signals ⇒ floor red (LLM may not soften)",
                "verdict": r2["verdict"], "engine_floor": r2.get("engine_floor"),
                "source": r2.get("source"), "passed": p2})
    ok = p1 and p2
    return _st("det", "readiness-floor",
               "engine safety floor: stop-symptom⇒red+halt; two poor signals⇒red, never softened",
               passed=ok, output=out)


# — data sanity —
def _stc_shape_sanity(db):
    row = latest_snapshot(db)
    if not row:
        return _st("data", "shape-sanity", "latest shape snapshot present + plausible",
                   skipped=True, note="no snapshot")
    vo2, ctl = row["effective_vo2max"], row["fitness"]
    ok = (vo2 is None or 20 <= vo2 <= 85) and (ctl is None or 0 <= ctl <= 200)
    return _st("data", "shape-sanity", "latest VO2max/CTL within plausible human ranges",
               passed=ok, got={"vo2max": vo2, "ctl": ctl, "date": row["snapshot_date"]})


def _stc_inventory(db):
    return _st("data", "inventory", "activity / duplicate / ignore counts (informational)",
               passed=None, output={
                   "activities": db.execute("SELECT count(*) c FROM activities").fetchone()["c"],
                   "duplicates": len(find_duplicates(db)),
                   "manually_ignored": len(manual_ignores(db))})


# — LLM scenarios (skip cleanly without a key; capture verbatim output for judgment) —
def _stc_chat_routing(db):
    if not llm_available():
        return _st("llm", "chat-routing", "free-text → reflection vs ease; medical ⇒ full-rest halt",
                   skipped=True, note="no ANTHROPIC_API_KEY — skipped")
    pace = latest_easy_pace(db)
    cases = [("felt great, easy 5k by the river this morning", "log", None),
             ("my knee's a bit sore, let me ease off for a few days", "adjust", "reduce"),
             ("had to stop mid-run — chest felt tight and I got a bit dizzy", "adjust", "medical")]
    out, ok = [], True
    for text, want, extra in cases:
        r = propose_adjustment(text, easy_pace=pace)
        if not r.get("ok"):
            out.append({"input": text, "error": r.get("error")}); ok = False; continue
        d, kind = r["directive"], r["kind"]
        passed = kind == want
        if extra == "reduce":
            passed = passed and d["volume_multiplier"] < 1.0
        if extra == "medical":
            passed = passed and d["medical_flag"] and d["volume_multiplier"] == 0.0
        ok = ok and passed
        out.append({"input": text, "want_kind": want, "got_kind": kind,
                    "multiplier": d["volume_multiplier"], "medical": d["medical_flag"],
                    "reply": r.get("reply"), "passed": passed})
    return _st("llm", "chat-routing",
               "free-text → reflection(log) vs ease(adjust); medical ⇒ full-rest halt",
               passed=ok, needs_human=True, output=out,
               note="reply wording captured for quality review")


def _stc_objective_parse():
    if not llm_available():
        return _st("llm", "objective-parse", "NL race goal → structured fields",
                   skipped=True, note="no ANTHROPIC_API_KEY — skipped")
    cases = [("sub-4 marathon in Berlin in late September", {"type": "marathon", "priority": "A"}),
             ("the 5k business run next month, just chasing a PB", {"type": "5k"})]
    out, ok = [], True
    for text, want in cases:
        r = parse_objective_nl(text)
        if not r.get("ok"):
            out.append({"input": text, "error": r.get("error")}); ok = False; continue
        passed = all(r.get(k) == v for k, v in want.items())
        ok = ok and passed
        out.append({"input": text, "want": want,
                    "got": {k: r.get(k) for k in ("type", "priority", "date", "target", "label", "confident")},
                    "passed": passed})
    return _st("llm", "objective-parse", "NL race goal → structured {type,priority,date,target}",
               passed=ok, needs_human=True, output=out)


def _stc_readiness_note_catch(db):
    if not llm_available():
        return _st("llm", "readiness-note-catch", "free-text stop-symptom in note ⇒ red+HALT",
                   skipped=True, note="no ANTHROPIC_API_KEY — skipped")
    r = assess_readiness(db, {"energy": "ok", "sleep": "ok",
                              "note": "had to stop running today, felt faint and my chest went tight"})
    ok = r["verdict"] == "red"
    return _st("llm", "readiness-note-catch",
               "LLM reads a stop-symptom in free text ⇒ red+HALT (extends the checkbox safety net)",
               passed=ok, needs_human=True,
               output={"verdict": r["verdict"], "halt": r.get("halt"),
                       "source": r.get("source"), "reasons": r.get("reasons")})


def _stc_plan_explain(db):
    if not llm_available():
        return _st("llm", "plan-explain", "plain-language narration of the computed plan",
                   skipped=True, note="no ANTHROPIC_API_KEY — skipped")
    r = explain_plan(db)
    if not r.get("ok"):
        no_plan = (r.get("error", "").startswith("no plan"))
        return _st("llm", "plan-explain", "plain-language narration of the computed plan",
                   skipped=no_plan, passed=None if no_plan else False,
                   error=r.get("error"), needs_human=True)
    structural = bool(r.get("headline")) and isinstance(r.get("points"), list) and len(r["points"]) >= 1
    # AUTO-ASSERT the cited race-day fitness isn't inflated: no "CTL N" in the narration may exceed
    # what the engine actually projects. The ceiling allows phase end_ctls (the model may walk the
    # path) + a rounding margin; a back-of-envelope ~54 lands far above it. Turns the old silent
    # ⚑-pass into a hard FAIL when the LLM ignores projected_race_ctl and extrapolates its own.
    row = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    plan = json.loads(row["plan"]) if row else {}
    proj = (plan.get("feasibility") or {}).get("projected_ctl") or 0
    ends = [(plan.get(k) or {}).get("end_ctl") or 0 for k in ("rebase", "base", "build", "peak", "taper")]
    ceiling = max([proj] + ends) + 6
    text = (r.get("headline", "") + " " + " ".join(r.get("points", []))).replace("≈", " ")
    cited = [int(n) for n in re.findall(r"CTL[^\d]{0,6}(\d{2,3})", text, re.IGNORECASE)]
    inflated = [c for c in cited if c > ceiling]
    return _st("llm", "plan-explain",
               "narrates the plan; AUTO-ASSERT no cited CTL exceeds the engine's projection (no inflation)",
               passed=structural and not inflated, needs_human=True,
               expect=f"structured + no CTL > {ceiling}", got={"cited_ctl": cited, "inflated": inflated},
               output={"headline": r.get("headline"), "points": r.get("points"),
                       "change_note": r.get("change_note"), "projected_race_ctl": proj})


def run_server_selftest(db, categories=None):
    """Run the in-process battery. Returns the full report dict (also persisted by the caller)."""
    scenarios = [lambda: _stc_clamp(), lambda: _stc_map_privacy(db), lambda: _stc_day_spacing(),
                 lambda: _stc_rebase_anchor(), lambda: _stc_unplanned_log(),
                 lambda: _stc_within_week(), lambda: _stc_bonus_affordance(),
                 lambda: _stc_doubles_log(), lambda: _stc_dedup(db),
                 lambda: _stc_local_delete(), lambda: _stc_settings(), lambda: _stc_multi_a_chain(),
                 lambda: _stc_periodize_chain(), lambda: _stc_multi_a_plan(),
                 lambda: _stc_latest_running(),
                 lambda: _stc_projector(db), lambda: _stc_acwr_ceiling(db),
                 lambda: _stc_block_generator(), lambda: _stc_base_phase(), lambda: _stc_polarized(),
                 lambda: _stc_taper(), lambda: _stc_freeze_continuity(), lambda: _stc_down_weeks(),
                 lambda: _stc_long_run(), lambda: _stc_ctl_floor(),
                 lambda: _stc_earned_lift(), lambda: _stc_earned_gate(db),
                 lambda: _stc_freq_advance(db), lambda: _stc_effort_discipline(db),
                 lambda: _stc_plan_structure(db), lambda: _stc_readiness_floor(db),
                 lambda: _stc_shape_sanity(db), lambda: _stc_inventory(db),
                 lambda: _stc_chat_routing(db), lambda: _stc_objective_parse(),
                 lambda: _stc_readiness_note_catch(db), lambda: _stc_plan_explain(db)]
    results = [_run_one(fn) for fn in scenarios]
    if categories:
        results = [r for r in results if r["category"] in categories]
    return _selftest_report(results, "server")


def _selftest_report(results, source):
    summary = {"passed": sum(1 for r in results if r["passed"] is True),
               "failed": sum(1 for r in results if r["passed"] is False),
               "skipped": sum(1 for r in results if r.get("skipped")),
               "needs_human": sum(1 for r in results if r.get("needs_human")),
               "total": len(results)}
    return {"created_at": _now_iso(), "source": source,
            "env": {"llm": llm_available(), "readonly": READONLY},
            "summary": summary, "scenarios": results}


def save_selftest_run(db, report):
    s = report["summary"]
    cur = db.execute(
        "INSERT INTO selftest_runs(created_at, source, passed, failed, skipped, needs_human, llm, report) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (report["created_at"], report.get("source", "server"), s["passed"], s["failed"],
         s["skipped"], s["needs_human"], 1 if report["env"]["llm"] else 0, json.dumps(report)))
    db.commit()
    return cur.lastrowid


def _selftest_text(report):
    """A compact terminal/markdown summary — readable in a shell and easy to paste back."""
    s = report["summary"]
    lines = [f"# Sparing Horse self-test — {report['created_at']}  (source: {report['source']})",
             f"# llm={report['env']['llm']}  readonly={report['env']['readonly']}",
             f"# {s['passed']}/{s['total']} PASS · {s['failed']} FAIL · {s['skipped']} skipped · "
             f"{s['needs_human']} need-human-eyes", ""]
    icon = {True: "PASS", False: "FAIL", None: "····"}
    for r in report["scenarios"]:
        tag = "SKIP" if r.get("skipped") else icon[r["passed"]]
        flag = " ⚑" if r.get("needs_human") else ""
        lines.append(f"[{tag}]{flag} {r['category']}/{r['id']} — {r['desc']}")
        if r.get("error"):
            lines.append(f"        error: {r['error']}")
        elif r.get("got") is not None and not r.get("skipped"):
            lines.append(f"        got: {json.dumps(r['got'], ensure_ascii=False)}")
    return "\n".join(lines)


# ── Self-test routes (private only — gated off the public container in _readonly_guard) ──
@app.post("/api/selftest/run")
def api_selftest_run():
    db = get_db()
    cats = request.args.get("only")
    report = run_server_selftest(db, set(cats.split(",")) if cats else None)
    report["id"] = save_selftest_run(db, report)
    return jsonify(report)


@app.get("/api/selftest")
def api_selftest_get():
    db = get_db()
    if request.args.get("list"):
        rows = db.execute(
            "SELECT id, created_at, source, passed, failed, skipped, needs_human, llm "
            "FROM selftest_runs ORDER BY id DESC LIMIT 50").fetchall()
        return jsonify([dict(r) for r in rows])
    rid = request.args.get("id")
    row = (db.execute("SELECT report FROM selftest_runs WHERE id=?", (rid,)).fetchone() if rid
           else db.execute("SELECT report FROM selftest_runs ORDER BY id DESC LIMIT 1").fetchone())
    if not row:
        return jsonify(ok=False, error="no self-test runs yet — POST /api/selftest/run"), 404
    if request.args.get("text"):
        return app.response_class(_selftest_text(json.loads(row["report"])), mimetype="text/plain")
    return app.response_class(row["report"], mimetype="application/json")


@app.post("/api/selftest/client")
def api_selftest_client():
    """Store browser self-check results (the client harness POSTs here) as a run row."""
    db = get_db()
    results = body().get("scenarios", [])
    for r in results:                       # normalise shape from the client
        r.setdefault("category", "client"); r.setdefault("needs_human", False)
        r.setdefault("skipped", False); r.setdefault("passed", None)
    report = _selftest_report(results, "client")
    report["env"]["ua"] = request.headers.get("User-Agent", "")
    report["id"] = save_selftest_run(db, report)
    return jsonify(report)


@app.get("/selftest")
def selftest_page():
    return html_page(SELFTEST_HTML)


# The browser self-check page (private). Drives the real §6c endpoints in a real browser — where
# the key lives on the NAS — and asserts each payload is render-ready (sandbox DOM render, no
# side-effects: only non-persisting endpoints). Results POST to /api/selftest/client and join the
# same run history; a button also triggers the in-process server battery. The whole point is to
# capture verbatim, machine-readable evidence so correctness is judged from the report.
SELFTEST_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sparing Horse — self-test</title>
<style>
  body{background:#141210;color:#ece7df;font:14px/1.5 system-ui,sans-serif;margin:0;padding:24px;max-width:1000px}
  h1{font-size:20px;margin:0 0 4px} .sub{color:#9a8f80;margin:0 0 18px;font-size:13px}
  button{background:#d4744e;color:#fff;border:0;border-radius:8px;padding:8px 14px;font-size:13px;cursor:pointer;margin-right:8px}
  button.ghost{background:#2a251f;color:#ece7df}
  table{border-collapse:collapse;width:100%;margin-top:14px;font-size:13px}
  th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #2a251f;vertical-align:top}
  th{color:#9a8f80;font-weight:600}
  .tag{font:600 11px/1 ui-monospace,monospace;padding:3px 6px;border-radius:5px;white-space:nowrap}
  .PASS{background:#1e3a23;color:#7fd093} .FAIL{background:#46211f;color:#ef8a7e}
  .SKIP{background:#2a251f;color:#9a8f80} .INFO{background:#21303f;color:#7eb6ef}
  .flag{color:#e3b34e} pre{margin:4px 0 0;white-space:pre-wrap;word-break:break-word;color:#b9ad9d;font-size:12px}
  .sumline{font:600 14px/1.6 ui-monospace,monospace;margin:12px 0}
  code{background:#2a251f;padding:2px 6px;border-radius:5px;font-size:12px}
  a{color:#d4744e}
</style></head><body>
<h1>Sparing Horse — self-test</h1>
<p class="sub">Private diagnostics. The <b>browser self-check</b> drives the live §6c endpoints here (real key on the NAS) and stores results; the <b>server battery</b> runs the in-process scenarios. Both land in <code>/api/selftest</code>.</p>
<div>
  <button id="run">Run browser self-check</button>
  <button id="server" class="ghost">Run server battery</button>
  <button id="json" class="ghost">Open latest JSON</button>
</div>
<div id="sum" class="sumline"></div>
<table id="tbl"><thead><tr><th>Result</th><th>Scenario</th><th>Detail</th></tr></thead><tbody></tbody></table>
<script>
const $=s=>document.querySelector(s), tb=$("#tbl tbody");
const TAG={true:"PASS",false:"FAIL",null:"INFO"};
function row(r){
  const tag = r.skipped?"SKIP":TAG[r.passed];
  const detail = r.error ? ("error: "+r.error)
    : (r.output!=null ? JSON.stringify(r.output,null,1) : (r.note||r.got!=null?JSON.stringify(r.got):""));
  const tr=document.createElement("tr");
  tr.innerHTML=`<td><span class="tag ${tag}">${tag}</span>${r.needs_human?' <span class="flag" title="captured for human/AI judgment">⚑</span>':''}</td>
    <td><b>${r.category}/${r.id}</b><div class="sub">${r.desc||""}</div></td>
    <td><pre>${(detail||"").replace(/</g,"&lt;")}</pre></td>`;
  tb.appendChild(tr);
}
function summarise(s){
  $("#sum").innerHTML=`${s.passed}/${s.total} PASS · ${s.failed} FAIL · ${s.skipped} skipped · ${s.needs_human} need-human-eyes ⚑`;
}
// Render a payload into a detached node and assert expected structure — no side effects.
function sandbox(html){ const d=document.createElement("div"); d.innerHTML=html; return d; }
function t(){ return performance.now(); }
function classify(status, j){
  // a missing key surfaces as 502 / "not configured" — that's a SKIP, not a FAIL.
  const noKey = status===502 || (j && j.ok===false && /not configured|ANTHROPIC/i.test(j.error||""));
  return noKey ? "skip" : null;
}
async function chatProbe(){
  const t0=t();
  const res=await fetch("/api/adjustment/propose",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({text:"my knee's a bit sore, let me ease off a few days"})});
  const j=await res.json(); const ms=Math.round(t()-t0);
  if(classify(res.status,j)) return {category:"client",id:"chat-render",desc:"propose → render-ready reply",skipped:true,note:"no key",ms};
  const okData = j.ok && ["log","adjust"].includes(j.kind);
  const node = sandbox(`<div class="adjreply">${(j.reply||"").replace(/</g,"&lt;")}</div>`);
  const rendered = !!node.querySelector(".adjreply") && (j.reply||"").length>0;
  return {category:"client",id:"chat-render",desc:"propose → reply renders (kind + non-empty reply)",
    passed:okData&&rendered,needs_human:true,ms,
    output:{kind:j.kind,multiplier:j.directive&&j.directive.volume_multiplier,medical:j.directive&&j.directive.medical_flag,reply:j.reply}};
}
async function objProbe(){
  const t0=t();
  const res=await fetch("/api/objectives/parse",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({text:"sub-4 marathon in Berlin in late September"})});
  const j=await res.json(); const ms=Math.round(t()-t0);
  if(classify(res.status,j)) return {category:"client",id:"objective-render",desc:"parse → render-ready",skipped:true,note:"no key",ms};
  const ok = j.ok && !!j.type;
  return {category:"client",id:"objective-render",desc:"NL goal parses to structured fields",
    passed:ok,needs_human:true,ms,output:{type:j.type,priority:j.priority,date:j.date,target:j.target,confident:j.confident}};
}
async function explainProbe(){
  const t0=t();
  const res=await fetch("/api/plan/explain",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
  const j=await res.json(); const ms=Math.round(t()-t0);
  if(classify(res.status,j)) return {category:"client",id:"explain-render",desc:"explain → render-ready",skipped:true,note:"no key",ms};
  if(!j.ok && /no plan/i.test(j.error||"")) return {category:"client",id:"explain-render",desc:"plan explanation",skipped:true,note:"no plan yet",ms};
  const ok = j.ok && !!j.headline && Array.isArray(j.points) && j.points.length>0;
  return {category:"client",id:"explain-render",desc:"plan explanation renders (headline + bullets)",
    passed:ok,needs_human:true,ms,output:{headline:j.headline,points:j.points,change_note:j.change_note}};
}
async function readyProbe(){
  const t0=t();
  const res=await fetch("/api/readiness"); const j=await res.json(); const ms=Math.round(t()-t0);
  const verdict=j.verdict||(j.readiness&&j.readiness.verdict);
  const ok=["green","amber","red"].includes(verdict);
  const node=sandbox(`<span class="tag ${ok?'PASS':'FAIL'}">${verdict||"?"}</span>`);
  return {category:"client",id:"readiness-render",desc:"readiness verdict renders (green/amber/red)",
    passed:ok&&!!node.querySelector(".tag"),ms,output:{verdict}};
}
async function runClient(){
  tb.innerHTML=""; $("#sum").textContent="running…";
  const probes=[chatProbe,objProbe,explainProbe,readyProbe]; const scenarios=[];
  for(const p of probes){ try{ scenarios.push(await p()); }catch(e){ scenarios.push({category:"client",id:p.name,desc:"probe threw",passed:false,error:String(e)}); } }
  scenarios.forEach(row);
  const res=await fetch("/api/selftest/client",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({scenarios})});
  const stored=await res.json(); summarise(stored.summary);
  $("#sum").innerHTML+=` · saved run <code>#${stored.id}</code> · <a href="/api/selftest?id=${stored.id}">JSON</a>`;
}
async function runServer(){
  tb.innerHTML=""; $("#sum").textContent="running server battery…";
  const res=await fetch("/api/selftest/run",{method:"POST"}); const rep=await res.json();
  rep.scenarios.forEach(row); summarise(rep.summary);
  $("#sum").innerHTML+=` · saved run <code>#${rep.id}</code> · <a href="/api/selftest?id=${rep.id}">JSON</a> · <a href="/api/selftest?id=${rep.id}&text=1">text</a>`;
}
$("#run").addEventListener("click",runClient);
$("#server").addEventListener("click",runServer);
$("#json").addEventListener("click",()=>location.href="/api/selftest");
runClient();
</script></body></html>"""


# ── Main ────────────────────────────────────────────────────────────────────
init_db()
try:
    with app.app_context():
        apply_settings_overrides(get_db())   # overlay any saved meta settings onto the env defaults
except Exception as e:
    print(f"[settings] startup overlay skipped: {e}")
start_scheduler()   # runs under waitress (import) and the dev server alike (logs the effective TZ)

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":   # CLI: python SparingHorse.py selftest
        with app.app_context():
            db = get_db()
            rep = run_server_selftest(db)
            rep["id"] = save_selftest_run(db, rep)
        print(_selftest_text(rep))
        sys.exit(1 if rep["summary"]["failed"] else 0)
    print(f"Sparing Horse → http://127.0.0.1:{PORT}  (token {'set' if RUNALYZE_TOKEN else 'MISSING'})")
    app.run(host="127.0.0.1", port=PORT, debug=False)
