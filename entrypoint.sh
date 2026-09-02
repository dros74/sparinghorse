#!/bin/sh
# Sparing Horse — container entrypoint (0.55.2, review S4).
#
# The app runs as an UNPRIVILEGED user. It cannot simply be `USER sh` in the Dockerfile, because the
# database and the secrets store are bind mounts the host created as root (a Synology, a plain
# `docker compose up` as root): a process that starts unprivileged cannot write them, and the first
# deploy after the change would boot into "attempt to write a readonly database". So the container
# starts as root, gives the two writable mounts to the app user, and then DROPS to that user for the
# whole life of the process — the standard chown-then-setpriv shape. After the drop there is no root
# and no capability left in the process; compose also drops every capability but the four this
# script needs for its first second (CHOWN, SETUID, SETGID, DAC_OVERRIDE).
#
# SH_UID / SH_GID choose the user (default 10001:10001 — deliberately outside any host's usual range,
# so it never collides with a real account). Set them to a host user's ids if the host must read
# the files as that user. Files the app creates are 0644 (SQLite) or 0600 (the secrets store).
set -eu

APP_UID="${SH_UID:-10001}"
APP_GID="${SH_GID:-10001}"

if [ "$(id -u)" = "0" ]; then
  for d in /data /secrets; do
    [ -d "$d" ] || continue
    if ! chown -R "$APP_UID:$APP_GID" "$d" 2>/dev/null; then
      echo "[entrypoint] could not chown $d to $APP_UID:$APP_GID — the app may be unable to write there" >&2
    fi
  done
  # A secrets store pointed somewhere else (SH_SECRETS_DB) — make sure its directory is writable too.
  if [ -n "${SH_SECRETS_DB:-}" ]; then
    sdir="$(dirname "$SH_SECRETS_DB")"
    [ -d "$sdir" ] && chown "$APP_UID:$APP_GID" "$sdir" 2>/dev/null || true
  fi
  exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --clear-groups "$@"
fi

# Already unprivileged (someone ran the image with --user): nothing to drop.
exec "$@"
