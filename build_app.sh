#!/bin/bash
# Optional: builds a fully standalone "TV Episode Renamer.app" (bundles Python + Tk),
# so it runs on Macs without Python installed. Run with:  bash build_app.sh
# Requires Python 3 from python.org (it includes Tk).
set -e
cd "$(dirname "$0")"

APP="dist/TV Episode Renamer.app"
PLIST="$APP/Contents/Info.plist"

python3 -m pip install --upgrade pyinstaller
python3 -m PyInstaller --windowed --noconfirm --name "TV Episode Renamer" episode_renamer.py

# PyInstaller writes version 0.0.0 into the app's Info.plist, which is what the
# macOS "About" window shows. Replace it with the version from the script.
VERSION=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' episode_renamer.py)
if [ -z "$VERSION" ]; then
    echo "Could not read VERSION from episode_renamer.py" >&2
    exit 1
fi
for KEY in CFBundleShortVersionString CFBundleVersion; do
    /usr/libexec/PlistBuddy -c "Set :$KEY $VERSION" "$PLIST" 2>/dev/null \
        || /usr/libexec/PlistBuddy -c "Add :$KEY string $VERSION" "$PLIST"
done

# Editing the plist invalidates the signature PyInstaller applied, so sign again.
codesign --force --deep --sign - "$APP"

echo
echo "Done: $APP (version $VERSION)"
