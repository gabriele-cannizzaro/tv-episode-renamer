#!/bin/bash
# Optional: builds a fully standalone "TV Episode Renamer.app" (bundles Python + Tk),
# so it runs on Macs without Python installed. Run with:  bash build_app.sh
# Requires Python 3 from python.org (it includes Tk).
set -e
cd "$(dirname "$0")"
python3 -m pip install --upgrade pyinstaller
python3 -m PyInstaller --windowed --noconfirm --name "TV Episode Renamer" episode_renamer.py
echo
echo "Done: dist/TV Episode Renamer.app"
