#!/bin/zsh
# Offerloop frontend against the emulators and the e2e backend (port 8081).
set -e
cd "$(dirname "$0")/../../connect-grow-hire"
export VITE_USE_FIREBASE_EMULATOR=1
export VITE_API_BASE_URL=http://127.0.0.1:5002/api
exec node_modules/.bin/vite --host 127.0.0.1 --port 8081 --strictPort
