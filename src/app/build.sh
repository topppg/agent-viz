#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."

build_dir="$(pwd)/build/mac-app"
app="$build_dir/Spiritboard.app"
version="$(uv run --no-project --no-config python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
arch="$(uname -m)"

rm -rf "$build_dir"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources" "$app/Contents/Helpers"
cp src/app/Info.plist "$app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $version" "$app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $version" "$app/Contents/Info.plist"

pyinstaller_args=(
  --noconfirm --clean --onefile
  --name spiritboard-backend
  --distpath "$build_dir/backend"
  --workpath "$build_dir/pyinstaller"
  --specpath "$build_dir"
  --collect-data spiritboard
  --hidden-import keyring.backends.macOS
)
if [[ -n "${SPIRITBOARD_SIGN_IDENTITY:-}" ]]; then
  pyinstaller_args+=(--codesign-identity "$SPIRITBOARD_SIGN_IDENTITY")
fi
uv run --python 3.12 --managed-python --with pyinstaller==6.20.0 \
  pyinstaller "${pyinstaller_args[@]}" src/app/backend.py
cp "$build_dir/backend/spiritboard-backend" "$app/Contents/Helpers/"

iconset="$build_dir/Spiritboard.iconset"
mkdir -p "$iconset"
sips -s format png -Z 1024 src/spiritboard/static/favicon.svg \
  --out "$iconset/icon_512x512@2x.png" >/dev/null
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$iconset/icon_512x512@2x.png" \
    --out "$iconset/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z "$double" "$double" "$iconset/icon_512x512@2x.png" \
    --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$iconset" -o "$app/Contents/Resources/Spiritboard.icns"

swiftc -O -swift-version 5 -target "$(uname -m)-apple-macosx13.0" \
  -framework AppKit -framework WebKit \
  src/app/Spiritboard.swift -o "$app/Contents/MacOS/Spiritboard"

if [[ -n "${SPIRITBOARD_SIGN_IDENTITY:-}" ]]; then
  codesign --force --options runtime --timestamp --sign "$SPIRITBOARD_SIGN_IDENTITY" "$app"
  codesign --verify --deep --strict "$app"
fi

dmg="$build_dir/Spiritboard-$version-macos-$arch.dmg"
hdiutil create -quiet -volname Spiritboard -srcfolder "$app" -format UDZO "$dmg"
if [[ -n "${SPIRITBOARD_NOTARY_PROFILE:-}" ]]; then
  if [[ -z "${SPIRITBOARD_SIGN_IDENTITY:-}" ]]; then
    echo 'Notarization requires SPIRITBOARD_SIGN_IDENTITY.' >&2
    exit 1
  fi
  xcrun notarytool submit "$dmg" --keychain-profile "$SPIRITBOARD_NOTARY_PROFILE" --wait
  xcrun stapler staple "$dmg"
fi

echo "$dmg"
