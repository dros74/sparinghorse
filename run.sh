#!/usr/bin/env bash
# Local launcher for Sparing Horse. Loads secrets from .env (git-ignored) and runs the app
# in the project virtualenv, so you never have to type the token on the command line.
#
#   ./run.sh
#
# First-time setup on a new machine:
#   python -m venv venv && venv/bin/pip install -r requirements.txt
#   cp .env.example .env   # then put your RUNALYZE_TOKEN in .env
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "No .env found. Run: cp .env.example .env  and add your RUNALYZE_TOKEN" >&2
  exit 1
fi
set -a; . ./.env; set +a   # export everything defined in .env

if [ ! -x venv/bin/python ]; then
  echo "No venv found. Run: python -m venv venv && venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

exec venv/bin/python SparingHorse.py
