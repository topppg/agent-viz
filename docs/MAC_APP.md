# macOS app

The macOS app gives Spiritboard its own Dock icon and window. It contains the same Python
dashboard as the `uvx` package, plus a bundled Python runtime. Users can download the DMG,
drag `Spiritboard.app` to Applications, and open it without installing Python or uv. The app
uses the CLI's normal data directory. If a Spiritboard CLI dashboard is already running on the
standard port 8766, the app displays that dashboard, including its chosen data directory and
settings. Otherwise, the app starts its own server with the normal default data directory.

## Build

On a Mac with Xcode command-line tools and uv:

```sh
bash src/app/build.sh
```

The DMG is written to `build/mac-app/` and is built for the current Mac architecture. Build on
both Apple Silicon and Intel Macs to offer both architectures. The unsigned output is for local
testing only. For a public download, use a Developer ID Application identity and a configured
`notarytool` keychain profile:

```sh
SPIRITBOARD_SIGN_IDENTITY='Developer ID Application: Example (TEAMID)' \
SPIRITBOARD_NOTARY_PROFILE=spiritboard \
  bash src/app/build.sh
```

The build signs the bundled Python program and the app, notarizes the DMG, and staples its
ticket. Apple requires Developer ID signing and notarization for normal distribution outside
the Mac App Store. Keep the signing identity and notarization credentials out of the repo.

The existing `uvx spiritboard` command remains available for CLI users. DMGs should be added
to GitHub Releases only after the release pairing checks are extended to account for them;
the current pairing expects exactly the Python wheel, source archive, and checksum file.
