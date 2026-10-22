# macOS app

The macOS app gives Spiritboard its own Dock icon and window. It contains the same Python
dashboard as the `uvx` package, plus a bundled Python runtime. Users can download the DMG,
drag `Spiritboard.app` to Applications, and open it without installing Python or uv. The app
uses the CLI's normal data directory: it shows the dashboard that already owns that directory,
or starts its own server there on a free port.

A CLI dashboard started with a custom `--data-dir` keeps its own history. To show it in the app,
choose **File › Choose Data Folder…** and pick that folder; hidden folders such as
`.spiritboard` are listed. The app remembers the choice. It connects only after verifying the
dashboard through the folder's private instance details, so another program listening on a local
port cannot impersonate Spiritboard. If no dashboard is running for the chosen folder, the app
says so and offers to try again, choose another folder, or use the default; it never starts a
server there or shows other data instead. **File › Use Default Data Folder** returns to normal.

The app opens dashboard dropdowns as a compact Mac-style menu instead of WebKit's legacy popup.
It brings this menu itself (`src/app/select-menu.js`), so it also works with an older dashboard.

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
