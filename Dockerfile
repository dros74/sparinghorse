# Pinned by DIGEST, not tag (0.55.2, review S4): `python:3.12-slim` moves under a rebuild; this line
# does not. Bump it deliberately — look the new digest up (`docker manifest inspect python:3.12-slim`
# or the registry) and quote it in the commit. The tag stays in the line for the reader.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
WORKDIR /app
# The hash-pinned lock is what the image installs; requirements.txt is the human-edited source it
# was generated from (see DEPLOY.md). `--require-hashes` refuses a wheel that does not match.
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY SparingHorse.py .
# The plan engine (TECH-12): the deterministic core — everything a plan is computed from. The app
# imports it at start, so unlike the battery below this is NOT optional: without it the container
# does not boot at all. det/image-completeness fails the suite if a module the app imports is
# missing from this list.
COPY sh_engine.py .
# The self-test battery (TECH-1): a separate module the app imports LAZILY, so a typo in a det
# cannot take the web app and the nightly scheduler down at import time. It is also the entry
# point the /api/selftest/run route spawns as its own process.
COPY sh_selftest.py .
COPY test/golden ./test/golden
# The front end (TECH-11): shell + stylesheet + script, served from /static. Without these the
# app boots and then serves a page with no CSS and no JS — it must fail the build, not the user.
# static/vendor/ carries Leaflet (0.55.2) so the map never loads a script from a third-party host.
COPY static ./static
# The unprivileged app user (0.55.2). The container STARTS as root so entrypoint.sh can give the
# bind-mounted /data and /secrets to this user, then drops to it for good — see entrypoint.sh for
# why that beats a plain `USER` line on a host whose mounts are root-owned.
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh \
 && groupadd --gid 10001 sh \
 && useradd --uid 10001 --gid 10001 --system --no-create-home --shell /usr/sbin/nologin sh
ENV SH_DB=/data/sparinghorse.db
# §55e — WITHOUT THIS, EVERY `print()` IN THE APP IS INVISIBLE IN `docker logs`. Container stdout is a
# pipe, not a tty, so Python block-buffers it (8 KB); a long-running server never fills that buffer, so
# startup and diagnostic lines sit in it indefinitely. `waitress` logs through the `logging` module to
# STDERR, which is unbuffered — so the log looks alive while the app's own voice is missing entirely.
# That silently disarmed §55b, whose guard against a blank/malformed SH_SYNC_AT works by PRINTING why
# it fell back to the default. A guard whose diagnostic cannot be read is half a guard.
ENV PYTHONUNBUFFERED=1
# The root filesystem is read-only in compose (0.55.2); Python must not try to write .pyc files
# into /app on import.
ENV PYTHONDONTWRITEBYTECODE=1
EXPOSE 8770
# Liveness the orchestrator can see (0.55.2): `docker ps` reads healthy/unhealthy off /healthz,
# which also carries the scheduler's failure count. Compose repeats the same probe explicitly.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8770/healthz', timeout=4).status == 200 else 1)"]
ENTRYPOINT ["/entrypoint.sh"]
# Production server (not Flask's dev server). Imports `app` from SparingHorse.py.
CMD ["waitress-serve", "--listen=0.0.0.0:8770", "SparingHorse:app"]
