#!/bin/zsh
# Builds, signs and zips timelens.app, the launcher that makes macOS show "timelens" in Privacy & Security.
# Usage: launcher/build.sh "Developer ID Application: Your Name (TEAMID)"
# The output (launcher/build/timelens-launcher.zip) is attached to a GitHub release and installed by Homebrew.
set -e
cd ${0:A:h}
IDENTITY=${1:?usage: build.sh "Developer ID Application: …"}
OUT=build/timelens.app/Contents
mkdir -p build && find build -mindepth 1 -delete
mkdir -p $OUT/MacOS $OUT/Resources
swiftc -O -target arm64-apple-macos13 main.swift -o build/arm64
swiftc -O -target x86_64-apple-macos13 main.swift -o build/x86_64
lipo -create build/arm64 build/x86_64 -output $OUT/MacOS/timelens
cp Info.plist $OUT/
cp timelens.icns $OUT/Resources/
codesign --force --options runtime --timestamp --sign "$IDENTITY" build/timelens.app
codesign --verify --strict --verbose=1 build/timelens.app
ditto -c -k --keepParent build/timelens.app build/timelens-launcher.zip
shasum -a 256 build/timelens-launcher.zip
