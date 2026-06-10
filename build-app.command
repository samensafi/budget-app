#!/bin/bash
# Builds Budget.app, the double-click app with the icon, ready to upload to the GitHub
# Releases page. It wraps budget-launcher.command (the single source of truth for how
# Budget starts) and bakes in icon.png. Run this whenever the launcher changes, then
# upload the fresh Budget.zip to a new release.
#
# Output, both next to this script and both git-ignored (they belong on Releases, not in
# the repo): Budget.app (the app) and Budget.zip (the same app zipped for uploading).
#
# This only packages files. It never starts the app, so it is safe to run.

cd "$(dirname "$0")" || exit 1
HERE="$PWD"

APP="$HERE/Budget.app"
ZIP="$HERE/Budget.zip"
LAUNCHER="$HERE/budget-launcher.command"
ICON_SRC="$HERE/docs/icon.png"

if [ ! -f "$LAUNCHER" ]; then
  echo "budget-launcher.command not found next to this script."
  exit 1
fi

# Fresh start so an old build never lingers.
rm -rf "$APP" "$ZIP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# The app's executable IS the launcher, so the two can never drift apart.
cp "$LAUNCHER" "$APP/Contents/MacOS/Budget"
chmod +x "$APP/Contents/MacOS/Budget"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Budget</string>
  <key>CFBundleDisplayName</key><string>Budget</string>
  <key>CFBundleIdentifier</key><string>com.samensafi.budget</string>
  <key>CFBundleVersion</key><string>1.5</string>
  <key>CFBundleShortVersionString</key><string>1.5</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Budget</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>LSMinimumSystemVersion</key><string>10.13</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# Turn icon.png into a proper .icns so the icon shows everywhere (Finder, Dock, the
# Get Info panel) and travels inside the bundle. Skipped quietly if the tools or the
# source image aren't around, the app still works, it just shows a generic icon.
if [ -f "$ICON_SRC" ] && command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
  WORK="$(mktemp -d)"
  SET="$WORK/icon.iconset"
  mkdir -p "$SET"
  make() { sips -z "$1" "$1" "$ICON_SRC" --out "$SET/$2" >/dev/null 2>&1; }
  make 16   icon_16x16.png
  make 32   icon_16x16@2x.png
  make 32   icon_32x32.png
  make 64   icon_32x32@2x.png
  make 128  icon_128x128.png
  make 256  icon_128x128@2x.png
  make 256  icon_256x256.png
  make 512  icon_256x256@2x.png
  make 512  icon_512x512.png
  make 1024 icon_512x512@2x.png
  iconutil -c icns "$SET" -o "$APP/Contents/Resources/icon.icns" >/dev/null 2>&1
  rm -rf "$WORK"
fi

# Ad-hoc sign so macOS treats it as an ordinary unsigned app (the friendly right-click
# Open prompt) instead of flagging the downloaded bundle as damaged. Best-effort.
if command -v codesign >/dev/null 2>&1; then
  codesign --force --deep -s - "$APP" >/dev/null 2>&1 || true
fi

# Zip with ditto so the bundle and its resources survive the round trip to GitHub.
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"

echo "Built Budget.app and Budget.zip in:"
echo "  $HERE"
echo "Upload Budget.zip to a new GitHub release."
