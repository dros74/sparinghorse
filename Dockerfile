FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY SparingHorse.py .
# The self-test battery (TECH-1): a separate module the app imports LAZILY, so a typo in a det
# cannot take the web app and the nightly scheduler down at import time. It is also the entry
# point the /api/selftest/run route spawns as its own process.
COPY sh_selftest.py .
COPY test/golden ./test/golden
ENV SH_DB=/data/sparinghorse.db
# §55e — WITHOUT THIS, EVERY `print()` IN THE APP IS INVISIBLE IN `docker logs`. Container stdout is a
# pipe, not a tty, so Python block-buffers it (8 KB); a long-running server never fills that buffer, so
# startup and diagnostic lines sit in it indefinitely. `waitress` logs through the `logging` module to
# STDERR, which is unbuffered — so the log looks alive while the app's own voice is missing entirely.
# That silently disarmed §55b, whose guard against a blank/malformed SH_SYNC_AT works by PRINTING why
# it fell back to the default. A guard whose diagnostic cannot be read is half a guard.
ENV PYTHONUNBUFFERED=1
EXPOSE 8770
# Production server (not Flask's dev server). Imports `app` from SparingHorse.py.
CMD ["waitress-serve", "--listen=0.0.0.0:8770", "SparingHorse:app"]
