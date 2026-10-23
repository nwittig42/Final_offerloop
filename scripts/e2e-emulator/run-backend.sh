#!/bin/zsh
# Offerloop backend against the emulators (port 5002). See e2e_server.py.
set -e
cd "$(dirname "$0")/../.."
unset GOOGLE_APPLICATION_CREDENTIALS
exec .venv/bin/python scripts/e2e-emulator/e2e_server.py
