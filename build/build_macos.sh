#!/usr/bin/env bash
# Build the macOS distributable. Run from the repo root:
#   bash build/build_macos.sh
# Output: dist/Klearing.app and dist/Klearing-macos.zip
#
# Must run ON a Mac (PyInstaller cannot cross-compile). No Mac handy?
# The GitHub Actions workflow (.github/workflows/build.yml) builds it
# on a free macOS runner.

set -euo pipefail

python3 -m venv venv 2>/dev/null || true
venv/bin/python -m pip install --upgrade pip
venv/bin/pip install -r requirements.txt pyinstaller

venv/bin/pyinstaller build/klearing.spec --noconfirm --distpath dist --workpath build/work

# ditto preserves the app bundle's structure/attributes, unlike zip -r.
ditto -c -k --keepParent dist/Klearing.app dist/Klearing-macos.zip

echo
echo "Done: dist/Klearing.app  (zipped: dist/Klearing-macos.zip)"
echo "Reminder: unsigned builds trip Gatekeeper — see docs/FIRST-LAUNCH.md"
