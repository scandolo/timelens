#!/bin/zsh
# For a git checkout: prepares the Python environment (first run only) and opens the setup page.
# With Homebrew, use `timelens setup` instead.
# Uninstall: launchctl bootout gui/$UID/com.timelens.sampler
#            then delete ~/Library/LaunchAgents/com.timelens.*.plist and ~/Library/Application Support/timelens
exec "${0:A:h}/bin/timelens" setup "$@"
