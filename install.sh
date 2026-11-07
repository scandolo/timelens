#!/bin/zsh
# Sets up a Python environment for timelens, then opens the setup page in your browser.
# Uninstall: launchctl bootout gui/$UID/com.timelens.sampler; launchctl bootout gui/$UID/com.timelens.report
#            rm ~/Library/LaunchAgents/com.timelens.*.plist; rm -r ~/Library/Application\ Support/timelens
set -e
DIR=${0:A:h}
command -v uv >/dev/null || { echo "timelens needs uv. Install it with: brew install uv  (or see https://docs.astral.sh/uv/)"; exit 1; }
if [[ ! -x $DIR/.venv/bin/python ]]; then
  echo "Creating a Python environment (first run only)…"
  uv venv -q --python 3.13 "$DIR/.venv"
  uv pip install -q -p "$DIR/.venv" pyobjc-framework-Quartz pyobjc-framework-Cocoa
fi
exec "$DIR/.venv/bin/python" "$DIR/onboarding.py"
