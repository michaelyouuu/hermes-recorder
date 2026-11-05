#!/bin/sh
# S1c (throwaway): build a minimal helper .app around Hermes's own python so macOS TCC treats the
# helper as its own app ("Hermes Recorder Helper") instead of osascript / VS Code / Terminal.
# Generated locally, ad-hoc signed; nothing compiled is committed.
set -eu
PYROOT="$1"            # e.g. ~/.hermes/tools/python-3.14.9+...-darwin-arm64
OUT="$2"               # e.g. spikes/out/Hermes Recorder Helper.app
rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS"
cp "$PYROOT/bin/python3.14" "$OUT/Contents/MacOS/hermes-recorder-helper"
cat > "$OUT/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>io.github.michaelyouuu.hermes-recorder.helper</string>
  <key>CFBundleName</key><string>Hermes Recorder Helper</string>
  <key>CFBundleDisplayName</key><string>Hermes Recorder Helper</string>
  <key>CFBundleExecutable</key><string>hermes-recorder-helper</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>0.0.1</string>
  <key>CFBundleShortVersionString</key><string>0.0.1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Hermes Recorder transcribes your narration locally so Hermes can answer questions about it.</string>
  <key>NSAudioCaptureUsageDescription</key><string>Hermes Recorder transcribes audio from apps you choose, locally on this Mac.</string>
  <key>NSScreenCaptureUsageDescription</key><string>Hermes Recorder captures audio from apps you choose, locally on this Mac.</string>
  <key>LSEnvironment</key><dict><key>PYTHONHOME</key><string>$PYROOT</string></dict>
</dict></plist>
PLIST
codesign --force --sign - --identifier io.github.michaelyouuu.hermes-recorder.helper "$OUT"
codesign -dv "$OUT" 2>&1 | grep -E "Identifier|Signature"
