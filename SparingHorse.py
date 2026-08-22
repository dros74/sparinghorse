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
import base64
import functools
import html
import io
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
import zlib
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from flask import Flask, g, jsonify, redirect, request, send_file
from werkzeug.exceptions import HTTPException
from requests.adapters import HTTPAdapter, Retry

# ── Config ──────────────────────────────────────────────────────────────────
PORT = int(os.environ.get("SH_PORT", "8770"))
DB_PATH = Path(os.environ.get("SH_DB", "sparinghorse.db"))
RUNALYZE_BASE = os.environ.get("RUNALYZE_BASE", "https://runalyze.com/api/v1")
# The window-settable values (the Runalyze token, the Claude key, the athlete context, the house
# link, the private URL, the weather cities, the timezone) live in the TECH-4 config snapshot below,
# not in module globals — `config().runalyze_token`, `config().athlete_context`, and so on.
# Default to the latest capable model; adaptive thinking + low effort for the light parsing/
# judgment tasks the engine hands off. Overridable for cost/latency experiments.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
# Public read-only mode (§ two-version deploy): the public container runs with SH_READONLY=1, a
# read-only DB mount, and NO tokens — so it physically can't sync/write. On top of that the app
# blocks every mutating endpoint, hides all inputs, and withholds the medical sections (blood
# markers + readiness). The private container (behind Cloudflare Access) runs without the flag.
READONLY = os.environ.get("SH_READONLY", "").lower() in ("1", "true", "yes")
# On the public page, an optional "Log in" link to the private (Access-protected) console
# (`config().private_url`); optional house branding — a back-link in the header — in
# `config().house_url` / `.house_name`; and an optional per-user athlete context injected into the
# LLM prompts (`config().athlete_context`, e.g. "post-illness rebuild, cleared by my doctor").
# Empty context = a neutral generic runner. The medical SAFETY net (cardiac/exertional symptom →
# halt + see a doctor) is always on regardless of what the context says.
# Optional weather widget cities: "Name,lat,lon;Name,lat,lon". Empty = the widget is hidden.
RUNNING_SPORT = "Running"  # the canonical run sport name (used for seed/synthetic inserts)
# The engine counts the whole RUNNING FAMILY — Running, Trail Running, Treadmill Running, … — as runs.
# This SQL predicate is the SINGLE source of truth so trail/treadmill runs reach the plan-side run views
# (effort discipline, banking adherence, plan-vs-actual, the block log, weekly mileage, HR) the way they
# already reach the latest-activity tile. The CTL/ATL reconstruction (daily_trimp_series) is all-sport
# already, so broadening here never touches the digit-for-digit-validated fitness model.
RUN_FAMILY_SQL = "LOWER(sport) LIKE '%run%'"


def _is_run_family(sport):
    """True for any running-family sport name (Running, Trail Running, Treadmill Running, …)."""
    return "run" in (sport or "").lower()
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

# ── TECH-4 — the runtime config is ONE IMMUTABLE SNAPSHOT ───────────────────────────────────
# The window-settable values used to live in nine module globals that `apply_settings_overrides` and
# `apply_secret_overrides` REBOUND one at a time. Two problems with that, one live and one waiting:
#  · A save rebinds six names in sequence while request threads read them. There is no lock and no
#    barrier, so a thread can read the new house URL beside the old house name — a torn config. It
#    is a narrow window and nobody has seen it bite, which is exactly how it would stay until it did.
#  · The moment any of this moves into a package (the code-split's `config.py`), a reader that did
#    `from SparingHorse import RUNALYZE_TOKEN` holds a COPY of the binding, and every later save
#    updates a name that reader will never look at again. Rebinding module globals is a design that
#    only works while everything shares one module object.
# So: one immutable snapshot, swapped by a single assignment. A reader takes the snapshot ONCE and
# reads fields off it — whatever it does next, those fields are all from the same generation. The
# generation counter is what lets the cached HTTP session and LLM client notice they are stale;
# before this, `_http()` baked the token into its session headers at first build and never rebuilt
# it, so a Runalyze token changed in the Settings window kept authenticating REST calls with the OLD
# token until someone restarted the process. (`_mcp_headers` read the global per call, so MCP picked
# it up and REST did not — the inconsistency that gives that bug away.)
RuntimeConfig = namedtuple("RuntimeConfig",
                           "athlete_context house_url house_name private_url weather_cities "
                           "sync_tz runalyze_token anthropic_api_key generation")

_config_lock = threading.Lock()   # serializes the read-modify-write of a swap, not the reads

_CONFIG = RuntimeConfig(
    athlete_context=os.environ.get("SH_ATHLETE_CONTEXT", "").strip(),
    house_url=os.environ.get("SH_HOUSE_URL", ""),
    house_name=os.environ.get("SH_HOUSE_NAME", ""),
    private_url=os.environ.get("SH_PRIVATE_URL", ""),
    weather_cities=(),
    sync_tz=None,                 # set just below, once ZoneInfo + the parser are defined
    runalyze_token=os.environ.get("RUNALYZE_TOKEN", ""),
    anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
    generation=0,
)


def config():
    """The current runtime config. Take it ONCE at the top of a request, a job or a helper and read
    every field off that one snapshot: a save swaps the whole object in a single assignment, so one
    snapshot is never half-old — while two separate `config().x` reads can still straddle a save."""
    return _CONFIG


def _config_swap(**changes):
    """Publish a NEW config: build it off the current one, bump the generation, assign once. The lock
    covers the read-modify-write (two concurrent saves must not each build off the same base); the
    assignment itself is what readers rely on, and that is atomic without them taking any lock."""
    global _CONFIG
    with _config_lock:
        _CONFIG = _CONFIG._replace(generation=_CONFIG.generation + 1, **changes)
    return _CONFIG


_session = None
_session_gen = -1        # the config generation `_session` was built for (TECH-4)


def _http():
    global _session, _session_gen
    cfg = config()
    # TECH-4 — the token rides in the session HEADERS, so the session belongs to the generation that
    # supplied it: a key changed in the Settings window must not keep authenticating with the old one.
    if _session is None or _session_gen != cfg.generation:
        s = requests.Session()
        retries = Retry(total=2, backoff_factor=0.8,
                        status_forcelist=(429, 500, 502, 503, 504),
                        allowed_methods=frozenset(["GET"]))
        s.mount("https://", HTTPAdapter(max_retries=retries))
        s.headers.update({
            "token": cfg.runalyze_token,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        })
        _session, _session_gen = s, cfg.generation
    return _session

# ── Runalyze REST client ────────────────────────────────────────────────────
class RunalyzeError(RuntimeError):
    pass


def _get(path, params=None, timeout=25):
    """GET a Runalyze Personal API endpoint as JSON. Auth via the `token` header."""
    if not config().runalyze_token:
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
    h = {"Authorization": f"Bearer pt#{config().runalyze_token}", "Content-Type": "application/json",
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


_mcp_lock = threading.Lock()    # TECH-4 — one initialize at a time (see _mcp_init)


def _mcp_init():
    """(Re)establish the MCP session. TECH-4 — SERIALIZED: a page-load profile fetch and the nightly
    sync can both find a dead session and re-initialize at once, and the two handshakes then race to
    assign `_mcp_session` — the loser's id is what sticks, pointing at a session the server has
    already been told to forget. Under the lock the second caller waits and then uses the first
    caller's fresh session, which is also one fewer handshake against Runalyze."""
    global _mcp_session
    with _mcp_lock:
        return _mcp_init_locked()


def _mcp_init_locked():
    global _mcp_session
    # A NEW InitializeRequest carries no Mcp-Session-Id (MCP spec) — and the stale id is exactly what a
    # re-init exists to shed. Before 0.27.1 the old id rode along on the new initialize and was never
    # cleared, so a session the server had expired stayed sticky until restart (Gemini review #7).
    _mcp_session = None
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "sparinghorse", "version": "0.1"}}}
    r = _http().post(MCP_URL, json=body, headers=_mcp_headers(), timeout=30)
    if r.status_code >= 400:
        raise RunalyzeError(f"MCP initialize HTTP {r.status_code}: {r.text[:200]!r}")
    _mcp_session = r.headers.get("Mcp-Session-Id")
    _http().post(MCP_URL, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                 headers=_mcp_headers(), timeout=30)


def _mcp_post(body, timeout):
    """One JSON-RPC round trip → (parsed body, None) or (None, why). A non-2xx status or an unparseable
    body (a 404 'Not Found' for a dead session) is a reason to re-initialize, not a crash — before
    0.27.1 the parse raised ValueError BEFORE the re-init path, so one expired session failed every
    later MCP read (hover profiles, LTHR derive, §RD, the health/sleep sync) until restart."""
    r = _http().post(MCP_URL, json=body, headers=_mcp_headers(), timeout=timeout)
    if r.status_code >= 400:
        return None, f"HTTP {r.status_code}: {r.text[:120]!r}"
    try:
        return _mcp_parse(r.text), None
    except ValueError as e:
        return None, f"unparseable body: {e}"


def mcp_call(tool, args):
    """Call an MCP tool, returning its structuredContent. A dead session (non-2xx, non-JSON body, or a
    JSON-RPC error) re-initializes ONCE and retries; a second failure raises RunalyzeError — every
    caller catches it — rather than a KeyError/ValueError from inside the parse."""
    if not _mcp_session:
        _mcp_init()
    body = {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": tool, "arguments": args}}
    d, err = _mcp_post(body, 45)
    if err or "error" in d:                      # likely stale session → re-init once
        _mcp_init()
        d, err = _mcp_post(body, 45)
        if err:
            raise RunalyzeError(f"MCP {tool}: {err}")
        if "error" in d:
            raise RunalyzeError(f"MCP {tool}: {d['error']}")
    res = d.get("result", {})
    return res.get("structuredContent") or json.loads(res["content"][0]["text"])


def cadence_is_halved(source):
    """Suunto logs cadence as a one-leg step count → double it for true spm. Conditioned on
    source so other devices (which report full spm) aren't wrongly doubled."""
    return (source or "").lower() == "suunto"


# Bump when activity_profile's shape changes (new channel etc.) so cached profiles in trackcache
# that predate the bump are re-fetched instead of served stale. v2 = elevation; v3 = route path (lat/long).
PROFILE_VERSION = 3

# §PRO14 — the engine's own identity, stamped onto every plan it generates so a SAVED plan can say
# whether the engine that made it is the one now running. Plans are versioned artifacts and a deploy
# never regenerates them (§6f Step E), so after an upgrade the app serves a plan built by code that
# no longer exists — with no way for the view to know. §FT7 half-covered this by sniffing for a
# PRE-§FT payload shape (no band and no `today`), which by construction cannot see a plan that is
# merely one release old: on 2026-07-28 a 0.21.0 plan rendered under 0.21.1 with no marker at all,
# and the owner reasonably read the unchanged numbers as a failed deploy. A shape sniff can only ever
# recognise the breakages it was written for; an identity comparison catches every future one.
# WHY A CONSTANT, not a CHANGELOG parse: the container ships SparingHorse.py alone (see Dockerfile),
# so there is no changelog to read at runtime. WHY NOT A SOURCE HASH: it would fire on comment-only
# releases and train the owner to ignore the marker, which is the failure it exists to prevent.
# Drift is prevented instead by `det/engine-version`, which fails the suite whenever this constant
# and the newest CHANGELOG heading disagree — so cutting a release without bumping it cannot pass.
ENGINE_VERSION = "0.31.1"


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


# ── §RD — workout-structure classifier ("read the run back") ─────────────────
# Decode a recorded run's pace profile into the plan's OWN session vocabulary (easy/long/tempo/
# interval/long_mp). Two deliberately separate passes: STRUCTURE first by CONTRAST — a sustained
# relative pace shift opens a block, no pace table involved, so straddling a zone boundary can't
# split a rep — then NAMING, each block labeled against the runner's pace zones AS OF that date
# (zones move with fitness, so "what counts as tempo" tracks the athlete, not a constant).
# Pace-only structure (grade-adjusted where the elevation stream exists — hills must not fake
# intervals); HR rides along per segment for the private effort monitor but is never a structure
# input (HR lags short reps). Versioned in structcache; classified at sync for new runs, lazily on
# first view for old ones.
STRUCT_VERSION = 8   # v8 (2026-07-23): the workout ENDS — a trailing work-zone block whose gap since
#                      the previous rep dwarfs the session's own rest scale (≥ RD_TAIL_REST_MIN_S and
#                      ≥ RD_TAIL_REST_FACTOR × the longest real inter-rep rest) is cooldown drift,
#                      not a rep. First live §SJ-era interval read (2026-07-22): the uphill run-home
#                      flattened for its last 800m, pace jumped ~50s/km at unchanged effort (HR still
#                      172 from the reps), and 1:45 @5:55 after 10min of easy running minted rep 3 of
#                      a 2-rep VO₂ session. The v5 baseline fix caught the same ghost when the
#                      CONTRAST was wrong; this catches it when the contrast is honest but the
#                      session grammar had no notion of "over".
#                      v7 (2026-07-20, same night): a SHORT stride-dense recording (≥4 counted
#                      strides in ≤ RD_STRIDES_SHORT_S) reads Strides regardless of base pace, and
#                      the strides check now precedes the wall-to-wall-hard return (which also
#                      carries the stride fields now) — the first live 1+1 part (6 strides, jog
#                      recovery, 6min) had read "tempo, no easy bracket" with stride_reps dropped.
#                      ALSO v7 (owner's ground truth: 10 run, 6 counted; his hint "look at
#                      cadence"): the cadence-burst pass — sub-frame rests alias strides into pace
#                      blends, the raw ~1Hz cadence stream doesn't; bursts ≥10% over the local
#                      cadence floor, 5–60s wide, pace-corroborated at half the stride bar,
#                      deduped against pace-counted centers. Pace stays primary.
#                      v6 (2026-07-20, §SJ/§SQ): per-stride execution detail (`stride_reps`: peak
#                      pace + pre-stride floor HR + post-peak HR in a lag window + recovery floor)
#                      computed AT CLASSIFY TIME — frames aren't persisted, so a view-time read
#                      would need a re-fetch; the version bump makes old cached reads lazily
#                      re-classify on first view (the v4→v5 rollout pattern).
#                      v5 (2026-07-14): baseline = the whole slowest LEVEL's time/distance pace, not
#                      the anchor block's own — a 2-min float anchored the baseline on the first live
#                      §RD read and promoted a marathon-pace run-home to work rep 3 (n_work 3, want 2).
#                      v4 (2026-07-05): fused stride clusters counted by INTERNAL peaks (a wide
#                      episode was discarded whole — 6 of his real 11 counted at full streams),
#                      dedicated "Strides" kind + set grouping "(5+6)" + strides-only pace, per the
#                      owner's spec. v3: cadence corroboration (GPS spikes). v2: honest pace.
RD_FRAME_S = 15            # analysis frame: one pace sample per 15s slab
RD_CONTRAST = 0.08         # relative sustained pace shift that opens a new block
RD_SUSTAIN_FRAMES = 3      # the shift must hold ~45s — GPS jitter and a 10s surge don't cut
RD_MIN_BLOCK_S = 45        # shorter blocks are absorbed into the nearer-pace neighbour
# Strides are counted the way the owner reads the chart (his 2026-07-05 framing): a GLOBAL peak
# pass — short, prominent speed peaks over the local valley floor, width judged on the time axis,
# cadence countersigning. (The earlier incremental burst state leaked half a real session's
# strides through merges/voids/resets, then pace-only bars counted GPS spikes.)
RD_STRIDE_PEAK = 0.22      # a stride peak rides ≥22% over the local floor (raw grade-adjusted
#                            speed) — genuine strides run 25–40% over easy; 10–20% texture doesn't.
RD_STRIDE_MAX_S = 60       # a peak wider than this is a REP, not a stride — the block grammar owns
#                            it (plan vocabulary: reps ≥ ~2min; strides ≈ 15–30s + frame smear)
RD_STRIDE_FLOOR_WIN = 10   # ± frames (≈ ±2.5min) for the rolling-median valley floor — strides are
#                            short, so the local median sits on the easy/recovery floor around them
RD_STRIDE_CAD = 0.06       # cadence must corroborate: a stride is legs turning over faster
#                            (typically +10–20% spm), a GPS speed spike leaves cadence flat — pace
#                            alone counted 4 spikes on his no-strides 2026-07-04 run even at the
#                            22% bar. Applied only when the cadence stream is present; ratio-based,
#                            so one-leg (halved) cadence sources compare cleanly.
RD_STRIDE_DIP = 0.10       # inside a FUSED fast episode (strides bridged by a quick recovery),
#                            consecutive speed maxima separated by a ≥10% dip count individually —
#                            you can't run two strides without slowing between; a wide episode was
#                            previously discarded whole (6 of his real 11 counted, 2026-07-05)
RD_STRIDES_SESSION_MIN = 4 # ≥ this many strides over a NON-easy base (slower than the easy zone's
#                            slow edge = walking/standing recovery, not an easy run) ⇒ the run IS
#                            a strides session — his spec: "Strides — 18min @7:53/km · 11× strides
#                            (5+6) @4:20/km". Strides sprinkled on a genuine easy run stay "Easy
#                            run · N× strides".
RD_STRIDES_SHORT_S = 720   # ≤ this long AND ≥ SESSION_MIN counted strides ⇒ Strides REGARDLESS of
#                            the base pace (§SJ, first live 1+1 2026-07-20): a 6-min jog-recovery
#                            strides part smears stride speed into every 15s block — the blend read
#                            "threshold wall-to-wall" and the tempo branch won its race against the
#                            stride count. Inside ~12min there is no easy run to protect, and a
#                            genuine short tempo counts ~0 stride peaks (width + cadence gates), so
#                            the counted-strides discriminator is decisive on its own.
RD_STRIDE_SET_GAP = 1.4    # a gap between strides > 1.4× the median gap (and >90s) starts a new
#                            SET — the "(5+6)" grouping; uniform gaps read as one set
# §RD v7 cadence-burst counting (the owner's hint, 2026-07-20: he ran 10, frames counted 6 — rests
# shorter than the 15s grid alias every other stride into a blend; the RAW ~1Hz cadence stream
# still shows one distinct high-cadence run per stride). Pace stays PRIMARY; a cadence burst only
# counts with pace corroboration (half the stride bar), so a flat-pace cadence flutter never does.
RD_STRIDE_CAD_BURST = 0.10  # a burst rides ≥10% over the local cadence floor (strides +10–20% spm)
RD_STRIDE_BURST_MIN_S = 5   # … sustained ≥5s (a stride is 15–25s of fast legs; bounce is shorter)
RD_STRIDE_BURST_PAD = 20    # a burst within ±20s of a pace-counted stride IS that stride (dedupe):
#                             the pace center is frame-quantized (an episode's centre can sit ~15s
#                             off the burst midpoint), while real consecutive strides are ≥~30s
#                             apart — so ±20s merges duplicates without eating a neighbour
RD_MIN_RUN_S = 600         # under 10 minutes there's no structure worth reading
RD_LONG_MIN = 85           # a uniform easy run at/over this many minutes is a "long" run
RD_MP_BASE_MIN = 30        # long_mp needs at least this much easy base before the MP finish
RD_PAUSE_PACE = 1200       # slower than 20:00/km = standing/pause frame (breaks blocks)
RD_GOOD_VALID = 0.9        # ≥ this share of readable frames ⇒ "good" read; ≥0.7 ⇒ "rough"
RD_WORK_ZONES = ("marathon", "threshold", "interval")   # the zones a work block can be named
RD_WORK_CONTRAST = 0.10    # a WORK block must be ≥10% faster than the run's own easy baseline —
#                            a zone label alone is NOT work: at low fitness his ordinary easy-pace
#                            drift crosses easy_top (the easy-days-run-hard pattern the effort
#                            monitor owns), and calling that "intervals" misreads a plain easy run.
#                            Structure is what you SEE in the pace chart: contrast. Zones only name it.
RD_BASE_MIN_SHARE = 0.25   # the baseline = the SLOWEST pace level carrying ≥ this share of the run
#                            (or ≥10min) — so a 15min wu + 30min tempo still baselines on the wu side
RD_MIN_WORK_S = 100        # a work REP is ≥ ~2min in the plan's vocabulary (DAVIS_BASE_VO2_REP_MIN);
#                            margin under 120 for frame quantization. Shorter fast blocks = surges/
#                            smeared strides, never session elements.
RD_MIN_TEMPO_S = 480       # a LONE continuous work block must be ≥ ~8min (the plan's smallest tempo/
#                            MP element) — a single 3min surge is not a tempo session
RD_TAIL_REST_MIN_S = 480   # a trailing "rep" is cooldown drift once the gap since the previous rep
#                            is ≥8min AND ≥ the factor × the session's own longest inter-rep rest —
#                            nobody floats 8 minutes between VO₂ reps (2026-07-22 live: the flat last
#                            800m of an uphill run-home read as rep 3 of a 2-rep session)
RD_TAIL_REST_FACTOR = 3    # …judged against the session's OWN rest scale, so long-recovery formats
#                            (5min jog VO₂ classics) keep their genuine final reps


def _rd_median(xs):
    s = sorted(xs)
    n = len(s)
    return None if not n else (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0)


def _rd_frames(streams, min_s=RD_MIN_RUN_S):
    """Raw MCP streams → 15s frames of grade-adjusted pace (sec/km) + hr + km, by INTERPOLATING the
    cumulative distance/elevation/HR at the frame edges — so any sampling density works (1Hz Suunto,
    battery-save every 25s, whatever). A frame across a recording gap where the runner stood still
    reads slower than RD_PAUSE_PACE ⇒ None (pauses break blocks, never join them). Distance
    normalised with the same >100 ⇒ metres heuristic as activity_profile; grade adjustment is a
    Minetti-lite cost factor (1 + 0.029g + 0.0015g², g clamped ±15%) so a hill reads as its flat-
    equivalent effort pace. Returns (frames, valid_share)."""
    dist, tim, hr = streams.get("distance") or [], streams.get("time") or [], streams.get("heart_rate") or []
    cad = streams.get("cadence") or []
    elev = next((a for a in (streams.get("elevation_corrected"), streams.get("elevation_original"))
                 if a and any(v is not None for v in a)), [])
    pts = [(tim[i], dist[i],
            hr[i] if i < len(hr) else None,
            elev[i] if i < len(elev) else None,
            cad[i] if i < len(cad) else None)
           for i in range(min(len(dist), len(tim)))
           if tim[i] is not None and dist[i] is not None]
    pts = [p for i, p in enumerate(pts) if i == 0 or p[0] > pts[i - 1][0]]   # strictly increasing t
    if len(pts) < 2:
        return [], 0.0
    t0, total_t = pts[0][0], pts[-1][0] - pts[0][0]
    if not total_t or total_t < min_s:      # §SJ: a grouped part reads under a relaxed floor —
        return [], 0.0                      # the group supplies the context this bar demands
    m_scale = 1.0 if (pts[-1][1] or 0) > 100 else 1000.0     # metres already, or km → metres
    nf = int(total_t // RD_FRAME_S)
    import bisect
    times = [p[0] for p in pts]

    def interp(t, chan):
        i = max(0, min(len(pts) - 2, bisect.bisect_right(times, t) - 1))
        a, b = pts[i], pts[i + 1]
        va, vb = a[chan], b[chan]
        if va is None or vb is None:
            return va if vb is None else vb
        if b[0] == a[0]:
            return va
        w = (t - a[0]) / (b[0] - a[0])
        return va + (vb - va) * max(0.0, min(1.0, w))

    frames, valid = [], 0
    for k in range(nf):
        ta, tb = t0 + k * RD_FRAME_S, t0 + (k + 1) * RD_FRAME_S
        da = interp(ta, 1)
        db_ = interp(tb, 1)
        ea, eb = interp(ta, 3), interp(tb, 3)
        hm = interp((ta + tb) / 2.0, 2)
        cm = interp((ta + tb) / 2.0, 4)
        f = {"pace": None, "hr": round(hm) if hm else None,
             "cad": round(cm, 1) if cm else None, "km": 0.0, "t": ta}
        if da is not None and db_ is not None:
            dd = max(0.0, (db_ - da) * m_scale)
            f["km"] = dd / 1000.0
            if dd > 0:
                pace = RD_FRAME_S / dd * 1000.0              # sec/km, raw
                if ea is not None and eb is not None and dd > 5:
                    g = max(-15.0, min(15.0, (eb - ea) / dd * 100.0))
                    cost = max(0.6, min(1.6, 1 + 0.029 * g + 0.0015 * g * g))
                    pace /= cost                             # uphill ⇒ faster flat-equivalent
                if pace < RD_PAUSE_PACE:
                    f["pace"] = pace
                    valid += 1
        frames.append(f)
    for f in frames:
        f["raw"] = f["pace"]           # pre-smooth pace: STRIDE detection reads this — the median
    #                                    smooth below would flatten a genuine 1–2-frame burst into
    #                                    its neighbour mixture and cap its peak under any honest bar
    if len(frames) >= 3:                                     # 3-point median smooth (jitter, not shape)
        sm = [f["pace"] for f in frames]
        for i in range(1, len(frames) - 1):
            trio = [p for p in (sm[i - 1], sm[i], sm[i + 1]) if p is not None]
            if frames[i]["pace"] is not None and len(trio) == 3:
                frames[i] = {**frames[i], "pace": _rd_median(trio)}
    return frames, (valid / nf if nf else 0.0)


def _rd_strides(frames, cad_pts=None):
    """Strides counted the way the owner reads the chart (his 2026-07-05 framing: 'count the
    peaks, look at the time axis'): a GLOBAL peak pass over the RAW grade-adjusted speed, not
    incremental burst state (which leaked half a real session's strides through merges and
    resets). Per frame the valley FLOOR is the rolling ±RD_STRIDE_FLOOR_WIN median — strides are
    short, so the local median sits on the easy/recovery floor. A maximal run of frames riding
    ≥ RD_STRIDE_PEAK over the floor, no wider than RD_STRIDE_MAX_S (wider = a rep, the block
    grammar owns it), countersigned by a cadence rise over the surrounding frames (GPS spikes
    leave cadence flat), counts as ONE stride — and a WIDER episode is a fused cluster whose
    internal dip-separated peaks each count (his real 5+6 session fused at full resolution).
    v7: `cad_pts` (raw (t, cadence) samples) adds the CADENCE-BURST pass — rests shorter than
    the 15s frame grid alias strides into pace blends (his real 10 counted 6), but the ~1Hz
    cadence stream keeps one distinct high-cadence run per stride; bursts are pace-corroborated
    (half the stride bar) and deduped against pace-counted centers, so pace stays primary and a
    flat-pace cadence flutter never counts. Returns {"n", "sets", "pace", "reps"}."""
    spd = [(1000.0 / f["raw"]) if f.get("raw") else None for f in frames]
    cads = [f.get("cad") for f in frames]
    n, W = len(frames), RD_STRIDE_FLOOR_WIN
    fast, floors = [False] * n, [None] * n
    for i in range(n):
        if spd[i] is None:
            continue
        loc = [s for s in spd[max(0, i - W):i + W + 1] if s is not None]
        floor = _rd_median(loc)
        floors[i] = floor
        if floor and spd[i] >= floor * (1 + RD_STRIDE_PEAK):
            fast[i] = True
    centers, ep_frames = [], []           # one center per counted stride + all counted fast frames
    i = 0
    while i < n:
        if not fast[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and fast[j + 1]:
            j += 1
        # cadence countersign at EPISODE level (GPS spikes don't come as dipped clusters)
        cref = [c for c in (cads[max(0, i - W):i] + cads[j + 1:j + W + 1]) if c]
        cpk = max((c for c in cads[i:j + 1] if c), default=None)
        if (cref and cpk) and cpk < _rd_median(cref) * (1 + RD_STRIDE_CAD):
            i = j + 1
            continue
        if (j - i + 1) * RD_FRAME_S <= RD_STRIDE_MAX_S:
            centers.append((i + j) / 2.0)
        else:
            # a FUSED cluster (strides bridged by a quick recovery): count the INTERNAL peaks —
            # local speed maxima separated by a ≥RD_STRIDE_DIP dip, the chart-read inside the blob
            maxima = [k for k in range(i, j + 1)
                      if spd[k] is not None
                      and (k == i or (spd[k - 1] or 0) <= spd[k])
                      and (k == j or spd[k] >= (spd[k + 1] or 0))]
            kept = []
            for m in maxima:
                if not kept:
                    kept.append(m)
                    continue
                between = [s for s in spd[kept[-1]:m + 1] if s is not None]
                dip_ok = between and min(between) <= min(spd[kept[-1]], spd[m]) * (1 - RD_STRIDE_DIP)
                if dip_ok:
                    kept.append(m)
                elif spd[m] > spd[kept[-1]]:
                    kept[-1] = m                             # same peak, better summit
            centers.extend(float(m) for m in kept)
        ep_frames.extend(range(i, j + 1))
        i = j + 1
    # ── the cadence-burst pass (v7) — recover strides the frame grid aliased away ──
    if cad_pts and any(cads):
        t_org = frames[0].get("t", 0.0)

        def _p25(vals):       # the RECOVERY-quartile floor: in a DENSE strides region (~50% of a
            s = sorted(vals)  # window is stride content) the rolling MEDIAN is itself stride-
            return s[len(s) // 4] if s else None   # polluted and the threshold inflates past the
        #                                            bursts it exists to find; p25 sits on recovery

        def cfloor(k):
            loc = [c for c in cads[max(0, k - W):k + W + 1] if c]
            return _p25(loc)

        runs, cur = [], None
        for t, c in cad_pts:
            k = max(0, min(n - 1, int((t - t_org) // RD_FRAME_S)))
            fl = cfloor(k)
            if fl and c >= fl * (1 + RD_STRIDE_CAD_BURST):
                cur = [t, t] if cur is None else [cur[0], t]
            elif cur:
                runs.append(cur)
                cur = None
        if cur:
            runs.append(cur)
        for a, b in runs:
            if not (RD_STRIDE_BURST_MIN_S <= b - a <= RD_STRIDE_MAX_S):
                continue          # too brief = bounce; too wide = a rep, the block grammar owns it
            fidx = max(0.0, min(n - 1.0, ((a + b) / 2.0 - t_org) / RD_FRAME_S))
            if any(abs(fidx - c0) * RD_FRAME_S <= RD_STRIDE_BURST_PAD for c0 in centers):
                continue          # the pace pass already counted this stride
            # pace corroboration at HALF the stride bar, against the recovery-quartile speed floor
            # (the median floor is blend-inflated in exactly the dense regions this pass serves).
            # Judged on the FASTEST frame the burst touches — a burst straddles two frames and the
            # rounded midpoint can land on the mostly-recovery one (the same aliasing again). The
            # blended frame under a real stride rides clearly over the recovery floor; a flat-pace
            # cadence flutter does not.
            f_lo = max(0, min(n - 1, int((a - t_org) // RD_FRAME_S)))
            f_hi = max(f_lo, min(n - 1, int((b - t_org) // RD_FRAME_S)))
            smax = max((s for s in spd[f_lo:f_hi + 1] if s is not None), default=None)
            p25f = _p25([s for s in spd[max(0, f_lo - W):f_hi + W + 1] if s is not None])
            if smax is None or not p25f or smax < p25f * (1 + RD_STRIDE_PEAK / 2):
                continue
            centers.append(fidx)
        centers.sort()
    if not centers:
        return {"n": 0, "sets": [], "pace": None, "reps": []}
    # set grouping: a gap clearly longer than the typical stride cadence starts a new set
    sets, cur_set = [], 1
    gaps = [(centers[k + 1] - centers[k]) * RD_FRAME_S for k in range(len(centers) - 1)]
    med_gap = _rd_median(gaps) if gaps else None
    for g in gaps:
        if med_gap and g > max(RD_STRIDE_SET_GAP * med_gap, 90):
            sets.append(cur_set)
            cur_set = 1
        else:
            cur_set += 1
    sets.append(cur_set)
    # strides-only pace: time over distance across the counted fast frames
    sec = len(ep_frames) * RD_FRAME_S
    km = sum(frames[k]["km"] for k in ep_frames)
    # §SQ — per-stride execution detail (computed HERE because frames aren't persisted): the peak
    # frame's raw pace, the pre-stride floor HR, the HR peak inside a +45–60s LAG window (a 15–20s
    # stride's cardiac peak lands in the recovery — HR is reported as RESPONSE, never as the effort
    # verdict), and the recovery floor before the next stride (a creeping floor = rest too short).
    reps = []
    for k, c in enumerate(centers):
        i0 = min(n - 1, max(0, int(round(c))))
        pre = [f["hr"] for f in frames[max(0, i0 - 6):max(0, i0 - 1)] if f.get("hr")]
        peak = [f["hr"] for f in frames[i0:min(n, i0 + 4)] if f.get("hr")]
        nxt = min(n - 1, max(0, int(round(centers[k + 1])))) if k + 1 < len(centers) else min(n, i0 + 10)
        rec = [f["hr"] for f in frames[min(n, i0 + 3):max(min(n, i0 + 3), nxt - 1)] if f.get("hr")]
        # the rep's pace = the FASTEST raw frame it touches (±1): a cadence-recovered stride's
        # centre frame can be mostly rest (an aliased rep once recorded 882 s/km — standing)
        pcand = [f["raw"] for f in frames[max(0, i0 - 1):min(n, i0 + 2)] if f.get("raw")]
        reps.append({"t": i0 * RD_FRAME_S,
                     "pace": round(min(pcand)) if pcand else None,
                     "hr_pre": round(_rd_median(pre)) if pre else None,
                     "hr_peak": max(peak) if peak else None,
                     "hr_rec": min(rec) if rec else None})
    return {"n": len(centers), "sets": sets, "pace": round(sec / km) if km > 0 else None,
            "reps": reps}


def _rd_blocks(frames):
    """Contrast segmentation: walk the frames keeping a running block; a pace shift beyond
    RD_CONTRAST vs the block median that HOLDS for RD_SUSTAIN_FRAMES (same direction) cuts a new
    block. A non-sustained outlier is kept out of the median (strides are counted separately by
    the _rd_strides global peak pass). Pause frames break blocks. Then a settle pass: merge
    adjacent same-pace blocks and absorb sub-minimum slivers into the nearer-pace neighbour.
    Returns blocks; each = {sec, km, pace, cad, hr, i0, i1}."""
    thr = math.log1p(RD_CONTRAST)
    raw, cur, outl = [], [], set()

    def flush():
        if cur:
            paces = [frames[j]["pace"] for j in cur if j not in outl and frames[j]["pace"]]
            if paces:
                lo, hi = cur[0], cur[-1] + 1
                hrs = [frames[j]["hr"] for j in range(lo, hi) if frames[j]["hr"]]
                cads = [frames[j]["cad"] for j in cur if j not in outl and frames[j].get("cad")]
                raw.append({"i0": lo, "i1": hi, "sec": (hi - lo) * RD_FRAME_S,
                            "km": round(sum(frames[j]["km"] for j in range(lo, hi)), 3),
                            "pace": _rd_median(paces),
                            "cad": _rd_median(cads) if cads else None,
                            "hr": round(sum(hrs) / len(hrs)) if hrs else None})
        del cur[:]
        outl.clear()

    i = 0
    while i < len(frames):
        p = frames[i]["pace"]
        if p is None:
            flush()                                          # a pause breaks the block
            i += 1
            continue
        if not cur:
            cur.append(i)
            i += 1
            continue
        med = _rd_median([frames[j]["pace"] for j in cur if j not in outl])
        dev = math.log(p / med)
        if abs(dev) > thr:
            k = 0                                            # does the shift sustain?
            while i + k < len(frames) and k < RD_SUSTAIN_FRAMES:
                pk = frames[i + k]["pace"]
                if pk is None or abs(math.log(pk / med)) <= thr or (pk > med) != (p > med):
                    break
                k += 1
            if k >= RD_SUSTAIN_FRAMES or (k >= 1 and i + k >= len(frames)):
                flush()
                continue                                     # this frame opens the next block
            outl.add(i)                                      # transient: out of the block median
            cur.append(i)
            i += 1
            continue
        cur.append(i)
        i += 1
    flush()

    def merged(a, b):
        sec = a["sec"] + b["sec"]
        return {"i0": a["i0"], "i1": b["i1"], "sec": sec,
                "km": round(a["km"] + b["km"], 3),
                "pace": (a["pace"] * a["sec"] + b["pace"] * b["sec"]) / sec,
                "hr": (round((a["hr"] * a["sec"] + b["hr"] * b["sec"]) / sec)
                       if a["hr"] and b["hr"] else a["hr"] or b["hr"])}

    blocks = raw
    for _ in range(len(raw) + 1):                            # settle to a fixed point (bounded)
        changed = False
        out = []
        for b in blocks:                                     # 1 — re-merge same-pace neighbours
            if out and abs(math.log(b["pace"] / out[-1]["pace"])) <= thr:
                out[-1] = merged(out[-1], b)
                changed = True
            else:
                out.append(b)
        blocks = out
        if len(blocks) > 1:                                  # 2 — absorb sub-minimum slivers
            j = min(range(len(blocks)), key=lambda k: blocks[k]["sec"])
            if blocks[j]["sec"] < RD_MIN_BLOCK_S:
                nb = [k for k in (j - 1, j + 1) if 0 <= k < len(blocks)]
                k = min(nb, key=lambda q: abs(math.log(blocks[q]["pace"] / blocks[j]["pace"])))
                a, b = sorted((j, k))
                blocks[a:b + 1] = [merged(blocks[a], blocks[b])]
                changed = True
        if not changed:
            break
    return blocks


def _rd_zone(pace, zones):
    """Name a block by the NEAREST plan-zone pace target in log-speed space (easy_top / marathon /
    threshold / interval — exactly the vocabulary sessions prescribe in). Slower than the easy
    target is easy by definition."""
    targets = [("easy", zones.get("easy_top")), ("marathon", zones.get("marathon")),
               ("threshold", zones.get("threshold")), ("interval", zones.get("interval"))]
    targets = [(z, p) for z, p in targets if p]
    if not targets:
        return "easy"
    easy_t = dict(targets).get("easy")
    if easy_t and pace >= easy_t:
        return "easy"
    return min(targets, key=lambda zp: abs(math.log(pace / zp[1])))[0]


def _rd_fmt_dur(sec):
    return f"{round(sec)}s" if sec < 120 else f"{round(sec / 60)}min"


def _rd_fmt_range(vals, fmt):
    lo, hi = min(vals), max(vals)
    a, b = fmt(lo), fmt(hi)
    return a if a == b else f"{a}–{b}"


def classify_structure(streams, zones, min_s=RD_MIN_RUN_S):
    """§RD entry point: raw activity streams + that-date pace zones → the detected workout.
    Returns {"v", "ok", "kind", "kind_label", "summary", "segments", "n_work", "strides",
    "confidence"} — or ok=False with a reason when the run is honestly unreadable (never force a
    label). Kinds are the PLAN vocabulary: easy / long / tempo / interval / long_mp.
    `min_s` relaxes the structure floor for a §SJ grouped PART (SJ_PART_MIN_S): a 6-min strides
    recording is readable because of the session it belongs to — never relaxed for lone runs."""
    frames, valid = _rd_frames(streams, min_s)
    if not frames:
        return {"v": STRUCT_VERSION, "ok": False, "reason": "no usable pace/distance streams"}
    if valid < 0.7:
        return {"v": STRUCT_VERSION, "ok": False,
                "reason": f"pace unreadable ({round(valid * 100)}% of frames usable)"}
    blocks = _rd_blocks(frames)
    tarr, carr = streams.get("time") or [], streams.get("cadence") or []
    cad_pts = [(tarr[k], carr[k]) for k in range(min(len(tarr), len(carr)))
               if tarr[k] is not None and carr[k] is not None]
    sinfo = _rd_strides(frames, cad_pts)     # global peak pass + v7 cadence-burst recovery
    strides = sinfo["n"]

    def stride_note():
        # "11× strides (5+6) @4:20/km" — count, the set grouping when the gaps show one, and the
        # strides-only pace (the overall pace already covers the rest of the run — his spec)
        note = f"{strides}× strides"
        if len(sinfo["sets"]) > 1:
            note += f" ({'+'.join(str(x) for x in sinfo['sets'])})"
        if sinfo.get("pace"):
            note += f" @{fmt_pace(sinfo['pace'])}/km"
        return note
    if not blocks:
        return {"v": STRUCT_VERSION, "ok": False, "reason": "no coherent pace blocks"}
    segs = [{**b, "zone": _rd_zone(b["pace"], zones)} for b in blocks]
    for s in segs:
        s["pace"] = round(s["pace"])
        s.pop("i0"), s.pop("i1")
    conf = "good" if valid >= RD_GOOD_VALID else "rough"
    total_sec = sum(s["sec"] for s in segs)
    # The run's own easy BASELINE: cluster blocks by pace (within the segmentation contrast) and
    # take the SLOWEST level that carries real time (≥ RD_BASE_MIN_SHARE of the run, or ≥10min).
    # WORK then requires BOTH a work-zone name AND ≥ RD_WORK_CONTRAST vs that baseline — structure
    # is the contrast a human sees in the pace chart; the zone grid only supplies the name.
    thr = math.log1p(RD_CONTRAST)

    def level_sec(b):
        return sum(s["sec"] for s in segs if abs(math.log(s["pace"] / b["pace"])) <= thr)

    major = [b for b in segs if level_sec(b) >= min(max(RD_BASE_MIN_SHARE * total_sec, 600),
                                                    0.5 * total_sec)]
    base_blk = max(major or segs, key=lambda b: (b["pace"], level_sec(b)))   # slowest qualifying level
    # The baseline pace is the whole LEVEL's time-over-distance, not the anchor block's own pace:
    # a 2-min recovery float can anchor the slowest qualifying level (its ±contrast neighbourhood
    # borrows the warm-up's minutes) and a float-pace baseline then hands a marathon-effort run-home
    # a >10% "work" contrast it never had vs the run's real easy pace (2026-07-14 live read: a 15min
    # @6:29 cool-down became work rep 3 of a 2-rep VO₂ session).
    base_members = [s for s in segs if abs(math.log(s["pace"] / base_blk["pace"])) <= thr]
    base_km = sum(s["km"] for s in base_members)
    baseline = (sum(s["sec"] for s in base_members) / base_km) if base_km else base_blk["pace"]
    fast_ix = [i for i, s in enumerate(segs)
               if s["zone"] in RD_WORK_ZONES
               and math.log(baseline / s["pace"]) >= math.log1p(RD_WORK_CONTRAST)]
    work_ix = [i for i in fast_ix if segs[i]["sec"] >= RD_MIN_WORK_S]
    # (fast blocks too short to be reps — e.g. strides that segmented out on their own — are
    #  simply not work; the _rd_strides peak pass already counts them from the whole-run signal)
    if len(work_ix) == 1 and segs[work_ix[0]]["sec"] < RD_MIN_TEMPO_S:
        work_ix = []                                         # a lone short surge isn't a session
    # v8 — the workout ENDS: a trailing rep whose rest gap dwarfs the session's own observed rest
    # scale is cooldown drift (terrain letting go, legs rolling home), not a session element. Needs
    # ≥3 candidates: with only one real gap there is no scale to judge against, so a plain 2-rep
    # session can never lose its second rep. Iterative, so a run-home that segmented into TWO
    # work-zone blocks sheds both.
    while len(work_ix) >= 3:
        gaps = [sum(s["sec"] for s in segs[work_ix[k] + 1:work_ix[k + 1]])
                for k in range(len(work_ix) - 1)]
        if gaps[-1] >= max(RD_TAIL_REST_MIN_S, RD_TAIL_REST_FACTOR * max(gaps[:-1])):
            work_ix.pop()
        else:
            break

    def seg_roles():
        for i, s in enumerate(segs):
            if i in work_ix:
                s["role"] = "work"
            elif work_ix and i < work_ix[0]:
                s["role"] = "warmup"
            elif work_ix and i > work_ix[-1]:
                s["role"] = "cooldown"
            elif work_ix:
                s["role"] = "float"
            else:
                s["role"] = "easy"

    seg_roles()
    p = fmt_pace                                             # sec/km → "M:SS"
    if not work_ix:                                          # no contrast = one sustained effort
        total_km = sum(s["km"] for s in segs)
        # honest overall pace = time over distance — a time-weighted mean of BLOCK paces overweights
        # slow walking blocks (the strides session read @10:46 while the tile said 7:58)
        pace_all = round(total_sec / total_km) if total_km else segs[0]["pace"]
        z_all = _rd_zone(pace_all, zones)
        # a STRIDES SESSION — checked BEFORE the wall-to-wall-hard return (the 2026-07-20 first
        # live 1+1: 6 counted strides lost to a "threshold" blend verdict): enough strides over a
        # base too slow to be easy RUNNING (walking/standing recovery), OR a SHORT stride-dense
        # recording (a §SJ part: the blocks are stride-smeared, the peak count is the truth).
        # Strides on a genuine easy run stay "Easy run · N× strides".
        if strides >= RD_STRIDES_SESSION_MIN and \
                ((zones.get("easy") and baseline > zones["easy"]) or total_sec <= RD_STRIDES_SHORT_S):
            return {"v": STRUCT_VERSION, "ok": True, "kind": "strides", "kind_label": "Strides",
                    "summary": f"{_rd_fmt_dur(total_sec)} @{p(pace_all)}/km · {stride_note()}",
                    "segments": segs, "n_work": 0, "strides": strides,
                    "stride_sets": sinfo["sets"], "stride_pace": sinfo.get("pace"),
                    "stride_reps": sinfo.get("reps") or [], "confidence": conf}
        if z_all in ("threshold", "interval"):               # wall-to-wall HARD (race / straight tempo)
            return {"v": STRUCT_VERSION, "ok": True, "kind": "tempo", "kind_label": "Sustained effort",
                    "summary": f"{_rd_fmt_dur(total_sec)} @{p(pace_all)}/km {z_all}, no easy bracket",
                    "segments": segs, "n_work": 0, "strides": strides,
                    "stride_sets": sinfo["sets"], "stride_pace": sinfo.get("pace"),
                    "stride_reps": sinfo.get("reps") or [], "confidence": conf}
        # aerobic throughout — marathon-zone drift stays "easy" here BY DESIGN: whether an easy day
        # ran too hot is the effort monitor's verdict, not a structure claim
        kind = "long" if total_sec >= RD_LONG_MIN * 60 else "easy"
        summary = f"{_rd_fmt_dur(total_sec)} @{p(pace_all)}/km" + \
                  (f" · {stride_note()}" if strides else "")
        return {"v": STRUCT_VERSION, "ok": True, "kind": kind,
                "kind_label": "Long run" if kind == "long" else "Easy run",
                "summary": summary, "segments": segs, "n_work": 0, "strides": strides,
                "stride_sets": sinfo["sets"], "stride_pace": sinfo.get("pace"),
                "stride_reps": sinfo.get("reps") or [], "confidence": conf}

    works = [segs[i] for i in work_ix]
    wu = [s for s in segs if s["role"] == "warmup"]
    cd = [s for s in segs if s["role"] == "cooldown"]
    floats = [s for s in segs if s["role"] == "float"]
    parts = []
    if wu:
        parts.append(f"{_rd_fmt_dur(sum(s['sec'] for s in wu))} wu "
                     f"@{_rd_fmt_range([s['pace'] for s in wu], p)}")
    modal_zone = max(set(s["zone"] for s in works),
                     key=lambda z: sum(s["sec"] for s in works if s["zone"] == z))
    if len(works) >= 2:                                      # alternating reps ⇒ intervals
        kind, kind_label = "interval", "Intervals"
        rep = f"{len(works)}× {_rd_fmt_range([s['sec'] for s in works], _rd_fmt_dur)} " \
              f"@{_rd_fmt_range([s['pace'] for s in works], p)} {modal_zone}"
        if floats:
            rep += f" w/ {_rd_fmt_range([s['sec'] for s in floats], _rd_fmt_dur)} floats"
        parts.append(rep)
    else:
        w = works[0]
        easy_before = sum(s["sec"] for s in segs[:work_ix[0]])
        if w["zone"] == "marathon" and easy_before >= RD_MP_BASE_MIN * 60 and \
                sum(s["sec"] for s in segs[work_ix[0] + 1:]) <= max(900, 0.2 * total_sec):
            kind, kind_label = "long_mp", "Long run + MP finish"
            for s in segs[:work_ix[0]]:
                s["role"] = "easy_base"                      # the base IS the run, not a warm-up
            parts = [f"{_rd_fmt_dur(easy_before)} easy "
                     f"@{_rd_fmt_range([s['pace'] for s in segs[:work_ix[0]]], p)}",
                     f"{_rd_fmt_dur(w['sec'])} @{p(w['pace'])} MP finish"]
        else:
            kind, kind_label = "tempo", "Tempo"
            parts.append(f"{_rd_fmt_dur(w['sec'])} @{p(w['pace'])} {w['zone']}")
    if cd:
        parts.append(f"{_rd_fmt_dur(sum(s['sec'] for s in cd))} cd "
                     f"@{_rd_fmt_range([s['pace'] for s in cd], p)}")
    if strides:
        parts.append(stride_note())
    return {"v": STRUCT_VERSION, "ok": True, "kind": kind, "kind_label": kind_label,
            "summary": " · ".join(parts), "segments": segs, "n_work": len(works),
            "strides": strides, "stride_sets": sinfo["sets"], "stride_pace": sinfo.get("pace"),
            "stride_reps": sinfo.get("reps") or [], "confidence": conf}


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


def _structure_cached(db, aid, date_iso=None, fetch=True, min_s=RD_MIN_RUN_S):
    """§RD — the current-version detected structure for an activity: from structcache, else (when
    `fetch` allows) classified from freshly-pulled streams + stored. Mirrors _profile_cached:
    re-classifies on a VERSION mismatch, and on a fetch failure returns (stale_or_None, err).
    `fetch=False` = cached-only, for the public container (tokenless) and the effort monitor's
    bulk read (a panel load must never fan out into stream fetches). Tolerates a DB without the
    structcache table (minimal det fixtures) — absent reads as unclassified.
    §SJ: `min_s` < RD_MIN_RUN_S (a grouped part) also RETRIES a cached refusal — a short part
    refused as a lone run may be readable under the group's relaxed floor."""
    try:
        row = db.execute("SELECT structure FROM structcache WHERE activity_id=?", (aid,)).fetchone()
    except sqlite3.OperationalError:
        return None, None
    cached = json.loads(row["structure"]) if row else None
    if cached and cached.get("v") == STRUCT_VERSION:
        if cached.get("ok") or min_s >= RD_MIN_RUN_S or not fetch:
            return cached, None
    if not fetch:
        return cached, None
    try:
        det = mcp_call("get_activity_details", {"activity_id": int(aid)})
        streams = (det.get("activity", det)).get("streams") or {}
        st = classify_structure(streams, _zones_asof(db, date_iso), min_s=min_s)
    except (RunalyzeError, requests.RequestException, KeyError, ValueError) as e:
        return cached, e
    db.execute("INSERT OR REPLACE INTO structcache (activity_id, structure, cached_at) "
               "VALUES (?,?,?)", (aid, json.dumps(st), _now_iso()))
    db.commit()
    return st, None


def classify_recent(db, days=14, cap=12):
    """§RD sync hook — classify any still-unclassified recent run (idempotent, so a failed attempt
    self-heals next sync). Recent-window only: history stays lazy (classified on first view), per
    the from-now-on rollout. Best-effort by design — a classification failure must never fail a
    sync. Returns the number classified."""
    from datetime import timedelta
    since = (datetime.now().date() - timedelta(days=days)).isoformat()
    rows = db.execute(
        "SELECT a.id, a.date, a.date_time, a.distance, a.duration, a.elapsed_time, "
        "s.activity_id AS done FROM activities a LEFT JOIN structcache s ON s.activity_id=a.id "
        "WHERE " + RUN_FAMILY_SQL.replace("sport", "a.sport") + " AND a.date>=? "
        "ORDER BY a.date DESC", (since,)).fetchall()
    # §SJ — eligibility is per SESSION, not per recording: a lone run still needs ≥2 km, but a
    # short PART of a 1+1 group classifies (relaxed floor) because its group carries the session.
    todo = []
    for grp in _session_groups(rows):
        gkm = sum((p["distance"] or 0) for p in grp)
        for p in grp:
            if p["done"] is None and ((p["distance"] or 0) >= 2 or (len(grp) > 1 and gkm >= 2)):
                todo.append((p, SJ_PART_MIN_S if len(grp) > 1 else RD_MIN_RUN_S))
    todo.sort(key=lambda t: t[0]["date"], reverse=True)
    n = 0
    for i, (r, ms) in enumerate(todo[:cap]):
        if i:
            time.sleep(PAGE_DELAY)                           # WAF politeness between stream pulls
        st, err = _structure_cached(db, r["id"], date_iso=r["date"], min_s=ms)
        if st is not None and not err:
            n += 1
    return n


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
    created_at TEXT,
    outcome    TEXT,                 -- §RL: JSON result once resolved (finished/dnf/unrun/unverified)
    resolved_at TEXT                 -- §RL: when resolve_passed_races settled it
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

-- §RD — cached detected workout structure per activity (versioned JSON from classify_structure).
-- Written at sync for new runs, lazily on first view for older ones; the effort monitor reads it
-- cached-only (never fetches mid-panel).
CREATE TABLE IF NOT EXISTS structcache (
    activity_id INTEGER PRIMARY KEY,
    structure   TEXT,
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
    active       INTEGER DEFAULT 1, -- 0 once superseded/cleared
    medical      INTEGER DEFAULT 0, -- §H3 dominant medical hold: open-ended load + survives routine applies
    cleared_at   TEXT               -- §PRO3: date the hold stopped being in force (regime clean-window anchor)
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

-- §AV — availability: away days the plan must lay AROUND (travel, life). Deterministic UI input
-- (no LLM in the path — must work on a keyless install), date-granular, inclusive range. The
-- layout derives from these rows (constraints-not-edits: the plan stays a pure function of
-- (today, shape, objectives, adjustments, availability)); past rows are naturally inert.
-- PRIVACY (H7-class): these rows and every derived marker are PRIVATE-ONLY — away dates on the
-- public box are an empty-house broadcast. The public plan shows only the resulting laid days.
CREATE TABLE IF NOT EXISTS availability (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT,
    date_from  TEXT,               -- YYYY-MM-DD inclusive
    date_to    TEXT,               -- YYYY-MM-DD inclusive
    note       TEXT,               -- optional, owner's words ("flight", "family week")
    active     INTEGER DEFAULT 1   -- 0 once deleted
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

# A queryable per-run analysis table — one row per (non-dropped) run, every metric we capture, with
# the daily shape snapshot + HRV/weight joined on date. It's a VIEW, not a materialised table: the
# raw activity JSON + the owned shape_snapshots are already durable, so a join layer "re-runs itself"
# as data accrues and can never drift from them (the projector-snapshot-seam lesson). The exclusion
# clause faithfully mirrors dropped_ids(db) = find_duplicates ∪ manual_ignores, in pure SQL, so this
# table can't silently disagree with any other surface in the app. DROP+CREATE on every init so the
# column list tracks the code (a view has no data, so there's no cost). hr_cost = hr/speed is a
# known-nonlinear convenience column — the raw hr + speed_kmh sit beside it for a better metric later.
RUN_METRICS_VIEW = """
DROP VIEW IF EXISTS run_metrics;
CREATE VIEW run_metrics AS
SELECT
  a.id, a.date,
  json_extract(a.raw,'$.recurring_route.id')   AS route_id,
  a.distance                                    AS km,
  a.duration                                    AS dur_s,
  a.hr_avg                                       AS hr,
  a.hr_max                                       AS hr_max,
  a.trimp                                        AS trimp,
  a.training_effect                              AS te,
  json_extract(a.raw,'$.temperature')           AS temp_c,
  json_extract(a.raw,'$.humidity')              AS humidity,
  json_extract(a.raw,'$.uv_index')              AS uv,
  json_extract(a.raw,'$.wind_speed')            AS wind,
  json_extract(a.raw,'$.elevation_up')          AS elev_up,
  json_extract(a.raw,'$.percentage_hilly')      AS hilly_pct,
  json_extract(a.raw,'$.x_pace')                AS speed_kmh,
  json_extract(a.raw,'$.gap')                   AS gap_kmh,
  json_extract(a.raw,'$.cadence')               AS cadence,
  json_extract(a.raw,'$.stride_length')         AS stride,
  json_extract(a.raw,'$.aerobic_decoupling_pace') AS decoupling,
  json_extract(a.raw,'$.vo2max')                AS run_vo2max,
  json_extract(a.raw,'$.subjective_feeling')    AS feel,
  json_extract(a.raw,'$.is_night')              AS is_night,
  ROUND(a.hr_avg * 1.0 / NULLIF(json_extract(a.raw,'$.x_pace'),0), 2) AS hr_cost,
  -- GAP-normalised cost: HR per unit GRADE-ADJUSTED speed, so a hilly route doesn't inflate the cost
  -- (raw hr_cost correlates +0.26 with elevation — a terrain confound this removes).
  ROUND(a.hr_avg * 1.0 / NULLIF(json_extract(a.raw,'$.gap'),0), 2) AS hr_cost_gap,
  -- daily shape snapshot, joined on date. Named *_snapshot (not *_start): the snapshot is the day's
  -- capture and leads the activity frontier by a day (the documented seam), so it's not a guaranteed
  -- pre-run reading — especially for his evening runs.
  s.fitness            AS ctl_snapshot,
  s.fatigue            AS atl_snapshot,
  s.acwr               AS acwr_snapshot,
  s.effective_vo2max   AS evo2_snapshot,
  s.hrv_baseline       AS hrv_baseline,
  hv.value             AS hrv_today,
  wt.value             AS weight_kg
FROM activities a
LEFT JOIN shape_snapshots s ON s.snapshot_date = a.date
LEFT JOIN health_markers hv ON hv.marker = 'hrv'    AND hv.date = a.date
LEFT JOIN health_markers wt ON wt.marker = 'weight' AND wt.date = a.date
WHERE LOWER(a.sport) LIKE '%run%'
  AND a.id NOT IN (SELECT id FROM ignored_activities)
  -- duplicate drop, mirroring find_duplicates: keep the lowest id per
  -- (date_time, distance@2dp, sport) group; never collapse blank-timestamp rows (it skips them).
  AND (COALESCE(a.date_time,'') = ''
       OR a.id = (SELECT MIN(b.id) FROM activities b
                  WHERE b.date_time = a.date_time
                    AND ROUND(COALESCE(b.distance,0),2) = ROUND(COALESCE(a.distance,0),2)
                    AND COALESCE(b.sport,'') = COALESCE(a.sport,'')));
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
    # Watch-recorded daily metrics, synced from Runalyze (no fixed clinical band — they're individual;
    # the trend vs your OWN history is the signal). HRV = sleeping RMSSD.
    "resting_hr":        {"label": "Resting HR", "unit": "bpm", "ref": [None, None], "good": "low"},
    "hrv":               {"label": "HRV (sleeping RMSSD)", "unit": "ms", "ref": [None, None], "good": "high"},
    # Sleep, synced from Runalyze's per-night summary. No clinical band — the trend vs your own history
    # is the signal; displayed alongside HRV/RHR, never a plan input. night_hr = overnight lowest HR
    # (a de-facto resting-HR that, unlike resting_hr, continues through the Suunto era).
    "sleep_duration":    {"label": "Sleep", "unit": "h", "ref": [None, None], "good": "high"},
    "sleep_quality":     {"label": "Sleep quality", "unit": "/10", "ref": [None, None], "good": "high"},
    "sleep_deep":        {"label": "Deep sleep", "unit": "min", "ref": [None, None], "good": "high"},
    "sleep_rem":         {"label": "REM sleep", "unit": "min", "ref": [None, None], "good": "high"},
    "night_hr":          {"label": "Overnight low HR", "unit": "bpm", "ref": [None, None], "good": "low"},
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
    db.executescript(RUN_METRICS_VIEW)   # the queryable per-run analysis table (DROP+CREATE, tracks code)
    # §H3 migration: add the dominant-medical-track column to a pre-existing DB (idempotent) and
    # backfill it from the directive JSON, so a hold saved by the old code is recognised as medical
    # (dominant + open-ended) after the upgrade — not silently downgraded to a window-clamped ease.
    cols = {r["name"] for r in db.execute("PRAGMA table_info(adjustments)").fetchall()}
    if "medical" not in cols:
        db.execute("ALTER TABLE adjustments ADD COLUMN medical INTEGER DEFAULT 0")
        for row in db.execute("SELECT id, directive FROM adjustments").fetchall():
            try:
                if json.loads(row["directive"]).get("medical_flag"):
                    db.execute("UPDATE adjustments SET medical=1 WHERE id=?", (row["id"],))
            except (ValueError, TypeError):
                continue
    # §PRO3 migration: `cleared_at` — the date a hold actually stopped being in force, so the regime
    # gate's clean-window measures recency from when a (possibly long) medical hold ENDED, not from its
    # nominal `applies_until` (≤ raise+27d) which can lapse while the hold is still active.
    if "cleared_at" not in cols:
        db.execute("ALTER TABLE adjustments ADD COLUMN cleared_at TEXT")
    # §RL migration: race-lifecycle columns — a resolved race carries its outcome (JSON: finished
    # time / dnf km / goal comparison) and when the resolver settled it.
    ocols = {r["name"] for r in db.execute("PRAGMA table_info(objectives)").fetchall()}
    if "outcome" not in ocols:
        db.execute("ALTER TABLE objectives ADD COLUMN outcome TEXT")
    if "resolved_at" not in ocols:
        db.execute("ALTER TABLE objectives ADD COLUMN resolved_at TEXT")
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


# §TZ — ONE CLOCK. "Today" is a CALENDAR DAY, and the engine's day must be the ATHLETE'S day.
# The scheduler was the only thing that knew that: it reads `SYNC_TZ` and is tz-aware, so the nightly
# job fires at the right wall-clock hour. Everything ELSE asks the process for the date — 58 naive
# `datetime.now()` / `date.today()` sites, from `today` in the plan generator down to the key
# `snapshot_shape` writes rows under — and the containers run UTC. So between 00:00 and 02:00
# Luxembourg time the whole engine believes it is YESTERDAY, while activity rows carry Runalyze's
# LOCAL date. Two clocks in one database: the §PRO20 defect class. MEASURED on his DB: 22 of 53
# shape snapshots (42%) were captured at 22:00–24:00Z — i.e. after local midnight — and filed under
# the previous day, and 3 of 79 plans were built for the wrong day outright.
#
# ⚠⚠ THE OBVIOUS FIX — `TZ=${SH_TZ:-UTC}` in docker-compose.yml — IS INERT IN THIS IMAGE, and would
# have been inert SILENTLY, exactly the way the missing SH_SYNC_AT pass-through was. `time.tzset()`
# reads glibc's database at /usr/share/zoneinfo, which python:3.12-slim does not ship (requirements.txt
# says so in as many words); the `tzdata` PyPI package we depend on serves Python's `zoneinfo` only.
# Given an unloadable name glibc does not fail — it parses "Europe/Luxembourg" as a POSIX abbreviation
# with a ZERO offset and runs on UTC, reporting tzname ('Europe', 'Europe'). Verified both ways here.
# So: point TZDIR at the tzdata package when the system database is absent, and then PROVE the change
# took by comparing the naive clock against the tz-aware one. A timezone fix that cannot say whether
# it worked is the same failure one layer up.
PROCESS_TZ = None        # the zone this process's NAIVE datetimes are on — set only once proven
PROCESS_TZ_NOTE = None   # why it is not SH_TZ, when it is not (surfaced by det/one-clock)


def _glibc_tzdir(system_has_db=None):
    """Where glibc's tzset() must be pointed to find the IANA database, or None to leave TZDIR as it
    is. python:3.12-slim ships NO /usr/share/zoneinfo (requirements.txt says so where it declares the
    `tzdata` wheel), and that wheel serves Python's `zoneinfo` only — so in the container the wheel's
    own database is the only thing tzset() can read, and without this the whole timezone fix is inert
    there. `system_has_db` is injectable so the CONTAINER's case can be constructed on a host that has
    the system database and would otherwise mask this limb entirely (it did: the first revert test
    passed here and would have failed on his NAS)."""
    if system_has_db is None:
        system_has_db = os.path.isdir("/usr/share/zoneinfo")
    if os.environ.get("TZDIR") or system_has_db:
        return None                      # already discoverable — never override an explicit TZDIR
    try:
        import tzdata
        d = os.path.join(os.path.dirname(tzdata.__file__), "zoneinfo")
        return d if os.path.isdir(d) else None
    except Exception:
        return None


def _process_tz_for(value, source):
    """The zone the PROCESS clock should be on for a resolved tz setting — None = leave the host clock
    alone. Extracted so the rule that actually matters is testable on its own rather than implied by
    an `if` at the call site: an UNCONFIGURED timezone must not move anything. The spec's default is
    "UTC", and applying that would drag `today` onto UTC on every host whose own clock was already the
    athlete's — a laptop, a NAS set to local time — which is this very defect pointed backwards."""
    return None if source == "default" else ((value or "").strip() or None)


def _apply_process_tz(tzname):
    """Put this process's naive clock on `tzname` and return it, or None (leaving the clock alone).
    Idempotent, and safe to call again when the setting changes. Never raises: a timezone that cannot
    be applied must degrade to the previous behaviour loudly, never take the app down."""
    global PROCESS_TZ, PROCESS_TZ_NOTE
    name = (tzname or "").strip()
    if not name:
        return PROCESS_TZ            # nothing requested ⇒ nothing changed, and say so truthfully:
        #                              clobbering PROCESS_TZ here would report a clock we never moved
    PROCESS_TZ_NOTE = None
    try:
        zone = ZoneInfo(name)
    except Exception:
        PROCESS_TZ, PROCESS_TZ_NOTE = None, f"{name!r} is not a resolvable IANA zone"
        print(f"[tz] {PROCESS_TZ_NOTE} — the engine's 'today' stays on the container clock")
        return None
    if not hasattr(time, "tzset"):                    # non-POSIX host (Windows): zoneinfo works, tzset doesn't
        PROCESS_TZ, PROCESS_TZ_NOTE = None, "time.tzset() is unavailable on this platform"
        print(f"[tz] {PROCESS_TZ_NOTE} — the engine's 'today' stays on the container clock")
        return None
    _tzdir = _glibc_tzdir()
    if _tzdir:
        os.environ["TZDIR"] = _tzdir
    os.environ["TZ"] = name
    time.tzset()
    # PROVE IT. glibc silently falls back to UTC for a name it cannot load, so the only honest test is
    # whether the naive clock now agrees with the tz-aware one. Both reads are microseconds apart and
    # every real UTC offset is ≥ 15 minutes, so 5 minutes is a wide, unambiguous margin.
    if abs((datetime.now() - datetime.now(zone).replace(tzinfo=None)).total_seconds()) > 300:
        PROCESS_TZ, PROCESS_TZ_NOTE = None, (
            f"tzset() would not load {name!r} (no IANA database on this host) — naive clock left on "
            f"{time.tzname[0]}")
        print(f"[tz] {PROCESS_TZ_NOTE}")
        return None
    PROCESS_TZ = name
    return name


_apply_process_tz(os.environ.get("SH_TZ", ""))   # bootstrap from env; the settings store re-applies
#                                                  the stored value once the DB is open (see
#                                                  apply_settings_overrides), which is authoritative


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
    if key == "manual_lthr" and value.strip():
        v = value.strip()
        if not v.isdigit() or not (MANUAL_LTHR_RANGE[0] <= int(v) <= MANUAL_LTHR_RANGE[1]):
            return False, (f"a whole number {MANUAL_LTHR_RANGE[0]}–{MANUAL_LTHR_RANGE[1]} bpm "
                           "(or empty to derive from your runs)")
    if key == "athlete_age" and value.strip():
        v = value.strip()
        if not v.isdigit() or not (10 <= int(v) <= 100):
            return False, "a whole number of years, 10–100 (or empty for no prior)"
    return True, None


def apply_settings_overrides(db):
    """Resolve the effective (meta → env → default) values and publish them as ONE new config
    snapshot (TECH-4) — the read-sites take `config()`.
    Called once at startup and after every save. Single-process deployment (one waitress process, many
    threads sharing these globals), so a save is visible to every request thread at once. The scheduler
    thread reads the zone off the config live, but only re-arms its sleep on the NEXT cycle — so a tz
    change lands on the next scheduled sync (as the help text says), not the one already counting
    down. TECH-4: the values are resolved first and published in ONE swap, so no request thread can
    catch this half-applied."""
    tzval, tzsrc = _resolve_setting(db, SETTINGS_BY_KEY["tz"])
    tzname = tzval.strip() or "UTC"
    try:
        tz = ZoneInfo(tzname)
    except Exception:   # validated on save; a stored zone can still be absent from this host's tzdata
        tz = config().sync_tz
        print(f"[settings] ignoring unresolvable tz {tzname!r}; keeping {tz.key}")
    # TECH-4 — every value is resolved FIRST, then published in ONE swap: a request thread reading
    # the config while a save runs sees the whole old snapshot or the whole new one, never the new
    # house URL beside the old house name.
    _config_swap(
        athlete_context=_resolve_setting(db, SETTINGS_BY_KEY["athlete_context"])[0].strip(),
        house_url=_resolve_setting(db, SETTINGS_BY_KEY["house_url"])[0].strip(),
        house_name=_resolve_setting(db, SETTINGS_BY_KEY["house_name"])[0].strip(),
        private_url=_resolve_setting(db, SETTINGS_BY_KEY["private_url"])[0].strip(),
        weather_cities=_parse_weather_cities(
            _resolve_setting(db, SETTINGS_BY_KEY["weather_cities"])[0]),
        sync_tz=tz,
    )
    with _weather_lock:            # cities may have changed → drop the cached bundle so the next
        _weather_cache["at"] = 0.0  # /api/weather refetches instead of serving up-to-30-min-stale cities
    # §TZ — the SAME zone owns the engine's calendar day, not just the nightly job's wall clock. This
    # is the authoritative apply: the import-time bootstrap only sees the env var, and the stored
    # setting overrides it. Applying it here also means a tz changed in the Settings window moves
    # "today" for the next request, not only the next scheduled sync.
    # ⚠ ONLY WHEN IT WAS ACTUALLY CHOSEN. The spec's default is "UTC", and forcing the process onto it
    # would move `today` on every host whose own clock was ALREADY the athlete's — a developer laptop,
    # a NAS set to local time — which is the same defect pointed the other way, and not byte-identical.
    # An unconfigured timezone means "keep the host clock", exactly as before this existed. The
    # scheduler's UTC default is unchanged and stays where it was: it needs a concrete zone to arm a
    # wall-clock alarm; the calendar day does not.
    _apply_process_tz(_process_tz_for(tzval, tzsrc))


def _stamp_manual_lthr(db, val):
    """Stamp the manual-LTHR entry date — its confidence DECAYS with age (guardrail #2: LTHR moves
    with fitness). Only a CHANGED value re-stamps: re-saving the same number doesn't re-freshen it
    (re-test, don't re-type)."""
    if val.strip() and val.strip() != (get_meta(db, "set:manual_lthr") or "").strip():
        set_meta(db, "manual_lthr_set_on", datetime.now().date().isoformat())


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
        if key == "manual_lthr":
            _stamp_manual_lthr(db, val)
        set_meta(db, "set:" + key, val)
    db.commit()
    apply_settings_overrides(db)
    return True, {}


# ── Secrets store (private-only; NEVER the shared ./data DB) ───────────────────
# The Runalyze token + Claude API key. Unlike SETTINGS_SPEC these are SECRETS, so they live in a
# SEPARATE store (SH_SECRETS_DB) the deploy mounts ONLY to the private container — the public
# read-only container shares ./data and would otherwise READ them (the same leak class as §H7). A
# self-hoster sets them in the private Settings window (no .env edit, no restart) and they apply live.
# WRITE-ONLY at the API: status (configured + provenance) is returned, never the value back. In
# READONLY the store is never touched — even a mis-mounted file is ignored on the public box.
SECRETS_DB = Path(os.environ.get("SH_SECRETS_DB", "secrets.db"))

SECRET_SPEC = [
    {"key": "runalyze_token", "env": "RUNALYZE_TOKEN", "label": "Runalyze API token",
     "help": "From Runalyze → Settings → Personal API. Required to sync your training data."},
    {"key": "anthropic_api_key", "env": "ANTHROPIC_API_KEY", "label": "Claude API key",
     "help": "Optional — turns on AI plan explanations and natural-language adjustments. Without it "
             "the deterministic engine still does all the planning and safety clamping."},
    # Suunto Cloud API (partner program) — the three app credentials from apizone.suunto.com. All
    # optional: without them the watch push is simply off. The user OAuth tokens are NOT here —
    # they're stored internally (see SUUNTO_TOKENS_KEY) after the one-time Connect authorization.
    {"key": "suunto_client_id", "env": "SUUNTO_CLIENT_ID", "label": "Suunto app client ID",
     "help": "From apizone.suunto.com → your OAuth app (auto-generated client id). Optional — enables "
             "pushing planned sessions to a Suunto watch as SuuntoPlus Guides."},
    {"key": "suunto_client_secret", "env": "SUUNTO_CLIENT_SECRET", "label": "Suunto app client secret",
     "help": "The client secret you set on the same OAuth app in apizone.suunto.com."},
    {"key": "suunto_subscription_key", "env": "SUUNTO_SUBSCRIPTION_KEY", "label": "Suunto subscription key",
     "help": "API Zone → your profile → subscriptions (primary or secondary key both work)."},
]
SECRET_BY_KEY = {s["key"]: s for s in SECRET_SPEC}


def _secrets_conn():
    """Open (creating if needed) the private-only secrets store. Callers guarantee not-READONLY."""
    conn = sqlite3.connect(SECRETS_DB, timeout=15)
    try:                          # 0.27.0 — live credentials: owner-only on disk. Best-effort: an odd
        os.chmod(SECRETS_DB, 0o600)   # volume/Windows may refuse, and the store still works.
    except Exception:
        pass
    conn.execute("CREATE TABLE IF NOT EXISTS secret (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def _stored_secret(key):
    """The window-set secret value, or None. ALWAYS None in READONLY — the public container must never
    read a secret even if the store is somehow present beside it."""
    if READONLY:
        return None
    try:
        conn = _secrets_conn()
        row = conn.execute("SELECT value FROM secret WHERE key=?", (key,)).fetchone()
        conn.close()
        return row[0] if row and row[0] else None
    except Exception as e:
        print(f"[secrets] read {key} failed: {e}")
        return None


def _resolve_secret(spec):
    """Effective (value, source) for a secret: a window-set value wins, else the env var, else none."""
    v = _stored_secret(spec["key"])
    if v:
        return v, "saved"
    env = os.environ.get(spec["env"], "")
    if env:
        return env, "env"
    return "", "none"


def secret_status():
    """GET payload: per-secret configured flag + provenance ONLY — never the value itself."""
    out = []
    for s in SECRET_SPEC:
        value, source = _resolve_secret(s)
        out.append({"key": s["key"], "label": s["label"], "help": s["help"],
                    "configured": bool(value), "source": source})
    return out


def apply_secret_overrides():
    """Resolve the effective (stored → env) secrets and publish them as ONE new config snapshot
    (TECH-4). The generation it bumps is what makes the cached LLM client AND the cached REST session
    rebuild, so a key change takes effect live on both. Called at startup and after each save. No-op
    in READONLY — the public container keeps its empty env and never holds a secret."""
    if READONLY:
        return
    # TECH-4 — one swap for both secrets, and the generation it bumps is what makes the cached HTTP
    # session and LLM client rebuild themselves: no explicit cache-busting to forget. (The old code
    # reset `_anthropic_client` by hand and never reset `_session`, so a new Runalyze token reached
    # MCP — which reads it per call — but not REST, which had baked it into the session headers.)
    _config_swap(runalyze_token=_resolve_secret(SECRET_BY_KEY["runalyze_token"])[0],
                 anthropic_api_key=_resolve_secret(SECRET_BY_KEY["anthropic_api_key"])[0])


def save_secret(key, value):
    """Set (or clear, when blank) one secret in the private store, then apply live. An empty value =
    clear → fall back to env. Returns (ok, error); NEVER echoes the value. Refused in READONLY."""
    if READONLY:
        return False, "not available on the public instance"
    if key not in SECRET_BY_KEY:
        return False, "unknown key"
    value = "" if value is None else str(value).strip()
    try:
        conn = _secrets_conn()
        if value:
            conn.execute("INSERT INTO secret(key,value) VALUES(?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        else:
            conn.execute("DELETE FROM secret WHERE key=?", (key,))   # clear → env fallback
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[secrets] write {key} failed: {e}")
        return False, "could not save — check the server log"
    apply_secret_overrides()
    if key == "runalyze_token":
        start_scheduler()   # a freshly-set token enables the nightly sync (idempotent; no-op if on)
    return True, None


def validate_secret(key):
    """Live validity probe for one secret — lets the Settings window distinguish 'in use & valid' from
    'set but the provider rejected it' and 'not set'. Returns 'valid' | 'invalid' | 'unset' | 'unknown'
    ('unknown' = a network/transient error we can't pin on the key). Cheap: a single authenticated GET
    with NO generation cost — Runalyze `statistics/current`, Anthropic `GET /v1/models` (key check, not a
    completion). Always 'unset' in READONLY — the public box never holds a secret to test."""
    if READONLY or key not in SECRET_BY_KEY:
        return "unset"
    value = _resolve_secret(SECRET_BY_KEY[key])[0]
    if not value:
        return "unset"
    try:
        if key == "runalyze_token":
            r = requests.get(f"{RUNALYZE_BASE}/statistics/current",
                             headers={"token": value, "Accept": "application/json",
                                      "User-Agent": USER_AGENT}, timeout=8)
            if r.status_code == 200:
                return "valid"
            return "invalid" if r.status_code in (401, 403) else "unknown"
        if key == "anthropic_api_key":
            import anthropic
            try:
                # models.list() is a plain GET — validates the key, bills no tokens. max_retries=0 so a
                # bad key fails fast instead of backing off.
                anthropic.Anthropic(api_key=value, timeout=8.0, max_retries=0).models.list()
                return "valid"
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
                return "invalid"
            except Exception:
                return "unknown"
        if key.startswith("suunto_"):
            # The app credentials only prove themselves in the OAuth dance / an authorised call, so:
            # once CONNECTED we probe the guides list (exercises subscription key + access token);
            # before that the honest answer is "unknown" (badge shows plain "configured").
            if key == "suunto_subscription_key" and _suunto_tokens():
                tok = suunto_access_token()
                if not tok:
                    return "unknown"
                r = requests.get(f"{SUUNTO_API_BASE}/v2/guides/items",
                                 headers=_suunto_headers(tok, value), timeout=8)
                if r.status_code == 200:
                    return "valid"
                return "invalid" if r.status_code in (401, 403) else "unknown"
            return "unknown"
    except Exception as e:
        print(f"[secrets] validate {key} failed: {e}")
        return "unknown"
    return "unknown"


# ── Suunto Cloud API — SuuntoPlus Guides push (§SG) ─────────────────────────
# Partner-program integration (approved 2026-07-10): the plan's next few days are converted to
# SuuntoPlus Guides and pushed to the owner's watch, so each prescribed session shows its steps,
# pace band, and HR band ON the wrist during the run. Three layers, cleanly separable:
#   1. OAuth2 (authorization-code + refresh) — one-time "Connect Suunto" in Settings; the user
#      token pair lives in the PRIVATE secrets store under an internal key (never in the UI spec).
#   2. session_to_guide() — a PURE converter: plan session dict → guide.zip bytes (det-testable).
#   3. push_guides() — idempotent upload of the next window via externalId (update, never duplicate).
SUUNTO_OAUTH_BASE = "https://cloudapi-oauth.suunto.com"
SUUNTO_API_BASE = "https://cloudapi.suunto.com"
SUUNTO_TOKENS_KEY = "suunto_oauth_tokens"   # internal secrets-store row (JSON); not in SECRET_SPEC
SUUNTO_PUSH_DAYS = int(os.environ.get("SH_SUUNTO_PUSH_DAYS", "7"))  # nightly horizon (today + N-1)
_suunto_oauth_state = {}                    # state nonce → issue time (single-user; CSRF guard)


def _suunto_conf():
    """Effective app credentials (stored → env), or '' where unset."""
    return {k: _resolve_secret(SECRET_BY_KEY[k])[0]
            for k in ("suunto_client_id", "suunto_client_secret", "suunto_subscription_key")}


def _suunto_tokens():
    """The stored user token pair {access_token, refresh_token, expires_at, user} or None."""
    raw = _stored_secret(SUUNTO_TOKENS_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _save_suunto_tokens(tok):
    """Persist (or clear, when None) the user token pair. Internal key — save_secret() refuses
    non-SECRET_SPEC keys on purpose, so this writes the store directly. Refused in READONLY."""
    if READONLY:
        return False
    try:
        conn = _secrets_conn()
        if tok:
            conn.execute("INSERT INTO secret(key,value) VALUES(?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (SUUNTO_TOKENS_KEY, json.dumps(tok)))
        else:
            conn.execute("DELETE FROM secret WHERE key=?", (SUUNTO_TOKENS_KEY,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[suunto] token store write failed: {e}")
        return False


def _jwt_user(access_token):
    """The Suunto username from the JWT's custom 'user' claim (display only — no verification
    needed: the token came straight from Suunto's token endpoint over TLS)."""
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("user")
    except Exception:
        return None


def _suunto_token_request(form):
    """One POST to the token endpoint (code exchange or refresh), basic-auth'd with the app
    credentials. Returns the stored-shape token dict, or raises with the provider's error text."""
    conf = _suunto_conf()
    r = requests.post(f"{SUUNTO_OAUTH_BASE}/oauth/token", data=form,
                      auth=(conf["suunto_client_id"], conf["suunto_client_secret"]),
                      headers={"Accept": "application/json"}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"token endpoint {r.status_code}: {r.text[:200]}")
    d = r.json()
    return {"access_token": d["access_token"],
            # refresh responses may omit refresh_token → keep the old one (caller merges)
            "refresh_token": d.get("refresh_token"),
            "expires_at": time.time() + float(d.get("expires_in", 86400)),
            "user": _jwt_user(d["access_token"])}


def suunto_access_token():
    """A currently-valid access token, refreshing through the stored refresh_token when within
    2 minutes of expiry. None ⇒ not connected (or refresh failed — reconnect via Settings)."""
    tok = _suunto_tokens()
    if not tok:
        return None
    if time.time() < tok.get("expires_at", 0) - 120:
        return tok["access_token"]
    try:
        new = _suunto_token_request({"grant_type": "refresh_token",
                                     "refresh_token": tok["refresh_token"]})
        if not new.get("refresh_token"):
            new["refresh_token"] = tok["refresh_token"]
        if not new.get("user"):
            new["user"] = tok.get("user")
        _save_suunto_tokens(new)
        return new["access_token"]
    except Exception as e:
        print(f"[suunto] token refresh failed: {e}")
        return None


def _suunto_headers(access_token, sub_key=None):
    """Headers every Cloud-API call needs: the user JWT + the app's subscription key."""
    return {"Authorization": f"Bearer {access_token}",
            "Ocp-Apim-Subscription-Key": sub_key or _suunto_conf()["suunto_subscription_key"],
            "User-Agent": USER_AGENT}


def suunto_status():
    """Settings-window payload: app-credentials configured? user connected (as whom)? Never a token."""
    conf = _suunto_conf()
    tok = _suunto_tokens()
    return {"configured": all(conf.values()), "connected": bool(tok),
            "user": (tok or {}).get("user")}


# ── §SG guide converter (pure) ───────────────────────────────────────────────
# guide.json facts confirmed from apizone.suunto.com/suuntoplus-guide-description (2026-07-10):
# targetPace/targetSpeed are in m/s (NOT sec/km); stepDuration seconds / stepDistance metres;
# repeats exist but a flat 1–1000 step list is equally valid (our reps arrays are already flat,
# with per-rep detail text, so flat steps preserve more information than folding into `repeat`).
# Field/step title limits: step title ≤13 chars, field title ≤9 when several fields share a step;
# text field ≤54 chars; name ≤60, shortDescription ≤23, description ≤256, externalId ≤64.
# The guide's "more info" link shown in the Suunto app — cosmetic metadata. Overridable so a
# self-hoster can point it at their own instance; the neutral default is the project repo.
SUUNTO_GUIDE_URL = os.environ.get("SH_GUIDE_URL") or "https://github.com/dros74/sparinghorse"
SUUNTO_ACTIVITY_RUNNING = 1
# Plan intensity zone → the app's HR zone band (hr_zones() Z1–Z5 tuples). "easy" spans Z1–Z2 —
# the moving easy bar (§3.4) lives in pace; on the wrist the HR band is the honest easy guard.
_SUUNTO_ZONE_TO_HRZ = {"easy": ("Z1", "Z2"), "easy_top": ("Z2", "Z2"), "lt1": ("Z2", "Z3"),
                       "marathon": ("Z3", "Z3"), "threshold": ("Z4", "Z4"),
                       "interval": ("Z5", "Z5"), "p5k": ("Z5", "Z5")}
_icon_png_cache = None


def _guide_txt(s, limit):
    """Clamp to the watch charset (the docs' minimum supported set) + a length limit. Common
    typography in our notes (×, —, ’) is transliterated rather than dropped."""
    s = (s or "").replace("×", "x").replace("—", "-").replace("–", "-").replace("’", "'").replace("·", "-")
    s = "".join(ch for ch in s if ch in
                " !\"#$%&'()*+,-./0123456789:;<=>?ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "abcdefghijklmnopqrstuvwxyz|°")
    return s[:limit].strip()


def _icon_png():
    """A 300×300 solid-terracotta PNG built with stdlib only (no PIL in the image). Cached — it's
    byte-identical for every guide."""
    global _icon_png_cache
    if _icon_png_cache is None:
        w = h = 300
        rgb = bytes((0xb5, 0x54, 0x3b))                     # house terracotta accent
        raw = b"".join(b"\x00" + rgb * w for _ in range(h))  # filter 0 per row

        def chunk(tag, data):
            c = tag + data
            return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))
        _icon_png_cache = (b"\x89PNG\r\n\x1a\n"
                           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(raw, 6))
                           + chunk(b"IEND", b""))
    return _icon_png_cache


def _pace_target(sec_per_km, band=0.05):
    """targetPace field from a sec/km pace: centre m/s with a ±band window."""
    if not sec_per_km:
        return None
    v = 1000.0 / sec_per_km
    return {"type": "targetPace", "title": "pace",
            "value": round(v, 3), "min": round(v * (1 - band), 3), "max": round(v * (1 + band), 3)}


def _hr_target(zone, hrz):
    """targetHeartRate field for a plan zone, from the app's own hr_zones() grid. None when the
    grid is unavailable (no robust anchor) or the zone is unmapped — pace then guides alone."""
    if not hrz or not hrz.get("zones"):
        return None
    span = _SUUNTO_ZONE_TO_HRZ.get(zone)
    if not span:
        return None
    by_label = {z[0]: z for z in hrz["zones"]}
    lo_z, hi_z = by_label.get(span[0]), by_label.get(span[1])
    if not lo_z or not hi_z:
        return None
    lo = lo_z[1] if lo_z[1] is not None else max(60, (lo_z[2] or 200) - 40)   # Z1 has no floor
    hi = hi_z[2] if hi_z[2] is not None else (hi_z[1] or 150) + 15            # Z5 has no ceiling
    return {"type": "targetHeartRate", "title": "HR",
            "value": int(round((lo + hi) / 2)), "min": int(lo), "max": int(hi)}


def _rep_pace_sec(rep):
    """A rep's numeric sec/km, re-derived from its own km/minutes (the stored fields of record)."""
    if rep.get("km") and rep.get("minutes"):
        return rep["minutes"] * 60.0 / rep["km"]
    return None


def _guide_step(title, text, minutes=None, km=None, pace_sec=None, hr=None, lap=False):
    """One fields step: countdown + optional pace/HR targets + a detail text line, advancing on
    its own duration (or distance, for the distance-framed simple runs)."""
    fields, cond = [], None
    if minutes:
        fields.append({"type": "stepDurationCountdown", "title": "left",
                       "value": round(minutes * 60.0, 1)})
        cond = {"type": "stepDuration", "value": round(minutes * 60.0, 1)}
    elif km:
        fields.append({"type": "stepDistanceCountdown", "title": "left",
                       "value": round(km * 1000.0, 1)})
        cond = {"type": "stepDistance", "value": round(km * 1000.0, 1)}
    pt = _pace_target(pace_sec)
    if pt:
        fields.append(pt)
    if hr:
        fields.append(hr)
    txt = _guide_txt(text, 54)
    if txt:
        fields.append({"type": "text", "value": txt})
    step = {"type": "fields", "title": _guide_txt(title, 13) or "Run", "fields": fields}
    if lap:
        step["createManualLap"] = True
    if cond:
        step["transitions"] = [{"condition": cond}]
    return step


def session_guide_external_id(session):
    """The idempotency key one session maps to — stable across regenerations of the same date+kind,
    so a re-push UPDATES the watch guide instead of stacking duplicates."""
    return f"sh-{session['date']}-{session.get('kind', 'run')}"[:64]


def session_to_guide(session, hrz=None):
    """PURE: one plan session dict → (guide_dict, zip_bytes). Structured sessions (reps arrays from
    _build_quality/_build_long_mp) become one step per rep — duration-framed, with the work reps
    lap-marked; simple easy/long runs become a single distance-framed step. Raises on a session
    with nothing to guide (km≤0)."""
    kind = session.get("kind", "run")
    km = session.get("km") or 0
    if km <= 0:
        raise ValueError("session has no distance")
    steps = []
    reps = session.get("reps")
    if reps:
        n_work = sum(1 for r in reps if r["effort"] == "work")
        wi = 0
        titles = {"warmup": "Warm up", "recovery": "Recover", "cooldown": "Cool down",
                  "easy_base": "Easy base"}
        for r in reps:
            work = r["effort"] == "work"
            if work:
                wi += 1
            title = f"Work {wi}/{n_work}" if (work and n_work > 1) else titles.get(r["effort"], "Work")
            steps.append(_guide_step(
                title, r.get("detail", ""), minutes=r["minutes"],
                pace_sec=_rep_pace_sec(r), hr=_hr_target(r["zone"], hrz), lap=work))
    else:
        pace_sec = session["minutes"] * 60.0 / km if session.get("minutes") else None
        steps.append(_guide_step("Run", session.get("note", ""), km=km,
                                 pace_sec=pace_sec, hr=_hr_target("easy", hrz)))
    label = {"easy": "Easy run", "long": "Long run", "long_mp": "Long run + MP"}.get(kind) \
        or _guide_txt(kind, 20).title()
    guide = {"type": "sequence",
             "name": _guide_txt(f"{label} {km}km - {session['date']}", 60),
             "shortDescription": _guide_txt(f"{label} {km}km", 23),
             "description": _guide_txt(session.get("note") or f"{label}, {km}km", 256) or label,
             "owner": "Sparing Horse", "url": SUUNTO_GUIDE_URL, "usage": "workout",
             "activities": [SUUNTO_ACTIVITY_RUNNING], "localDate": session["date"],
             "externalId": session_guide_external_id(session), "steps": steps}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("guide.json", json.dumps(guide, indent=1))
        z.writestr("icon.png", _icon_png())
    return guide, buf.getvalue()


# ── §SG push (idempotent upload) ─────────────────────────────────────────────
def _suunto_existing_guides(headers):
    """externalId → guide id for the guides already on Suunto's side. Defensive about the list
    payload shape (docs don't pin it): accepts a bare array or the common wrapper keys."""
    r = requests.get(f"{SUUNTO_API_BASE}/v2/guides/items", headers=headers, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"guides list {r.status_code}: {r.text[:200]}")
    d = r.json()
    items = d if isinstance(d, list) else next(
        (d[k] for k in ("items", "payload", "guides", "data") if isinstance(d.get(k), list)), [])
    out = {}
    for it in items:
        if isinstance(it, dict) and it.get("externalId"):
            out[it["externalId"]] = it.get("id") or it.get("guideId")
    return out


def _guide_ext_date(ext):
    """The plan date inside one of OUR guide externalIds ('sh-YYYY-MM-DD-kind'), or None for any id
    we did not write. The cleanup below deletes guides, so this is the safety gate: the athlete's own
    guides — and anything another tool uploaded — must never match."""
    m = re.match(r"^sh-(\d{4}-\d{2}-\d{2})-", ext or "")
    if not m:
        return None
    try:                          # the SHAPE of a date is not a date: '2026-13-99' matches the
        _date(m.group(1))         # pattern and would then be compared as a string against the
    except ValueError:            # horizon. On a path that DELETES, an id we cannot read is an id
        return None               # we leave alone.
    return m.group(1)


def _suunto_delete_guide(headers, gid):
    """Remove one guide from the connected account. True when it is GONE — a 404 counts, because the
    goal is absence, not a particular status code. Never raises: cleanup runs after a push that has
    already succeeded and must not turn a good night into a failed one."""
    try:
        r = requests.delete(f"{SUUNTO_API_BASE}/v2/guides/files/{gid}", headers=headers, timeout=15)
        return r.status_code in (200, 202, 204, 404)
    except requests.RequestException:
        return False


def push_guides(db, days=None):
    """Push the plan's next `days` sessions (today inclusive) to the connected Suunto account as
    Guides. Idempotent via externalId: existing → PUT update, new → POST, so the nightly re-push
    after a re-plan silently keeps the watch current — and a cleanup pass then DELETES our own guides
    the re-plan superseded (a changed kind, a moved run, a day gone past), so the watch shows one
    guide per planned day and nothing behind today. Returns a per-session summary; never raises
    (the scheduler must survive a flaky Suunto night the same way it survives Runalyze)."""
    days = days or SUUNTO_PUSH_DAYS
    if READONLY:
        return {"ok": False, "error": "read-only instance"}
    st = suunto_status()
    if not st["configured"] or not st["connected"]:
        return {"ok": False, "error": "Suunto not connected", "skipped": True}
    tok = suunto_access_token()
    if not tok:
        return {"ok": False, "error": "Suunto token refresh failed — reconnect in Settings"}
    row = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return {"ok": False, "error": "no plan", "skipped": True}
    plan = json.loads(row[0])
    today = datetime.now().date()
    horizon = (today + timedelta(days=days - 1)).isoformat()
    sessions = [s for w in _plan_all_weeks(plan) for s in w.get("sessions", [])
                if today.isoformat() <= s["date"] <= horizon and (s.get("km") or 0) > 0]
    if not sessions:
        return {"ok": True, "pushed": 0, "results": [], "note": "no sessions in window"}
    hrz = None
    try:
        hrz = hr_zones(db)
    except Exception:
        pass                                   # pace guides alone — HR grid is an enhancement
    headers = _suunto_headers(tok)
    try:
        existing = _suunto_existing_guides(headers)
    except Exception as e:
        return {"ok": False, "error": f"could not list existing guides: {e}"}
    up_headers = dict(headers, **{"Content-Type": "application/zip"})
    results, pushed = [], 0
    current_ext, failed_dates = set(), set()   # §SG cleanup inputs — see the pass after this loop
    for s in sessions:
        ext = session_guide_external_id(s)
        try:
            _, blob = session_to_guide(s, hrz)
            gid = existing.get(ext)
            if gid:
                r = requests.put(f"{SUUNTO_API_BASE}/v2/guides/files/{gid}",
                                 data=blob, headers=up_headers, timeout=20)
                action = "updated"
            else:
                r = requests.post(f"{SUUNTO_API_BASE}/v2/guides/files",
                                  data=blob, headers=up_headers, timeout=20)
                action = "created"
            if r.status_code in (200, 201, 204):
                pushed += 1
                current_ext.add(ext)
                results.append({"date": s["date"], "kind": s.get("kind"), "action": action})
            elif r.status_code == 409 and action == "created":
                # already there under this externalId (list was stale) — counts as current
                current_ext.add(ext)
                results.append({"date": s["date"], "kind": s.get("kind"), "action": "exists"})
            else:
                failed_dates.add(s["date"])
                results.append({"date": s["date"], "kind": s.get("kind"),
                                "error": f"{action} {r.status_code}: {r.text[:120]}"})
        except Exception as e:
            failed_dates.add(s["date"])
            results.append({"date": s["date"], "kind": s.get("kind"), "error": str(e)[:200]})
    # §SG — the watch MIRRORS the current plan; it is not an append-only log. Two ways one of our
    # guides goes stale, and until 2026-08-22 neither could be cleared because no DELETE existed:
    #  · a KIND FLIP on a date already pushed. The externalId carries the kind (`sh-{date}-{kind}`)
    #    and kind is NOT stable across regenerations — an easy-only check-in demotes a tempo to easy
    #    (`_apply_adjustment`), and the ACWR ceiling relabels a clipped long run as easy
    #    (`_mark_load_integrity`). The new id misses the lookup, POSTs a second guide, and the
    #    superseded one stays on the wrist. Every regen with a flip could stack another.
    #  · a date that has gone PAST, or one still inside the window that no longer carries a session
    #    at all (the re-plan moved the run, or eased the day to rest).
    # Both reduce to one rule: OUR guide, dated at or before this push's horizon, that is not what we
    # just pushed, goes. Two guards on top — a date whose push FAILED keeps its old guide (a stale
    # guide beats no guide), and `_guide_ext_date` returning None means the id is not ours to delete.
    # Best-effort: a delete that fails is reported, never raised, so a flaky Suunto night still ends
    # with the sessions pushed.
    removed, rm_failed = [], []
    for ext, gid in existing.items():
        d = _guide_ext_date(ext)
        if not gid or not d or d > horizon or ext in current_ext or d in failed_dates:
            continue
        (removed if _suunto_delete_guide(headers, gid) else rm_failed).append(ext)
    errs = [r for r in results if r.get("error")]
    return {"ok": not errs, "pushed": pushed, "results": results,
            "removed": removed, **({"remove_failed": rm_failed} if rm_failed else {}),
            **({"error": f"{len(errs)} of {len(results)} failed"} if errs else {})}


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
      incremental stop-condition would otherwise never reach the older history.
    Already-synced rows whose upstream content CHANGED are refreshed in place (§DB1 MED-1) so an
    edit-down on Runalyze converges instead of leaving stale-high load; this never counts as 'new',
    so the incremental stop is unchanged."""
    existing = {r["id"]: r["raw"] for r in db.execute("SELECT id, raw FROM activities").fetchall()}
    known = set(existing)
    added = 0
    refreshed = 0
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
            aid = a.get("id")
            if aid not in known:
                upsert_activity(db, a)
                known.add(aid)
                new_here += 1
            elif json.dumps(a, separators=(",", ":")) != existing.get(aid):
                # §DB1 MED-1 — an already-synced activity changed upstream (e.g. Runalyze recomputed
                # TRIMP, or the owner cropped an over-long run → load edited DOWN). The old code SKIPPED
                # every known id, so a stale-high load lingered forever. Refresh it so the local copy
                # converges. NOT counted as new (new_here untouched), so the incremental stop-condition
                # below is preserved: recent edits (page 1, always fetched) converge on the next sync;
                # older ones on a full backfill (which walks every page anyway).
                upsert_activity(db, a)
                existing[aid] = json.dumps(a, separators=(",", ":"))
                refreshed += 1
        db.commit()  # commit per page → durable progress, no all-or-nothing stall
        added += new_here
        if new_here == 0 and not backfill:
            break  # caught up (incremental only; backfill keeps going to the end)
    return {"added": added, "refreshed": refreshed, "pages_fetched": pages}


# Single source of the shape_snapshots column contract — shared by the live API capture
# (snapshot_shape) and the synthetic seeder, so the column list never drifts between them.
SHAPE_COLUMNS = ("snapshot_date", "captured_at", "effective_vo2max", "effective_vo2max_progress",
                 "fitness", "fatigue", "performance", "fitness_pct", "acwr", "marathon_shape",
                 "hrv_baseline", "monotony", "training_strain", "raw")


def upsert_shape_snapshot(db, snapshot_date, *, effective_vo2max=None, effective_vo2max_progress=None,
                          fitness=None, fatigue=None, performance=None, fitness_pct=None, acwr=None,
                          marathon_shape=None, hrv_baseline=None, monotony=None, training_strain=None,
                          raw="{}", captured_at=None):
    """Write/replace one daily shape snapshot (one row per day). Keyword-only so callers can't
    misorder the 14 columns; missing fields default to NULL."""
    db.execute(
        f"INSERT OR REPLACE INTO shape_snapshots ({', '.join(SHAPE_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(SHAPE_COLUMNS))})",
        (snapshot_date, captured_at or _now_iso(), effective_vo2max, effective_vo2max_progress,
         fitness, fatigue, performance, fitness_pct, acwr, marathon_shape,
         hrv_baseline, monotony, training_strain, raw))


def snapshot_shape(db):
    """Append today's 'current shape' (one row per day; replace if re-run same day)."""
    s = fetch_statistics_current()
    today = datetime.now().strftime("%Y-%m-%d")
    upsert_shape_snapshot(
        db, today,
        effective_vo2max=s.get("effectiveVO2max"), effective_vo2max_progress=s.get("effectiveVO2maxProgress"),
        fitness=s.get("fitness"), fatigue=s.get("fatigue"), performance=s.get("performance"),
        fitness_pct=s.get("fitnessInPercent"), acwr=s.get("acuteChronicWorkloadRatio"),
        marathon_shape=s.get("marathonShape"), hrv_baseline=s.get("hrvBaseline"),
        monotony=s.get("monotonyValue"), training_strain=s.get("trainingStrain"),
        raw=json.dumps(s, separators=(",", ":")))
    return s


# Watch-recorded daily metrics → health_markers. marker key -> (MCP trend tool, item value field).
# (The per-day HRV item carries metric='RMSSD'|'SDNN'|… ; we keep RMSSD, the one the baseline uses.)
HEALTH_SYNC = {
    "hrv":        ("get_hrv_trend", "hrv"),
    "weight":     ("get_weight_trend", "weight"),
    "resting_hr": ("get_resting_heart_rate_trend", "heart_rate"),
}

# Sleep is one MCP summary tool feeding SEVERAL markers (one row per night). Field -> (marker, transform).
# night_hr is the overnight LOWEST HR — kept a DISTINCT marker, NOT merged into resting_hr, which is
# Runalyze's algorithmic "dynamic resting HR" (a different measurement that dead-ends at the
# Garmin→Suunto switch). Splicing the two would misread a device change as a physiological trend.
SLEEP_MARKERS = {
    "duration":            ("sleep_duration", lambda v: round(v / 60.0, 2)),  # minutes → hours
    "quality":             ("sleep_quality", float),
    "deep_sleep_duration": ("sleep_deep", float),
    "rem_duration":        ("sleep_rem", float),
    "hr_lowest":           ("night_hr", float),
}


def _sleep_main_by_date(items):
    """Runalyze can return several sleep records for one day (naps, split sleep). Attribute each to the
    morning you WOKE (start + duration) and keep the single longest per wake-date — the main overnight
    sleep — so the series is one honest point per night. Returns {wake_date_iso: item}."""
    from datetime import datetime as _dt, timedelta as _td
    best = {}
    for it in items or []:
        dur, dtm = it.get("duration"), it.get("datetime")
        if not dur or not dtm:
            continue
        try:
            wake = (_dt.fromisoformat(dtm) + _td(minutes=dur)).date().isoformat()
        except ValueError:
            continue
        cur = best.get(wake)
        if cur is None or dur > cur["duration"]:
            best[wake] = it
    return best


def sync_health_metrics(db, backfill=False):
    """Pull watch-recorded daily metrics (HRV / weight / resting HR) from Runalyze's MCP trend tools into
    the health_markers series (source='runalyze'), so the health view charts them next to the manual lab
    markers — and the long horizon shows what the watch's short rolling baseline can't. Routine sync pulls
    the last ~60 days (cheap, idempotent upsert on marker+date); backfill pulls the full history. Best
    effort: a metric whose tool errors is skipped, never failing the whole sync. Returns {marker: count}."""
    from datetime import timedelta
    end = datetime.now().date()
    start = "2015-01-01" if backfill else (end - timedelta(days=60)).isoformat()
    out = {}
    for marker, (tool, field) in HEALTH_SYNC.items():
        try:
            res = mcp_call(tool, {"start_date": start, "end_date": end.isoformat()})
        except (RunalyzeError, requests.RequestException, KeyError, ValueError, TypeError):
            continue
        n = 0
        for it in (res or {}).get("items") or []:
            val, dt = it.get(field), it.get("date")
            if val is None or not dt:
                continue
            if marker == "hrv" and it.get("metric") and it.get("metric") != "RMSSD":
                continue   # one canonical HRV metric (RMSSD), ignore SDNN/etc. if returned
            db.execute("INSERT OR REPLACE INTO health_markers (marker, date, value, source, note) "
                       "VALUES (?,?,?,?,?)", (marker, dt[:10], float(val), "runalyze", it.get("source") or ""))
            n += 1
        out[marker] = n
    # Sleep summary → per-night markers (duration/stages/quality/overnight-HR). DISPLAY ONLY: the study
    # found no acute sleep→next-day-quality signal, so sleep is never a plan/readiness input (PROJECT_LOG).
    try:
        sc = mcp_call("get_sleep_summary", {"start_date": start, "end_date": end.isoformat()})
    except (RunalyzeError, requests.RequestException, KeyError, ValueError, TypeError):
        sc = None
    for wake, it in _sleep_main_by_date((sc or {}).get("items")).items():
        for field, (marker, tf) in SLEEP_MARKERS.items():
            val = it.get(field)
            if val is None:
                continue
            db.execute("INSERT OR REPLACE INTO health_markers (marker, date, value, source, note) "
                       "VALUES (?,?,?,?,?)", (marker, wake, float(tf(val)), "runalyze", it.get("source") or ""))
            out[marker] = out.get(marker, 0) + 1
    db.commit()
    return out


def run_sync(backfill=False):
    """Routine incremental pull (default) or a one-time full-history backfill. Backfill is
    needed whenever the local copy is partial — e.g. a fresh machine — because incremental
    stops at the first already-known page and can never reach older history behind it."""
    db = connect_db()
    try:
        act = sync_activities(db, backfill=backfill)
        snapshot_shape(db)
        try:                                    # watch metrics are a nice-to-have — never fail the sync
            health = sync_health_metrics(db, backfill=backfill)
        except Exception:
            health = None
        try:                                    # §RD — read new runs back; never fail the sync over it
            structures = classify_recent(db)
        except Exception:
            structures = None
        set_meta(db, "last_sync", _now_iso())
        db.commit()
        return {"ok": True, "activities": act, "health": health, "structures": structures,
                "last_sync": get_meta(db, "last_sync"), "backfill": backfill}
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


def delete_activity_local(db, aid):
    """Hard-remove an activity from the OWNED local copy + its derived rows (ignore-list entry,
    cached profile). `sync_activities` is insert-only, so a Runalyze deletion never propagates —
    this is the only way to drop a row Runalyze no longer holds. Returns True if a row was removed,
    False if no such id. CAVEAT: if the activity STILL exists on Runalyze, the next incremental sync
    re-inserts it (page 1 is always re-fetched) — this is for activities already removed upstream;
    an accidental delete of a live activity self-heals on re-sync (or a full backfill).
    §DB1 — we deliberately KEEP any `ignored_activities` tombstone: if this row was a manually-ignored
    near-dup (one `find_duplicates` can't catch, e.g. a drifted timestamp) and is still upstream, a
    re-sync re-inserts the activity; the surviving tombstone keeps it excluded instead of letting it
    double-count. An orphan tombstone (id matches no activity) is a harmless no-op in `dropped_ids`."""
    if not db.execute("SELECT 1 FROM activities WHERE id=?", (aid,)).fetchone():
        return False
    db.execute("DELETE FROM activities WHERE id=?", (aid,))
    db.execute("DELETE FROM trackcache WHERE activity_id=?", (aid,))
    try:
        db.execute("DELETE FROM structcache WHERE activity_id=?", (aid,))   # §RD — no orphan structure
    except sqlite3.OperationalError:
        pass                                       # minimal det fixture without the table
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
EASY_PACE_GRACE = 0.03      # public PACE read: allow GAP up to 3% quicker than the easy ceiling = 'on'
AEROBIC_KINDS = {"easy", "long"}    # the well-calibrated direction (his documented failure mode)
EFFORT_MATCH_DAYS = 2       # a session shuffled within ±2 days reads as a reschedule, not a new run
NON_SESSION_KINDS = ("rest",)   # plan entries that prescribe NO run — never matchable as executed work

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
#                       demands, so a 6-min strides recording is readable because of what it follows

# ── LTHR (lactate-threshold HR) derivation — see [[hr-zones-lthr-design]] ─────
# A data-derived LTHR anchors HR zones + the effort monitor more accurately than %HRmax at the
# easy↔threshold turnpoint (two runners, same HRmax, can have thresholds 15+ bpm apart). Slice #1 is
# STREAMLESS on purpose (reads activities.hr_avg/duration only) → token-free + seed-testable; the
# best-20-min-window via live MCP streams is a later refinement.
LTHR_MIN_SEC = 20 * 60      # a qualifying sustained effort lasts ≥20 min …
LTHR_MAX_SEC = 70 * 60      # … and ≤70 min (longer drifts below threshold; whole-run avg understates LTHR)
LTHR_QUAL_FRAC = 0.85       # … at ≥85% robust HRmax — a genuine threshold+ effort, not an easy run
LTHR_PCTL = 0.85            # robust-HIGH statistic over the pool (spike-resistant vs a raw max)
LTHR_HRMAX_PROXY = 0.92     # thin-data fallback: LTHR ≈ 92% HRmax (Friel-ish run default), provisional
LTHR_RECENT_DAYS = 120      # efforts within this window are "recent" (LTHR drifts up as fitness returns)

# Friel run-zone grid as a fraction of LTHR (5 zones → 4 ascending boundaries). The classic Friel run
# split: Z1<0.85, Z2 0.85–0.89, Z3 0.90–0.94, Z4 0.95–0.99, Z5 ≥1.00 (LTHR sits at the top of Z4).
LTHR_ZONE_FRACS = (0.85, 0.90, 0.95, 1.00)
# The effort monitor's easy/hard ceilings ARE the chart's zone boundaries — DERIVED from the one grid, not
# re-typed, so the chart, the zone band, and the monitor can never silently disagree (the whole point of
# unifying the model). det/hr-zones locks the equality so a future un-derive is caught.
LTHR_EASY_FRAC = LTHR_ZONE_FRACS[0]   # Z1/Z2 boundary (Friel easy/recovery ceiling): above this an 'easy'
#                                       run drifted hot. =0.85 → ≤ the old %HRmax ceiling, so the LTHR switch
#                                       never LOOSENS his easy bar (the streamless LTHR is biased low).
LTHR_HARD_FRAC = LTHR_ZONE_FRACS[2]   # Z3/Z4 boundary: at/above threshold ⇒ an 'easy' run was threshold+
LTHR_MIN_CONFIDENCE = {"moderate", "high"}   # only ANCHOR on a derived LTHR this trustworthy (else %HRmax)
HRMAX_ZONE_FRACS = (0.60, 0.70, 0.80, 0.90)  # %HRmax fallback grid — the values the reconstruction confirmed
LTHR_TRUSTED = ("derived", "manual")         # sources the zone/monitor anchor may trust (gate ∧ confidence)
# Manual LTHR (slice #2): a field-tested number the owner typed in. It goes STALE as fitness moves
# (guardrail #2 — LTHR drifts UP through a rebuild, so an old manual value can mis-anchor either way):
# confidence decays with the age of the entry, and the derived estimate takes back over once it out-ranks
# the decayed manual one. An UNDATED manual value (env-provided) is capped at moderate.
MANUAL_LTHR_FRESH_DAYS = 42   # ≤ this old ⇒ high confidence (a recent field test beats the streamless read)
MANUAL_LTHR_OK_DAYS = 84      # ≤ this old ⇒ moderate; older ⇒ low (re-test or clear it)
MANUAL_LTHR_RANGE = (100, 220)  # plausible human LTHR band (bpm) — validation for the setting
_CONF_RANK = {"high": 3, "moderate": 2, "low": 1, "none": 0}


def _robust_hrmax(db):
    """A spike-resistant HRmax: the 95th percentile of per-run hr_max (one bad strap reading hits 210
    where his real max is ~189). HR is the gate, so this anchors the zones. None if too little data."""
    hrs = sorted(r["hr_max"] for r in db.execute(
        "SELECT hr_max FROM activities WHERE " + RUN_FAMILY_SQL + " AND hr_max IS NOT NULL"
        ).fetchall() if r["hr_max"])
    if not hrs:
        # §FT5 — cold-start prior: with NO measured HR at all, an age-based HRmax (Tanaka,
        # 208 − 0.7·age) anchors the %HRmax fallback grid until real straps land. Prior, never
        # a measurement — the first synced hr_max row takes over on the next call.
        age = _athlete_age(db)
        return round(208 - 0.7 * age) if age else None
    return hrs[min(len(hrs) - 1, round(0.95 * (len(hrs) - 1)))]


def _pctile(xs, q):
    """Spike-resistant percentile (nearest-rank, like _robust_hrmax). xs need not be sorted."""
    xs = sorted(xs)
    if not xs:
        return None
    return xs[min(len(xs) - 1, round(q * (len(xs) - 1)))]


def _manual_lthr(db, today):
    """The owner's manual LTHR entry (Settings → 'Manual LTHR'), age-decayed per guardrail #2.
    Returns (bpm, confidence, set_on, age_days) or (None, None, None, None) when unset/invalid.
    Tolerates a DB without the meta table (det fixtures) — absent means unset. The set-date is
    stamped by save_settings; an undated value (env-provided) is capped at moderate."""
    try:
        raw, _src = _resolve_setting(db, SETTINGS_BY_KEY["manual_lthr"])
    except sqlite3.OperationalError:
        return None, None, None, None
    raw = (raw or "").strip()
    if not raw or not raw.isdigit() or not (MANUAL_LTHR_RANGE[0] <= int(raw) <= MANUAL_LTHR_RANGE[1]):
        return None, None, None, None
    try:
        set_on = get_meta(db, "manual_lthr_set_on")
    except sqlite3.OperationalError:
        set_on = None
    age = None
    if set_on:
        from datetime import date as _d
        try:
            age = (today - _d.fromisoformat(set_on)).days
        except (TypeError, ValueError):
            age = None
    conf = ("moderate" if age is None else
            "high" if age <= MANUAL_LTHR_FRESH_DAYS else
            "moderate" if age <= MANUAL_LTHR_OK_DAYS else "low")
    return int(raw), conf, set_on, age


def derive_lthr(db, today=None):
    """Estimate LTHR (lactate-threshold HR) from sustained hard efforts the athlete already ran — no
    field test, self-calibrating. For a CONTINUOUS hard effort (a race, or a tempo with little
    warmup/cooldown) the whole-run avg HR ≈ LTHR; we pool qualifying efforts (≥20 min, ≤70 min, ≥85%
    robust HRmax) and take a robust-high percentile. Thin/zero data ⇒ a %HRmax proxy at LOW confidence
    (provisional — that crudeness is the very reason we prefer a derived LTHR). STREAMLESS by design:
    reads activities.hr_avg/duration only, so it's token-free and testable on the synthetic seed (the
    best-20-min-window via live MCP streams is a later slice). Known bias: understates LTHR for
    STRUCTURED tempos (warmup/cooldown dilute the whole-run avg) — fine for a confidence-flagged v1.

    Returns {lthr, source, confidence, n, n_recent, hrmax, pct_hrmax, provisional}:
      • source: 'manual' (owner's field-tested entry, age-decayed) | 'derived' (from efforts) |
        'hrmax_proxy' (fallback) | None (no HRmax at all)
      • confidence: 'high' | 'moderate' | 'low' | 'none'
      • a MANUAL entry (slice #2) wins while its age-decayed confidence out-ranks (or ties) the
        automatic read — a fresh field test beats the streamless estimate, then hands back as it
        goes stale; ties go to the human's number. Manual works even with no HR data at all.
      • lthr is None only when there's no manual entry AND no robust HRmax to even proxy from."""
    from datetime import date as _d
    today = today or _d.today()
    rmax = _robust_hrmax(db)
    base = {"hrmax": rmax, "n": 0, "n_recent": 0, "provisional": False}
    if not rmax:
        auto = {**base, "lthr": None, "source": None, "confidence": "none", "pct_hrmax": None}
    else:
        floor = int(rmax * LTHR_QUAL_FRAC)
        rows = db.execute(
            "SELECT date, hr_avg, duration FROM activities WHERE " + RUN_FAMILY_SQL +
            " AND hr_avg IS NOT NULL AND duration IS NOT NULL AND duration BETWEEN ? AND ? AND hr_avg>=?",
            (LTHR_MIN_SEC, LTHR_MAX_SEC, floor)).fetchall()
        quals = []   # (days_ago, hr_avg) for every qualifying sustained hard effort
        for r in rows:
            try:
                days_ago = (today - _d.fromisoformat(r["date"][:10])).days
            except (TypeError, ValueError):
                days_ago = None
            quals.append((days_ago, int(r["hr_avg"])))
        n = len(quals)
        n_recent = sum(1 for d, _ in quals if d is not None and 0 <= d <= LTHR_RECENT_DAYS)
        if n == 0:
            # No sustained hard effort to read — proxy off HRmax, honestly flagged provisional/low.
            auto = {**base, "lthr": round(rmax * LTHR_HRMAX_PROXY), "source": "hrmax_proxy",
                    "confidence": "low", "pct_hrmax": LTHR_HRMAX_PROXY, "provisional": True}
        else:
            # Prefer the RECENT pool when it's substantial (LTHR drifts up as fitness returns);
            # otherwise read all qualifiers but let confidence reflect the staleness.
            recent_hrs = [hr for d, hr in quals if d is not None and 0 <= d <= LTHR_RECENT_DAYS]
            pool = recent_hrs if len(recent_hrs) >= 3 else [hr for _, hr in quals]
            lthr = _pctile(pool, LTHR_PCTL)
            confidence = ("high" if n_recent >= 5 else "moderate" if n_recent >= 2 else "low")
            auto = {**base, "lthr": lthr, "source": "derived", "confidence": confidence,
                    "n": n, "n_recent": n_recent, "pct_hrmax": round(lthr / rmax, 3)}
    mv, mconf, set_on, age = _manual_lthr(db, today)
    if mv and _CONF_RANK[mconf] >= _CONF_RANK[auto["confidence"]]:
        return {**base, "n": auto["n"], "n_recent": auto["n_recent"], "lthr": mv,
                "source": "manual", "confidence": mconf, "set_on": set_on, "age_days": age,
                "alt_derived": auto["lthr"] if auto["source"] == "derived" else None,
                "pct_hrmax": round(mv / rmax, 3) if rmax else None}
    return auto


def hr_zones(db, today=None):
    """The app's OWN 5-zone HR model in bpm — the bridge until Runalyze exposes real boundaries. Anchors
    on a DATA-DERIVED LTHR (Friel %LTHR grid) when that LTHR is trustworthy (source='derived' and
    confidence ≥ moderate — see derive_lthr), else falls back to a fixed %HRmax grid (60/70/80/90, the
    values the Runalyze reconstruction already confirmed for him, so the fallback is continuous with the
    chart today). PURE + token-free (derive_lthr is streamless), so it's seed-testable and det-lockable.
    Distinct from derive_hr_zones, which stays the (token-gated, slow) corroboration against Runalyze's
    own zones — this is what the app should USE, that is what checks our work.

    Returns {anchor, ref, cutoffs, zones, lthr_confidence}:
      • anchor: 'lthr' | 'hrmax' | None  (None ⇒ no robust HRmax to scale from at all)
      • ref:    the bpm the grid is scaled from (LTHR, or robust HRmax in fallback)
      • cutoffs: 4 ascending bpm boundaries (Z1/Z2 … Z4/Z5), or None
      • zones:  [(label, lo, hi)] for Z1–Z5 (lo None on Z1, hi None on Z5)
      • lthr_confidence: carried through so the caller/UI can gate how much to trust the anchor."""
    info = derive_lthr(db, today=today)
    labels = ["Z1", "Z2", "Z3", "Z4", "Z5"]
    if info.get("source") in LTHR_TRUSTED and info.get("confidence") in LTHR_MIN_CONFIDENCE:
        anchor, ref, fracs = "lthr", info["lthr"], LTHR_ZONE_FRACS
    elif info.get("hrmax"):
        anchor, ref, fracs = "hrmax", info["hrmax"], HRMAX_ZONE_FRACS
    else:
        return {"anchor": None, "ref": None, "cutoffs": None, "zones": None,
                "lthr_confidence": info.get("confidence")}
    cutoffs = [round(ref * f) for f in fracs]
    bounds = [None] + cutoffs + [None]
    zones = [(labels[i], bounds[i], bounds[i + 1]) for i in range(5)]
    return {"anchor": anchor, "ref": ref, "cutoffs": cutoffs, "zones": zones,
            "lthr_confidence": info.get("confidence")}


def training_zones(db, today=None):
    """The 'Current zones' card payload — one table of training-INTENT rows (easy / marathon /
    threshold / interval, the vocabulary the plan prescribes in), each with its PACE window and its
    HR band, both tracking CURRENT fitness:
      • PACE (the prescription anchor, §6.3): fixed fractions of vVO2max from the CURRENT effective
        VO2max (Daniels grid); the easy bar is LT1 ≈ 80% of 5k pace (Davis) — run easy SLOWER than it.
      • HR (the monitoring cross-check): bands cut from the SAME unified hr_zones cutoffs the effort
        monitor + the activity chart band use (easy top = the Z1/Z2 boundary = the monitor's easy
        ceiling; threshold = Z4) — derived from ONE grid, so this card can't drift from the verdicts.
    The two columns are INDEPENDENT estimators of the same fitness (VDOT vs LTHR) and may visibly
    disagree under cardiac decoupling — that's surfaced by pace_hr_coherence, never averaged away
    here. Pure read; carries HR + fitness ⇒ PRIVATE (H7)."""
    snap = latest_snapshot(db)
    pz = pace_zones(snap["effective_vo2max"]) if snap else {}
    hz = hr_zones(db, today=today)
    info = derive_lthr(db, today=today)
    cut = hz.get("cutoffs")            # 4 ascending bpm boundaries (Z1/Z2 … Z4/Z5) or None
    def hr_band(i, j):                 # bpm band between cutoff i and j (None = open end)
        return {"lo": cut[i] if (cut and i is not None) else None,
                "hi": cut[j] if (cut and j is not None) else None}
    def pace(key):
        p = pz.get(key)
        return {"sec_km": p, "fmt": fmt_pace(p) if p else None}
    rows = [
        {"key": "easy", "label": "Easy / recovery", "zone_idx": 0,
         "pace_slower_than": pace("lt1"), "hr": hr_band(None, 0)},          # ≤ Z1/Z2 = monitor ceiling
        {"key": "marathon", "label": "Marathon", "zone_idx": 2,
         "pace_target": pace("marathon"), "hr": hr_band(1, 2)},             # Z3
        {"key": "threshold", "label": "Threshold", "zone_idx": 3,
         "pace_target": pace("threshold"), "hr": hr_band(2, 3)},            # Z4 (LTHR = its top)
        {"key": "interval", "label": "Interval / VO₂max", "zone_idx": 4,
         "pace_target": pace("interval"), "hr": hr_band(3, None)},          # Z5 (HR lags short reps)
    ]
    return {"ok": bool(pz or cut), "rows": rows,
            "pace_anchor": {"vo2max": snap["effective_vo2max"] if snap else None,
                            "p5k": pz.get("p5k"), "p5k_fmt": fmt_pace(pz.get("p5k")),
                            "lt1_5k_frac": LT1_5K_FRAC},
            "hr_anchor": {"anchor": hz.get("anchor"), "ref": hz.get("ref"),
                          "source": info.get("source"), "confidence": info.get("confidence"),
                          "age_days": info.get("age_days"), "hrmax": info.get("hrmax")}}


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
        "SELECT id, date FROM activities WHERE " + RUN_FAMILY_SQL + " AND hr_max IS NOT NULL AND hr_max>=? "
        "ORDER BY date DESC LIMIT ?", (int(rmax * 0.8), sample)).fetchall()
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


# §RD × §6m — the rep-based quality read. When a quality run's detected structure is cached, the
# monitor grades the WORK reps against the prescribed zone instead of the whole-run average (which
# blends reps with warm-up/recovery — why the old quality verdict was capped at 'low' confidence).
EFFORT_SEG_TOL = 3          # bpm tolerance around the prescribed HR band
EFFORT_SEG_PACE_TOL = 0.04  # ±4% (log) around the zone pace target when reps carry no HR
EFFORT_SEG_HR_MIN_S = 180   # reps SHORTER than this are judged on PACE even when HR exists: a
#                             short rep starts rested, HR climbs through it and PEAKS INTO the
#                             recovery (the owner's 2026-07-05 observation — pace and HR peaks are
#                             out of phase), so a within-rep average systematically under-reads
#                             and would call every 2-min VO₂ rep 'sandbagged'. The zones card's
#                             own caveat ('Z5 — HR lags short reps'), applied to the verdict.
KIND_ZONE = {"interval": "interval", "tempo": "threshold", "long_mp": "marathon"}  # prescription zone


def _seg_band(cutoffs, zone):
    """The bpm band a prescribed zone maps to on the unified hr_zones cutoffs — the SAME Z3/Z4/Z5
    rows the Current-zones card shows (training_zones), so the rep verdict can't drift from it."""
    i, j = {"marathon": (1, 2), "threshold": (2, 3), "interval": (3, None)}[zone]
    return (cutoffs[i] if i is not None else None,
            cutoffs[j] if j is not None else None)


def _effort_verdict(kind, hrf, te, easy_frac=EASY_HR_FRAC, hard_frac=HARD_HR_FRAC):
    """Pure per-run verdict — HR-LED, TE corroborates (returns (verdict, confidence)). `hrf` is the
    run's avg HR as a fraction of an anchor; `easy_frac`/`hard_frac` are the ceilings ON THAT SAME
    anchor. Default anchor = %HRmax (0.78/0.85); when a derived LTHR is trustworthy the caller passes
    %LTHR fractions instead (0.90/0.95 = Friel Z2-top / Z4-start), a sharper read at the easy↔threshold
    turnpoint. For an aerobic (easy/long) session: on / hot / too_hard by fraction, confidence rising to
    'high' when a too-hard read is backed by a high Training Effect. For a quality session: 'did you hit
    it' — too_easy if HR never reached the aerobic ceiling (sandbagged), else on — always LOW confidence
    (little compliant-quality data yet, and his problem is the too-hard direction). hrf None ⇒
    ('unknown','none')."""
    if hrf is None:
        return "unknown", "none"
    if kind in AEROBIC_KINDS:
        if hrf > hard_frac:
            return "too_hard", ("high" if (te or 0) >= TE_HARD_CORROBORATE else "moderate")
        if hrf > easy_frac:
            return "hot", "moderate"
        return "on", "moderate"
    return ("too_easy" if hrf < easy_frac else "on"), "low"


def _effort_verdict_pace(kind, gap_pace, zones, ceiling_key="easy_top"):
    """The PACE-based easy-discipline verdict — no heart rate. An aerobic (easy/long) run is judged on
    grade-adjusted pace vs the pace zones: 'on' at/slower than the easy ceiling (a 3% grace for GPS/grade
    noise), 'too_hard' faster than marathon pace, 'hot' between. A quality run isn't pace-judged here — the
    honest 'did you hit it' read needs HR — so it's 'unknown' (excluded). gap_pace/zones are sec/km; larger
    = slower. `ceiling_key` selects the easy bar: 'easy_top' (the conservative public ceiling) or 'lt1' (the
    §3.4 fitness-tracking LT1 = Davis's aerobic-threshold easy bar; used as the private moving anchor)."""
    easy_ceiling = (zones or {}).get(ceiling_key) or (zones or {}).get("easy_top")
    if not gap_pace or not easy_ceiling or kind not in AEROBIC_KINDS:
        return "unknown"
    if gap_pace >= easy_ceiling * (1 - EASY_PACE_GRACE):
        return "on"
    mp = zones.get("marathon")
    if mp and gap_pace < mp:
        return "too_hard"
    return "hot"


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


def _sj_group_for(db, aid):
    """§SJ — the group containing activity `aid` (list of rows, time-ordered), or None when it is a
    plain singleton / unknown id. Owned rows only — an ignored part has left its group."""
    row = db.execute("SELECT date FROM activities WHERE id=?", (aid,)).fetchone()
    if not row or not row["date"]:
        return None
    drop = dropped_ids(db)
    rows = [r for r in db.execute(
        "SELECT id, date, date_time, distance, duration, elapsed_time FROM activities "
        "WHERE date=? AND " + RUN_FAMILY_SQL, (row["date"],)).fetchall()
        if r["id"] not in drop and r["distance"]]
    for g in _session_groups(rows):
        if len(g) > 1 and any(r["id"] == aid for r in g):
            return g
    return None


SJ_KIND_PRIORITY = ("interval", "tempo", "long_mp", "strides", "long", "easy")   # §SJ composite kind


def _sj_composite(db, group, fetch=True):
    """§SJ — the 1+1 composite read, assembled at VIEW time from the parts' own §RD reads (the
    per-activity structcache stays the storage; nothing composite is persisted). kind = the
    highest-priority part read, summary = part summaries joined in time order, strides/n_work
    summed; the per-part reads ride along under `parts` so the UI can show the seam. Parts read
    under the relaxed SJ_PART_MIN_S floor — the group is their context. None until at least one
    part has a readable structure."""
    parts, kinds = [], []
    strides = n_work = 0
    for p in group:
        st, _e = _structure_cached(db, p["id"], date_iso=p["date"], fetch=fetch,
                                   min_s=SJ_PART_MIN_S)
        read = st if st else {"ok": False, "reason": "not classified yet"}
        parts.append({"id": p["id"], "km": round(p["distance"] or 0, 2),
                      "min": round((p["duration"] or 0) / 60), "read": read})
        if read.get("ok"):
            kinds.append(read["kind"])
            strides += read.get("strides") or 0
            n_work += read.get("n_work") or 0
    oks = [e["read"] for e in parts if e["read"].get("ok")]
    if not oks:
        return None
    kind = next((k for k in SJ_KIND_PRIORITY if k in kinds), "easy")
    labels = [r.get("kind_label") or "" for r in oks]
    kind_label = labels[0] + "".join(f" + {l.lower()}" for l in labels[1:] if l)
    return {"v": STRUCT_VERSION, "ok": True, "composite": True, "n_parts": len(parts),
            "kind": kind, "kind_label": kind_label,
            "summary": " · then ".join(r["summary"] for r in oks),
            "strides": strides, "n_work": n_work,
            "confidence": "rough" if any(r.get("confidence") == "rough" for r in oks) else "good",
            "km": round(sum(e["km"] for e in parts), 1), "parts": parts}


def _sq_read(db, reps, n, sets, pace, date_iso):
    """§SQ — the strides EXECUTION read: how many vs prescribed, the strides-only pace, and HR as
    RESPONSE — the post-stride peak (a 15–20s stride's cardiac peak lands in the recovery: the
    §RD HR-lag rule, so HR is never the effort verdict; pace is the trick) and the recovery floor
    between reps (a creeping floor = rest ran short). Count verdict vs the prescribed set band:
    `strides: S` prescribes S sets of 4–6. Display/verdict only — no training lever consumes this."""
    out = {"n": n, "sets": sets, "pace": pace}
    # §PRO12 — resolved across plan history, so a road that has moved past this date still knows
    # what it prescribed (the latest road alone silently drops the count verdict).
    laid = _laid_sessions(db, date_iso, (_date(date_iso) + timedelta(days=1)).isoformat())
    presc = int(laid[date_iso]["strides"]) if laid.get(date_iso, {}).get("strides") else None
    if presc:
        lo, hi = 4 * presc, 6 * presc
        out["prescribed"] = [lo, hi]
        out["count_verdict"] = "on" if lo <= n <= hi else ("short" if n < lo else "over")
    peaks = [r["hr_peak"] for r in reps if r.get("hr_peak")]
    floors = [r["hr_rec"] for r in reps if r.get("hr_rec")]
    if peaks:
        out["hr_peak_first"], out["hr_peak_last"] = peaks[0], peaks[-1]
    if floors:
        out["hr_floor_first"], out["hr_floor_last"] = floors[0], floors[-1]
        out["recovered"] = (floors[-1] - floors[0]) <= 10
    bits = [f"{n}× strides" + (f" ({'+'.join(map(str, sets))})" if len(sets) > 1 else "")
            + (f" @{fmt_pace(pace)}/km" if pace else "")]
    if out.get("prescribed"):
        lo, hi = out["prescribed"]
        bits.append({"on": f"count on target ({lo}–{hi})",
                     "short": f"{n} of {lo}–{hi} prescribed",
                     "over": f"{n} vs {lo}–{hi} prescribed"}[out["count_verdict"]])
    if peaks:
        bits.append(f"HR peaks {peaks[0]}→{peaks[-1]}")
    if floors:
        bits.append(f"recovery floor {floors[0]}→{floors[-1]}"
                    + ("" if out.get("recovered", True) else " — creeping, rest ran short"))
    out["line"] = " · ".join(bits)
    return out


def _strip_structure_hr(st):
    """§H7 — remove every HR field from a structure read (segments, §SQ stride_reps + narrative),
    recursing into §SJ composite parts. The pace-based label is as public as pace; HR is not."""
    if not st:
        return st
    out = {**st}
    if out.get("segments"):
        out["segments"] = [{k: v for k, v in s.items() if k != "hr"} for s in out["segments"]]
    if out.get("stride_reps"):
        out["stride_reps"] = [{k: v for k, v in r.items() if not k.startswith("hr")}
                              for r in out["stride_reps"]]
    if out.get("sq"):
        sq = {k: v for k, v in out["sq"].items() if not k.startswith("hr") and k != "recovered"}
        if sq.get("line"):
            sq["line"] = " · ".join(b for b in sq["line"].split(" · ")
                                    if not b.startswith(("HR peaks", "recovery floor")))
        out["sq"] = sq
    if out.get("parts"):
        out["parts"] = [{**e, "read": _strip_structure_hr(e.get("read"))} for e in out["parts"]]
    return out


def _match_prescriptions(run_dates, prescribed, match_days=EFFORT_MATCH_DAYS):
    """Assign each run the kind of the plan session it belongs to (§6m). The runner doesn't always run a
    session on its prescribed calendar day — they anticipate or postpone by a day or two. Exact-date logic
    mis-reads that: an anticipated tempo lands on a rest day, defaults to 'easy', and gets flagged 'hot'
    against the wrong band, while its real prescription shows as a silent miss. So:
      • an exact-date prescription always wins (unambiguous — the runner kept the calendar);
      • a run on a day with NO prescription adopts the NEAREST still-unclaimed session within ±match_days
        (the same nearest-match posture §6s uses for race day);
      • each prescription is claimed by at most ONE run (closest wins, deterministic tie-break), so two
        runs can't both inherit one session and a moved session is matched once;
      • a run with nothing in range falls back to 'easy' (the polarized default).
    A REST day is not a session and is dropped before any of this. It was being claimed like one, so
    a run taken on a rest day inherited kind 'rest' and graded `too_easy` — a sentence that makes no
    sense about a run (it was never asked for, so it cannot have been run too gently), and it also
    consumed the slot, distorting the nearest-match for its neighbours. Dropped, such a run falls to
    the 'easy' default and is judged on the easy bar: the standard it WOULD have been held to had it
    been prescribed, which is the honest comparison for an unasked-for extra run.

    Pure function over date strings → list of kinds aligned to `run_dates` (for testability)."""
    from datetime import date as _date
    out = [None] * len(run_dates)
    prescribed = [(pd, pk) for pd, pk in prescribed if pk not in NON_SESSION_KINDS]
    by_date = {}
    for i, (pd, pk) in enumerate(prescribed):
        by_date.setdefault(pd, []).append(i)
    consumed = set()
    for ri, rd in enumerate(run_dates):                 # pass 1 — exact-date matches consume their session
        free = [pi for pi in by_date.get(rd, []) if pi not in consumed]
        if free:
            out[ri] = prescribed[free[0]][1]
            consumed.add(free[0])
    pairs = []                                          # pass 2 — nearest match for runs on unprescribed days
    for ri, rd in enumerate(run_dates):
        if out[ri] is not None:
            continue
        rdd = _date.fromisoformat(rd)                   # hoisted — constant across the inner loop
        for pi, (pd, pk) in enumerate(prescribed):
            if pi in consumed:
                continue
            dist = abs((rdd - _date.fromisoformat(pd)).days)
            if dist <= match_days:
                pairs.append((dist, pd, ri, pi))
    pairs.sort()                                        # closest first; ISO date + indices = deterministic
    for dist, pd, ri, pi in pairs:
        if out[ri] is not None or pi in consumed:
            continue
        out[ri] = prescribed[pi][1]
        consumed.add(pi)
    return [k or "easy" for k in out]


def effort_discipline(db, window_days=EFFORT_WINDOW_DAYS, public=False):
    """Per-run effort vs prescription over the recent window (§6m). Each run's prescribed kind comes
    from the saved plan (frozen past weeks included); an unplanned run defaults to 'easy' (the
    polarized expectation), and the easy-discipline SCORE is the headline (his easy days run hard).

    PRIVATE (default): the HR-led read — HR fraction gates, Training Effect corroborates, GAP is a
    terrain-fair pace, subjective_feeling + decoupling as context.
    PUBLIC (`public=True`, the read-only showcase): SANITIZED — no heart rate, TE, or feeling reach the
    open box (the same posture that drops per-run HR from the activity payload, §H7). Runs are judged on
    grade-adjusted PACE vs the easy-pace ceiling instead; the score is the public, conservative read."""
    from datetime import timedelta
    hrmax = None if public else _robust_hrmax(db)
    # Anchor the easy/hard ceilings on a DERIVED LTHR when it's trustworthy (sharper at the
    # easy↔threshold turnpoint); otherwise fall back byte-for-byte to today's %HRmax read.
    lthr_info = None if public else derive_lthr(db)
    use_lthr = bool(lthr_info and lthr_info.get("source") in LTHR_TRUSTED
                    and lthr_info.get("confidence") in LTHR_MIN_CONFIDENCE)
    hz_cut = None if public else (hr_zones(db) or {}).get("cutoffs")   # §RD — rep-band grid
    snap = latest_snapshot(db)
    zones = pace_zones(snap["effective_vo2max"]) if snap else {}
    since = (datetime.now().date() - timedelta(days=window_days)).isoformat()
    drop = dropped_ids(db)
    # §PRO12 — across PLAN HISTORY, not the latest road: a road that has advanced past these dates
    # (a re-base re-anchor) would otherwise hide the very quality sessions this monitor must exclude
    # from the easy score, re-grading them against the easy bar.
    prescribed = [(d, s["kind"]) for d, s in _laid_sessions(db, since).items() if s.get("kind")]
    rows = [r for r in db.execute(
        "SELECT id, date, date_time, distance, duration, elapsed_time, hr_avg, raw FROM activities "
        "WHERE " + RUN_FAMILY_SQL + " AND date>=? ORDER BY date DESC", (since,)).fetchall()
        if not (r["id"] in drop or not r["distance"])]
    # §SJ — group deliberately-split recordings into logical sessions BEFORE matching: two same-day
    # parts used to fight over the day's prescription, the loser "rescheduling" onto a neighbouring
    # day's quality session (the Wed-steal), and the ≥2 km junk floor now tests the SESSION, not the
    # part — a 1.5 km strides part is the analysable half of a 1+1, not noise.
    groups = [g for g in _session_groups(rows)
              if sum((p["distance"] or 0) for p in g) >= 2]
    groups.sort(key=lambda g: g[0]["date"], reverse=True)      # the panel's date-DESC order
    matched = _match_prescriptions([g[0]["date"] for g in groups], prescribed)
    runs = []
    for g, kind in zip(groups, matched):
        r = g[0]
        km_total = sum((p["distance"] or 0) for p in g)
        if len(g) == 1:
            raw = json.loads(r["raw"] or "{}")
            gap = raw.get("gap")                          # Runalyze grade-adjusted speed (km/h)
            gap_pace = (round(3600.0 / gap) if gap else
                        (round(r["duration"] / r["distance"]) if r["duration"] else None))
            hr_avg = r["hr_avg"]
        else:
            # §SJ multi-part: the easy-discipline verdict judges the session's aerobic BODY —
            # duration-weighted HR/GAP over the parts whose cached read is easy/long. The strides/
            # quality part's numbers stay out of the easy read (the entire point of the owner's
            # split-recording workflow); its reps are graded by the per-rep read below. Preference
            # order read-aerobic → unread → all: a still-unclassified addendum must not tilt the
            # verdict while a read body exists (it converges to exclusion once read anyway).
            parts = []
            for p in g:
                st, _e = _structure_cached(db, p["id"], fetch=False)
                parts.append((p, st.get("kind") if (st and st.get("ok")) else None))
            body = ([p for p, pk in parts if pk in AEROBIC_KINDS]
                    or [p for p, pk in parts if pk is None] or [p for p, _ in parts])
            r = max(body, key=lambda p: p["duration"] or 0)   # context (TE/feeling) from the body
            hrs = [(p["hr_avg"], p["duration"] or 0) for p in body if p["hr_avg"]]
            hr_avg = round(sum(h * w for h, w in hrs) / sum(w for _, w in hrs)) if hrs else None

            def _part_pace(p):
                pg = json.loads(p["raw"] or "{}").get("gap")
                return ((3600.0 / pg) if pg else
                        ((p["duration"] / p["distance"]) if p["duration"] and p["distance"] else None))
            pp = [(v, p["duration"] or 0) for p in body for v in [_part_pace(p)] if v]
            gap_pace = round(sum(v * w for v, w in pp) / sum(w for _, w in pp)) if pp else None
            raw = json.loads(r["raw"] or "{}")
        if public:
            if not gap_pace:                              # pace-judged → needs a pace
                continue
            row_pub = {"date": g[0]["date"], "km": round(km_total, 1), "kind": kind,
                       "gap_pace": gap_pace, "verdict": _effort_verdict_pace(kind, gap_pace, zones)}
            if len(g) > 1:
                row_pub["joined"] = len(g)
            runs.append(row_pub)
        else:
            if not hr_avg:                                # HR-judged → needs HR
                continue
            te = raw.get("fit_training_effect")
            # §3.4 verdict switch — the PACE-vs-LT1 cross-check on the MOVING, fitness-tracking LT1 bar
            # (the §6.3 anchor), computed for every run so the monitor visibly reads against LT1, not a
            # fixed %HRmax. HR stays the PRIMARY easiness truth WHERE a trustworthy (moving) LTHR exists —
            # his own data shows HR is the honest read of easiness (low HR = easy whatever the pace) and a
            # naive flip to pace-primary would over-police a detrained rebuild (the reconciled §3.4 finding).
            pace_verdict = _effort_verdict_pace(kind, gap_pace, zones, ceiling_key="lt1")
            if use_lthr:                                  # HR-led on the MOVING LTHR (Friel %LTHR ceilings)
                verdict, conf = _effort_verdict(kind, hr_avg / lthr_info["lthr"], te,
                                                LTHR_EASY_FRAC, LTHR_HARD_FRAC)
            elif pace_verdict != "unknown":               # no trustworthy LTHR ⇒ the moving pace-LT1 bar
                verdict, conf = pace_verdict, "moderate"   #   (RETIRES the fixed %HRmax fallback, §3.4)
                # SAFETY CATCH (§3.4 fix 2026-07-01) — pace-easy can NEVER override a genuine HR redline.
                # Mild cardiac decoupling (easy pace, HR merely elevated 78–85%) is deliberately NOT policed
                # — the reconciled finding, and why HR isn't primary here. But an easy-PACED run whose HR sat
                # at THRESHOLD+ effort (≥ the hard bar) was hard on the honest axis (TRIMP already scores it
                # so), so it can't read easy/hot — else the monitor tells a detrained returner his redline was
                # fine. Retiring the fixed %HRmax bar dropped this catch; restore it as a one-way escalation.
                if hrmax and verdict in ("too_easy", "on", "hot") and hr_avg / hrmax >= HARD_HR_FRAC:
                    verdict, conf = "too_hard", "moderate"
            else:                                         # no pace either ⇒ last-resort %HRmax cross-check
                hrf = (hr_avg / hrmax) if hrmax else None
                verdict, conf = _effort_verdict(kind, hrf, te)
            # §RD — per-rep quality read: with the detected structure cached (sync/tile already
            # classified it — CACHED-ONLY here, a panel load never fans out into stream fetches),
            # grade the work reps against the prescribed zone. HR-led on the unified hr_zones grid;
            # pace vs the zone target when the segments carry no HR. Replaces the whole-run 'low'
            # read with a 'moderate' one that actually isolates the reps.
            seg = None
            if kind not in AEROBIC_KINDS:
                works = []                                # §SJ — reps live in whichever PART ran them
                for p in g:
                    stp, _e = _structure_cached(db, p["id"], fetch=False)
                    if stp and stp.get("ok"):
                        works += [s for s in stp.get("segments", []) if s.get("role") == "work"]
                if works:
                    zone = KIND_ZONE.get(kind, "threshold")
                    wsec = sum(s["sec"] for s in works)
                    wpace = round(sum(s["pace"] * s["sec"] for s in works) / wsec)
                    whr_s = [s for s in works if s.get("hr")]
                    v2 = whr = None
                    if whr_s:                                 # reported either way (context)
                        whr = sum(s["hr"] * s["sec"] for s in whr_s) / sum(s["sec"] for s in whr_s)
                    # HR judges only reps long enough for HR to be IN PHASE: a short rep starts
                    # rested and its HR peak lands in the recovery, so the within-rep average
                    # under-reads — short reps are judged on pace (EFFORT_SEG_HR_MIN_S).
                    if whr and hz_cut and wsec / len(works) >= EFFORT_SEG_HR_MIN_S:
                        lo, hi = _seg_band(hz_cut, zone)
                        v2 = ("too_easy" if (lo and whr < lo - EFFORT_SEG_TOL) else
                              "too_hard" if (hi and whr > hi + EFFORT_SEG_TOL) else "on")
                    elif zones.get(zone):
                        dev = math.log(wpace / zones[zone])
                        v2 = ("on" if abs(dev) <= EFFORT_SEG_PACE_TOL else
                              "too_easy" if dev > 0 else "too_hard")
                    if v2:
                        verdict, conf = v2, "moderate"
                        seg = {"n_work": len(works), "work_hr": round(whr) if whr else None,
                               "work_pace": fmt_pace(wpace), "zone": zone}
            row_out = {"date": g[0]["date"], "km": round(km_total, 1), "kind": kind,
                       "hr_avg": hr_avg,
                       "hr_pct": round(hr_avg / hrmax * 100) if hrmax else None,
                       "gap_pace": gap_pace, "te": te, "feeling": raw.get("subjective_feeling"),
                       "decoupling": raw.get("aerobic_decoupling_pace"),    # context only (units TBD)
                       "verdict": verdict, "confidence": conf, "pace_verdict": pace_verdict}
            if len(g) > 1:                                # §SJ — surface the join (UI chip + honesty)
                row_out["joined"] = len(g)
            if seg:
                row_out["seg_read"] = True
                row_out["seg"] = seg
            runs.append(row_out)
    aerobic = [x for x in runs if x["kind"] in AEROBIC_KINDS]
    quality = [x for x in runs if x["kind"] not in AEROBIC_KINDS]
    on = sum(1 for x in aerobic if x["verdict"] == "on")
    out = {
        "window_days": window_days, "public": public,
        "easy_score": round(100 * on / len(aerobic)) if aerobic else None,
        "easy_counts": {"judged": len(aerobic), "on": on,
                        "hot": sum(1 for x in aerobic if x["verdict"] == "hot"),
                        "too_hard": sum(1 for x in aerobic if x["verdict"] == "too_hard")},
        "quality_counts": {"judged": len(quality),
                           "too_easy": sum(1 for x in quality if x["verdict"] == "too_easy")},
        "runs": runs,
    }
    if public:
        out["easy_pace_ceiling"] = fmt_pace(zones["easy_top"]) if zones.get("easy_top") else None
    else:
        out["hrmax"] = hrmax
        if use_lthr:
            out["anchor"] = "lthr"
            out["lthr"] = lthr_info["lthr"]
            out["lthr_confidence"] = lthr_info["confidence"]
            out["easy_hr_ceiling"] = round(LTHR_EASY_FRAC * lthr_info["lthr"])
        elif zones.get("lt1"):
            # §3.4 verdict switch — no trustworthy LTHR but a VO2max snapshot exists ⇒ the PRIMARY easy bar
            # is the moving pace-LT1 (Davis), NOT the old fixed %HRmax; %HRmax is kept as a context cross-check.
            out["anchor"] = "lt1_pace"
            out["easy_pace_ceiling"] = fmt_pace(zones["lt1"])
            out["easy_hr_ceiling"] = round(EASY_HR_FRAC * hrmax) if hrmax else None
        else:
            # last resort: no trustworthy LTHR AND no VO2max snapshot for a pace bar ⇒ the %HRmax read
            out["anchor"] = "hrmax"
            out["easy_hr_ceiling"] = round(EASY_HR_FRAC * hrmax) if hrmax else None
        # §3.4 — the moving, PACE-anchored LT1 the easy bar sits under (Davis; HR above is the cross-check).
        out["lt1"] = lt1(db)
    return out


PACE_HR_OVER_FRAC = 0.5     # ≥ this share of easy-PACED runs landing over the easy HR ceiling ⇒ the
#                             two models disagree (his easy pace is ahead of his aerobic fitness)
PACE_HR_MIN_RUNS = 3        # need at least this many easy-paced runs with HR to judge coherence


def pace_hr_coherence(db, window_days=EFFORT_WINDOW_DAYS):
    """Cross-check the app's TWO intensity models for internal consistency — the seam the engine never
    closed. The plan PRESCRIBES effort as pace (VO2max → Daniels VDOT); the monitor JUDGES it by HR
    (LTHR-anchored, %HRmax fallback). They're independent fitness estimates that SHOULD agree: running at
    the easy-pace ceiling should keep HR under the easy-HR ceiling. They diverge most under cardiac
    decoupling — a detrained athlete's given easy pace drives a HIGHER HR than VDOT predicts — i.e. the
    divergence is largest exactly for the post-illness restart this app serves.

    This SURFACES the divergence as a diagnostic; it does NOT touch the prescription (feeding it back into
    the engine would be a separate, deliberate slice). Pure read, private (uses HR). Returns:
      {ok, verdict, n_easy_paced, n_hr_over, frac_over, easy_pace_ceiling, easy_hr_ceiling, anchor, note}
      verdict: 'coherent' | 'pace_ahead_of_hr' | 'insufficient' | 'no_model'."""
    from datetime import timedelta
    snap = latest_snapshot(db)
    zones = pace_zones(snap["effective_vo2max"]) if snap else {}
    easy_top = zones.get("easy_top")                       # sec/km (larger = slower)
    lthr_info = derive_lthr(db)
    use_lthr = (lthr_info.get("source") in LTHR_TRUSTED and lthr_info.get("confidence") in LTHR_MIN_CONFIDENCE)
    hrmax = _robust_hrmax(db)
    if use_lthr:
        easy_hr_ceiling, anchor = round(LTHR_EASY_FRAC * lthr_info["lthr"]), "lthr"
    elif hrmax:
        easy_hr_ceiling, anchor = round(EASY_HR_FRAC * hrmax), "hrmax"
    else:
        easy_hr_ceiling, anchor = None, None
    if not easy_top or not easy_hr_ceiling:
        return {"ok": False, "verdict": "no_model", "easy_pace_ceiling": easy_top,
                "easy_hr_ceiling": easy_hr_ceiling, "anchor": anchor,
                "note": "need both a pace zone (VO2max snapshot) and an HR ceiling"}
    since = (datetime.now().date() - timedelta(days=window_days)).isoformat()
    drop = dropped_ids(db)
    rows = [r for r in db.execute(
        "SELECT id, date, distance, duration, hr_avg, raw FROM activities WHERE " + RUN_FAMILY_SQL +
        " AND date>=? AND hr_avg IS NOT NULL ORDER BY date DESC", (since,)).fetchall()
        if not (r["id"] in drop or not r["distance"] or r["distance"] < 2)]
    n_easy_paced = n_hr_over = 0
    for r in rows:
        raw = json.loads(r["raw"] or "{}")
        gap = raw.get("gap")                              # grade-adjusted speed (km/h), terrain-fair
        gap_pace = (round(3600.0 / gap) if gap else
                    (round(r["duration"] / r["distance"]) if r["duration"] else None))
        if not gap_pace:
            continue
        if gap_pace >= easy_top * (1 - EASY_PACE_GRACE):  # ran AT or slower than the easy-pace ceiling
            n_easy_paced += 1
            if r["hr_avg"] > easy_hr_ceiling:
                n_hr_over += 1
    frac_over = round(n_hr_over / n_easy_paced, 2) if n_easy_paced else None
    if n_easy_paced < PACE_HR_MIN_RUNS:
        verdict = "insufficient"
    elif frac_over >= PACE_HR_OVER_FRAC:
        verdict = "pace_ahead_of_hr"
    else:
        verdict = "coherent"
    note = {
        "coherent": "Easy pace keeps HR under the easy ceiling — the pace and HR models agree.",
        "pace_ahead_of_hr": "Easy-paced runs are landing above the easy HR ceiling: your easy pace is ahead "
                            "of your current aerobic fitness (cardiac decoupling). Trust HR on easy days.",
        "insufficient": "Not enough easy-paced runs with HR in the window to judge coherence.",
    }[verdict]
    return {"ok": True, "verdict": verdict, "n_easy_paced": n_easy_paced, "n_hr_over": n_hr_over,
            "frac_over": frac_over, "easy_pace_ceiling": easy_top, "easy_hr_ceiling": easy_hr_ceiling,
            "anchor": anchor, "note": note}


def lt1(db, today=None):
    """§3.4 (ENGINE_SCIENCE.md §3.4) — the fitness-tracking LT1 (aerobic threshold): the PACE-anchored easy
    bar, per the §6.3 decision that pace is the intensity anchor and HR the cross-check. Davis: LT1 ≈ 80% of
    5k pace, and easy runs must sit BELOW LT1. Two anchors, reconciled:
      • PACE (primary): LT1 velocity = LT1_5K_FRAC × (V5K_VVO2MAX_FRAC × vVO2max), off the CURRENT effective
        VO2max — so the bar MOVES with fitness (a detrained rebuild gets a SLOWER LT1, never a stale fast one).
      • HR (cross-check): the derived-LTHR easy ceiling (LTHR_EASY_FRAC × LTHR) when LTHR is trustworthy.
    Also flags DETRAINED (pace ahead of HR — cardiac decoupling): on a rebuild his easy runs sit a touch above
    LT1, which is NORMAL and self-corrects, so we DON'T over-police it (the reconciled §3.4 finding — trust
    HR/effort on easy days then). Read-only; carries HR ⇒ PRIVATE. Does NOT change any prescription or the
    effort verdict — it's the surfaced, moving easy-bar reference the monitor reads against."""
    snap = latest_snapshot(db)
    zones = pace_zones(snap["effective_vo2max"]) if snap else {}
    lt1_pace, p5k = zones.get("lt1"), zones.get("p5k")     # sec/km (larger = slower; easy sits BELOW LT1)
    info = derive_lthr(db, today=today)
    use_lthr = info.get("source") in LTHR_TRUSTED and info.get("confidence") in LTHR_MIN_CONFIDENCE
    hr_ceiling = round(LTHR_EASY_FRAC * info["lthr"]) if (use_lthr and info.get("lthr")) else None
    coh = pace_hr_coherence(db)                            # the existing pace-vs-HR consistency diagnostic
    detrained = coh.get("verdict") == "pace_ahead_of_hr"
    if not lt1_pace:
        return {"ok": False, "reason": "no VO2max snapshot to derive an LT1 pace",
                "hr_easy_ceiling": hr_ceiling}
    note = ("LT1 (aerobic threshold) is your easy-day ceiling — keep easy runs SLOWER than this. It's "
            "≈80% of your 5k pace and tracks your current fitness; HR is the cross-check.")
    if detrained:
        note += (" Right now your easy pace is a touch faster than LT1 for the heart rate it costs (cardiac "
                 "decoupling during the rebuild) — normal, and it self-corrects as fitness returns, so trust "
                 "HR/effort on easy days; we don't police easy pace here.")
    return {
        "ok": True,
        "lt1_pace": lt1_pace, "lt1_pace_fmt": fmt_pace(lt1_pace),
        "p5k_pace": p5k, "p5k_pace_fmt": fmt_pace(p5k), "lt1_5k_frac": LT1_5K_FRAC,
        "vo2max": snap["effective_vo2max"] if snap else None,
        "hr": {"anchor": ("lthr" if use_lthr else None), "easy_ceiling": hr_ceiling,
               "lthr": info.get("lthr") if use_lthr else None, "confidence": info.get("confidence"),
               "source": info.get("source"), "age_days": info.get("age_days")},
        "agreement": coh.get("verdict"), "detrained": detrained, "note": note,
        "tt_offer": lthr_tt_offer(db, today=today, _lthr_info=info),
    }


def lthr_tt_offer(db, today=None, _lthr_info=None):
    """§HR slice #2, guardrail #1 — may the app SUGGEST the 30-min LTHR field test? A TT is a
    near-maximal effort; the whole safety arch (readiness gate, conservative restart) exists because
    of a real exertional event, so the suggestion is gated on EVERY clearance holding:
      • the regime is ASSERTIVE (fitness established — never prompt a max test during the restart;
        read from the STORED plan, the regime the dashboard actually shows);
      • no active medical hold, and the latest check-in is clean (no stop-symptom, not heavy);
      • the current threshold anchor is actually improvable (confidence below high).
    Pure read; returns {offer, held_because, confidence, source}. `held_because` names every failed
    clearance so the UI/manual can say WHY it isn't being offered (never a silent gate)."""
    info = _lthr_info or derive_lthr(db, today=today)
    held = []
    if info.get("confidence") == "high":
        held.append("threshold anchor is already high-confidence")
    try:
        row = db.execute("SELECT plan FROM plans ORDER BY created_at DESC LIMIT 1").fetchone()
        mode = ((json.loads(row["plan"]) or {}).get("regime") or {}).get("mode") if row else None
    except (sqlite3.OperationalError, ValueError, TypeError):
        mode = None
    if mode != "assertive":
        held.append("regime is conservative — no max-effort test during the restart")
    try:
        if active_medical_halt(db):
            held.append("medical hold in force")
        rd = db.execute("SELECT energy, stop_symptom FROM readiness ORDER BY date DESC LIMIT 1").fetchone()
        if rd and (rd["stop_symptom"] or rd["energy"] == "heavy"):
            held.append("readiness is not green")
    except sqlite3.OperationalError:
        held.append("readiness unknown")
    return {"offer": not held, "held_because": held or None,
            "confidence": info.get("confidence"), "source": info.get("source")}


# ── Per-run metrics table + self-re-running analysis (the feel/heat/load data foundation) ────────
# The `run_metrics` VIEW (see RUN_METRICS_VIEW) is the queryable per-run table. These read it and run
# the same deep-dive that produced the design direction, so the findings refresh as data accrues. The
# honest result on the current data: ACCUMULATED FATIGUE (ATL/ACWR), not heat, is the dominant
# correlate of efficiency — and the day-to-day swing at FIXED temperature already exceeds a clean 5°
# heat step, so heat can't yet be isolated. We surface that, we don't bake a noisy coefficient into a
# feature: the robust signal is the same-route paired contrast, the rho's stay flagged as exploratory.

def run_metrics(db, route_id=None, days=None, limit=None, with_projection=True):
    """Rows from the run_metrics view (newest first), optionally filtered to one recurring route, a
    recency window, and/or a row cap. Pure read; HR/health-derived → callers must keep it private.

    with_projection (default on) backfills ctl_proj/atl_proj/acwr_proj from the projector's
    reconstructed EWMA curve (reconstruct_history) — modeled, NOT Runalyze's authoritative values, but
    available for EVERY run instead of only the ~7 days shape_snapshots covers (Runalyze's API exposes
    only TODAY's shape; there's no history endpoint). The projector is validated against Runalyze by
    det/projector-validation. Computed on the fly (never materialised) so it can't drift from activities."""
    sql = "SELECT * FROM run_metrics"
    where, args = [], []
    if route_id is not None:
        where.append("route_id = ?"); args.append(route_id)
    if days is not None:
        from datetime import timedelta
        since = (datetime.now().date() - timedelta(days=int(days))).isoformat()
        where.append("date >= ?"); args.append(since)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY date DESC"
    if limit is not None:
        sql += " LIMIT ?"; args.append(int(limit))
    rows = [dict(r) for r in db.execute(sql, args).fetchall()]
    if with_projection and rows:
        proj = {h["date"]: h for h in reconstruct_history(db)}     # one reconstruction, keyed by date
        for r in rows:
            h = proj.get(r["date"])
            ctl = round(h["ctl"], 1) if h else None
            atl = round(h["atl"], 1) if h else None
            r["ctl_proj"] = ctl
            r["atl_proj"] = atl
            r["acwr_proj"] = round(atl / ctl, 2) if (ctl and atl is not None) else None
    return rows


def _spearman(pairs):
    """Spearman rho on a list of (x, y) with Nones already dropped. None if n<4 or no variance."""
    import math
    n = len(pairs)
    if n < 4:
        return None
    def ranks(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0.0] * len(vals)
        i = 0
        while i < len(vals):
            j = i
            while j + 1 < len(vals) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    xr, yr = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    mx, my = sum(xr) / n, sum(yr) / n
    num = sum((a - mx) * (b - my) for a, b in zip(xr, yr))
    den = math.sqrt(sum((a - mx) ** 2 for a in xr) * sum((b - my) ** 2 for b in yr))
    return round(num / den, 2) if den else None


def run_metrics_analysis(db):
    """Re-run the feel/heat/load deep-dive on whatever data exists now. Two tiers, by trustworthiness:
      • same_route_pairs — the ROBUST signal: consecutive runs on one recurring route (terrain held), with
        the Δtemp / Δhr_cost / Δatl / Δfeel between them. A heat effect is only real if it exceeds the
        Δhr_cost seen between SAME-temperature pairs (the noise floor).
      • exploratory_rho — Spearman of hr_cost vs candidate drivers. EXPLORATORY only: observational,
        season×fitness×temperature confounded. Association, never causation. Carried with that caveat.
    Fatigue (ATL/ACWR) uses the PROJECTOR-reconstructed columns (atl_proj/acwr_proj) so the correlation
    spans the FULL history (~1000 runs), not the ~7 days of Runalyze snapshots — modeled but validated.
    Returns the caveats inline so no consumer can read a coefficient as settled."""
    rows = run_metrics(db, with_projection=True)             # newest first, fatigue backfilled
    by_route = {}
    for r in rows:
        if r.get("route_id") is not None:
            by_route.setdefault(r["route_id"], []).append(r)

    def _d(a, b, k):                                          # b is earlier, a later (chronological Δ)
        if a.get(k) is None or b.get(k) is None:
            return None
        return round(a[k] - b[k], 2)

    from datetime import date as _date
    def _days(a, b):
        try:
            return (_date.fromisoformat(a) - _date.fromisoformat(b)).days
        except (ValueError, TypeError):
            return None
    pairs = []
    for rid, rs in by_route.items():
        rs = sorted(rs, key=lambda x: x["date"])             # chronological
        for earlier, later in zip(rs, rs[1:]):
            if later.get("hr_cost") is None or earlier.get("hr_cost") is None:
                continue
            pairs.append({
                "route_id": rid, "from": earlier["date"], "to": later["date"],
                "gap_days": _days(later["date"], earlier["date"]),   # a wide gap = fitness changed, not a clean contrast
                "d_temp": _d(later, earlier, "temp_c"),
                "d_hr_cost": _d(later, earlier, "hr_cost"),
                "d_hr_cost_gap": _d(later, earlier, "hr_cost_gap"),  # terrain-fair (GAP-normalised)
                "d_atl": _d(later, earlier, "atl_proj"),             # projector-backfilled ⇒ present for every pair
                "d_feel": _d(later, earlier, "feel"),
            })
    # The noise floor (day-to-day hr_cost swing at fixed temp) is only meaningful from NEAR-IN-TIME pairs —
    # a same-route revisit months later conflates fitness, so it can't tell us what "same conditions" scatter
    # looks like. Gate on a ≤2-week gap; that's the bar a real heat effect must clear to be credible.
    NEAR_DAYS = 14
    near = [p for p in pairs if p["gap_days"] is not None and p["gap_days"] <= NEAR_DAYS]
    same_temp = [abs(p["d_hr_cost"]) for p in near if p["d_temp"] == 0 and p["d_hr_cost"] is not None]
    noise_floor = round(sum(same_temp) / len(same_temp), 2) if same_temp else None

    # ── THE headline, and the only VALID powered test ──────────────────────────────────────────────
    # hr_cost is nonlinear across pace regimes, so a full-history Spearman of it is meaningless (fit-fast
    # 2024 vs detrained-slow 2026). The valid question asks it WITHIN a controlled comparison: same route
    # (terrain held), ≤14 days apart (fitness held) — i.e. on the Δ between a near pair. There the n=7
    # "fatigue dominates (ρ≈0.9)" coincidence and the cross-regime full-history number both dissolve into
    # the truth: heat and fatigue each move per-run efficiency only weakly, below the day-to-day noise.
    def _pair_rho(xk, yk="d_hr_cost"):
        pr = [(p[xk], p[yk]) for p in near if p.get(xk) is not None and p.get(yk) is not None]
        return {"rho": _spearman(pr), "n": len(pr)}
    controlled = {
        "d_temp_vs_d_hr_cost":  _pair_rho("d_temp"),
        "d_atl_vs_d_hr_cost":   _pair_rho("d_atl"),
        "d_temp_vs_d_hr_cost_gap": _pair_rho("d_temp", "d_hr_cost_gap"),
        "d_atl_vs_d_hr_cost_gap":  _pair_rho("d_atl", "d_hr_cost_gap"),
    }

    def _rho(xk, yk="hr_cost"):
        pr = [(r[xk], r[yk]) for r in rows if r.get(xk) is not None and r.get(yk) is not None]
        return {"rho": _spearman(pr), "n": len(pr)}

    # CROSS-REGIME, NOT VALID for hr_cost — kept only to show it differs from the controlled test above.
    cross_regime = {f"{k}_vs_hr_cost": _rho(k)
                    for k in ("temp_c", "atl_proj", "acwr_proj", "ctl_proj",
                              "humidity", "hrv_today", "elev_up")}
    with_load_proj = sum(1 for r in rows if r.get("atl_proj") is not None)
    with_load_snap = sum(1 for r in rows if r.get("atl_snapshot") is not None)
    return {
        "n_runs": len(rows),
        "n_with_load_proj": with_load_proj,
        "n_with_load_snapshot": with_load_snap,
        "same_route_pairs": sorted(pairs, key=lambda p: (p["to"]), reverse=True),
        "same_temp_noise_floor": noise_floor,
        "controlled_pairs_rho": controlled,           # ← the headline: powered AND valid
        "controlled_pairs_n": len(near),
        "durability": durability_signal(db),          # §3.3 Davis resilience — MEASURE-FIRST, read-only
        "cross_regime_rho": cross_regime,             # ← invalid for hr_cost; do not headline
        "caveats": [
            "Association, NOT causation — all of this is observational; the controlled test removes terrain "
            "and fitness confounds but can't prove cause.",
            "HEADLINE = controlled_pairs_rho: Spearman on the Δ between same-route runs ≤14 days apart "
            "(terrain held, fitness held). It's the ONLY test that's both powered and valid for hr_cost.",
            "cross_regime_rho (full-history) is NOT valid for hr_cost: hr/speed is nonlinear and the "
            "history spans fit-fast→detrained-slow regimes. Shown only to contrast with the controlled test. "
            "ctl_proj-vs-hr_cost there is also near-circular (both proxy aerobic fitness).",
            "The n=7 snapshot-window ρ≈0.9 for fatigue was an underpowered coincidence (one "
            "detrain-then-rebuild-in-heat stretch); it does not survive the controlled test.",
            f"Fatigue (atl_proj/acwr_proj/ctl_proj) is the PROJECTOR's reconstructed EWMA — modeled, not "
            f"Runalyze-authoritative — but validated vs Runalyze (det/projector-validation) and present for "
            f"{with_load_proj} of {len(rows)} runs; Runalyze's snapshots cover only {with_load_snap} "
            "(its API exposes today's shape only). eVO2 ground-truth stays snapshot-gated.",
            "A heat effect is credible only if a route's Δhr_cost across a temp step exceeds the "
            f"same-temperature noise floor ({noise_floor if noise_floor is not None else 'n/a'} hr_cost).",
            "hr_cost = hr/speed is nonlinear (penalises slow running); compare within a route, not across "
            "pace regimes. Raw hr + speed_kmh are kept for a better metric later.",
        ],
    }


# §3.3 (Davis durability / resilience — ENGINE_SCIENCE.md §3.3). Durability = how little running economy
# decays over a long run; the proxy is Runalyze's aerobic decoupling (the pace:HR drift, first half →
# second half). MEASURE-FIRST (🔵): we SURFACE the signal + a trend and accumulate cases — it governs NO
# prescription until his corpus shows it predicts his race fade (the heat-coefficient discipline, §0).
DURABILITY_MIN_KM = 16.0       # a run long enough for economy decay to manifest (durability is a long-run trait)
DURABILITY_GOOD_RAW = 500.0    # decoupling below this on a long run ≈ durable (economy held); Runalyze raw units
DURABILITY_HIGH_RAW = 1000.0   # above this ≈ notable economy decay over the distance
# Runalyze stores aerobic_decoupling_pace in raw units ≈ percentage ×100 (his median long-run ≈540 ≈5.4%; his
# last marathon ≈1740 ≈17.4% w/ feel=1) — the ×100→% is INFERRED (units officially TBD upstream), so the RAW
# value is the source of truth and % is only a reading aid.
DURABILITY_PCT_SCALE = 100.0


def durability_signal(db, recent_n=6):
    """§3.3 durability read — MEASURE-FIRST, read-only. Long-run aerobic decoupling as a resilience proxy:
    low = economy held over the distance (durable); high/rising = economy decaying (a durability limit).
    It SURFACES + trends + accumulates cases; it does NOT feed any prescription (earn it from his corpus
    first, like the heat coefficient). Decoupling rises with distance, so the read carries distance and is
    flagged exploratory. Decoupling/HR-derived → callers must keep it PRIVATE (rides /api/run-metrics)."""
    import statistics as _st
    rows = run_metrics(db, with_projection=False)      # newest first; projection not needed for this read
    longs = [r for r in rows
             if (r.get("km") or 0) >= DURABILITY_MIN_KM and r.get("decoupling") is not None]
    def _pct(v): return round(v / DURABILITY_PCT_SCALE, 1)
    series = [{"date": r["date"], "km": round(r["km"], 1), "decoupling_raw": round(r["decoupling"]),
               "decoupling_pct": _pct(r["decoupling"]), "hr": r["hr"], "feel": r["feel"]}
              for r in longs]
    if not series:
        return {"ok": False, "min_km": DURABILITY_MIN_KM,
                "reason": f"no long runs (≥{DURABILITY_MIN_KM:.0f}km) with decoupling yet"}
    def _med(ss, k): return round(_st.median([s[k] for s in ss]), 1)
    recent = series[:recent_n]
    prior = series[recent_n:recent_n * 2]
    med_recent = _med(recent, "decoupling_raw")
    med_prior = _med(prior, "decoupling_raw") if prior else None
    trend = None
    if med_prior is not None:
        # improving = decoupling DOWN over time — but only trust it if the distance mix held (decoupling
        # rises with distance, so a shift in the recent-vs-prior distance mix would masquerade as a trend).
        d = med_recent - med_prior
        dist_shift = abs(_med(recent, "km") - _med(prior, "km"))
        trend = ("distance mix shifted — trend unreliable" if dist_shift > 4
                 else "improving" if d < -100 else "declining" if d > 100 else "steady")
    verdict = ("durable" if med_recent < DURABILITY_GOOD_RAW
               else "high fade" if med_recent >= DURABILITY_HIGH_RAW else "moderate fade")
    return {
        "ok": True, "min_km": DURABILITY_MIN_KM, "n_long": len(series),
        "recent_median_raw": med_recent, "recent_median_pct": _pct(med_recent),
        "recent_median_km": _med(recent, "km"),
        "prior_median_raw": med_prior, "trend": trend, "verdict": verdict,
        "good_below_raw": DURABILITY_GOOD_RAW, "high_above_raw": DURABILITY_HIGH_RAW,
        "recent": recent,
        "caveats": [
            "MEASURE-FIRST: surfaced + accumulating, NOT feeding the plan (durability governs nothing until "
            "the corpus shows it predicts race fade — the heat-coefficient discipline).",
            "Durability = economy decay over a long run, proxied by aerobic decoupling (first→second half "
            "pace:HR drift). Lower = more durable.",
            "Decoupling RISES with distance, so compare like distances; the trend is void when the distance "
            "mix shifts. Exploratory.",
            "Decoupling units are Runalyze-raw (≈ percentage ×100, INFERRED — officially TBD upstream); the "
            "raw value is the source of truth, the % is a reading aid.",
        ],
    }


WORKED_EXAMPLE_LOOKBACK = 21   # days back to find a same-route peer (terrain held, fitness ~held)

def worked_example(db, activity_id=None):
    """Auto-build a CONTROLLED worked example for one run (default: the latest run with a route+hr_cost):
    the recent SAME-ROUTE runs (terrain held) + the directional deltas vs the nearest-in-time same-route
    peer (fitness ~held), and whether subjective feel diverged from the objective readiness markers.

    It records FACTS for a growing casebook — it deliberately does NOT adjudicate 'feel led' or score a
    composite readiness: a per-case verdict is an n=1 judgment, the exact artifact this session proved
    unreliable (the n=7 ρ≈0.9 that collapsed). The corpus earns conclusions later; here we store clean,
    directional cases. On the fly — no casebook table yet (the schema of what we'll tune on isn't known)."""
    from datetime import date as _d
    rows = run_metrics(db, with_projection=True)
    target = (next((r for r in rows if r["id"] == activity_id), None) if activity_id is not None
              else next((r for r in rows if r.get("hr_cost") is not None), None))
    if not target or target.get("route_id") is None or target.get("hr_cost") is None:
        return {"ok": False, "reason": "no run with a recurring route + hr_cost to anchor on"}
    td = _d.fromisoformat(target["date"])
    peers = [r for r in rows if r.get("route_id") == target["route_id"] and r["id"] != target["id"]
             and r.get("hr_cost") is not None
             and 0 < (td - _d.fromisoformat(r["date"])).days <= WORKED_EXAMPLE_LOOKBACK]
    if not peers:
        return {"ok": False, "date": target["date"], "route_id": target["route_id"],
                "reason": f"no same-route peer within {WORKED_EXAMPLE_LOOKBACK}d to control terrain "
                          "(an uncontrolled run — banked, not comparable)"}
    peers.sort(key=lambda r: r["date"], reverse=True)
    nearest = peers[0]                                   # nearest-in-time = cleanest fitness-held contrast

    keep = ("date", "temp_c", "hr", "speed_kmh", "hr_cost", "hr_cost_gap", "decoupling",
            "run_vo2max", "feel", "atl_proj", "acwr_proj", "hrv_today")
    def slim(r): return {k: r.get(k) for k in keep}
    def delta(k):
        a, b = target.get(k), nearest.get(k)
        return round(a - b, 2) if (a is not None and b is not None) else None
    deltas = {k: delta(k) for k in ("temp_c", "hr", "hr_cost", "hr_cost_gap", "feel",
                                    "decoupling", "run_vo2max", "atl_proj", "acwr_proj", "hrv_today")}

    def _sgn(x): return 0 if not x else (1 if x > 0 else -1)
    # objective readiness DIRECTION per marker (+1 = more ready than the peer). Kept per-marker, NOT
    # collapsed into a score (a composite would be another unvalidated model). ATL/ACWR lower = readier;
    # HRV higher = readier.
    obj_readiness = {
        "atl_proj":  -_sgn(deltas["atl_proj"]) if deltas["atl_proj"] is not None else None,
        "acwr_proj": -_sgn(deltas["acwr_proj"]) if deltas["acwr_proj"] is not None else None,
        "hrv_today":  _sgn(deltas["hrv_today"]) if deltas["hrv_today"] is not None else None,
    }
    feel_dir = _sgn(deltas["feel"]) if deltas["feel"] is not None else None
    # divergence = a FACT: feel pointed opposite to ≥1 objective readiness marker.
    opposed = [m for m, d in obj_readiness.items()
               if d is not None and feel_dir not in (None, 0) and _sgn(d) != feel_dir]
    diverged = (bool(opposed) if feel_dir not in (None, 0) else None)

    eff = ("better" if (deltas["hr_cost"] or 0) < 0 else "worse" if (deltas["hr_cost"] or 0) > 0 else "level")
    note = (f"Same route as {nearest['date']} ({(td - _d.fromisoformat(nearest['date'])).days}d earlier): "
            f"Δtemp {deltas['temp_c']}°, efficiency {eff} (Δhr_cost {deltas['hr_cost']}).")
    if diverged:
        note += (f" Feel moved {'up' if feel_dir > 0 else 'down'} while {', '.join(opposed)} pointed the "
                 "other way — subjective feel and the objective markers diverged this run.")
    return {
        "ok": True, "route_id": target["route_id"],
        "target": slim(target), "nearest_peer": slim(nearest),
        "context": [slim(r) for r in ([target] + peers[:3])],   # the same-route table, newest first
        "deltas_vs_nearest": deltas,
        "feel_direction": feel_dir,                # +1 better / -1 worse / 0 same / None if no feel
        "objective_readiness": obj_readiness,      # per-marker +1 readier / -1 less ready
        "feel_objective_diverged": diverged,       # the casebook fact, not a verdict
        "diverged_markers": opposed,
        "note": note,
        "caveat": "n=1 controlled observation for the casebook — directional facts only, no claim about "
                  "cause or which signal to trust; the corpus earns that, not any single run.",
    }


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
#                    crushed a week into 0.3–1.4km stubs): a governed budget too thin for the
#                    template's run count sheds DAYS instead. Mild by design — it fires only on
#                    sub-floor stubs, never grows a normal week's runs (the min-dose consolidation
#                    experiment stays REVERTED), and the ACWR/peak governors still project whatever
#                    layout results. Sits at his historical junk bar (~2.6km).

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
REBASE_LONG_CAP = 0.35     # pure-easy blocks (re-base) keep the original cap — leave the cautious restart untouched
LONG_RUN_MIN_KM = 4.0      # a "long run" the ACWR governor clips below this isn't functioning as a long run —
                           # relabel it a shakeout (never force load past the safety ceiling). See _mark_load_integrity.
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
                      # the "Peak" prefix is LOAD-BEARING: generate_block's §PRO6 deload exemption
                      # sniffs it (is_peak) — the peak rides into the taper, its recovery
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
    long_cap = LONG_RUN_MAX_FRAC if zones else REBASE_LONG_CAP
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
    min_tr = 0.0 if _is_taper(wk.get("intent")) else \
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


def _is_down(intent):
    """A week is a deliberate down/recovery week iff its intent text says so — uniform across every
    shape (re-base wk4, base/build 3:1). The single test the banking gates + the earned lift share."""
    return str(intent or "").lower().startswith("down")


def _is_taper(intent):
    """A taper or race week — deliberately low-volume by design. Its short long run is the plan
    working, not a fatigue cap, so the load-integrity honesty pass must NOT relabel/flag it."""
    t = str(intent or "").lower()
    return t.startswith("taper") or t.startswith("race week")


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
    if _is_down(intent) or _is_taper(intent):
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
            if assertive and not _is_taper(wk.get("intent")):
                _sd, _sp = _is_down(wk.get("intent")), \
                    str(wk.get("intent") or "").lower().startswith("peak")
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
                          LONG_RUN_MAX_FRAC if zones else REBASE_LONG_CAP)
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
                min_left = 0.0 if _is_taper(wk.get("intent")) else \
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
                     "intent_km": wk["km"], "adjusted": adjusted["touched"],
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
                if _is_down(wk.get("intent")) or _is_taper(wk.get("intent")):
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
        is_down = _is_down(wk.get("intent"))
        is_taper = _is_taper(wk.get("intent"))
        is_peak = str(wk.get("intent") or "").lower().startswith("peak")
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
                        if _is_down(shape[j].get("intent"))), None)
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
                "intent_km": wk["km"], "adjusted": adjusted["touched"], "clipped": clipped}
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
        band = ft.get("band") or {}
        err_log = math.log(actual_s / ft["seconds"])
        out = {"plan_id": r["id"], "for_date": r["for_date"],
               "p50_seconds": ft["seconds"], "p50_hms": ft.get("hms"),
               "actual_seconds": actual_s,
               "err_pct": round((math.exp(err_log) - 1) * 100, 1),
               "lo_hms": band.get("lo_hms"), "hi_hms": band.get("hi_hms"),
               "in_band": (band["lo_seconds"] <= actual_s <= band["hi_seconds"]
                           if band.get("lo_seconds") else None),
               "log_score": None}
        sig = band.get("sigma_log")
        if sig:
            out["log_score"] = round(0.5 * math.log(2 * math.pi * sig * sig)
                                     + err_log ** 2 / (2 * sig * sig), 3)
        return out
    return None


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

# §6s — post-race reckoning. Matches the race-day activity to the objective on the standard race
# distances (RACE_KM, defined once with the §FT1 speed axis that also solves against them — this
# used to be a second, identical literal here, so a distance added to one would silently not exist
# for the other), plus a best-effort goal-time parser (the `target` field is free-form: 'finish',
# '3:45', '42:00', 'sub-45'). H:MM vs MM:SS is disambiguated by race type (marathon/half = hours,
# 5k/10k = minutes); unparseable goals (incl. 'finish') return None, shown without a delta.
RECKON_WINDOW_WEEKS = 12   # §6s — how long after a race the scorecard keeps reckoning it


def _parse_goal_seconds(target, race_type):
    """Free-form goal string → seconds, or None if not a time ('finish', 'PB', unparseable)."""
    if not target:
        return None
    t = re.sub(r"^(sub-?|under\s*)", "", target.strip().lower()).strip()
    m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", t)
    if not m:
        if re.fullmatch(r"\d{1,3}", t) and race_type in ("5k", "10k"):
            return int(t) * 60                            # bare minutes for a short race ('sub-45' → 45:00)
        return None
    h_or_m, mid, sec = int(m.group(1)), int(m.group(2)), m.group(3)
    if sec is not None:                                   # H:MM:SS — unambiguous
        return h_or_m * 3600 + mid * 60 + int(sec)
    if race_type in ("marathon", "half"):                # H:MM for the long races
        return h_or_m * 3600 + mid * 60
    return h_or_m * 60 + mid                              # MM:SS for 5k/10k/custom


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


def _race_day_activity(db, race_date_iso, race_type):
    """The run that IS the race, with its status. Returns (row_or_None, status):
      • "finished" — a run within ±2 days whose distance is within 15% of the race distance. A race is a
        MAX effort, so when several near-distance runs qualify (a same-distance easy run can sit nearby
        for a short race) we pick nearest-date then FASTEST pace, not just nearest date.
      • "dnf" — no full-distance match, but a run ON race day that fell well short (≤75% of the distance):
        a did-not-finish, distinct from a missing sync.
      • (None, None) — DNS / not synced / a custom race with no standard distance to match on."""
    target_km = RACE_KM.get(race_type)
    if not target_km:
        return None, None
    rd = _date(race_date_iso)
    rows = db.execute(
        "SELECT id, date, date_time, distance, duration, elapsed_time FROM activities "
        "WHERE date BETWEEN ? AND ? AND " + RUN_FAMILY_SQL + " AND distance > 0 AND duration > 0",
        ((rd - timedelta(days=2)).isoformat(), (rd + timedelta(days=2)).isoformat())).fetchall()
    full = [r for r in rows if abs(r["distance"] - target_km) / target_km <= 0.15]
    if full:
        full.sort(key=lambda r: (abs((_date(r["date"]) - rd).days), r["duration"] / r["distance"]))
        return full[0], "finished"
    # §SJ fallback — a race recorded in CHUNKS (watch save mid-race + restart): no single row
    # matches, but a split-group's SUM does. Single-row match stays first — a split warm-up +
    # race day must resolve to the race part alone, never to warm-up+race summed.
    gfull = [g for g in _session_groups(rows) if len(g) > 1
             and abs(sum(p["distance"] for p in g) - target_km) / target_km <= 0.15]
    if gfull:
        gfull.sort(key=lambda g: (abs((_date(g[0]["date"]) - rd).days),
                                  sum(p["duration"] for p in g) / sum(p["distance"] for p in g)))
        g = gfull[0]
        big = max(g, key=lambda p: p["distance"])
        return {"id": big["id"], "date": g[0]["date"],
                "distance": round(sum(p["distance"] for p in g), 2),
                "duration": round(sum(p["duration"] for p in g)),
                # §33f-4 — carry a summed gun clock too, so a chunk-recorded race scores on the
                # same axis as a single-row one (parts without an elapsed contribute their moving time)
                "elapsed_time": round(sum(_race_seconds(p) for p in g))}, "finished"
    same_day_short = [r for r in rows if _date(r["date"]) == rd and r["distance"] <= target_km * 0.75]
    if same_day_short:
        return max(same_day_short, key=lambda r: r["distance"]), "dnf"   # how far they got
    return None, None


RACE_RESOLVE_GRACE_DAYS = 3   # §RL — sync lag allowance before a passed race with no run lapses


def resolve_passed_races(db, today=None):
    """§RL — the race lifecycle's missing transition: settle every 'upcoming' objective whose date has
    passed. Before this, a run race stayed 'upcoming' forever — dropped by select_chain (future-only)
    but never resolved, so the UI listed it as a goal and the schema's done/lapsed states were dead
    letters. Rules, from owned data only (`_race_day_activity`):
      • matched run (finished or dnf)      → 'done', outcome JSON records the result + goal comparison.
      • no match yet, within a grace window → left 'upcoming' (a missing sync isn't a DNS yet).
      • no match after the grace window     → 'lapsed' (matchable distance) — the race passed unrun;
        a 'custom' type has no distance to match on, so it settles 'done' with an unverified outcome
        rather than accusing the runner of skipping it.
    Idempotent and side-effect-bounded (only rows it transitions); returns the transitions. The plan
    itself is untouched — select_chain already ignores passed races, so plans stay byte-identical.
    Private-side only: the read-only mirror never writes."""
    if READONLY:
        return []
    today = today or datetime.now().date()
    if isinstance(today, str):
        today = _date(today)
    out = []
    rows = db.execute("SELECT * FROM objectives WHERE status='upcoming' AND date < ? "
                      "ORDER BY date", (today.isoformat(),)).fetchall()
    for o in rows:
        act, race_status = _race_day_activity(db, o["date"], o["type"])
        in_grace = (today - _date(o["date"])).days <= RACE_RESOLVE_GRACE_DAYS
        if act is None and race_status is None:
            if o["type"] in RACE_KM and in_grace:
                continue                       # matchable race, result may simply not be synced yet
            new_status = "done" if o["type"] not in RACE_KM else "lapsed"
            outcome = {"status": "unverified" if new_status == "done" else "unrun"}
        else:
            new_status = "done"
            goal_s = _parse_goal_seconds(o["target"], o["type"])
            actual_s = _race_seconds(act) if race_status == "finished" else None
            outcome = {"status": race_status, "activity_id": act["id"],
                       "actual_seconds": actual_s, "actual": _fmt_hms(actual_s),
                       "goal": o["target"], "goal_seconds": goal_s,
                       "beat": (None if (goal_s is None or actual_s is None) else actual_s <= goal_s),
                       "dnf_km": (round(act["distance"], 1) if race_status == "dnf" else None)}
            # §FT4 — score the engine's final pre-race prediction against the clock, into the
            # permanent record (the outcome JSON): the product's bet settles when the race does.
            pred = _ft_prediction_score(db, o["date"], o["type"], actual_s)
            if pred:
                outcome["prediction"] = pred
        db.execute("UPDATE objectives SET status=?, outcome=?, resolved_at=? WHERE id=?",
                   (new_status, json.dumps(outcome), _now_iso(), o["id"]))
        out.append({"id": o["id"], "label": o["label"], "date": o["date"],
                    "status": new_status, "outcome": outcome})
    # §FT4 backfill — races that resolved BEFORE the scoring hook existed get their prediction
    # settled retroactively (idempotent: only rows still missing one; the ledger was always there,
    # the score just hadn't been read out of it).
    for o in db.execute("SELECT id, date, type, outcome FROM objectives WHERE status='done'").fetchall():
        try:
            oc = json.loads(o["outcome"] or "{}")
        except (ValueError, TypeError):
            continue
        if oc.get("status") == "finished" and oc.get("actual_seconds") and "prediction" not in oc:
            pred = _ft_prediction_score(db, o["date"], o["type"], oc["actual_seconds"])
            if pred:
                oc["prediction"] = pred
                db.execute("UPDATE objectives SET outcome=? WHERE id=?", (json.dumps(oc), o["id"]))
                out.append({"id": o["id"], "date": o["date"], "status": "done",
                            "outcome": oc, "backfilled_prediction": True})
    if out:
        db.commit()
    return out


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
        if _is_down(w.get("intent")) or _is_taper(w.get("intent")):
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
                    nd = [w["km"] for w in block["weeks"] if not _is_down(w.get("intent"))]
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


def regenerate(db, baseline=None, today=None):
    """Regenerate the plan, save a new version, and return it with a diff. If `baseline` (a
    plan computed for today BEFORE the triggering change) is given, diff against it so only the
    change's own effect shows. Otherwise fall back to the last saved plan (a manual regenerate
    has no 'before' action to isolate).

    `today` overrides the clock for the WHOLE regeneration — the race settlement and the plan — so a
    fixture built around a fixed date is judged on that date throughout. Production never passes it
    (and then this is byte-for-byte what it always was); without it, `resolve_passed_races` settled
    races against the process's day while `generate_plan` was given none either, which is how a
    seeded fixture ended up half-pinned (det/clock-purity)."""
    resolve_passed_races(db, today)   # §RL — settle any race that has passed before re-reading objectives
    if baseline is None:
        prev = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
        baseline = json.loads(prev["plan"]) if prev else None
    plan = generate_plan(db, today=today)
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
_anthropic_gen = -1      # the config generation `_anthropic_client` was built for (TECH-4)


def _anthropic():
    """Lazy Anthropic client. Returns None (never raises) when the SDK isn't installed or no
    key is set, so the rest of the app is unaffected."""
    global _anthropic_client, _anthropic_gen
    cfg = config()
    if not cfg.anthropic_api_key:
        return None
    # TECH-4 — the client belongs to the generation whose key built it, so a key changed in the
    # Settings window takes effect on the next call rather than at the next restart.
    if _anthropic_client is None or _anthropic_gen != cfg.generation:
        try:
            import anthropic
            _anthropic_client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
            _anthropic_gen = cfg.generation
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
        return {"ok": False, "error": "AI features aren't set up — add a Claude API key in Settings"}
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
                              "hold — never above 1; the plan already ramps to the ACWR ceiling."},
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
                  "description": "A warm, specific one-or-two-sentence reply spoken to the runner. For "
                  "a pure reflection (no load change) this is the whole response — acknowledge what they "
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
    _ctx = config().athlete_context
    ctx_line = f"Athlete context: {_ctx}. " if _ctx else ""
    system = (
        "You read a runner's free-text status. Most days it's a REFLECTION on how a run felt "
        "(no change needed); sometimes it's a real signal to ease back. "
        f"Today is {today}. " + ctx_line + pace_line +
        "You can ONLY ease or hold load (volume_multiplier 0..1) and force easy effort; you CANNOT "
        "add load — the deterministic engine already ramps to the ACWR ceiling, so a positive "
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


def _save_adjustment(db, note, directive):
    """Persist `directive` as the active adjustment, honouring the §H3 dominant medical track:
      • a MEDICAL hold supersedes everything (any prior hold) and becomes the dominant active row;
      • a ROUTINE ease deactivates other ROUTINE rows but LEAVES a medical hold in force — applying a
        routine adjustment can never silently release a medical halt (only an explicit clear or a new
        medical hold does). One routine + at most one medical may be active at once; the medical row
        wins every read (active_adjustment / active_medical_halt)."""
    medical = 1 if directive.get("medical_flag") else 0
    cd = datetime.now().date().isoformat()   # §PRO3 — stamp the clear date as a hold leaves force
    if medical:
        db.execute("UPDATE adjustments SET active=0, cleared_at=COALESCE(cleared_at,?) WHERE active=1", (cd,))
    else:                                                                       # spare the hold
        db.execute("UPDATE adjustments SET active=0, cleared_at=COALESCE(cleared_at,?) "
                   "WHERE active=1 AND medical=0", (cd,))
    db.execute(
        "INSERT INTO adjustments (created_at, note, directive, applies_from, applies_until, active, medical) "
        "VALUES (?,?,?,?,?,1,?)",
        (_now_iso(), note, json.dumps(directive),
         directive["applies_from"], directive["applies_until"], medical))


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
        "fitness. " + (f"Athlete context: {config().athlete_context}. "
                       if config().athlete_context else "") +
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


def _phase_keys(plan):
    """The phase keys a plan actually carries, in road order, re-base excluded: the §6q keyed phases
    (chain segments like bridge1/peak1 included), or the classic four for a LEGACY saved plan whose
    phases carry no keys — the same fallback `_plan_all_weeks` applies."""
    keys = [ph.get("key") for ph in plan.get("phases", []) if ph.get("key") and ph.get("key") != "rebase"]
    return keys or ["base", "build", "peak", "taper"]


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
    # whole-road weeks (every phase, pk-tagged) — an assertive plan has NO re-base weeks, and the
    # explainer must narrate the road that exists, not an empty Phase 0
    weeks = [f"{w.get('pk','?')} wk{w['wk']}: {w['km']}km/{w['runs']} runs, end-ACWR~{w.get('proj_acwr')}"
             + (" [eased]" if w.get("adjusted") else "") + (" [clipped-to-ACWR]" if w.get("clipped") else "")
             + (" [frozen/done]" if w.get("frozen") else "")
             for w in _plan_all_weeks(plan)]
    # `bd7df91` (2026-07-04) moved the week list onto _plan_all_weeks and dropped this binding while the
    # re-base fields below kept reading it: every /api/plan/explain answered 502 "name 'rb' is not
    # defined" for seven weeks — invisible to the LLM-gated det on a keyless box (det/plan-summary now
    # builds this summary without a key). An assertive plan has no re-base: the fields read None.
    rb = plan.get("rebase") or {}
    return {
        "mode": plan.get("mode"),
        "objective": plan.get("objective"),
        # drop `estimate_ctl` — the generic optimistic fallback — so the narration can't anchor on
        # it and inflate the race-day CTL; `projected_race_ctl` (the real chained projection) is the
        # one authoritative number (§6f Step E/F; caught by the real-key plan-explain self-test).
        "feasibility": {k: v for k, v in (plan.get("feasibility") or {}).items()
                        if k != "estimate_ctl"},
        "phases": plan.get("phases"),
        "phase_blocks": {k: _phase_block_summary(plan.get(k))      # §6f Step F — the whole phase path,
                         for k in _phase_keys(plan)},              # chain segments included (0.27.1)
        "projected_race_ctl": (plan.get("feasibility") or {}).get("projected_ctl"),
        "shape_now": plan.get("shape"),
        "rebase_start": rb.get("start"),
        "rebase_end_ctl": rb.get("end_ctl"),
        "rebase_end_atl": rb.get("end_atl"),
        "regime": plan.get("regime"),                # §PRO3 — caution vs assertive build posture + reason
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
        (f"Athlete context: {config().athlete_context}. " if config().athlete_context else "") +
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
        "isn't rewritten, only the road ahead. If regime.mode is "
        "'assertive', note the plan follows their measured form and rides the safe "
        "ACWR headroom to build fitness as fast as is safe; if "
        "'caution', it's the conservative post-illness posture and regime.reason names the medical/"
        "symptom evidence that triggered it. If last_replan or "
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
        (f"Athlete context: {config().athlete_context}. " if config().athlete_context else "") +
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
# "had-to-stop" exertional symptom RED-flags the day and halts the plan (calibrated on the owner's 2025
# history; the halt WORDING is generic since 0.27.0 — the product ships to other runners, log §70).
READINESS_ENERGY = ("good", "ok", "heavy")   # the check-in vocabulary — the UI's three legs options;
READINESS_SLEEP = ("good", "ok", "poor")     # the engine tests "heavy" / "poor", the API rejects the rest
def hrv_signal(db):
    """Objective HRV readiness from the latest shape snapshot: 'low' | 'ok' | 'high' | None."""
    row = latest_snapshot(db)
    if not row:
        return {"state": None}
    # `raw` is a nullable TEXT column: every sync-written row carries the payload, but a row written
    # by any other path (a fixture, a partial write, an import) can hold NULL — and a bare
    # json.loads(None) raises TypeError out of a READINESS read, i.e. the safety-critical card 500s
    # over a missing nice-to-have. Same `or "{}"` guard the other raw readers already use.
    try:
        s = json.loads(row["raw"] or "{}")
    except ValueError:                      # malformed payload — no HRV signal, not a broken page
        return {"state": None}
    b, rng = s.get("hrvBaseline"), s.get("hrvNormalRange")
    if b is None or not rng:
        return {"state": None}
    lo, hi = rng
    state = "low" if b < lo else "high" if b > hi else "ok"
    return {"state": state, "baseline": round(b, 1),
            "band": [round(lo, 1), round(hi, 1)]}


# §H2 — a deterministic keyword backstop for the free-text readiness note. The LLM net
# (llm_readiness, below) only runs when a key is configured; the live NAS runs llm:false, so without
# this the free-text safety catch is DEAD in production and a symptom typed into the note (rather than
# ticked in the checkbox) sails through green. Curated, high-precision exertional/cardiac phrases.
# Deliberately NO negation guard: on a cardiac net a missed symptom is the catastrophe and a false
# halt is merely recoverable, so we bias to catch ("didn't seem bad but my chest got tight and I had
# to stop" must still fire). The LLM, when present, adds nuance ON TOP of this floor — it can escalate
# but the floor itself is non-softenable.
_STOP_SYMPTOM_PHRASES = (
    "chest pain", "chest tight", "tight chest", "chest pressure", "chest pound",
    "tightness in my chest", "pressure in my chest", "pressure in chest",
    "couldn't breathe", "could not breathe", "couldnt breathe", "can't breathe", "cant breathe",
    "cannot breathe", "couldn't catch my breath", "couldnt catch my breath",
    "passed out", "blacked out", "blackout", "black out", "faint", "collapse",
    "had to stop", "forced to stop", "couldn't continue", "could not continue", "couldnt continue",
    "heart racing", "racing heart", "heart pounding", "pounding heart", "palpitation",
    "irregular heartbeat", "skipped beat",
    "dizz", "light headed", "lightheaded", "light-headed",
)


def _deterministic_stop_symptom(note):
    """True if the free-text note contains a curated exertional/cardiac stop-symptom phrase.
    Substring match on the lowercased note; works with no LLM (the production path). See §H2 above."""
    if not note:
        return False
    t = note.lower()
    return any(p in t for p in _STOP_SYMPTOM_PHRASES)


def assess_readiness(db, checkin):
    """Combine the HRV signal + the day's check-in → a traffic-light verdict + action.
    GREEN proceed · AMBER hold (keep easy, no progression) · RED rest/walk (and, on a
    returning stop-symptom, HALT the plan and advise the doctor)."""
    hrv = hrv_signal(db)
    energy = (checkin or {}).get("energy", "ok")
    sleep = (checkin or {}).get("sleep", "ok")
    stop = bool((checkin or {}).get("stop_symptom"))
    note = (checkin or {}).get("note", "")
    reasons = []

    if stop:
        return {"verdict": "red", "halt": True, "hrv": hrv,
                "action": "Stop — do not train. A stop-the-run exertional symptom was flagged: rest "
                          "and contact your doctor before resuming.",
                "reasons": ["A 'had-to-stop' exertional symptom was flagged"]}

    if _deterministic_stop_symptom(note):  # §H2 — non-softenable floor, runs with or without the LLM
        return {"verdict": "red", "halt": True, "hrv": hrv,
                "action": "Stop — your note describes a stop-the-run exertional symptom (chest pain, "
                          "breathlessness, dizziness, fainting, having to stop). Rest and contact your "
                          "doctor before resuming.",
                "reasons": ["A stop-the-run exertional symptom was detected in your note"],
                "source": "engine"}

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
    if llm.get("stop_symptom_detected"):  # free-text safety catch — the §H2 backstop to the check-in stop control
        return {"verdict": "red", "halt": True, "hrv": hrv,
                "action": "Stop — your note reads like a stop-the-run exertional symptom. Rest and "
                          "contact your doctor before resuming.",
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
        "SELECT id, distance, duration FROM activities WHERE date=? AND " + RUN_FAMILY_SQL,
        (date,)
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


def todays_session(db, today):
    """Today's prescription from the latest plan. Returns a session, a rest day, or a
    block-state marker so the readiness tile can tell apart 'no plan at all' (None) from
    'a plan exists but the block hasn't started / has finished' — the latter must NOT read
    as "no active plan". A run already logged for today marks the session `done`. Reads the
    WHOLE road (every phase), not just the re-base — an assertive plan has no re-base, and a
    caution plan's Base/Build days are just as much 'today's session' as Phase-0 days."""
    row = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None  # genuinely no plan generated yet
    plan = json.loads(row["plan"])
    weeks = _plan_all_weeks(plan)
    if not weeks:
        return None
    if today < weeks[0]["start"]:  # plan active, but the road hasn't begun yet
        return {"kind": "pre", "start": weeks[0]["start"]}
    for wk in weeks:
        for s in wk.get("sessions", []):
            if s.get("date") == today:
                actual = runs_on_date(db, today)
                return {**s, "week": wk["wk"], "pk": wk.get("pk"),
                        "easy_pace": plan["pace_zones"].get("easy_top"),
                        "done": bool(actual), "actual": actual}
    # inside the plan window but nothing scheduled → rest day
    last_end = max((s["date"] for w in weeks for s in w.get("sessions", [])), default="")
    if today <= last_end:
        return {"kind": "rest", "note": "Rest day — recovery is part of the plan."}
    return {"kind": "post"}  # the road is complete — time to periodize the next phase


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
    """The training log for the live plan: each planned session enriched with whether a matching
    run was actually done (by date), the actual km/pace, and any reflection note. 'Done' and
    actual-vs-planned are DERIVED from synced `activities` — the journal only stores the free-text
    note. Covers EVERY phase block (each week tagged with its phase key `pk`), not just the
    re-base — the assertive regime SKIPS the re-base, so an assertive plan's elapsed weeks live in
    Base/Build and would otherwise lose their actuals overlay entirely (the bug Duarte caught
    2026-07-04). Returns {weeks, adherence, start, end} or None when there's no plan."""
    row = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    plan = json.loads(row["plan"])
    weeks = _plan_all_weeks(plan)
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
        "WHERE date>=? AND date<=? AND " + RUN_FAMILY_SQL + " ORDER BY date_time", (start, end)
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
    # §H3 — a flagged exertional symptom persists as a medical HOLD until explicitly cleared (doctor
    # clearance), not just a one-day red light. Surface it as red+halt on any later day — even with no
    # new check-in, or a green one — so the gate never silently reverts to green tomorrow. Applied
    # before the bonus-run / done rewords below so they see the halt and stand down.
    if not assessment.get("halt") and active_medical_halt(db):
        assessment = {**assessment, "verdict": "red", "halt": True,
                      "action": "Plan halted — an exertional symptom was flagged and the hold is "
                                "still active. Rest and contact your doctor; clear it here once "
                                "they've cleared you.",
                      "reasons": ["Active medical hold — awaiting doctor clearance"], "source": "engine"}
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

# PNG home-screen / launcher icons. Rasterized as a full-bleed square (no rounded
# corners — iOS re-masks to a squircle, Android masks for adaptive icons) from the
# brand mark in FAVICON_SVG. Base64-embedded because only this file is baked into the
# image (no static dir); the bytes are tiny and decode once at import.
_ICON_180_B64 = "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"
_ICON_192_B64 = "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"
_ICON_512_B64 = "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"
ICON_180_PNG = base64.b64decode(_ICON_180_B64)
ICON_192_PNG = base64.b64decode(_ICON_192_B64)
ICON_512_PNG = base64.b64decode(_ICON_512_B64)
_PNG_HEADERS = {"Content-Type": "image/png", "Cache-Control": "public, max-age=86400"}

@app.get("/apple-touch-icon.png")
def apple_touch_icon():
    return ICON_180_PNG, 200, _PNG_HEADERS

@app.get("/icon-192.png")
def icon_192():
    return ICON_192_PNG, 200, _PNG_HEADERS

@app.get("/icon-512.png")
def icon_512():
    return ICON_512_PNG, 200, _PNG_HEADERS



# ── PWA (installable + offline app shell) ────────────────────────────────────────────────────────
# A web manifest + a small service worker make the app installable (home-screen / desktop) and give it
# an offline shell. Both are public-safe static assets (no secrets, no token) so they serve on BOTH the
# private and the public read-only container — they're not in _private_only_path and carry nothing
# personal. The private and public boxes live on different origins, so each gets its own SW scope and
# nothing crosses. The SVG covers crisp/tab use; the full-bleed PNGs give iOS a real apple-touch-icon
# and Android adaptive (maskable) launcher icons so the installed home-screen icon never falls back.
WEB_MANIFEST = json.dumps({
    "name": "Sparing Horse",
    "short_name": "Sparing Horse",
    "description": "Your current running shape and a dynamic, objective-driven training plan, "
                   "built on your own Runalyze data.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#f4f1ea",
    "theme_color": "#f4f1ea",
    "icons": [
        {"src": "/favicon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}, separators=(",", ":"))

# The service worker: app-shell caching only. Deliberately NEVER caches /api/* (would risk serving stale
# or — across the shared deploy — privacy-sensitive data) and ignores non-GET + cross-origin (fonts /
# tiles / Leaflet pass straight through). Navigations are network-first with an offline fallback to the
# cached shell; same-origin static is stale-while-revalidate. Bump SHELL to invalidate the old cache.
SERVICE_WORKER_JS = """\
const SHELL='sh-shell-__SH_VER__';
const SHELL_URLS=['/','/favicon.svg','/manifest.webmanifest',
                  '/static/app.css?v=__SH_VER__','/static/app.js?v=__SH_VER__'];
self.addEventListener('install',e=>{
  e.waitUntil(caches.open(SHELL).then(c=>c.addAll(SHELL_URLS)).then(()=>self.skipWaiting()));
});
self.addEventListener('activate',e=>{
  e.waitUntil(caches.keys()
    .then(ks=>Promise.all(ks.filter(k=>k!==SHELL).map(k=>caches.delete(k))))
    .then(()=>self.clients.claim()));
});
self.addEventListener('fetch',e=>{
  const req=e.request;
  if(req.method!=='GET') return;                       // never touch writes
  const url=new URL(req.url);
  if(url.origin!==self.location.origin) return;        // let cross-origin (fonts/tiles/leaflet) pass
  if(url.pathname.startsWith('/api/')) return;         // never cache the API (stale / privacy-sensitive)
  if(req.mode==='navigate'){                           // app shell: network-first, offline -> cached shell
    e.respondWith(fetch(req)
      .then(r=>{const cp=r.clone();caches.open(SHELL).then(c=>c.put('/',cp));return r;})
      .catch(()=>caches.match('/')));
    return;
  }
  e.respondWith(caches.match(req).then(cached=>{        // same-origin static: stale-while-revalidate
    const net=fetch(req)
      .then(r=>{const cp=r.clone();caches.open(SHELL).then(c=>c.put(req,cp));return r;})
      .catch(()=>cached);
    return cached||net;
  }));
});
"""


# The installed window's chrome follows the theme too: the shell rewrites the manifest link with
# ?theme=<name> (whitelisted — anything else gets the Daylight default), so the title bar and
# splash match the page being installed. The colours are each theme's --bg (static/app.css).
_MANIFEST_THEME_BG = {"light": "#f4f1ea", "dark": "#191a1d", "aurora": "#121226"}


@app.get("/manifest.webmanifest")
def web_manifest():
    man = json.loads(WEB_MANIFEST)
    man["theme_color"] = man["background_color"] = \
        _MANIFEST_THEME_BG.get(request.args.get("theme"), _MANIFEST_THEME_BG["light"])
    return json.dumps(man, separators=(",", ":")), 200, {"Content-Type": "application/manifest+json",
                                                         "Cache-Control": "public, max-age=86400"}


@app.get("/sw.js")
def service_worker():
    # no-cache so a new SW is picked up promptly; Service-Worker-Allowed lets it claim the root scope.
    # The cache name and the precached asset URLs carry the release, so a deploy retires the old cache
    # instead of a returning visitor being served the previous release's JS out of it.
    return SERVICE_WORKER_JS.replace("__SH_VER__", ENGINE_VERSION), 200, {
                                    "Content-Type": "application/javascript",
                                    "Cache-Control": "no-cache",
                                    "Service-Worker-Allowed": "/"}

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
    `/api/settings` + `/api/secrets` (athlete context + keys are personal, owner-only control surfaces),
    and `/api/geocode` (the city-picker proxy). NOTE `/api/effort-discipline` is NOT here: it self-
    sanitizes on the public box (HR/TE/feeling dropped, judged on pace — `effort_discipline(public=…)`),
    so the score is public while the HR-led critique stays private."""
    return (p in ("/api/health", "/api/settings", "/api/geocode", "/api/secrets",
                  "/api/secrets/validate", "/api/runs")   # §RB — calendar carries HR-zone grades
            or p.startswith("/api/suunto")                # §SG — OAuth + watch push are owner-only
            or p.startswith("/api/backup") or p.startswith("/api/export")   # §BX — the owner's data
            or p.startswith("/api/availability")          # §AV — away days = empty-house broadcast
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


# The self-test battery used to run IN this process, rebinding module globals (READONLY, the tokens,
# `regenerate`) for ~40 s — so every other request answered 503 + Retry-After meanwhile. That was the
# app's only self-inflicted downtime, and its own test suite inflicted it. TECH-1 moved the battery to
# a SUBPROCESS against a database snapshot (`_selftest_spawn`), which cannot reach this process's
# globals at all, so the gate, the thread-ident bookkeeping and the "wait for the battery" hold that
# the nightly job needed are all gone with it. `/healthz` now answers 200 throughout a battery —
# det/selftest-subprocess pins exactly that.


@app.errorhandler(Exception)
def _unhandled_json_500(e):
    """Blanket last resort (TECH-9): /api/* and /healthz answer JSON {ok:false,error} 500, pages get a
    quiet HTML 500 — and NEITHER leaks the exception (the public box serves strangers; the traceback
    always lands in the server log instead). Flask's own HTTPException (404/405/…) keeps its answer."""
    if isinstance(e, HTTPException):
        return e
    app.logger.exception("unhandled %s %s", request.method, request.path)
    if request.path.startswith("/api/") or request.path == "/healthz":
        return jsonify(ok=False, error="internal error — the server log has the details"), 500
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<title>Sparing Horse — something broke</title></head>"
            "<body style=\"font-family:system-ui,sans-serif;max-width:38em;margin:4em auto;line-height:1.5\">"
            "<h1 style=\"font-size:1.4em\">Something broke.</h1>"
            "<p>Reload the page. If it keeps happening, the server log has the full traceback.</p>"
            "</body></html>"), 500


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
    # `<script>` → nonce'd covers the inline blocks; `__SH_NONCE__` is the explicit slot for a tag
    # that already carries attributes (the external /static/app.js), which the bare replace cannot see.
    return (html.replace("<script>", f'<script nonce="{nonce}">')
                .replace("__SH_NONCE__", nonce))


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


def _int_arg(name, default, lo=1, hi=None):
    """A bounded integer query parameter, or a JSON 400 — never a bare int() that answers junk with an
    HTML 500 (0.26.3; det/api-validation). Returns (value, None) or (None, (response, 400)): absent/blank
    ⇒ default; non-integer or out of [lo, hi] ⇒ the 400."""
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default, None
    v = request.args.get(name, type=int)
    if v is None or v < lo or (hi is not None and v > hi):
        bound = f"between {lo} and {hi}" if hi is not None else f"≥ {lo}"
        return None, (jsonify(ok=False, error=f"{name} must be an integer {bound}"), 400)
    return v, None


def replan(db, mutate):
    """Re-periodize around a write (§6b): snapshot the plan, apply `mutate`, regenerate, commit, return
    the diff. Centralises the invariant that every objective/adjustment change re-anchors the road
    ahead — and (0.26.3) that the write and its re-plan land TOGETHER. The mutation stays uncommitted
    until `regenerate` has digested it: save_plan commits the write and the new plan at once; when no
    plan can be built without raising (the last race removed ⇒ maintenance) the explicit commit keeps
    the write; and a re-plan that RAISES rolls the write back and answers JSON 500 — never an HTML 500
    over a half-applied change. Before this the write was committed FIRST: a malformed objective date
    landed, then poisoned every later regeneration (the nightly included — /api/plan kept serving the
    last saved plan while every generate raised) until the row was deleted by hand (Codex review,
    2026-08-20; det/api-validation drives the raise for real)."""
    try:
        base = plan_baseline(db)
        mutate()
        out = regenerate(db, baseline=base)
        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify(ok=False, error=f"change not applied — re-planning failed: {e}"), 500
    return jsonify(out)


# ── §PV — the public projection: an ALLOWLIST, one serializer per resource ───────────────────
# TECH-3. Until now every public endpoint redacted by SUBTRACTION: serve the private payload, then
# `pop()` the fields someone remembered were personal. A blocklist is only ever as good as the last
# person to think about it, and this one had already failed twice in the field — the §AV away dates
# rode into /api/log inside week dicts nobody enumerated (0.27.1), and building this allowlist found
# two more the pops never named: `/api/shape` was serving `latest.raw`, the WHOLE Runalyze snapshot
# payload (HRV baseline + normal range, the easy-TRIMP bands, rest-day counts), and the same endpoint
# handed out `last_sync` — the household routine timestamp /healthz deliberately reduces to booleans
# (TECH-8) and the freshness chip keeps private (UX-4). The public page footer printed it.
#
# So the posture inverts: a field reaches the public box because it is NAMED here, never because
# nobody remembered to remove it. A new column, a new engine field, a new nested dict is PRIVATE
# until someone adds it to a spec — the failure mode becomes "the public page is missing something",
# which is visible, instead of "the public page is showing something", which is not.
#
# A spec is data: `True` publishes the value verbatim (a scalar, or a leaf whose whole subtree is
# public); a dict is an allowlist applied to a dict — or to every dict in a list. Nothing else
# survives. The specs are deliberately FLAT and boring to read: this is a file a reviewer must be
# able to audit in one pass, which is the property `pop()` calls scattered over 30 endpoints lacked.
_PV_REPS = {"detail": True, "effort": True, "km": True, "minutes": True, "pace_zone": True,
            "trimp": True, "zone": True}
_PV_QUALITY = {"attach": True, "component": True, "frac": True, "kind": True, "label": True,
               "rec_min": True, "rep_min": True, "structure": True, "zone": True}
# NOT `reflection`: the free-text "how it felt" is the readiness-note posture — private (§H7).
_PV_SESSION = {"activity_id": True, "actual": {"km": True, "pace": True}, "component": True,
               "date": True, "done": True, "kind": True, "km": True, "minutes": True,
               "missed": True, "note": True, "pace_zone": True, "reps": _PV_REPS, "runs": True,
               "strides": True, "trimp": True, "zone": True}
# NOT `av_dates` / `av_shed`: away days are an empty-house broadcast (§AV). The 0.27.1 recursive
# strip stays in place for anything outside these views; here the allowlist makes them unreachable
# by construction — a new phase key or a new payload cannot outrun it.
_PV_WEEK = {"adjusted": True, "clipped": True, "deload_forced": True, "deload_pulled": True,
            "elapsed": True, "eq_km": True, "freq_actual": True, "frequency_met": True,
            "frozen": True, "intent": True, "intent_km": True, "intent_runs": True, "km": True,
            "km_ahead": True, "km_done": True, "long": True, "partial": True, "peak_acwr": True,
            "pk": True, "prog_ridden": True, "proj_acwr": True, "proj_acwr_flat": True,
            "proj_acwr_soft": True, "proj_ctl": True, "quality": _PV_QUALITY, "runs": True,
            "runs_ahead": True, "runs_done": True, "sessions": _PV_SESSION, "start": True,
            "strides": True, "trimp_total": True, "volume_met": True, "wk": True}
_PV_PHASE = {"builds": True, "clipped_by_acwr": True, "end_atl": True, "end_ctl": True,
             "full_len": True, "start": True, "weeks": _PV_WEEK}
_PV_FINISH = {"anchor_stale": True, "at_ctl": True, "at_evo2": True,
              "band": {"components": {"calibration": True, "disp_a": True, "horizon": True,
                                      "race": True},
                       "hi_hms": True, "hi_seconds": True, "level": True, "lo_hms": True,
                       "lo_seconds": True, "sigma_log": True},
              "correction": True, "curve": {"ctl": True, "evo2": True, "hms": True,
                                            "plus_weeks": True},
              "distance": True, "hms": True, "long_km": True, "note": True, "seconds": True,
              "today": {"at_ctl": True, "at_evo2": True, "gain_seconds": True, "hms": True,
                        "long_km": True, "seconds": True}}
_PV_FEASIBILITY = {"estimate_ctl": True, "finish_time": _PV_FINISH, "note": True,
                   "projected_ctl": True, "verdict": True}

# NOT `adjustment` (free-text/medical context) and NOT `cold_start` (§33f-5 — the seeds carry AGE
# and an HRmax prior in bpm, H7-class). The phase blocks are matched STRUCTURALLY below, so a chain
# segment (bridge1 / peak1 / taper2 …) is allowlisted like any other phase without being named.
_PV_PLAN = {"chain": {"date": True, "feasibility": True, "label": True, "proj_ctl": True,
                      "role": True, "type": True},
            "engine_running": True, "engine_version": True, "feasibility": _PV_FEASIBILITY,
            "generated_at": True, "mode": True, "note": True,
            "objective": {"date": True, "label": True, "priority": True, "target": True,
                          "type": True, "weeks_away": True},
            "ok": True,
            "pace_zones": {"easy": True, "easy_top": True, "interval": True, "lt1": True,
                           "marathon": True, "p5k": True, "threshold": True},
            "phases": {"key": True, "kind": True, "phase": True, "race": True, "role": True,
                       "type": True, "weeks": True},
            "prog": {"note": True, "ramp": True},
            "regime": {"mode": True, "reason": True},
            "seed_now": {"atl": True, "bridged_days": True, "ctl": True, "effective_vo2max": True,
                         "fallback": True, "from": True, "moved": True,
                         "was": {"atl": True, "ctl": True, "effective_vo2max": True},
                         "was_from": True},
            "shape": {"atl": True, "ctl": True, "effective_vo2max": True,
                      "seed": {"bridged_days": True, "fallback": True, "from": True}},
            "shape_response": {"basis": True, "factor": True, "projected": True, "realized": True,
                               "ride_cap": True},
            "tune_ups": {"date": True, "label": True, "priority": True, "type": True}}

# NOT `last_sync` (the household's routine — /healthz gives booleans, so must this) and NOT
# `latest.raw` (the entire upstream snapshot payload, HRV band included), nor the snapshot's own
# `hrv_baseline` / `monotony` / `training_strain`: physiological, H7-class, and the front end reads
# none of them on either box.
_PV_SHAPE = {"duplicate_count": True, "duplicates": True, "ignored": True,
             "history": {"acwr": True, "effective_vo2max": True, "fatigue": True, "fitness": True,
                         "performance": True, "snapshot_date": True},
             "latest": {"acwr": True, "captured_at": True, "effective_vo2max": True,
                        "effective_vo2max_progress": True, "fatigue": True, "fitness": True,
                        "fitness_pct": True, "marathon_shape": True, "performance": True,
                        "snapshot_date": True}}

_PV_LOG = {"adherence": {"done": True, "scheduled": True}, "end": True,
           "ran": {"km": True, "min": True, "runs": True}, "start": True, "today": True,
           "weeks": _PV_WEEK}

# The readiness projection the public box already built by hand (verdict + today's session only):
# same shape, now stated as a spec. The check-in inputs, the free-text note, the HRV signal, the
# reasons and any halt/medical guidance stay private — see api_readiness for how `assessment` is
# rebuilt into generic copy BEFORE this runs (the words are a projection too, not just the fields).
_PV_READINESS = {"date": True,
                 "assessment": {"verdict": True, "action": True, "public": True, "done": True},
                 "session": dict(_PV_SESSION, easy_pace=True, pk=True, week=True)}

# NOT `hr_avg` / `hr_max` (per-run HR is private, the same posture that drops HR from the public
# effort-discipline read) and NOT `cross_training` (which sport, and when — personal).
_PV_ACTIVITY = {"cadence": True, "date": True, "date_time": True, "distance": True,
                "duration": True, "elapsed": True, "elevation_up": True, "empty_run": True,
                "id": True, "ignored": True, "pace_min_km": True, "sport": True,
                "sj": {"ids": True, "index": True, "km": True, "min": True, "parts": True},
                "title": True, "trimp": True}

# NOT `counterfactual.reason` (the regime rationale names the athlete's own history) and NOT
# `scorecard.reckoning` — the settled race result. The public box does not COMPUTE the reckoning
# (api_plandrift gates it on `not READONLY`), which is stronger than redacting it; leaving it out of
# the spec too means dropping that gate one day cannot quietly publish a finish time. The card reads
# `if(sc.reckoning)`, so absent renders exactly as the null it already received.
_PV_DRIFT = {"anchor": {"created_at": True, "for_date": True, "is_current": True, "versions": True},
             "counterfactual": {"ctl": {"ctl": True, "date": True},
                                "distance": {"cum": True, "date": True, "kind": True},
                                "effort": {"date": True, "trimp": True}, "envelope": True,
                                "outcome": {"curve": {"ctl": True, "evo2": True, "hms": True,
                                                      "plus_weeks": True},
                                            "finish": True, "now_finish": True,
                                            "now_peak_ctl": True, "peak_ctl": True},
                                "regime": True, "vs": True},
             "ctl": {"actual": {"ctl": True, "date": True}, "current": {"ctl": True, "date": True},
                     "initial": {"ctl": True, "date": True}},
             "distance": {"current": {"cum": True, "date": True, "kind": True},
                          "initial": {"cum": True, "date": True, "kind": True}},
             "duplicate_count": True,
             "effort": {"actual": {"date": True, "trimp": True},
                        "current": {"date": True, "trimp": True},
                        "initial": {"date": True, "trimp": True}},
             "error": True,
             "finish_drift": {"at_ctl": True, "at_evo2": True, "date": True, "hi": True,
                              "hms": True, "lo": True, "p50": True},
             "ok": True, "outcome": {"ctl": True, "date": True, "verdict": True},
             "race": {"date": True, "label": True, "weeks_away": True},
             "scorecard": {"chain": True, "fitness": {"founding": True, "gap": True, "now": True,
                                                      "state": True},
                           "headline": True, "open": True,
                           "race": {"caveat": True, "founding": True, "gap": True, "now": True,
                                    "trend": True, "verdict": True},
                           "settled": True,
                           "volume": {"founding": True, "gap": True, "now": True, "state": True},
                           "weeks_to_go": True},
             "today": True}

# NOT `outcome` / `resolved_at` — a race RESULT is personal (§RL/H7). `SELECT *` feeds this, so the
# allowlist is also what keeps a NEW objectives column off the public box.
_PV_OBJECTIVES = {"date": True, "id": True, "label": True, "priority": True, "status": True,
                  "target": True, "type": True}

_PV_WEEKLY = {"km": True, "week": True}

# NOT `hr` (the per-second HR stream), `hr_avg`, `hrmax` or `hrzones` (bpm cutoffs are HR-derived),
# and NOT `path` — route geo leaves only by the private /map (`_strip_geo` runs before this on both
# boxes). `has_hr` stays: the chart needs to know the band is unavailable, not why.
_PV_PROFILE = {"cadence": True, "dist": True, "elevation": True, "error": True, "has_cadence": True,
               "has_elevation": True, "has_gps": True, "has_hr": True, "has_pace": True,
               "pace": True, "v": True}

# NOT `last_sync` / `last_ok` — an unauthenticated probe must not learn when the nightly runs
# (TECH-8). api_healthz builds the public booleans itself; this is the gate that keeps them so.
_PV_HEALTHZ = {"consecutive_failures": True, "db": True, "llm": True, "ok": True, "readonly": True,
               "sync_ok": True, "sync_stale": True, "token_configured": True}

PUBLIC_VIEWS = {"activity": _PV_ACTIVITY, "drift": _PV_DRIFT, "healthz": _PV_HEALTHZ,
                "log": _PV_LOG, "objectives": _PV_OBJECTIVES, "plan": _PV_PLAN,
                "profile": _PV_PROFILE, "readiness": _PV_READINESS, "shape": _PV_SHAPE,
                "weekly": _PV_WEEKLY}


def _pv_project(spec, value):
    """Apply one allowlist to one value. `True` publishes verbatim; a dict allowlists a dict — or
    every dict in a list, so a spec never has to know whether a field holds one week or twenty."""
    if spec is True:
        return value
    if isinstance(value, list):
        return [_pv_project(spec, v) for v in value]
    if not isinstance(value, dict):
        return value            # a spec'd key holding a scalar (or None) — nothing to project
    return {k: _pv_project(sub, value[k]) for k, sub in spec.items() if k in value}


def public_view(resource, payload):
    """§PV — project a payload down to what the PUBLIC box may serve. Pure: it takes no view of
    READONLY, so the call sites stay greppable and the specs stay unit-testable.

    An unknown resource RAISES rather than passing the payload through: a new public endpoint that
    forgets its allowlist must fail loudly on the public box (the blanket handler turns it into a
    clean JSON 500 — §error-shape), never serve a private payload quietly. Fail closed, and loud."""
    if payload is None:
        return None
    try:
        spec = PUBLIC_VIEWS[resource]
    except KeyError:
        raise KeyError(f"no public view defined for {resource!r} — refusing to serve it publicly")
    return _pv_project(spec, payload)


def plan_public_view(plan):
    """§PV — the plan, whose phase blocks are keyed dynamically: base/build/peak/taper/rebase, plus
    a §PER1 chain's own segments (bridge1, peak1, taper2 …). Matching them STRUCTURALLY — any value
    that is a dict carrying `weeks` — means a new segment key is allowlisted like every other phase
    the day it appears, instead of leaking whole until someone enumerates it. That enumeration is
    exactly what the 0.27.0 away-date strip got wrong."""
    out = public_view("plan", plan)
    if isinstance(plan, dict):
        for k, v in plan.items():
            if k not in out and isinstance(v, dict) and "weeks" in v:
                out[k] = _pv_project(_PV_PHASE, v)
    return out


@app.get("/healthz")
def healthz():
    """Liveness + scheduler telemetry (TECH-8). The private box gets the raw timestamps; the PUBLIC box
    gets booleans only — an unauthenticated probe must not learn when the owner's nightly runs (their
    routine is private), but an uptime check can still see that syncing works and is fresh."""
    db = get_db()
    last_sync = get_meta(db, "last_sync")
    last_ok = get_meta(db, "sched:last_ok")
    fails = int(get_meta(db, "sched:fail_count", "0") or 0)
    out = dict(ok=True, token_configured=bool(config().runalyze_token), db=DB_PATH.exists(),
               llm=llm_available(), readonly=READONLY, consecutive_failures=fails)
    if READONLY:
        out.update(sync_ok=bool(last_ok),
                   sync_stale=(not last_ok) or _seconds_since(last_ok) > 36 * 3600)
        return jsonify(public_view("healthz", out))    # §PV — the booleans are the whole allowlist
    out.update(last_sync=last_sync, last_ok=last_ok)
    return jsonify(out)


_sync_lock = threading.Lock()   # one Runalyze pull at a time — page-load syncs, "Sync now", the nightly


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
        # The throttle is check-then-act: N tabs opening together all passed it and fanned out into N
        # incremental pulls (data-safe — INSERT OR REPLACE — but N× the Runalyze calls; Gemini review
        # 2026-08-21 #4). The first to take the lock syncs; the rest report the in-flight one.
        if not _sync_lock.acquire(blocking=False):
            return jsonify(ok=True, skipped=True, in_flight=True, last_sync=last)
    else:
        _sync_lock.acquire()          # "Sync now" / backfill queue behind an in-flight pull, never race it
    try:
        return jsonify(run_sync(backfill=backfill))
    except RunalyzeError as e:
        return jsonify(ok=False, error=str(e)), 502
    except Exception as e:                       # never leak an HTML 500 to the JSON client
        return jsonify(ok=False, error=f"sync failed: {e}"), 500
    finally:
        _sync_lock.release()


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
    out = dict(
        latest=dict(latest) if latest else None,
        history=[dict(r) for r in history],
        last_sync=get_meta(db, "last_sync"),
        duplicate_count=len(dups),
        duplicates=dup_rows,
        ignored=[dict(r) for r in ignored],
    )
    # §PV — `latest` is a whole snapshot ROW: it carried `raw`, the entire upstream payload (HRV
    # baseline + normal range, easy-TRIMP bands, rest-day counts), out to the public box, and
    # `last_sync` handed a stranger the household's nightly time that /healthz reduces to booleans.
    # Neither was ever named by a pop; both are absent now because neither is named by the allowlist.
    return jsonify(public_view("shape", out) if READONLY else out)


@app.get("/api/hr-zones/derive")
def api_hr_zones_derive():
    """Private diagnostic: reconstruct the HR zone cutoffs (%HRmax) from Runalyze's per-activity
    zone distribution. Read-only, derives nothing into the DB — for eyeballing before the chart
    colours by them. Needs the MCP token, so it's private-only."""
    if READONLY:
        return jsonify(ok=False, error="diagnostics are private"), 403
    return jsonify(derive_hr_zones(get_db()))


@app.get("/api/lthr")
def api_lthr():
    """Private diagnostic: the data-derived LTHR (lactate-threshold HR) + its confidence/source. Pure
    read, derives nothing into the DB. HR is private (H7), so private-only even though it needs no token."""
    if READONLY:
        return jsonify(ok=False, error="diagnostics are private"), 403
    return jsonify(derive_lthr(get_db()))


@app.get("/api/hr-zones")
def api_hr_zones():
    """Private diagnostic: the app's OWN HR-zone model (bpm) — LTHR-anchored when trustworthy, %HRmax
    fallback otherwise (see hr_zones). Pure read, token-free; distinct from /api/hr-zones/derive, which
    reconstructs Runalyze's own zones for corroboration. HR is private (H7), so private-only."""
    if READONLY:
        return jsonify(ok=False, error="diagnostics are private"), 403
    return jsonify(hr_zones(get_db()))


@app.get("/api/lt1")
def api_lt1():
    """Private diagnostic: the fitness-tracking LT1 (§3.4) — the PACE-anchored easy bar (≈80% 5k pace, off
    current VO2max) + the HR cross-check + a detrained flag. Pure read; bundles the HR cross-check, so
    private-only (H7)."""
    if READONLY:
        return jsonify(ok=False, error="diagnostics are private"), 403
    return jsonify(lt1(get_db()))


@app.get("/api/zones")
def api_zones():
    """Private: the 'Current zones' card — training-intent rows with fitness-tracking pace windows
    (VDOT) + HR bands (unified hr_zones grid). Carries HR + the LTHR anchor ⇒ private-only (H7),
    same guard as /api/lt1."""
    if READONLY:
        return jsonify(ok=False, error="diagnostics are private"), 403
    return jsonify(training_zones(get_db()))


@app.get("/api/pace-hr-coherence")
def api_pace_hr_coherence():
    """Private diagnostic: do the pace-prescription and HR-judgment models agree? (See pace_hr_coherence.)
    Pure read, surfaces divergence only — never adjusts the plan. HR-derived ⇒ private-only."""
    if READONLY:
        return jsonify(ok=False, error="diagnostics are private"), 403
    return jsonify(pace_hr_coherence(get_db()))


@app.get("/api/effort-discipline")
def api_effort_discipline():
    """§6m — effort vs prescription over the recent window. PRIVATE console = the HR-led read (per-run
    HR + TE + feeling); PUBLIC read-only showcase = a SANITIZED pace-based easy-discipline score with no
    HR or personal critique (READONLY → public=True). `?days=N` (default 21)."""
    days, err = _int_arg("days", EFFORT_WINDOW_DAYS, hi=3650)
    if err:
        return err
    return jsonify(effort_discipline(get_db(), window_days=days, public=READONLY))


@app.get("/api/run-metrics")
def api_run_metrics():
    """The queryable per-run metrics table + the self-re-running feel/heat/load analysis. Every column
    is HR/health-derived, so this is PRIVATE-ONLY — 403 under READONLY (the coherence pattern, never the
    sanitized effort-discipline one). `?route=<id>` filters to one recurring route, `?days=N` to a
    window, `?limit=N` caps rows; `?analysis=0` returns just the table."""
    if READONLY:
        return jsonify(ok=False, error="per-run metrics are private"), 403
    db = get_db()
    route, err = _int_arg("route", None)
    if err:
        return err
    days, err = _int_arg("days", None, hi=3650)
    if err:
        return err
    limit, err = _int_arg("limit", None, hi=10000)
    if err:
        return err
    example, err = _int_arg("example", None)
    if err:
        return err
    out = {"ok": True, "rows": run_metrics(db, route_id=route, days=days, limit=limit)}
    if request.args.get("analysis", "1") != "0":
        out["analysis"] = run_metrics_analysis(db)
        # the worked example anchors on the latest run (or ?example=<id>), independent of the row filters
        out["worked_example"] = worked_example(db, activity_id=example)
    return jsonify(out)


@app.get("/api/projector")
def api_projector():
    """The reconstructed fitness/fatigue curve + a validation of the model against
    Runalyze's reported values. `?days=N` trims the returned history (default 180)."""
    db = get_db()
    days, err = _int_arg("days", 180, hi=3650)
    if err:
        return err
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


def _chain_drift(anchor, current, today, race_date, dup_count):
    """§6q/#3 — multi-peak awareness for the drift scorecard. The scorecard's `race` axis settles only
    the FINAL peak, but a chained build (§6q) has earlier A-races whose race-day projection also drifts.
    Returns (chain_drift, next_peak):
      • chain_drift — one entry per A-race across the founding (anchor) + current chains, its founding vs
        current projected race-day CTL matched BY DATE (graceful when a pre-§6q founding plan carries no
        chain → founding None → trend 'unknown'), with the same ±0.5 gaining/slipping/steady trend the
        race axis uses (suppressed to 'unknown' while a duplicate inflates the snapshot).
      • next_peak — the nearest A-race still ahead of today but before the final goal: the peak to point
        at in the live headline. None on a single-A build or once only the final remains.
    Pure (no DB, no globals). Single-A collapses to one entry ≡ the race axis (the caller suppresses it)."""
    def by_date(plan):
        return {c["date"]: c for c in (plan.get("chain") or []) if c.get("date")}
    a_chain, c_chain = by_date(anchor), by_date(current)

    def trend(g):
        return ("unknown" if dup_count or g is None else
                "gaining" if g > 0.5 else "slipping" if g < -0.5 else "steady")

    drift = []
    for d in sorted(set(a_chain) | set(c_chain)):
        cc = c_chain.get(d) or a_chain.get(d) or {}
        fc = (a_chain.get(d) or {}).get("proj_ctl")
        nc = (c_chain.get(d) or {}).get("proj_ctl")
        g = None if (fc is None or nc is None) else round(nc - fc, 1)
        drift.append({"label": cc.get("label"), "date": d, "role": cc.get("role"),
                      "founding": fc, "now": nc, "gap": g, "trend": trend(g),
                      "verdict": cc.get("feasibility"), "passed": _date(d) < today})
    next_peak = None
    for d in sorted(c_chain):
        if today < _date(d) and (race_date is None or _date(d) < race_date):
            next_peak = c_chain[d]
            break
    return drift, next_peak


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
        # the public box removes the Generate button — its copy must not point at it (UX-11)
        return jsonify(ok=False, error="no plan history yet" + ("" if READONLY else
                                                                " — generate a plan first"))
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
    today = datetime.now().date()
    # §6s — the engine drops a race the day after it passes (select_chain is future-only), so a just-run
    # race no longer rides the current plan. To keep RECKONING it (the honest endgame), re-anchor the
    # whole scorecard to the most-recent A-race that has passed within the reckoning window — its
    # founding plans (built while it was ahead) hold the projection we settle against. Only when the
    # current plan carries NO objective at all: a live FUTURE goal must keep the open score (it wins),
    # and a race the engine still carries is handled on its own path.
    if not obj.get("date"):
        past_a = db.execute(
            "SELECT * FROM objectives WHERE status IN ('upcoming','done','lapsed') "   # §RL: resolution (incl. an
            # unrun race lapsing after the grace window) must not kill the reckoning inside its window
            "AND priority='A' AND date<=? AND date>=? "
            "ORDER BY date DESC LIMIT 1",
            (today.isoformat(), (today - timedelta(weeks=RECKON_WINDOW_WEEKS)).isoformat())).fetchone()
        if past_a and any(((json.loads(r["plan"]).get("objective") or {}).get("date")) == past_a["date"]
                          for r in rows):                 # only if a founding plan for it exists to anchor
            obj = dict(past_a)
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

    # — §FT4 ledger: predicted finish over time, one point per regen DAY (last wins) — the product
    #   watching itself. Same-goal filter as the founding road (a different race's time isn't this
    #   series). Pre-§FT3 rows carry no band (lo/hi None) — the P50 line still plots; the day the
    #   band shipped, the envelope appears. Seeded day one by the whole banked plans history. —
    fin_byday = {}
    for r in rows:
        p = json.loads(r["plan"])
        if ((p.get("objective") or {}).get("date")) != cur_goal:
            continue
        ft = (p.get("feasibility") or {}).get("finish_time") or {}
        if not ft.get("seconds"):
            continue
        band = ft.get("band") or {}
        fin_byday[r["for_date"]] = {"date": r["for_date"], "p50": ft["seconds"],
                                    "hms": ft.get("hms"),
                                    "lo": band.get("lo_seconds"), "hi": band.get("hi_seconds"),
                                    "at_ctl": ft.get("at_ctl"), "at_evo2": ft.get("at_evo2")}
    finish_drift = [fin_byday[k] for k in sorted(fin_byday)]

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

    # §6q/#3 — multi-peak awareness: per-race founding-vs-now projection drift across the whole A-race
    # chain, plus the next peak still ahead (for the live headline). Single-A → one entry (suppressed).
    chain_drift, next_peak = _chain_drift(anchor, current, today, race_date, dup_count)

    settled = race_date is not None and today >= race_date

    # §6s — post-race reckoning: once the race date passes, stop PROJECTING and settle against what
    # ACTUALLY happened — the fitness you arrived with vs what the founding road promised, and the
    # finish vs the goal. The honest endgame §6j left open (its race axis was projection-vs-projection).
    # The finish time + goal are the runner's personal result — a category beyond §6j's public-safe
    # "shape + plan only" posture — so the reckoning is PRIVATE-only (withheld on the read-only mirror).
    reckoning = None
    if settled and not READONLY:
        arrived = None                                    # actual CTL on race day, from the full reconstruction
        for p in reconstruct_history(db):                 # (not the anchor-windowed series — the race may pre-date it)
            if _date(p["date"]) <= race_date:
                arrived = round(p["ctl"], 1)
            else:
                break
        # same t0 seam as the fitness axis: projected_ctl is on the plan/snapshot scale, the arrived
        # CTL is locally reconstructed — subtract the constant offset so the gap is real divergence.
        fit_reck_gap = (None if (arrived is None or race_found is None)
                        else round((arrived - race_found) - fit_seam, 1))
        act, race_status = _race_day_activity(db, obj.get("date"), obj.get("type"))
        goal_s = _parse_goal_seconds(obj.get("target"), obj.get("type"))
        actual_s = _race_seconds(act) if (act and race_status == "finished") else None
        reckoning = {
            "fitness": {"projected": race_found, "arrived": arrived, "gap": fit_reck_gap,
                        "state": _state(fit_reck_gap, 2.0)},
            "result": {"goal": obj.get("target"), "goal_seconds": goal_s, "status": race_status,
                       "actual_seconds": actual_s, "actual": _fmt_hms(actual_s), "found": bool(act),
                       "dnf_km": (round(act["distance"], 1) if race_status == "dnf" else None),
                       "beat": (None if (goal_s is None or actual_s is None) else actual_s <= goal_s)},
            # §FT4 — the engine's own bet, settled: the final pre-race prediction vs the clock
            # (same scorer resolve_passed_races persists into the outcome record).
            "prediction": _ft_prediction_score(db, obj.get("date"), obj.get("type"), actual_s),
        }

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
    if reckoning:                                         # §6s — the race is run; reckon, don't project
        race_name = obj.get("label") or "The race"
        fr, rr = reckoning["fitness"], reckoning["result"]
        fg = fr["gap"]
        # arrived is the REAL reconstructed CTL; the gap is the §6j seam-corrected divergence from the
        # plan's projection, so we phrase the shortfall rather than print an inconsistent "X vs Y" pair.
        if fg is None:
            fit_clause = "your race-day fitness can't be reconstructed"
        elif abs(fg) <= 2.0:
            fit_clause = f"you arrived right on the plan's target (CTL {fr['arrived']:.0f})"
        else:
            fit_clause = (f"you arrived at CTL {fr['arrived']:.0f}, "
                          f"{abs(fg):g} {'short of' if fg < 0 else 'above'} the plan's target")
        if rr["status"] == "dnf":
            res_clause = f"you stopped at {rr['dnf_km']:g} km (DNF)"
        elif not rr["found"]:
            res_clause = "the race result isn't synced yet"
        elif rr["goal_seconds"] is None:
            res_clause = f"you finished in {rr['actual']}"
        else:
            delta = rr["actual_seconds"] - rr["goal_seconds"]
            res_clause = (f"goal {rr['goal']}, you ran {rr['actual']} "
                          f"({'beat it by ' + _fmt_hms(-delta) if rr['beat'] else 'missed by ' + _fmt_hms(delta)})")
        headline = f"{race_name} is run. On fitness, {fit_clause}; on the clock, {res_clause}."
        pred = reckoning.get("prediction")
        if pred and pred.get("in_band") is not None:      # §FT4 — the product's own bet, settled
            headline += (f" The engine's final call was {pred['lo_hms']}–{pred['hi_hms']} — the clock "
                         f"{'landed inside' if pred['in_band'] else 'fell outside'} the band "
                         f"(median off by {abs(pred['err_pct']):g}%).")
    elif settled:                                        # race passed, reckoning withheld (public view)
        headline = f"{obj.get('label') or 'The race'} is complete."
    elif is_current:
        # The "settle the score" wager voice is a private in-joke (owner's bet); the public site gets
        # neutral copy. READONLY = the public read-only container.
        headline = ("Baseline just sealed — the score isn't open yet; week one is the only live signal."
                    if not READONLY else
                    "Baseline just sealed — too early to call; week one is the only live signal.")
    elif fit_state == "unknown" or vol_state == "unknown":
        headline = ("Not enough reconstructed history yet to call the score." if not READONLY else
                    "Not enough reconstructed history yet to call it.")
    else:
        race_name = obj.get("label") or "Race-day"
        tail = "" if race_trend in ("unknown", "steady") else f" {race_name} projection {race_trend}."
        # §6q/#3 — point at the next peak first when the build chains an earlier A-race still ahead.
        peak_tail = ""
        if next_peak:
            wa = max(0, (_date(next_peak["date"]) - today).days // 7)
            peak_tail = f" Next peak: {next_peak.get('label') or 'an earlier A-race'} in {wa} week{'' if wa == 1 else 's'}."
        # settled is handled by the §6s reckoning/complete branches above, so this is the open score:
        verdict = "Score open." if not READONLY else "Too early to call."
        headline = f"The rebuild is {PHRASE[(fit_state, vol_state)]}.{tail}{peak_tail} " + verdict

    scorecard = {
        "open": not is_current,
        "settled": settled,
        "weeks_to_go": obj.get("weeks_away"),
        "volume":  {"founding": vol_found, "now": vol_now, "gap": vol_gap, "state": vol_state},
        "fitness": {"founding": fit_found, "now": fit_now, "gap": fit_gap, "state": fit_state},
        "race":    {"founding": race_found, "now": race_now, "gap": race_gap, "trend": race_trend,
                    "caveat": bool(dup_count), "verdict": (current.get("feasibility") or {}).get("verdict")},
        "chain":   chain_drift if len(chain_drift) > 1 else None,   # §6q/#3 — per-peak drift (multi-A only)
        "reckoning": reckoning,     # §6s — present only once the race is run (settled)
        "headline": headline,
    }

    # §PRO10 — counterfactual regime overlay (lazy, ?compare=1): the road the OTHER regime would give
    # from today. generate_plan is PURE (no persist) so this never touches plan history. Forward-only
    # series in the SAME shape as the `current` projection, sharing today's actual frontier + t0 fitness.
    counterfactual = None
    if request.args.get("compare") and modeled:
        regime_now = (current.get("regime") or {}).get("mode") or "caution"
        other = "assertive" if regime_now == "caution" else "caution"
        cf_plan = generate_plan(db, force_regime=other)
        cfw = _plan_weeks(cf_plan)
        cf_cum = cur_actual[-1]["cum"] if cur_actual else 0.0     # continue the running total
        cf_dist = []
        for w in cfw:
            if _date(w["start"]) > today_mon:
                cf_cum += w.get("km") or 0.0
                cf_dist.append({"date": w["start"], "cum": round(cf_cum, 1)})
        cf_fwd = _plan_daily_trimps(cf_plan, since=(today_mon + timedelta(days=7)).isoformat())
        cf_ctl = _weekly_ctl(project_forward(cf_fwd, modeled["ctl"], modeled["atl"],
                                             (today_mon + timedelta(days=7)).isoformat())) if cf_fwd else []
        if actual_ctl and cf_ctl:
            cf_ctl = [actual_ctl[-1]] + cf_ctl                    # stitch so the lines meet at today
        cf_eff = [{"date": w["start"], "trimp": round(w.get("trimp_total") or 0.0, 1)}
                  for w in cfw if _date(w["start"]) > today_mon]
        if actual_eff and cf_eff:
            cf_eff = [actual_eff[-1]] + cf_eff
        cf_ft = (cf_plan.get("feasibility") or {}).get("finish_time") or {}
        counterfactual = {
            # envelope: only the assertive counterfactual is an UPPER envelope ("what earning it
            # unlocks"); from an assertive plan the caution road is a FLOOR, not an envelope.
            "regime": other, "vs": regime_now, "envelope": other == "assertive",
            "reason": (current.get("regime") or {}).get("reason"),   # §PV withholds it publicly
            "distance": cf_dist, "ctl": cf_ctl, "effort": cf_eff,
            "outcome": {"peak_ctl": cf_ft.get("at_ctl"), "finish": cf_ft.get("hms"),
                        "curve": cf_ft.get("curve"),
                        "now_peak_ctl": (current.get("feasibility") or {}).get("finish_time", {}).get("at_ctl"),
                        "now_finish": (current.get("feasibility") or {}).get("finish_time", {}).get("hms")},
        }

    out = dict(
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
        finish_drift=finish_drift,       # §FT4 — the prediction ledger series (P50 + band envelope)
        scorecard=scorecard,
        counterfactual=counterfactual,
        duplicate_count=dup_count,
    )
    return jsonify(public_view("drift", out) if READONLY else out)


@app.get("/api/objectives")
def api_objectives():
    db = get_db()
    seed_objectives(db)
    resolve_passed_races(db)   # §RL — idempotent; keeps the list honest even between nightly re-plans
    rows = [dict(r) for r in db.execute("SELECT * FROM objectives ORDER BY date").fetchall()]
    if READONLY:               # §PV/§RL/H7 — race RESULTS are personal, redacted at the DATA layer
        rows = public_view("objectives", rows)   # (the UI hiding the strip is cosmetic). `SELECT *`
                                                 # feeds this, so the allowlist is also what keeps a
                                                 # NEW objectives column off the public box.
    return jsonify(rows)


@app.post("/api/objectives")
def api_objectives_add():
    """Add an objective → re-periodize the road ahead and return the change (§6b)."""
    d = body()
    if not d.get("date"):
        return jsonify(ok=False, error="need a date"), 400
    try:                                   # the engine reads this with _date() — reject junk HERE, not
        _date(str(d["date"]))              # after it is in the table (0.26.3; det/api-validation)
    except (ValueError, TypeError):
        return jsonify(ok=False, error="date must be YYYY-MM-DD"), 400
    if d.get("priority", "A") not in ("A", "B", "C"):
        return jsonify(ok=False, error="priority must be A, B or C"), 400
    db = get_db()
    return replan(db, lambda: db.execute(
        "INSERT INTO objectives (type,label,date,target,priority,status,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (d.get("type", "custom"), d.get("label", "Race"), str(d["date"]),
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


@app.get("/api/availability")
def api_availability():
    """§AV — the owner's away-day windows still in play (this week's Monday onward, so a window
    covering days already lived stays listed until its week closes). PRIVATE-ONLY: the whole
    /api/availability surface is blocked on the public box (_private_only_path) — away dates are
    an empty-house broadcast."""
    from datetime import timedelta
    db = get_db()
    monday = datetime.now().date()
    monday -= timedelta(days=monday.weekday())
    rows = [dict(r) for r in db.execute(
        "SELECT id, date_from, date_to, note, created_at FROM availability "
        "WHERE active=1 AND date_to >= ? ORDER BY date_from", (monday.isoformat(),)).fetchall()]
    return jsonify(rows)


@app.post("/api/availability")
def api_availability_add():
    """§AV — declare away days; the plan re-lays around them immediately (constraints-not-edits:
    the layout derives from the declared reality, spacing stays engine-owned)."""
    d = body()
    f = (d.get("date_from") or "").strip()
    t = (d.get("date_to") or f).strip()
    try:
        df, dto = _date(f), _date(t)
    except (ValueError, TypeError):
        return jsonify(ok=False, error="need date_from (and optional date_to) as YYYY-MM-DD"), 400
    if dto < df:
        return jsonify(ok=False, error="date_to is before date_from"), 400
    if (dto - df).days + 1 > AV_HORIZON_DAYS:
        return jsonify(ok=False, error=f"range longer than {AV_HORIZON_DAYS} days"), 400
    db = get_db()
    return replan(db, lambda: db.execute(
        "INSERT INTO availability (created_at, date_from, date_to, note, active) VALUES (?,?,?,?,1)",
        (_now_iso(), df.isoformat(), dto.isoformat(), (d.get("note") or "").strip() or None)))


@app.post("/api/availability/<int:avid>/remove")
def api_availability_remove(avid):
    """§AV — clear an away window; the plan re-lays immediately."""
    db = get_db()
    return replan(db, lambda: db.execute(
        "UPDATE availability SET active=0 WHERE id=?", (avid,)))


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
        _save_adjustment(db, note, directive)  # §H3 dominant medical track (routine spares a hold)
    return replan(db, mutate)


@app.post("/api/adjustment/clear")
def api_adjustment_clear():
    """Drop the active adjustment and re-plan back to the unadjusted road."""
    db = get_db()
    cd = datetime.now().date().isoformat()   # §PRO3 — record when the hold stopped being in force
    return replan(db, lambda: db.execute(
        "UPDATE adjustments SET active=0, cleared_at=COALESCE(cleared_at,?) WHERE active=1", (cd,)))


@app.get("/api/log")
def api_log():
    """The training log for the live block — planned sessions with done/actual/reflection.
    Done + actual-vs-planned are training-side (public-safe); the free-text reflections are
    withheld on the public view, like the readiness note."""
    log = block_log(get_db())
    if log and READONLY:
        log = public_view("log", log)   # §PV — the free-text reflections and the §AV away dates the
                                        # log spreads whole are both absent by allowlist (0.27.1's
                                        # leak was precisely a week dict nobody had enumerated)
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
    # §PRO14 — the UI renders THIS response directly (refreshPlan(p) skips the GET), so it must
    # carry the running engine too or the staleness banner silently can't evaluate. Annotated after
    # save_plan has already serialized the artifact, so the stamp never reaches the stored row.
    return jsonify(_plan_for_view(plan))


@app.post("/api/plan/explain")
def api_plan_explain():
    """§6c — plain-language explanation of the latest plan + the most recent change (advisory)."""
    d = body()
    out = explain_plan(get_db(), d.get("diff"))
    return jsonify(out), (200 if out.get("ok") else 502)


def _seed_now(db, plan, today=None):
    """§56 — the OTHER way a saved plan goes stale: not the engine moving under it (§PRO14), but the
    DAY moving. A plan is seeded from the load state at the end of the day before it was generated
    (§PRO20), so once tomorrow arrives that seed describes a state the athlete has since left. The
    nightly regenerates at 22:30, which is the right time to INGEST the day's runs and the wrong time
    to be the plan you wake up to — on 2026-07-31 the 22:30 plan correctly read the week as spent and
    laid nothing for the weekend, while the same engine run on Saturday morning brought back Sat easy
    7.4 km + Sun long 9.4 km. Nothing was broken; the plan was simply built for the previous day.

    ⛔ WHAT THIS DELIBERATELY DOES NOT CLAIM. It compares the SEED — the input — and says only that
    the input has moved. It does NOT claim the sessions would change, because knowing that costs a
    full counterfactual `generate_plan` (measured on the owner's DB at 1.6–2.0 s) and this runs on
    every dashboard load, behind a 4-thread server already seen queuing to depth 9. `plan_seed` is
    0.1 ms. A cheap true statement beats an expensive one served slowly, and beats a cheap false one
    absolutely — §6e2's hardcoded sentence is the standing lesson here.

    TRIGGERED ON VALUES, NOT PROVENANCE, and that distinction is the whole honesty of it: if the seed
    is drawn from a different DAY but reads the same to the displayed precision, NOTHING has moved and
    firing would be crying wolf — the exact failure §PRO14's docstring warns trains the owner to
    ignore the marker. So a new day alone does not fire it; a new day whose numbers differ does.

    Returns None — and the banner is then OMITTED, never guessed — when the plan predates §PRO20 (no
    `shape.seed` to compare) or came from the §FT5 cold-start path (no snapshot at all). Same
    discipline as §6e2: a marker that cannot know says nothing rather than asserting a default."""
    saved = (plan.get("shape") or {}).get("seed")
    if not saved:
        return None
    cur = plan_seed(db, today or datetime.now().date())
    if not cur:
        return None
    vo2, ctl, atl, meta = cur
    sh = plan.get("shape") or {}

    def _num(x, nd):
        try:
            return round(float(x), nd)
        except (TypeError, ValueError):
            return None
    now = {"effective_vo2max": _num(vo2, 2), "ctl": _num(ctl, 1), "atl": _num(atl, 1)}
    was = {"effective_vo2max": _num(sh.get("effective_vo2max"), 2),
           "ctl": _num(sh.get("ctl"), 1), "atl": _num(sh.get("atl"), 1)}
    # eVO₂max rides along because it IS part of the seed tuple (§PRO20 keeps it on the newest row) and
    # it moves the pace zones — a changed fitness read is as much a reason to re-read today as a
    # changed load state. Compared at the precision each is shown at, so an invisible last-decimal
    # wobble never fires a banner the owner cannot see the cause of.
    return {"from": meta.get("from"), "bridged_days": meta.get("bridged_days"),
            "fallback": meta.get("fallback"), "was_from": saved.get("from"),
            "moved": now != was, **now, "was": was}


def _plan_for_view(plan, db=None):
    """§PRO14 — stamp the SERVED payload with the engine actually running, so the view can compare it
    against the `engine_version` baked into the artifact. Serve-time only: never persisted, so a
    saved plan can never disagree with itself.

    §56 — carries the day-staleness read (`seed_now`) for the same reason and by the same route: it is
    a serve-time comparison against live state, so it must never be baked into the artifact, and it
    must be attached HERE so both the GET and the generate response evaluate it. That second point is
    not theoretical — it is precisely the bug §PRO14's own note records.

    ONE definition, because the first cut had two paths and only annotated one. `/api/plan` got it;
    the `/api/plan/generate` response did not — and the UI renders that response DIRECTLY
    (`refreshPlan(p)` skips the GET). So `engine_running` was undefined for the whole render after a
    regeneration, `planStale` fell to false, and the banner was suppressed UNCONDITIONALLY — it
    looked correct only because a regeneration usually does make the plan current. A staleness
    marker that cannot be wrong is not a marker; that is the exact failure §PRO14 exists to prevent.
    Every path that hands a plan to a client goes through here."""
    if plan:
        plan["engine_running"] = ENGINE_VERSION
        try:
            plan["seed_now"] = _seed_now(db or get_db(), plan)
        except Exception as e:      # a staleness read must never cost the owner his plan
            print(f"[§56] seed staleness read skipped: {e}")
            plan["seed_now"] = None
    return plan


@app.get("/api/plan")
def api_plan():
    """The latest generated plan (or null if none yet)."""
    db = get_db()
    row = db.execute("SELECT plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    plan = json.loads(row["plan"]) if row else None
    _plan_for_view(plan)   # §PRO14 — one definition, shared with /api/plan/generate
    if plan and READONLY:
        plan = plan_public_view(plan)  # §PV — allowlist: the adjustment (free-text/medical), the
                                       # §33f-5 cold-start seeds (AGE + an HRmax prior) and the §AV
                                       # away dates are absent because they are not NAMED, not
                                       # because they were remembered
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
        # §PV — the WORDS are a projection too (generic copy above, never the private reasons), and
        # the allowlist then decides which FIELDS survive: the check-in inputs, the free-text note,
        # the HRV signal, the reasons and any halt/medical guidance are absent because unnamed. The
        # session rides through the same session allowlist the plan and log use.
        data = public_view("readiness", {"date": data.get("date"), "assessment": assess,
                                         "session": data.get("session")})
    else:
        # §W1 — current pace/HR zones ride along so the workout instruction card can't render
        # from stale numbers (same det-locked grid as the effort monitor + zones card). Private
        # only: the public projection above never carries HR (H7).
        data["zones"] = training_zones(get_db())
    return jsonify(data)


@app.post("/api/readiness")
def api_readiness_post():
    """Submit today's check-in: {energy, sleep, stop_symptom, note}. energy/sleep must be in the
    check-in vocabulary (READINESS_ENERGY / READINESS_SLEEP): the engine only ever tests for "heavy"
    and "poor", so an unknown word used to be stored verbatim and read back as "all signals normal"
    (0.26.3; det/api-validation)."""
    d = body()
    energy, sleep = d.get("energy") or "ok", d.get("sleep") or "ok"
    if energy not in READINESS_ENERGY or sleep not in READINESS_SLEEP:
        return jsonify(ok=False, error=f"energy must be one of {'|'.join(READINESS_ENERGY)} and "
                                       f"sleep one of {'|'.join(READINESS_SLEEP)}"), 400
    db = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    db.execute(
        "INSERT OR REPLACE INTO readiness (date,energy,sleep,stop_symptom,note,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (today, energy, sleep,
         1 if d.get("stop_symptom") else 0, str(d.get("note") or ""), _now_iso()),
    )
    db.commit()
    # §H3 — if THIS check-in flags a stop-symptom (checkbox or the §H2 deterministic note catch),
    # persist a medical hold (mult 0, parity with the chat-apply path) AND regenerate the plan, so the
    # prescription is actually cut to rest and the halt survives to tomorrow — not just a one-day red
    # tile. Gated on this check-in's own signal (not the assessment's halt, which also reflects an
    # already-active hold), so a later green check-in never silently re-arms or extends the window.
    if bool(d.get("stop_symptom")) or _deterministic_stop_symptom(d.get("note", "")):
        directive, _ = clamp_adjustment(
            {"situation": "medical", "volume_multiplier": 0.0, "scope_days": 28, "medical_flag": True,
             "summary": "Exertional stop-symptom flagged in the daily check-in — rest and see your doctor."},
            today)
        base = plan_baseline(db)
        _save_adjustment(db, "Daily check-in: exertional stop-symptom → medical hold", directive)
        db.commit()
        regenerate(db, baseline=base)   # rebuild + persist the plan so today's load drops to rest
    return jsonify(today_readiness(db))


def _activity_payload(db, a):
    """The 'activity tile' view of one activity (raw REST JSON → derived pace/cadence). Shared by the
    latest-activity default and the by-id view (a completed planned session's run)."""
    dist, dur = a.get("distance") or 0, a.get("duration") or 0
    pace = (dur / 60) / dist if dist else None  # min/km
    cad = a.get("cadence")
    if cad and cadence_is_halved(a.get("source")):  # Suunto logs one-leg cadence → ×2 for spm
        cad *= 2
    payload = {
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
    grp = _sj_group_for(db, a.get("id")) if a.get("id") else None
    if grp:                           # §SJ — this recording is part of a 1+1: tell the tile (pace/
        payload["sj"] = {             # distance-class data only, so it serves on both containers)
            "parts": len(grp), "ids": [p["id"] for p in grp],
            "index": next(i for i, p in enumerate(grp) if p["id"] == a.get("id")) + 1,
            "km": round(sum(p["distance"] or 0 for p in grp), 1),
            "min": round(sum(p["duration"] or 0 for p in grp) / 60)}
    if READONLY:                      # §PV — per-run HR is private (the same posture that drops HR
        payload = public_view("activity", payload)   # from the public effort-discipline read),
                                      # withheld server-side, not just in the UI
    return payload


def latest_running_activity(db):
    """For the 'latest running activity' tile: the most-recent RUNNING-FAMILY activity (any sport
    whose name contains 'run' — Running, Trail Running, Treadmill Running, …), plus a note when the
    OVERALL most-recent activity is a non-run (e.g. a tennis match). The non-run still reaches the
    plan via Runalyze's all-sport CTL/ATL snapshot — it just isn't a run to show here. Returns
    (run_row_or_None, cross_note_or_None)."""
    run = db.execute("SELECT raw FROM activities WHERE " + RUN_FAMILY_SQL + " "
                     "ORDER BY date_time DESC LIMIT 1").fetchone()
    top = db.execute("SELECT sport, date FROM activities ORDER BY date_time DESC LIMIT 1").fetchone()
    cross = ({"sport": top["sport"], "date": top["date"]}
             if top and not _is_run_family(top["sport"]) else None)
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


@app.get("/api/activity/<int:aid>/structure")
def api_activity_structure(aid):
    """§RD — the detected workout structure for one activity, classified lazily on first view when
    the sync hook hasn't already (that's the agreed backfill: from-now-on eager, history on open).
    §SJ: when the activity is a PART of a split-recording group, the COMPOSITE read is served —
    whichever part is open, the read-back line tells the whole 1+1 session. §SQ rides along when
    the read carries strides. Public read-only container: served from CACHE ONLY (tokenless — no
    stream fetch) and every HR field withheld server-side — the same H7 posture as the activity
    payload; the pace-based label itself is as public as pace."""
    db = get_db()
    row = db.execute("SELECT date FROM activities WHERE id=?", (aid,)).fetchone()
    if not row:
        return jsonify(None), 404
    err = None
    grp = _sj_group_for(db, aid)
    st = _sj_composite(db, grp, fetch=not READONLY) if grp else None
    if st is None:
        st, err = _structure_cached(db, aid, date_iso=row["date"], fetch=not READONLY)
    if not st:
        return jsonify({"ok": False, "reason": "not classified yet" if READONLY
                        else f"streams unavailable ({err})"})
    if st.get("ok") and (st.get("strides") or 0) > 0:
        reps = ([r for e in st.get("parts", []) for r in (e["read"].get("stride_reps") or [])]
                if st.get("composite") else (st.get("stride_reps") or []))
        src = st
        if st.get("composite"):     # the strides-carrying part owns the sets/pace detail
            src = next((e["read"] for e in st["parts"]
                        if e["read"].get("ok") and (e["read"].get("strides") or 0) > 0), st)
        st = {**st, "sq": _sq_read(db, reps, st.get("strides") or 0,
                                   src.get("stride_sets") or [], src.get("stride_pace"),
                                   row["date"])}
    if READONLY:
        st = _strip_structure_hr(st)
    return jsonify(st)


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
    if READONLY:
        # The public box is tokenless and its DB mount is query_only: never fetch, never write. Serve
        # what is cached (a stale shape is still a pace curve) or say so — before 0.27.1 a miss here
        # made a doomed MCP call and, with a token present, an INSERT on the query_only connection
        # (an HTML 500 instead of the JSON 502; Gemini review #3, verified). The private side fills
        # the cache the owner's views share.
        return cached, RunalyzeError("profile not cached — the public view cannot fetch it")
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


def _zone_idx(hr, cutoffs):
    """Zone index (0–4) of an average HR against the unified hr_zones cutoffs — colours the run
    browser's calendar dots with the SAME grid the chart band/effort monitor read. None without an
    HR value or a usable model (the dot degrades to neutral, never guesses)."""
    if not hr or not cutoffs:
        return None
    return sum(1 for c in cutoffs if hr >= c)


def _agg_new():
    return {"n": 0, "dist": 0.0, "sec": 0.0, "trimp": 0.0, "longest": 0.0, "hw": 0.0, "hs": 0.0}


def _agg_add(a, r):
    a["n"] += 1
    a["dist"] += r["distance"]
    a["sec"] += r["duration"] or 0.0
    a["trimp"] += r["trimp"] or 0.0
    a["longest"] = max(a["longest"], r["distance"])
    if r["hr_avg"] and r["duration"]:                 # duration-weighted avg HR — a long easy
        a["hw"] += r["hr_avg"] * r["duration"]        # hour outweighs a short blast
        a["hs"] += r["duration"]


def _agg_stats(a):
    """One stats-rail column — the same dict shape for the month, 12-month and all-time windows."""
    if not a["n"]:
        return {"runs": 0}
    pace = a["sec"] / (a["dist"] * 60) if (a["sec"] and a["dist"]) else None
    return {"runs": a["n"], "km": round(a["dist"], 1),
            "hms": f"{int(a['sec'] // 3600)}h {int((a['sec'] % 3600) // 60):02d}m" if a["sec"] else None,
            "pace": f"{int(pace)}:{int((pace * 60) % 60):02d}" if pace else None,
            "hr_avg": round(a["hw"] / a["hs"]) if a["hs"] else None,
            "trimp": round(a["trimp"]) if a["trimp"] else None,
            "longest_km": round(a["longest"], 1) if a["longest"] else None}


def _runs_month(db, month):
    """§RB — one calendar month of activity for the /runs explorer: every non-dropped run grouped
    per day (time-ordered, so a double shows both), each with the id the profile/map pipeline needs
    plus a dot colour (dominant-intensity proxy = zone of avg HR; None degrades to neutral). Days
    with only non-run activity are listed separately (faint tick, not clickable — the tile is
    run-centric). `first`/`last` bound the month navigation to where data actually exists.
    The stats rail gets three windows over the same run set in one pass: the browsed month, the
    trailing 12 calendar months ending with it (moves with the nav, so browsing history compares
    like with like), and all time — plus `since`, the first counted run's date."""
    drop = dropped_ids(db)
    cut = (hr_zones(db) or {}).get("cutoffs")
    days, other = {}, set()
    y, m = int(month[:4]), int(month[5:7])
    mlo, mhi = month + "-01", month + "-31"           # ISO strings compare as dates
    k = y * 12 + (m - 1) - 11                         # first month of the trailing-12 window
    ylo = f"{k // 12:04d}-{k % 12 + 1:02d}-01"
    a_month, a_12mo, a_all = _agg_new(), _agg_new(), _agg_new()
    since = None
    month_runs = []
    for r in db.execute(
        "SELECT id, date, date_time, sport, distance, duration, elapsed_time, hr_avg, trimp "
        "FROM activities ORDER BY date_time").fetchall():
        if r["id"] in drop or not r["date"]:
            continue
        in_month = mlo <= r["date"] <= mhi
        if not (_is_run_family(r["sport"]) and (r["distance"] or 0) > 0):
            if in_month:
                other.add(r["date"])
            continue
        since = since or r["date"]
        _agg_add(a_all, r)
        if ylo <= r["date"] <= mhi:
            _agg_add(a_12mo, r)
        if not in_month:
            continue
        _agg_add(a_month, r)
        month_runs.append(r)
    # §SJ — a split recording renders as ONE day entry (the session), km summed, pace over the
    # whole outing, dot colour from the duration-weighted HR; click lands on the first part (the
    # composite read is served for any part). Stats above stay PER RECORDING (they count data rows).
    for grp in _session_groups(month_runs):
        r = grp[0]
        km = sum((p["distance"] or 0) for p in grp)
        dur = sum((p["duration"] or 0) for p in grp)
        hrs = [(p["hr_avg"], p["duration"] or 0) for p in grp if p["hr_avg"]]
        hr_w = round(sum(h * w for h, w in hrs) / sum(w for _, w in hrs)) if hrs else None
        pace = (dur / (km * 60)) if (dur and km) else None
        entry = {"id": r["id"], "t": (r["date_time"] or "")[11:16], "km": round(km, 1),
                 "pace": f"{int(pace)}:{int((pace * 60) % 60):02d}" if pace else None,
                 "z": _zone_idx(hr_w, cut)}
        if len(grp) > 1:
            entry["sj"] = len(grp)
        days.setdefault(r["date"], []).append(entry)
    b = db.execute("SELECT MIN(date) AS lo, MAX(date) AS hi FROM activities WHERE "
                   + RUN_FAMILY_SQL).fetchone()
    return {"ok": True, "month": month, "days": days, "stats": _agg_stats(a_month),
            "stats12": _agg_stats(a_12mo), "statsAll": _agg_stats(a_all), "since": since,
            "other": sorted(other - set(days)),
            "first": (b["lo"] or "")[:7] or None, "last": (b["hi"] or "")[:7] or None}


@app.get("/api/runs")
def api_runs():
    """§RB — the run browser's calendar month. Carries HR-zone classification and feeds the
    route-map viewer ⇒ private-only (H7), same guard as /api/zones."""
    if READONLY:
        return jsonify(ok=False, error="not available on the public view"), 403
    month = request.args.get("month") or datetime.now().strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        return jsonify(ok=False, error="month must be YYYY-MM"), 400
    return jsonify(_runs_month(get_db(), month))


@app.get("/api/activity/<int:aid>/profile")
def api_activity_profile(aid):
    """Downsampled pace/HR/cadence/elevation profile for the hover backgrounds. Cached locally so we
    hit the MCP at most once per activity. Geo is stripped — the route map is private, served by /map."""
    db = get_db()
    prof, err = _profile_cached(db, aid)
    if prof is None:
        return jsonify(error=str(err), dist=[], pace=[], hr=[]), 502
    out = _strip_geo(prof)
    out["hrmax"] = _robust_hrmax(db)   # kept for the avg line / defensive zone fallback
    # The unified HR-zone model (LTHR-anchored when confident, %HRmax fallback) — ONE definition shared by
    # the chart hover, the zone band, and the effort monitor. Set on the endpoint (not baked into the cached
    # blob) so it stays live as LTHR drifts. bpm cutoffs are HR-derived ⇒ private, stripped on the public box.
    out["hrzones"] = hr_zones(db)
    if READONLY:                       # §PV — the per-second HR stream is private, like avg/max HR:
        out = public_view("profile", out)   # the public container serves the profile for the pace
                                       # overlay, HR-stripped (the chart reads `has_hr` and degrades)
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
    weeks, err = _int_arg("weeks", 26, lo=0, hi=520)   # 0 = the full history (the chart's own call)
    if err:
        return err
    rows = db_weekly_running()
    rows = rows[-weeks:] if weeks > 0 else rows
    return jsonify(public_view("weekly", rows) if READONLY else rows)


@app.get("/api/vo2max")
def api_vo2max():
    """Per-activity VO₂max trend over the last `months` (default 6) — feeds the VO₂max tile's
    background sparkline. shape_snapshots only holds today's value, so the trend comes from
    each run's own vo2max estimate (in the raw activity JSON), lightly smoothed."""
    db = get_db()
    months, err = _int_arg("months", 6, hi=120)
    if err:
        return err
    return jsonify(vo2max_trend(db, months))


def vo2max_trend(db, months=6):
    """Build a smoothed VO₂max series from runs Runalyze counts toward fitness
    (`use_vo2max`), within the window. Per-run vo2max is noisy, so we EWMA-smooth it to
    mirror the 'effective' value the tile shows; we return both raw and smoothed."""
    from datetime import timedelta
    cutoff = (datetime.now().date() - timedelta(days=round(months * 30.4))).isoformat()
    rows = db.execute(
        "SELECT date, raw FROM activities WHERE " + RUN_FAMILY_SQL + " AND date >= ? ORDER BY date ASC",
        (cutoff,),
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
        "SELECT date, distance FROM activities WHERE " + RUN_FAMILY_SQL + " AND date IS NOT ''"
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


_config_swap(weather_cities=_parse_weather_cities(os.environ.get("SH_WEATHER_CITIES", "")))
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
    for c in config().weather_cities:
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


# ── §BX — Backup / export (private-only) ────────────────────────────────────
# The 1.0 data-safety story. Two artifacts, both owner-only downloads:
#   • /api/backup/db     — a consistent FULL snapshot of the main DB (VACUUM INTO: WAL-safe, compact).
#                          Restore = stop the container, drop the file in ./data as sparinghorse.db.
#   • /api/export/json   — a portable JSON of the NON-REBUILDABLE, user-authored tables. Runalyze can
#                          re-backfill activities/snapshots and every cache is derived; what CANNOT be
#                          rebuilt is the owner's judgment + history: objectives (+outcomes), readiness
#                          check-ins, session_log reflections, adjustments, health_markers (labs are
#                          manual), ignored_activities (dedup verdicts), the versioned plans (banked
#                          evidence + frozen weeks), and meta (settings, rebase_start, LTHR stamps).
# Secrets are structurally absent: keys/tokens live in the separate SH_SECRETS_DB store (§ above),
# never the main DB — asserted by det/backup-export so a future table can't silently leak.
# Import (fresh-instance restore of the JSON) is CLI-only: `python SparingHorse.py import <file>` —
# it refuses a target whose user tables aren't empty; a live instance restores via the DB snapshot.

EXPORT_TABLES = ["objectives", "readiness", "session_log", "adjustments", "health_markers",
                 "ignored_activities", "plans", "meta"]
EXPORT_FORMAT = 1


def export_user_data(db):
    """The portable JSON payload: every non-rebuildable table, rows verbatim."""
    return {"app": "SparingHorse", "format": EXPORT_FORMAT, "exported_at": _now_iso(),
            "tables": {t: [dict(r) for r in db.execute(f"SELECT * FROM {t}").fetchall()]
                       for t in EXPORT_TABLES}}


def import_user_data(db, payload):
    """Fresh-instance restore of an export_user_data payload. Refuses unless every target table
    (meta aside — init writes there) is EMPTY: this is a restore, not a merge, and silently mixing
    two histories would corrupt banked evidence. Columns are intersected with the live schema so an
    export from an older version loads into a newer one (new columns default)."""
    if payload.get("app") != "SparingHorse" or "tables" not in payload:
        return {"ok": False, "error": "not a SparingHorse export file"}
    if payload.get("format", 0) > EXPORT_FORMAT:
        return {"ok": False, "error": f"export format {payload['format']} is newer than this app understands"}
    for t in EXPORT_TABLES:
        if t != "meta" and db.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone():
            return {"ok": False, "error": f"table '{t}' is not empty — import only restores into a fresh instance"}
    counts = {}
    for t in EXPORT_TABLES:
        rows = payload["tables"].get(t) or []
        if not rows:
            counts[t] = 0
            continue
        live_cols = [r["name"] for r in db.execute(f"PRAGMA table_info({t})").fetchall()]
        cols = [c for c in live_cols if c in rows[0]]
        db.executemany(
            f"INSERT OR REPLACE INTO {t} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [tuple(r.get(c) for c in cols) for r in rows])
        counts[t] = len(rows)
    db.commit()
    return {"ok": True, "restored": counts}


@app.get("/api/backup/db")
def api_backup_db():
    """Consistent full-DB snapshot download (private-only via `_private_only_path`)."""
    import io, tempfile
    tmp = Path(tempfile.mkdtemp()) / "snapshot.db"
    try:
        get_db().execute("VACUUM INTO ?", (str(tmp),))   # WAL-safe, page-consistent, compacted
        buf = io.BytesIO(tmp.read_bytes())
    finally:
        if tmp.exists():
            tmp.unlink()
        tmp.parent.rmdir()
    return send_file(buf, mimetype="application/vnd.sqlite3", as_attachment=True,
                     download_name=f"sparinghorse-backup-{datetime.now().date().isoformat()}.db")


@app.get("/api/export/json")
def api_export_json():
    """Portable user-data export download (private-only via `_private_only_path`)."""
    import io
    buf = io.BytesIO(json.dumps(export_user_data(get_db()), indent=1).encode())
    return send_file(buf, mimetype="application/json", as_attachment=True,
                     download_name=f"sparinghorse-export-{datetime.now().date().isoformat()}.json")


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


@app.get("/api/secrets")
def api_secrets():
    """Status of the Runalyze token + Claude key — configured flag + provenance ONLY, never the value.
    Private-only via `_private_only_path` (the public container 403s it)."""
    return jsonify(ok=True, secrets=secret_status())


@app.post("/api/secrets")
def api_secrets_save():
    """Set or clear a secret in the private-only store; applied live (no restart, no .env edit). The
    `_readonly_guard` already 403s this on the public container. Body: {key, value}; an empty value
    clears the secret (reverting to the env var, if any). Never echoes the value back."""
    d = body()
    ok, err = save_secret(d.get("key", ""), d.get("value", ""))
    if not ok:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, secrets=secret_status())


@app.get("/api/secrets/validate")
def api_secrets_validate():
    """Live validity of each configured secret — valid / invalid / unset / unknown. A cheap authenticated
    probe with NO generation cost (Runalyze statistics, Anthropic GET /v1/models). Private-only via
    `_private_only_path`; the public container 403s it. Probes run CONCURRENTLY so the worst case is one
    timeout (~8s), not the sum across keys — `validate_secret` touches only module globals + its own
    sqlite connection + the network, so it's safe off the request thread."""
    from concurrent.futures import ThreadPoolExecutor
    keys = [s["key"] for s in SECRET_SPEC]
    with ThreadPoolExecutor(max_workers=max(1, len(keys))) as ex:
        results = dict(zip(keys, ex.map(validate_secret, keys)))
    return jsonify(ok=True, results=results)


# ── §SG Suunto endpoints (all under /api/suunto → private-only + readonly-guarded) ──
def _suunto_redirect_uri():
    """The callback URL the browser lands back on — MUST byte-match the redirect URI registered on
    the Suunto OAuth app. https behind the proxy (Cloudflare terminates TLS, so request.url_root
    says http), plain http only for local dev hosts."""
    host = request.host
    scheme = "http" if host.split(":")[0] in ("127.0.0.1", "localhost", "0.0.0.0") else "https"
    return f"{scheme}://{host}/api/suunto/callback"


@app.get("/api/suunto/status")
def api_suunto_status():
    return jsonify(ok=True, **suunto_status(), redirect_uri=_suunto_redirect_uri())


@app.get("/api/suunto/connect")
def api_suunto_connect():
    """Kick off the one-time OAuth dance: bounce the browser to Suunto's authorize page. A `state`
    nonce (10-min validity) rides along so the callback can reject forged redirects."""
    conf = _suunto_conf()
    if not (conf["suunto_client_id"] and conf["suunto_client_secret"]):
        return jsonify(ok=False, error="set the Suunto client ID + secret in Settings first"), 400
    state = secrets.token_urlsafe(24)
    now = time.time()
    _suunto_oauth_state.clear()   # single-user app: one dance in flight at a time
    _suunto_oauth_state[state] = now
    from urllib.parse import urlencode
    q = urlencode({"response_type": "code", "client_id": conf["suunto_client_id"],
                   "redirect_uri": _suunto_redirect_uri(), "state": state})
    return redirect(f"{SUUNTO_OAUTH_BASE}/oauth/authorize?{q}")


@app.get("/api/suunto/callback")
def api_suunto_callback():
    """The browser returns here with ?code — exchange it, store the token pair, land on the
    dashboard. Errors render as a plain redirect with a flag the Settings window surfaces."""
    state, code = request.args.get("state", ""), request.args.get("code", "")
    issued = _suunto_oauth_state.pop(state, None)
    if not issued or time.time() - issued > 600:
        return redirect("/?suunto=state_error")
    if not code:
        return redirect("/?suunto=denied")
    try:
        tok = _suunto_token_request({"grant_type": "authorization_code", "code": code,
                                     "redirect_uri": _suunto_redirect_uri()})
    except Exception as e:
        print(f"[suunto] code exchange failed: {e}")
        return redirect("/?suunto=exchange_error")
    _save_suunto_tokens(tok)
    print(f"[suunto] connected as {tok.get('user')}")
    return redirect("/?suunto=connected")


@app.post("/api/suunto/push")
def api_suunto_push():
    """Manual 'push my week to the watch now' — same code path as the nightly push."""
    d = body()
    res = push_guides(get_db(), days=int(d.get("days") or SUUNTO_PUSH_DAYS))
    return jsonify(**res), (200 if res.get("ok") or res.get("skipped") else 502)


@app.post("/api/suunto/disconnect")
def api_suunto_disconnect():
    """Forget the stored user tokens (the app credentials in Settings stay)."""
    _save_suunto_tokens(None)
    return jsonify(ok=True, **suunto_status())


def _render_app(page="dash"):
    # inject the mode flag + private-console URL synchronously so the UI gates with no round-trip
    # HOUSE_URL/NAME can now be set via the Settings panel (validated) OR raw env (unvalidated), and
    # are injected into header HTML — so escape at the render site regardless of source (defence in
    # depth, not relying on the save-time char check alone).
    cfg = config()      # TECH-4 — one snapshot for the whole page render
    hublink = (f'<a class="hublink" href="{html.escape(cfg.house_url, quote=True)}">'
               f'← {html.escape(cfg.house_name or cfg.house_url)}</a>'
               if cfg.house_url else "")
    doc = html_page(INDEX_HTML
            .replace("__SH_READONLY__", "true" if READONLY else "false")
            # json.dumps escapes quotes/backslashes but NOT "/", so neutralise "</" → a value with
            # "</script>" (e.g. a raw env SH_PRIVATE_URL that bypassed validate_setting) can't close
            # the inline <script> and inject markup into the (public) page.
            .replace("__SH_PRIVATE_URL__", json.dumps(cfg.private_url).replace("</", "<\\/"))
            .replace("__RUNALYZE_LOGO__", RUNALYZE_LOGO_SVG)
            # §RB — one document, two pages: <body data-page> selects the dashboard (status) or the
            # /runs explorer (look-up); CSS shows each page's sections, JS gates its loaders. Keeps
            # the single-file SPA while the explorer surfaces get their own URL.
            .replace("__SH_PAGE__", page)
            # The public read-only box removes the health section, so its Body tab would open empty —
            # drop the Body nav button there (private keeps all four). The mobile grid auto-sizes to the
            # button count, and the nav wiring derives its tab list from the buttons actually present.
            .replace("__MOBNAV_BODY__", "" if READONLY else
                     '<button class="mnav-btn" type="button" data-goto="body" aria-current="false" '
                     'aria-label="Body"><svg viewBox="0 0 24 24" aria-hidden="true">'
                     '<path d="M12 3s6 6.4 6 11a6 6 0 0 1-12 0c0-4.6 6-11 6-11z"/></svg><span>Body</span></button>')
            # §RB — the Runs explorer is PRIVATE-ONLY (route geo + HR), so the public shell gets no
            # Runs tab at all (same reasoning as the Body drop above: never a dead/empty destination).
            .replace("__MOBNAV_RUNS__", "" if READONLY else
                     '<a class="mnav-btn" id="mnavruns" href="/runs" aria-current="false" '
                     'aria-label="Runs"><svg viewBox="0 0 24 24" aria-hidden="true">'
                     '<circle cx="5.5" cy="18.5" r="2.3"/><circle cx="18.5" cy="5.5" r="2.3"/>'
                     '<path d="M7.5 17C14 15.5 10.5 8 16.5 6.5"/></svg><span>Runs</span></a>')
            .replace("__SH_HUBLINK__", hublink)
            # Cache-bust CSS/JS per release: the shell itself is no-cache (below), so a deploy lands
            # on an ordinary reload instead of serving yesterday's app out of the browser cache.
            .replace("__SH_VER__", ENGINE_VERSION))
    # The whole SPA — markup + inline JS — is this one document. Tell the browser to revalidate it
    # every load so a deploy takes effect on an ordinary reload (no hard-refresh needed): browsers
    # otherwise heuristically cache an un-headered HTML doc and serve stale JS after a release.
    return (doc, 200, {"Cache-Control": "no-cache"})


@app.get("/")
def index():
    return _render_app("dash")


@app.get("/runs")
def runs_page():
    """§RB — the run-browser explorer page. PRIVATE-ONLY: it exists to browse route maps + HR-graded
    history (the H7 surface), so the public container redirects home rather than serving a husk."""
    if READONLY:
        return redirect("/")
    return _render_app("runs")


# ── The SPA (house terracotta theme + daylight light mode) ───────────────────
# ── The SPA — served from static/, not carried in this file (TECH-11) ────────
# The whole front end used to be a 270 KB string literal here: 74 KB of CSS and 182 KB of JavaScript
# that no editor, linter or reviewer could see as code. It lives in `static/` now — index.html is the
# shell (markup + the server-substituted bits), app.css and app.js are ordinary files a browser fetches
# and `node --check` can parse. The split was mechanical and byte-parity-checked: re-inlining the three
# files reproduces the old document exactly.
#
# Read once at import, like the literal was. The dets scan `UI_SOURCE` — all three concatenated — so a
# text assertion still sees the whole front end however it is split up.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_HTML = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
APP_CSS = (_STATIC_DIR / "app.css").read_text(encoding="utf-8")
APP_JS = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")
UI_SOURCE = INDEX_HTML + "\n" + APP_CSS + "\n" + APP_JS


# ── Scheduled daily sync ─────────────────────────────────────────────────────
# The private (writable, tokened) side pulls the day's activities once a night so the shared DB —
# and so the public page — stays current without a manual "Sync now". Runalyze has no push we can
# rely on, so it's a tiny in-process daily timer: no extra deps, no host cron, runs the same locally
# and on the NAS (under waitress too, since it starts at import). Default 22:00 Luxembourg time
# (late enough to catch the day's runs). Inert on the read-only/tokenless public container.
_scheduler_started = False
# The hour the nightly job fires, in SH_TZ. It must land AFTER the day's last run has been ingested
# UPSTREAM, not merely after the run ends: the job syncs and then re-plans, so firing early re-plans
# the current week from actuals it cannot see yet. §PRO20 took the SEED off this clock (it is
# end-of-yesterday whenever it is read), but `week_actuals` — which decides whether the week's volume
# is already run — still reads the day. Overridable via SH_SYNC_AT (validated in start_scheduler).
SYNC_AT_DEFAULT = "22:00"
# Fire the nightly sync at your wall-clock hour, not the container's. Set SH_TZ to your IANA zone
# (e.g. "Europe/Lisbon", "America/New_York"); defaults to UTC. Falls back to UTC on a bad name.
try:
    _config_swap(sync_tz=ZoneInfo(os.environ.get("SH_TZ", "UTC")))
except Exception:
    _config_swap(sync_tz=ZoneInfo("UTC"))


def _seconds_until(hhmm):
    """Seconds until the next HH:MM in Luxembourg local time (DST-aware), so the job fires at the
    same wall-clock hour whatever timezone the container runs in (the NAS containers run UTC)."""
    h, m = (int(x) for x in hhmm.split(":"))
    now = datetime.now(config().sync_tz)
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


def _backup_rotate():
    """After a successful nightly: a consistent snapshot beside the DB (VACUUM INTO — WAL-safe,
    page-consistent, compacted), dated, newest 7 kept — the NAS volume then holds a week of nightlies
    with zero cron. Best-effort: a failed snapshot must never fail the nightly (caller catches)."""
    day = datetime.now().strftime("%Y-%m-%d")
    target = DB_PATH.parent / f"{DB_PATH.stem}-backup-{day}.db"
    if target.exists():
        target.unlink()          # VACUUM INTO wants a fresh path; a same-day re-run replaces it
    db = connect_db()
    try:
        db.execute("VACUUM INTO ?", (str(target),))
    finally:
        db.close()
    for f in sorted(DB_PATH.parent.glob(f"{DB_PATH.stem}-backup-*.db"))[:-7]:
        f.unlink()


def _nightly_job(kind="nightly"):
    """One full nightly pass — the scheduled run AND the boot catch-up share this: sync → daily
    re-plan → Suunto guides → rotated DB snapshot, with the outcome recorded in meta
    (sched:last_run / sched:last_ok / sched:fail_count) so /healthz and the catch-up can see it."""
    ok = False
    try:
        with _sync_lock:           # never overlap a "Sync now" / page-load pull
            res = run_sync()
        print(f"[scheduler] {kind} sync ok: {res.get('activities')}")
        ok = True
    except Exception as e:
        print(f"[scheduler] {kind} sync failed: {e}")
    db = connect_db()
    try:
        set_meta(db, "sched:last_run", _now_iso())
        if ok:
            set_meta(db, "sched:last_ok", _now_iso())
            set_meta(db, "sched:fail_count", "0")
        else:
            set_meta(db, "sched:fail_count", str(int(get_meta(db, "sched:fail_count", "0") or 0) + 1))
        db.commit()
    finally:
        db.close()
    # §6b — recompute against today even if the sync failed: advancing the plan's date and
    # §6e banking needs no fresh pull, so a flaky Runalyze night must not also freeze the plan.
    try:
        _daily_replan()
    except Exception as e:
        print(f"[scheduler] daily re-plan failed: {e}")
    # §SG — after the re-plan, keep the watch current: push the refreshed next-days sessions
    # as SuuntoPlus Guides. No-ops (skipped=True) when Suunto isn't connected; push_guides
    # never raises, but the belt-and-braces try keeps a converter surprise from killing the loop.
    try:
        db = connect_db()
        try:
            res = push_guides(db)
        finally:
            db.close()
        if not res.get("skipped"):
            print(f"[scheduler] suunto guides push: {res.get('pushed', 0)} pushed"
                  + (f" — {res['error']}" if res.get("error") else ""))
    except Exception as e:
        print(f"[scheduler] suunto guides push failed: {e}")
    if ok:
        try:
            _backup_rotate()
        except Exception as e:
            print(f"[scheduler] backup rotation failed: {e}")


def _sched_catchup_needed(db):
    """Boot check: is a nightly owed? True when no successful run is recorded, or the last success is
    older than 26 h. 26, not 24: the nightly's own wall-clock jitter (a slow night, a restart inside
    the trigger minute) must not fire a duplicate pass on every boot."""
    last_ok = get_meta(db, "sched:last_ok")
    return (not last_ok) or _seconds_since(last_ok) > 26 * 3600


def _scheduler_loop(hhmm):
    while True:
        time.sleep(_seconds_until(hhmm))
        _nightly_job()
        time.sleep(61)  # step past the trigger minute before recomputing the next wait


def start_scheduler():
    """Start the nightly sync thread — only on a writable, tokened instance, and only once."""
    global _scheduler_started
    if _scheduler_started or READONLY or not config().runalyze_token:
        return
    if os.environ.get("SH_SCHEDULE", "1").lower() not in ("1", "true", "yes"):
        return
    # An UNSET compose pass-through (`- SH_SYNC_AT=${SH_SYNC_AT:-}`) arrives as an EMPTY string, not as
    # absent — so `.get(key, default)` would hand "" straight to `_seconds_until`, whose split(":")
    # raises inside the daemon thread and takes the whole nightly sync down silently. Blank or
    # malformed ⇒ fall back to the default and SAY SO, rather than dying quietly at 22:00.
    hhmm = (os.environ.get("SH_SYNC_AT") or "").strip() or SYNC_AT_DEFAULT
    try:
        _h, _m = (int(x) for x in hhmm.split(":"))
        if not (0 <= _h <= 23 and 0 <= _m <= 59):
            raise ValueError(hhmm)
    except (ValueError, TypeError):
        print(f"[scheduler] SH_SYNC_AT={hhmm!r} is not HH:MM — falling back to {SYNC_AT_DEFAULT}")
        hhmm = SYNC_AT_DEFAULT
    threading.Thread(target=_scheduler_loop, args=(hhmm,), daemon=True).start()
    _scheduler_started = True
    print(f"Sparing Horse → scheduled daily sync at {hhmm} {config().sync_tz.key}")
    # Boot catch-up (TECH-8): a container restart across the nightly minute used to skip the night
    # silently. If no successful run is recorded in the last 26 h, run one pass now — in its own
    # thread, so boot never blocks on a Runalyze pull.
    try:
        db = connect_db()
        try:
            owed = _sched_catchup_needed(db)
        finally:
            db.close()
        if owed:
            print("[scheduler] no successful nightly in the last 26 h — running a catch-up pass now")
            threading.Thread(target=_nightly_job, args=("catch-up",), daemon=True).start()
    except Exception as e:
        print(f"[scheduler] boot catch-up check failed: {e}")




# ── Self-test routes (private only — gated off the public container in _readonly_guard) ──
def _selftest_module():
    """Import the battery LAZILY (TECH-1). It lives in `sh_selftest.py` now, so a typo in a det can
    no longer take the web app and the nightly scheduler down at import time — it breaks the
    self-test, and nothing else. The alias is what makes the harness bind to THIS module object:
    `python SparingHorse.py` loads the app as `__main__`, and a bare `import SparingHorse` over
    there would load a SECOND copy of it, so every global the battery rebinds (READONLY, the
    tokens, `regenerate`) would land on an object this process never reads."""
    sys.modules.setdefault("SparingHorse", sys.modules[__name__])
    import sh_selftest
    return sh_selftest


_selftest_proc = None                  # {"proc", "out", "copy", "started", "cats"} while one runs
_selftest_proc_lock = threading.Lock()


def _selftest_spawn(cats, extra_args=()):
    """Run the battery as its OWN PROCESS against a snapshot of this instance's database (TECH-1).

    In-process, the battery rebound module globals (READONLY, the tokens, `regenerate`) for ~40 s,
    which is why every other request had to answer 503 meanwhile — the app's only self-inflicted
    downtime, and it was self-inflicted by its own test suite. A separate process cannot reach those
    globals at all, so the app keeps serving normally while a battery runs.

    The snapshot is VACUUM INTO, so the scenarios that read the host DB judge THIS instance's real
    data (its inventory, its shape, its card truth) without any of them being able to write to it.
    The child inherits the container's environment, so key-gated checks still test THIS box's keys."""
    st_path = str(Path(__file__).resolve().parent / "sh_selftest.py")
    tmp = Path(tempfile.mkdtemp(prefix="sh-selftest-"))
    # Only a SPAWNED child gets a reaper to clean up after it, so anything that raises between here
    # and a live process must take the snapshot with it — otherwise a failed start leaves a full copy
    # of the database sitting in /tmp until the box reboots.
    try:
        copy, out = tmp / "snapshot.db", tmp / "report.json"
        db = connect_db()
        try:
            db.execute("VACUUM INTO ?", (str(copy),))
        finally:
            db.close()
        argv = [sys.executable, st_path, "--db", str(copy), "--json", str(out)]
        if cats:
            argv += ["--only", ",".join(sorted(cats))]
        # `extra_args` exists for exactly one caller: det/selftest-subprocess passes --dry-run so it
        # can exercise this whole path — real snapshot, real process, real argv — for the cost of one
        # spawn instead of a nested battery (which would contain that det, and recurse).
        argv += list(extra_args)
        proc = subprocess.Popen(argv, cwd=str(Path(st_path).parent), env=dict(os.environ),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return {"proc": proc, "out": out, "tmp": tmp, "started": _now_iso(), "cats": sorted(cats or [])}


def _selftest_reap(handle):
    """Wait for the child, persist its report to the LIVE db, and clear the temp snapshot."""
    global _selftest_proc
    try:
        tail = (handle["proc"].communicate()[0] or "")[-4000:]
        report = None
        if handle["out"].exists():
            report = json.loads(handle["out"].read_text(encoding="utf-8"))
        if report is None:                      # the child died before writing a report
            report = {"created_at": _now_iso(), "source": "server", "env": {"llm": llm_available(),
                      "readonly": READONLY},
                      "summary": {"passed": 0, "failed": 1, "skipped": 0, "needs_human": 0, "total": 1},
                      "scenarios": [{"category": "error", "id": "subprocess", "desc": "the battery "
                                     "process died before writing a report", "passed": False,
                                     "error": tail or f"exit {handle['proc'].returncode}"}]}
        if report.get("source") == "dry-run":
            # det/selftest-subprocess drives this path with a child that runs no scenarios. Saving it
            # would file an all-zero run against a real database every time the battery runs inline.
            report["id"] = None
        else:
            with app.app_context():
                db = get_db()
                report["id"] = _selftest_module().save_selftest_run(db, report)
    except Exception as e:                      # never let the reaper thread die silently
        print(f"[selftest] reaping the battery failed: {e}")
    finally:
        shutil.rmtree(handle["tmp"], ignore_errors=True)
        with _selftest_proc_lock:
            # ONLY if it is still ours: a battery started in the window between this child exiting
            # and this line would otherwise have its handle cleared out from under it, and the next
            # start would be waved through instead of answering 409.
            if _selftest_proc is handle:
                _selftest_proc = None


@app.post("/api/selftest/run")
def api_selftest_run():
    """Start a battery. Answers immediately — poll /api/selftest/status, then read the saved run."""
    global _selftest_proc
    cats = request.args.get("only")
    with _selftest_proc_lock:
        if _selftest_proc and _selftest_proc["proc"].poll() is None:
            return jsonify(ok=False, error="a self-test battery is already running",
                           started=_selftest_proc["started"]), 409
        try:
            _selftest_proc = _selftest_spawn(set(cats.split(",")) if cats else None)
        except Exception as e:
            return jsonify(ok=False, error=f"could not start the battery: {e}"), 500
        handle = _selftest_proc
    threading.Thread(target=_selftest_reap, args=(handle,), daemon=True).start()
    return jsonify(ok=True, running=True, started=handle["started"]), 202


@app.get("/api/selftest/status")
def api_selftest_status():
    """Is a battery running, and what was the last saved run? The page polls this."""
    with _selftest_proc_lock:
        h = _selftest_proc
        running = bool(h and h["proc"].poll() is None)
        started = h["started"] if h else None
    row = get_db().execute("SELECT id, created_at, passed, failed, skipped, needs_human "
                           "FROM selftest_runs ORDER BY id DESC LIMIT 1").fetchone()
    return jsonify(ok=True, running=running, started=started,
                   last=dict(row) if row else None)


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
        return app.response_class(_selftest_module()._selftest_text(json.loads(row["report"])),
                                  mimetype="text/plain")
    return app.response_class(row["report"], mimetype="application/json")


@app.post("/api/selftest/client")
def api_selftest_client():
    """Store browser self-check results (the client harness POSTs here) as a run row."""
    db = get_db()
    results = body().get("scenarios", [])
    for r in results:                       # normalise shape from the client
        r.setdefault("category", "client"); r.setdefault("needs_human", False)
        r.setdefault("skipped", False); r.setdefault("passed", None)
    st = _selftest_module()
    report = st._selftest_report(results, "client")
    report["env"]["ua"] = request.headers.get("User-Agent", "")
    report["id"] = st.save_selftest_run(db, report)
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
// Every scenario field goes through esc() before innerHTML — the page only ever renders its own
// constants and the fresh server report, but a raw sink is a raw sink (0.27.0 hygiene, log §70).
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function row(r){
  const tag = r.skipped?"SKIP":TAG[r.passed];
  const detail = r.error ? ("error: "+r.error)
    : (r.output!=null ? JSON.stringify(r.output,null,1) : (r.note||r.got!=null?JSON.stringify(r.got):""));
  const tr=document.createElement("tr");
  tr.innerHTML=`<td><span class="tag ${tag}">${tag}</span>${r.needs_human?' <span class="flag" title="captured for human/AI judgment">⚑</span>':''}</td>
    <td><b>${esc(r.category)}/${esc(r.id)}</b><div class="sub">${esc(r.desc||"")}</div></td>
    <td><pre>${esc(detail||"")}</pre></td>`;
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
  // The verdict lives at j.assessment.verdict and always has (today_readiness returns
  // {date, assessment, session}). This probe read j.verdict / j.readiness.verdict — neither of which
  // the endpoint has ever returned — so it reported FAIL with an empty output from the day it was
  // written (2026-06-19) until an owner ran the browser check and read the report. A manual harness
  // nobody re-runs is a test that rots in silence; det/client-probe now pins both ends of this.
  const a=j.assessment||{};
  const verdict=a.verdict||j.verdict||(j.readiness&&j.readiness.verdict);
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
  // The battery runs as its own PROCESS now (TECH-1), so this starts it and polls instead of
  // holding a 40-second request open — and the app keeps serving normally while it runs.
  tb.innerHTML=""; $("#sum").textContent="starting server battery…";
  const start=await fetch("/api/selftest/run",{method:"POST"});
  if(start.status===409){ const e=await start.json();
    $("#sum").textContent=`a battery is already running (started ${e.started||"just now"}) — waiting for it…`; }
  else if(!start.ok){ const e=await start.json().catch(()=>({}));
    $("#sum").textContent=`could not start the battery: ${e.error||start.status}`; return; }
  const before=(await (await fetch("/api/selftest/status")).json()).last;
  for(let i=0;i<300;i++){
    await new Promise(r=>setTimeout(r,2000));
    const st=await (await fetch("/api/selftest/status")).json();
    $("#sum").textContent=`running server battery… ${2*(i+1)}s`;
    if(!st.running && st.last && (!before || st.last.id!==before.id)){
      const rep=await (await fetch(`/api/selftest?id=${st.last.id}`)).json();
      rep.scenarios.forEach(row); summarise(rep.summary);
      $("#sum").innerHTML+=` · saved run <code>#${st.last.id}</code> · <a href="/api/selftest?id=${st.last.id}">JSON</a> · <a href="/api/selftest?id=${st.last.id}&text=1">text</a>`;
      return;
    }
  }
  $("#sum").textContent="the battery is taking longer than 10 minutes — check /api/selftest";
}
$("#run").addEventListener("click",runClient);
$("#server").addEventListener("click",runServer);
$("#json").addEventListener("click",()=>location.href="/api/selftest");
runClient();
</script></body></html>"""


# ── Synthetic seed (local test instance / demo mode) ─────────────────────────
# A deterministic, TOKEN-FREE fixture: generate a believable ~24-week running history into
# `activities`, then derive the daily shape series with the engine's OWN reconstruction
# (reconstruct_history) so every downstream view — tiles, fitness/fatigue chart, projector,
# drift, effort — agrees because they all read the same numbers. Lets a local PRIVATE instance
# render fully populated with NO RUNALYZE_TOKEN, so real UI flows (open→close→reopen dialogs,
# re-plan, drill-downs) can be exercised end-to-end — the gap an isolated CSS harness can't
# cover. Doubles as an open-source demo: `python SparingHorse.py seed` then run with SH_DB
# pointed at the seeded file. Synthetic data only — no personal/real numbers.
def seed_synthetic_db(db, weeks=24, end=None, seed=42, with_objective=True, past_race=False,
                      cold=False):
    import random
    rnd = random.Random(seed)
    today = _date(end) if end else datetime.now().date()
    # wipe the tables we own so re-seeding an existing file is idempotent
    for t in ("activities", "shape_snapshots", "objectives", "health_markers", "plans",
              "readiness", "session_log", "ignored_activities", "adjustments", "trackcache"):
        db.execute(f"DELETE FROM {t}")
    db.execute("DELETE FROM meta WHERE key IN ('last_sync', 'rebase_start')")

    if cold:
        # §FT5 — the cold-start fixture, exactly the spec's "any runner" intake: ONE hard 10k
        # (with HR fields, no per-run vo2max — the corpus EWMA must stay empty) + one objective +
        # NO snapshots, NO history. generate_plan must seed itself from the intake alone.
        d = (today - timedelta(days=1)).isoformat()
        db.execute("INSERT INTO activities(id,date,date_time,sport,sport_id,distance,duration,"
                   "elapsed_time,hr_avg,hr_max,trimp,raw,synced_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (1, d, d + "T09:00:00+00:00", "Running", RUNNING_SPORT, 10.05, 3000.0, 3010.0,
                    172, 184, 95.0, json.dumps({"id": 1, "distance": 10.05, "duration": 3000.0,
                                                "hr_avg": 172, "hr_max": 184}), _now_iso()))
        db.execute("INSERT INTO objectives(type,label,date,target,priority,status,created_at) "
                   "VALUES('marathon','Cold-Start Marathon',?,'finish','A','upcoming',?)",
                   ((today + timedelta(weeks=20)).isoformat(), _now_iso()))
        set_meta(db, "last_sync", _now_iso())
        set_meta(db, "synthetic_seed", "1")
        db.commit()
        plan = regenerate(db)
        return {"activities": 1, "snapshots": 0, "plan_ok": bool(plan and plan.get("ok")),
                "from": d, "to": d}

    # last run lands yesterday, so 'today' is a fresh planning day
    last_day = today - timedelta(days=1)
    start_monday = (last_day - timedelta(days=last_day.weekday())
                    - timedelta(weeks=weeks - 1))
    # per-zone fixture knobs: pace (sec/km), avg HR, spread from avg→max HR, tile title
    ZONES = {
        "easy":      {"pace": 375, "hr": 136, "spread": 14, "title": "Easy run"},
        "threshold": {"pace": 295, "hr": 166, "spread": 16, "title": "Threshold"},
        "long":      {"pace": 390, "hr": 146, "spread": 18, "title": "Long run"},
    }
    base_km, aid = 32.0, 0
    for w in range(weeks):
        wk_monday = start_monday + timedelta(weeks=w)
        ramp = 1.0 + 0.55 * (w / max(1, weeks - 1))     # ~+55% volume by the end
        down = 0.7 if (w % 4 == 3) else 1.0             # cut-back every 4th week
        week_km = base_km * ramp * down
        long_km = week_km * 0.32
        quality_km = week_km * 0.18
        easy_km = (week_km - long_km - quality_km) / 3
        # Tue easy, Wed quality, Thu easy, Sat easy, Sun long (production Monday-anchors too)
        for dow, zone, km in [(1, "easy", easy_km), (2, "threshold", quality_km),
                              (3, "easy", easy_km), (5, "easy", easy_km),
                              (6, "long", long_km)]:
            day = wk_monday + timedelta(days=dow)
            if day > last_day:                          # don't seed past yesterday
                continue
            km = max(3.0, round(km + rnd.uniform(-0.6, 0.6), 1))
            z = ZONES[zone]
            dur = int(km * z["pace"])
            hr_avg = z["hr"] + rnd.randint(-4, 4)
            hr_max = hr_avg + z["spread"] + rnd.randint(0, 6)
            aid += 1
            # per-run effective VO2max (what /api/vo2max charts as the VO2max tile sparkline) — a
            # rising baseline ~46→54 tracking the build, with a small zone bump (quality reads higher)
            # and mild run-to-run noise. use_vo2max gates it on, matching Runalyze's per-activity field.
            run_vo2 = round(46.0 + 8.0 * (w / max(1, weeks - 1))
                            + {"easy": 0.0, "threshold": 1.5, "long": 0.5}[zone]
                            + rnd.uniform(-0.8, 0.8), 1)
            upsert_activity(db, {
                "id": aid, "date_time": f"{day.isoformat()}T18:30:00", "title": z["title"],
                "sport": {"id": 1, "name": RUNNING_SPORT},
                "distance": km, "duration": dur, "elapsed_time": dur + rnd.randint(20, 90),
                "hr_avg": hr_avg, "hr_max": hr_max, "trimp": est_trimp(dur / 60.0, zone),
                "cadence": rnd.randint(168, 176), "elevation_up": round(km * 6),
                "vo2max": run_vo2, "use_vo2max": True,
                "source": "synthetic",
            })
    race_day = today - timedelta(days=5)
    if past_race:   # §6s — the race itself (5 days ago), so the settled scorecard can reckon the result
        aid += 1
        upsert_activity(db, {
            "id": aid, "date_time": f"{race_day.isoformat()}T09:00:00", "title": "Demo Marathon",
            "sport": {"id": 1, "name": RUNNING_SPORT}, "distance": 42.2,
            "duration": 13920, "elapsed_time": 13950,    # 3:52:00 finish (goal was 3:45)
            "hr_avg": 168, "hr_max": 182, "trimp": est_trimp(13920 / 60.0, "marathon"),
            "cadence": 172, "elevation_up": 120,
            "vo2max": 54.0, "use_vo2max": True,
            "source": "synthetic",
        })
    db.commit()

    # derive the shape time-series with the engine's OWN reconstruction (no token needed),
    # plus a gentle effective-VO2max trend that tracks CTL growth (~46→~54)
    hist = reconstruct_history(db, end=last_day.isoformat())
    max_ctl = max((h["ctl"] for h in hist), default=1.0) or 1.0
    prev_vo2 = None
    for h in hist:
        ctl = h["ctl"]
        vo2 = round(46.0 + 8.0 * (ctl / max_ctl), 1)
        prog = None if prev_vo2 is None else round(vo2 - prev_vo2, 2)
        prev_vo2 = vo2
        upsert_shape_snapshot(db, h["date"], effective_vo2max=vo2, effective_vo2max_progress=prog,
                              fitness=ctl, fatigue=h["atl"], performance=h["tsb"],
                              fitness_pct=round(100 * ctl / max_ctl, 1), acwr=h["acwr"])
    db.commit()

    # one upcoming A-race ~16 weeks out + a B tune-up — gives the periodizer a real runway.
    # with_objective=False leaves the instance race-less AND plan-less (history only) — the genuine
    # first-run "pulled data, not yet planned" state, used to exercise the first-run step-③ CTA.
    if past_race:
        # §6s — reproduce the POST-race state: a founding plan was built while the race was ahead (it
        # recorded the projection), then the race ran and the engine dropped it. Build that founding plan
        # with the race in the future, then move BOTH the objective and the plan's recorded race date
        # back to 5 days ago — exactly the history the scorecard reckons from. The final regenerate below
        # then adds today's race-less maintenance plan as `current`, like the real nightly replan would.
        db.execute("INSERT INTO objectives (type,label,date,target,priority,status,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("marathon", "Demo Marathon", (today + timedelta(weeks=10)).isoformat(),
                    "3:45", "A", "upcoming", _now_iso()))
        regenerate(db)                                    # founding plan: objective + projected_ctl on file
        db.execute("UPDATE objectives SET date=? WHERE label='Demo Marathon'", (race_day.isoformat(),))
        prow = db.execute("SELECT id, plan FROM plans ORDER BY id DESC LIMIT 1").fetchone()
        pj = json.loads(prow["plan"])
        if pj.get("objective"):
            pj["objective"]["date"] = race_day.isoformat()
        # make the founding projection believable vs what was actually arrived (a small ~2-CTL shortfall),
        # so the demo reckoning reads "landed just short" rather than off the engine's re-base artifact.
        rd_ctl = None
        for h in reconstruct_history(db, end=race_day.isoformat()):
            rd_ctl = h["ctl"]
        if pj.get("feasibility") and rd_ctl:
            pj["feasibility"]["projected_ctl"] = round(rd_ctl + 2)
        db.execute("UPDATE plans SET plan=? WHERE id=?", (json.dumps(pj), prow["id"]))
        db.commit()
    elif with_objective:
        for typ, label, wks, target, pri in [("marathon", "Demo Marathon", 16, "3:45", "A"),
                                             ("10k", "Tune-up 10k", 7, "42:00", "B")]:
            db.execute("INSERT INTO objectives (type,label,date,target,priority,status,created_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (typ, label, (today + timedelta(weeks=wks)).isoformat(),
                        target, pri, "upcoming", _now_iso()))

    # a few synthetic health markers across the build (improving metabolic trend)
    for wago, tg, hdl, wt in [(20, 150, 48, 74.0), (12, 132, 52, 73.2), (4, 116, 56, 72.5)]:
        d = (today - timedelta(weeks=wago)).isoformat()
        for marker, val in (("triglycerides", tg), ("hdl", hdl), ("weight", wt)):
            db.execute("INSERT OR REPLACE INTO health_markers (marker,date,value,source,note) "
                       "VALUES (?,?,?,?,?)", (marker, d, val, "manual", None))

    # a couple of recent readiness check-ins + a session reflection (so those panels aren't bare)
    for dago, en, sl in [(1, "good", "good"), (2, "ok", "ok")]:
        db.execute("INSERT OR REPLACE INTO readiness (date,energy,sleep,stop_symptom,note,created_at) "
                   "VALUES (?,?,?,?,?,?)",
                   ((today - timedelta(days=dago)).isoformat(), en, sl, 0, None, _now_iso()))
    db.execute("INSERT OR REPLACE INTO session_log (date,note,created_at) VALUES (?,?,?)",
               (last_day.isoformat(), "Felt strong, legs came around after 3k.", _now_iso()))

    set_meta(db, "last_sync", _now_iso())
    set_meta(db, "synthetic_seed", "1")   # marks this DB as a throwaway seed → the `seed` guard
    db.commit()                           # may re-wipe it, but refuses any DB without this marker

    # generate + persist an initial plan so the instance opens fully populated (a configured
    # instance has a stored plan from the nightly replan; the dashboard's GET /api/plan reads it).
    # With no objective there is deliberately no plan — that's the state being reproduced.
    # the fixture's own plan is built for the fixture's day, not the process's
    plan = regenerate(db, today=today) if (with_objective or past_race) else None
    return {"activities": aid, "snapshots": len(hist), "plan_ok": bool(plan and plan.get("ok")),
            "from": start_monday.isoformat(), "to": last_day.isoformat()}


# ── Main ────────────────────────────────────────────────────────────────────
init_db()
try:
    with app.app_context():
        apply_settings_overrides(get_db())   # overlay any saved meta settings onto the env defaults
except Exception as e:
    print(f"[settings] startup overlay skipped: {e}")
apply_secret_overrides()   # overlay window-set secrets (Runalyze token / Claude key) before the scheduler
start_scheduler()   # runs under waitress (import) and the dev server alike (logs the effective TZ)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":   # CLI: python SparingHorse.py selftest
        with app.app_context():
            db = get_db()
            st = _selftest_module()
            rep = st.run_server_selftest(db)
            rep["id"] = st.save_selftest_run(db, rep)
        print(st._selftest_text(rep))
        sys.exit(1 if rep["summary"]["failed"] else 0)
    if len(sys.argv) > 1 and sys.argv[1] == "golden":     # CLI: python SparingHorse.py golden
        # (Re)write test/golden/*.json from the pinned fixtures. Deliberate act: run it only when an
        # engine change is INTENDED, and quote the resulting diff in the commit message. A refactor
        # that needs this run is not a refactor.
        st = _selftest_module()
        names = st._golden_write()
        print(f"Wrote {len(names)} goldens to {st.GOLDEN_DIR}: {', '.join(names)}")
        print("Review `git diff test/golden/` — every line is a change in what the engine prescribes.")
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1] == "import":     # §BX CLI: python SparingHorse.py import export.json
        if len(sys.argv) < 3:
            print("Usage: python SparingHorse.py import <sparinghorse-export.json>")
            sys.exit(2)
        try:
            payload = json.loads(Path(sys.argv[2]).read_text())
        except (OSError, ValueError) as e:
            print(f"Could not read export file: {e}")
            sys.exit(2)
        with app.app_context():
            out = import_user_data(get_db(), payload)
        if out.get("ok"):
            print("Restored:", ", ".join(f"{t}={n}" for t, n in out["restored"].items()))
            sys.exit(0)
        print(f"Import refused: {out.get('error')}")
        sys.exit(1)
    if len(sys.argv) > 1 and sys.argv[1] == "seed":       # CLI: SH_DB=test.db python SparingHorse.py seed
        # Populates a TOKEN-FREE local test/demo instance. seed_synthetic_db DELETEs the data
        # tables first, so two independent guards keep it from ever wiping a REAL database:
        #   1. SH_DB must be set (never target the default path implicitly), AND
        #   2. the target must not already hold real data — it must be empty or a prior synthetic
        #      seed (the `synthetic_seed` meta marker). This is DATA-aware, not filename-aware, so
        #      an absolute prod path (the deploy uses SH_DB=/data/sparinghorse.db) is also refused.
        # Pass --force to wipe-and-reseed a DB that has real data anyway (explicit opt-in).
        target = os.environ.get("SH_DB")
        if not target:
            print("Refusing to seed: SH_DB is not set (would target the default DB).\n"
                  "Point SH_DB at a throwaway file, e.g.:\n"
                  "  SH_DB=test_local.db python SparingHorse.py seed")
            sys.exit(2)
        with app.app_context():
            db = get_db()
            has_real = (db.execute("SELECT 1 FROM activities LIMIT 1").fetchone() is not None
                        and get_meta(db, "synthetic_seed") != "1")
            if has_real and "--force" not in sys.argv:
                n = db.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
                print(f"Refusing to seed {target}: it already holds real data ({n} activities, "
                      f"no synthetic-seed marker).\nUse a fresh SH_DB path, or pass --force to "
                      f"wipe and reseed it.")
                sys.exit(2)
            info = seed_synthetic_db(db, with_objective="--no-objective" not in sys.argv,
                                     past_race="--past-race" in sys.argv,
                                     cold="--cold" in sys.argv)   # §FT5 — one 10k + objective, no history
        print(f"Seeded {target}: {info['activities']} activities, {info['snapshots']} daily "
              f"snapshots, history {info['from']} → {info['to']}.")
        print(f"Run it:  SH_DB={target} RUNALYZE_TOKEN= python SparingHorse.py   "
              f"# private console, no token, fully populated")
        sys.exit(0)
    print(f"Sparing Horse → http://127.0.0.1:{PORT}  "
          f"(token {'set' if config().runalyze_token else 'MISSING'})")
    app.run(host="127.0.0.1", port=PORT, debug=False)
