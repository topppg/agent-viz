# macOS app

The macOS app gives Spiritboard its own Dock icon and window. It contains the same Python
dashboard as the `uvx` package, plus a bundled Python runtime. Signed Apple Silicon and Intel
DMGs are built by the release workflow but have not been published yet. Once distributed, users
can drag `Spiritboard.app` to Applications and open it without installing Python or uv. The app uses the
CLI's normal data directory: it shows the dashboard that already owns that directory, or starts
its own server there on a free port.

A CLI dashboard started with a custom `--data-dir` keeps its own history. To show it in the app,
choose **File › Choose Data Folder…** and pick that folder; hidden folders such as
`.spiritboard` are listed. The app remembers the choice. It connects only after verifying the
dashboard through the folder's private instance details, so another program listening on a local
port cannot impersonate Spiritboard. If no dashboard is running for the chosen folder, the app
says so and offers to try again, choose another folder, or use the default; it never starts a
server there or shows other data instead. **File › Use Default Data Folder** returns to normal.

The app opens dashboard dropdowns as a compact Mac-style menu instead of WebKit's legacy popup.
It brings this menu itself (`src/app/select-menu.js`), so it also works with an older dashboard.
Everything else uses the dashboard's own layout, so the window looks like the browser at every size.

## Updates

The app bundles the dashboard code in this checkout when `src/app/build.sh` runs. Rebuild the app
after changing the web dashboard to include those changes in its own server. If a dashboard is
already running for the selected data folder, the app connects to it and shows that server's web
UI; restart that server to load its updated code. Mac-specific controls are bundled
with the app, so changes to them always need an app rebuild.

## Build

On a Mac with Xcode command-line tools and uv, run this from a clean `main` checkout to
update it, build the latest app, install it in `~/Applications`, and open it:

```sh
bash src/app/install.sh
```

Run the same command to update the app later. The installer stops if the checkout has
local changes or is on another branch. It keeps the current installed app if the new
copy fails. To build a DMG without installing the app, run:

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

The release workflow builds, signs, notarizes, and checks both Mac architectures before uploading
the Python package to PyPI. It attaches both DMGs to the matching GitHub Release, with their
SHA-256 digests in `SHA256SUMS`. The existing `uvx spiritboard` command remains available. See
the [distribution guide](DISTRIBUTION.md#maintainer-setup-macos-releases) for the required Apple
credentials and release procedure. No public Mac DMG exists until a new version is released.
