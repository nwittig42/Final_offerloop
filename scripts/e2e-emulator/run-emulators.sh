#!/bin/zsh
# Local Firebase Emulator Suite (Auth 9099, Firestore 8180, Storage 9199, UI 4000).
# Data persists in ./data between runs. Needs Java 21 and firebase-tools.
set -e
cd "$(dirname "$0")"
export JAVA_HOME=/opt/homebrew/opt/openjdk@21
export PATH="$JAVA_HOME/bin:$PATH"
mkdir -p data
exec firebase emulators:start --config ../../firebase.e2e.json --project offerloop-native \
  --only auth,firestore,storage --import ./data --export-on-exit ./data
