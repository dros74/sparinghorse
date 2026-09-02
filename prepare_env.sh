#!/bin/sh
# prepare_env.sh — add the console-access variables to an EXISTING .env, safely (Sparing Horse 0.56.0+).
#
# Run on the host, in the compose directory, before `docker compose up -d --build`:
#
#     sh prepare_env.sh --check                       # preview: prints what would change, writes nothing;
#                                                     #   exit 0 = nothing pending, 3 = something is (deploy scripts gate on it)
#     sh prepare_env.sh                               # asks for the Cloudflare Access team + audience tag
#     sh prepare_env.sh --team myteam --aud <64 hex>  # non-interactive
#     sh prepare_env.sh --passphrase                  # also set SH_PASSPHRASE (skips the /setup page)
#     sh prepare_env.sh --from-container              # read team + tag from the running console (0.60.0):
#                                                     #   deploy once without the bypass, open the private
#                                                     #   site through Access once, then run this
#
# What it does, and only this:
#   • backs up .env to .env.bak-<stamp> (mode 600) before touching anything;
#   • SH_SECRET_KEY: generated from /dev/urandom (96 hex chars) when absent or empty — kept when set.
#     This key is what decrypts the token store from now on: keep a copy of the new .env somewhere safe;
#   • SH_TRUST_PROXY_AUTH=1 + SH_CF_ACCESS_TEAM + SH_CF_ACCESS_AUD when a team and an audience tag are
#     given (shape-checked; a paste error is refused, not written). Blank = no bypass: the console
#     simply asks for its passphrase behind Access, which also works;
#   • never prints a secret value; never rewrites a line that already carries a value; appends the
#     rest under a dated header; leaves .env at mode 600.
# Needs: sh, awk, od, head — present on a Synology, a Debian box, a Mac.
set -eu

ENV_FILE="${ENV_FILE:-.env}"
CHECK=0; TEAM=""; AUD=""; ASK_PP=0; FROM_C=0; TEAM_GIVEN=0
usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1 ;;
    --team) TEAM="${2:-}"; TEAM_GIVEN=1; shift ;;
    --from-container) FROM_C=1 ;;
    --aud) AUD="${2:-}"; shift ;;
    --passphrase) ASK_PP=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[ -f "$ENV_FILE" ] || { echo "✗ $ENV_FILE not found — run this in the compose directory (cp .env.example .env first on a new box)" >&2; exit 1; }
# the three host directories the compose file bind-mounts; a Synology's Docker refuses to start a
# container whose bind source is missing (./backups arrived in 0.56.1 and bit the first upgrade)
PENDING=0
for d in data secrets backups; do
  if [ ! -d "$d" ]; then
    if [ "$CHECK" = 1 ]; then echo "  would create ./$d (bind mount source)"; PENDING=1; else mkdir -p "$d" && echo "  created ./$d (bind mount source)"; fi
  fi
done

# ── helpers (values are read to test emptiness only; nothing echoes them) ──────────────────────────
has_key()   { grep -q "^$1=" "$ENV_FILE"; }
is_empty()  { ! grep -q "^$1=." "$ENV_FILE"; }         # absent, or present with nothing after '='
gen_key()   { head -c 48 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
replace_or_append() {                                  # $1 key, $2 value — in place when an empty line exists
  if has_key "$1"; then
    awk -v k="$1" -v v="$2" 'BEGIN{done=0} $0 ~ ("^" k "=") && !done {print k "=" v; done=1; next} {print}' \
      "$ENV_FILE" > "$ENV_FILE.tmp" && cat "$ENV_FILE.tmp" > "$ENV_FILE" && rm -f "$ENV_FILE.tmp"
  else
    printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
  fi
}
shape_team() { printf '%s' "$1" | grep -Eq '^[a-z0-9][a-z0-9-]*(\.cloudflareaccess\.com)?$'; }
shape_aud()  { printf '%s' "$1" | grep -Eq '^[0-9a-f]{64}$'; }

# ── the console can say what it saw (0.60.0) — unverified claims, so the team is CONFIRMED by a human ──
if [ "$FROM_C" = 1 ]; then
  OUT=$(docker compose exec -T sparinghorse python SparingHorse.py access-seen 2>/dev/null) \
    || { echo "✗ could not read from the container — is it up (docker compose ps)? On a Synology run this with sudo." >&2
         echo "  (the console must have seen one Access token: open the private site through Cloudflare Access once, then retry)" >&2; exit 1; }
  SEEN_TEAM=$(printf '%s\n' "$OUT" | sed -n 's/^team=//p' | head -1)
  SEEN_AUD=$(printf '%s\n' "$OUT" | sed -n 's/^aud=//p' | head -1)
  [ -n "$SEEN_TEAM" ] && [ -n "$SEEN_AUD" ] || { echo "✗ the console has not seen an Access token yet — open the private site through Cloudflare Access once, then retry" >&2; exit 1; }
  if [ "$TEAM_GIVEN" = 1 ] && [ "$TEAM" != "$SEEN_TEAM" ]; then
    echo "✗ --team '$TEAM' is not the team the console saw ('$SEEN_TEAM') — one of the two is wrong; nothing written" >&2; exit 1
  fi
  echo "The console saw Access team '$SEEN_TEAM' (audience tag: 64 hex). It must be YOUR team: a stranger's token would print a stranger's name here."
  if [ "$TEAM_GIVEN" = 1 ]; then :
  elif [ -t 0 ]; then
    printf "Is '%s' your Cloudflare Access team? [y/N] " "$SEEN_TEAM"; read -r yn
    case "$yn" in y|Y|yes|YES) ;; *) echo "not confirmed — nothing written"; exit 1 ;; esac
  else
    echo "✗ non-interactive: confirm the team by passing --team '$SEEN_TEAM'" >&2; exit 1
  fi
  TEAM="$SEEN_TEAM"; AUD="$SEEN_AUD"
