"""
App-wide constants for Klearing.

One place for the name, version, and port so nothing else hardcodes
them. The port deliberately differs from the original Go Mode
instance's 8000 so both apps can run on the same machine.
"""

import os

APP_NAME = "Klearing"
APP_ID = "klearing"          # filesystem / keyring / task-name safe
APP_VERSION = "7"

DEFAULT_PORT = 8642
PORT = int(os.environ.get("KLEARING_PORT", DEFAULT_PORT))

APP_URL = f"http://localhost:{PORT}"

# The public home of the project: release downloads, issue tracker,
# and what the update checker polls.
GITHUB_REPO = "Trostky1/klearing"
GITHUB_URL = f"https://github.com/{GITHUB_REPO}"
# Stable even before the first Release object exists; future downloads will
# appear here without changing an already-packaged refusal message.
GITHUB_RELEASES_URL = f"{GITHUB_URL}/releases"