fi

# ── decide ─────────────────────────────────────────────────────────────────────────────────────────
PLAN=""; note() { PLAN="$PLAN  $1
"; }
SET_KEY=0
if ! is_empty SH_SECRET_KEY; then note "SH_SECRET_KEY       · kept (already set)"
elif [ -f secrets/secrets.key ]; then
  # the store is already encrypted with the key FILE; an env key is scrypt-derived and would not open it
  note "SH_SECRET_KEY       · left unset — ./secrets/secrets.key holds the store's key (an env key now would orphan the store)"
else SET_KEY=1; note "SH_SECRET_KEY       → generate (96 hex from /dev/urandom)"; fi

if [ -z "$TEAM" ] && [ -z "$AUD" ] && [ -t 0 ]; then
  printf 'Cloudflare Access team (name, or team.cloudflareaccess.com; blank = no bypass): '; read -r TEAM
  if [ -n "$TEAM" ]; then printf 'Access application audience tag (64 hex chars): '; read -r AUD; fi
fi
SET_CF=0
if [ -n "$TEAM" ] || [ -n "$AUD" ]; then
  [ -n "$TEAM" ] && [ -n "$AUD" ] || { echo "✗ both --team and --aud are needed for the bypass (or neither)" >&2; exit 1; }
  shape_team "$TEAM" || { echo "✗ team '$TEAM' does not look like a Cloudflare Access team (lowercase name, or name.cloudflareaccess.com)" >&2; exit 1; }
  shape_aud "$AUD"   || { echo "✗ the audience tag must be 64 hex characters (copy it from the Access application's overview)" >&2; exit 1; }
  if ! is_empty SH_CF_ACCESS_AUD && ! is_empty SH_CF_ACCESS_TEAM; then
    note "Access bypass        · kept (team + audience already set; not overwritten)"
  else
    SET_CF=1; note "SH_TRUST_PROXY_AUTH → 1"; note "SH_CF_ACCESS_TEAM   → $TEAM"; note "SH_CF_ACCESS_AUD    → (given, 64 hex)"
  fi
elif ! is_empty SH_CF_ACCESS_AUD && ! is_empty SH_CF_ACCESS_TEAM; then
  note "Access bypass        · kept (team + audience already set)"
else
  note "Access bypass        · not set — the console will ask for its passphrase behind the proxy (fine)"
fi

PP=""
if [ "$ASK_PP" = 1 ]; then
  if ! is_empty SH_PASSPHRASE; then
    note "SH_PASSPHRASE       · kept (already set)"
  elif [ -t 0 ]; then
    stty -echo 2>/dev/null || true
    printf 'Console passphrase (12+ characters): '; read -r PP; printf '\n'
    printf 'Again: '; read -r PP2; printf '\n'
    stty echo 2>/dev/null || true
    [ "$PP" = "$PP2" ] || { echo "✗ the two entries differ" >&2; exit 1; }
    [ "${#PP}" -ge 12 ] || { echo "✗ at least twelve characters" >&2; exit 1; }
    note "SH_PASSPHRASE       → set (the /setup page will not appear)"
  else
    echo "✗ --passphrase needs a terminal to ask on" >&2; exit 1
  fi
fi

grep -q "^SH_WEATHER_CITIES=" "$ENV_FILE" && note "SH_WEATHER_CITIES    ! present but gone since 0.57.0 — harmless, remove when convenient"

echo "Plan for $ENV_FILE:"; printf '%s' "$PLAN"
if [ "$CHECK" = 1 ]; then
  if [ "$SET_KEY" = 1 ] || [ "$SET_CF" = 1 ] || [ "$PENDING" = 1 ]; then echo "(--check: nothing written — run without --check to apply)"; exit 3; fi
  echo "(--check: nothing pending)"; exit 0
fi
if [ "$SET_KEY" = 0 ] && [ "$SET_CF" = 0 ] && [ -z "$PP" ]; then echo "Nothing to do."; exit 0; fi

# ── write ──────────────────────────────────────────────────────────────────────────────────────────
STAMP=$(date +%Y%m%d-%H%M%S)
cp -p "$ENV_FILE" "$ENV_FILE.bak-$STAMP" && chmod 600 "$ENV_FILE.bak-$STAMP"
printf '\n# — added by prepare_env.sh on %s (Sparing Horse console access, DEPLOY.md §2a) —\n' "$STAMP" >> "$ENV_FILE"
[ "$SET_KEY" = 1 ] && replace_or_append SH_SECRET_KEY "$(gen_key)"
if [ "$SET_CF" = 1 ]; then
  replace_or_append SH_TRUST_PROXY_AUTH 1
  replace_or_append SH_CF_ACCESS_TEAM "$TEAM"
  replace_or_append SH_CF_ACCESS_AUD "$AUD"
fi
[ -n "$PP" ] && replace_or_append SH_PASSPHRASE "$PP"
chmod 600 "$ENV_FILE"
echo "✓ written. Backup: $ENV_FILE.bak-$STAMP (keep a copy of the NEW $ENV_FILE safe — SH_SECRET_KEY decrypts the token store)."
echo "Next: docker compose up -d --build   (sudo on a Synology) → open the console → /setup once unless SH_PASSPHRASE is set → check the footer version."
