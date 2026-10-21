# Installing and releasing Spiritboard

Spiritboard is a local application, not a hosted dashboard. The Python package contains the
collector, command, and browser assets. End users do not need Node.js, a source checkout, an API
key, or a separate web server. Python 3.12 or newer is required; `uv` can manage Python for you.
The installer also resolves `PyJWT[crypto]` for verified ChatGPT sign-in and `keyring` for
native credential storage, along with their platform dependencies. No Codex CLI or Node.js
runtime is needed for that connection.

Saving provider connections requires macOS Keychain, Windows Credential Manager, or an unlocked
Linux Secret Service collection on a D-Bus user session. A headless Linux machine may need a
desktop keyring service before it can save credentials. Monitoring and demo mode do not require
one. Spiritboard never selects a plaintext, null, third-party, or chained storage fallback.

## Install from GitHub today

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```bash
uvx --from git+https://github.com/topppg/spiritboard@main spiritboard
```

This installs into uv's isolated tool cache and opens the local dashboard. Keep the terminal
running; Ctrl-C stops Spiritboard, not the agents being observed. Nothing is uploaded and model
summaries are off unless explicitly enabled (a saved choice is restored on later launches).
Provider connections are optional and configured in the local **AI summaries** panel.
Fetching the package itself requires internet access.
The GitHub repository, package, and installed command are all named `spiritboard`.

For an installed command you can use repeatedly:

```bash
uv tool install git+https://github.com/topppg/spiritboard@main
spiritboard
```

If uv reports that its tool directory is not on your PATH, run `uv tool update-shell` and open a
new terminal. To upgrade this GitHub installation, rerun the install command with `--force --refresh`.
For a reproducible installation, replace `@main` with a reviewed full commit SHA or a published
release tag. The moving `main` branch is a development channel, not an immutable release.

Useful launch options:

```bash
spiritboard --demo
spiritboard --no-open
spiritboard --data-dir /path/to/private/spiritboard-data
spiritboard --help
spiritboard --version
```

The first command uses synthetic sessions rather than reading native profiles. `--no-open`
prints the local address without launching a browser. Data normally lives in an OS user-data
directory, so launching from another folder does not create another database. The demo uses
separate default storage. Reopening the same configured instance reuses its dashboard rather
than starting a second collector. Run `serve --help` for discovery, storage, and summary settings.

Existing installations keep their data: explicitly pass the former `--data-dir` to continue
using it. No old folders are silently moved or deleted. Uninstalling the tool with
`uv tool uninstall spiritboard` removes its environment, not your saved dashboard data.

## PyPI is a separate release step

The workflows prepare publishing, but do not mean that the `spiritboard` name has been reserved,
that a package is available on PyPI, or that a release has happened. A maintainer must verify the
name, configure the account and publishing permissions, and explicitly approve the first
upload. The package is MIT-licensed (`LICENSE`, `license = "MIT"` in `pyproject.toml`).

Only after a successful, verified PyPI release should the default installation become:

```bash
uvx spiritboard
# Or install the released command:
uv tool install spiritboard
```

Do not advertise those short commands before the intended package is actually available.

## What CI checks

`Distribution` builds both a source archive and a wheel on Linux, macOS, and Windows with Python
3.12 and 3.13. It validates metadata, installs each artifact into an isolated environment, and
checks the command's help/version and the bundled HTML, CSS, and JavaScript from outside the
checkout, without reusing cached installations of an older same-version build. Synthetic
subprocess tests also check startup, storage, instance reuse, conflicting
options, demo isolation and restart against each installed artifact. These checks do not
discover profiles, read transcripts, start model calls, or prove
that every native integration works on every OS. The normal `Checks` workflow runs the unit tests.

Local checks before preparing a release:

```bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build --no-sources
uvx --from twine==6.2.0 twine check --strict dist/*
```

Inspect the archives for accidental local data. Test the installed wheel from a fresh directory,
including demo startup, dashboard assets, a second launch, and clean shutdown. Never use real
transcripts as release fixtures. Include only the intended artifacts in `dist/`; use a clean
checkout for each release build rather than mixing versions in an old output folder.

## Maintainer setup: trusted publishing

One-time actions, performed by the repository/package owner:

1. Verify that the PyPI project name can be used. Update metadata and docs if the final package
   name differs from `spiritboard`.
2. Create a GitHub environment named `pypi`. Restrict deployment branches to `main`, require a
   release reviewer where available, and disable self-review where appropriate.
3. Add a PyPI trusted publisher (or pending publisher for the first release) with owner `topppg`,
   repository `spiritboard`, workflow filename `release.yml`, and environment `pypi`. All fields must
   match exactly. If the GitHub repository is renamed, update the publisher and project URLs first.
4. Protect the default branch and require the normal checks and distribution matrix before release.

Do not add a long-lived PyPI token to the repository. The publishing job requests a short-lived
identity token; the build and test jobs do not have that permission. See the official
[PyPI trusted publishing guide](https://docs.pypi.org/trusted-publishers/using-a-publisher/) and
[uv publishing guidance](https://docs.astral.sh/uv/guides/integration/github/#publishing-to-pypi).

## Release procedure

1. Prepare and review a version change in `pyproject.toml` and
   `src/spiritboard/__init__.py`; keep the two values equal and update `uv.lock` as needed.
2. Merge the approved changes and verify `Checks` and `Distribution` on `main`.
3. In GitHub Actions, select **Release package**, choose the `main` branch, enter the exact
   committed version, and leave **publish** unchecked. This reruns the distribution matrix,
   tests, and metadata validation, then retains the built files as a seven-day artifact.
4. Inspect that run and its artifacts. When ready to upload, manually run the same workflow
   against the reviewed `main` commit with **publish** checked. Ensure `main` has not advanced
   since review. Approve the `pypi` deployment only after inspecting that run's source and outputs.
5. The publishing job rejects an existing `vVERSION` tag that resolves to another commit,
   including annotated tags, or conflicting existing GitHub release metadata or assets.
   After PyPI accepts the upload, a separate GitHub release job checks
   PyPI's SHA-256 receipts against both files from that run's validated build artifact. It then
   creates `vVERSION` at the run's exact `main` commit SHA, not the latest moving `main` branch,
   and a matching GitHub Release. The release links PyPI and the source commit and attaches the
   same wheel, source archive, and a `SHA256SUMS` file. Existing tags and assets are never moved
   or overwritten.
6. Verify the resulting PyPI project, version, README, GitHub tag/release, and installation in
   a clean environment. Only then update public installation instructions and announce the release.

The workflow is manual-only, refuses non-`main` refs, and checks the requested version against
both package and source metadata. Pushes, pull requests, tags, and GitHub release events do not
publish packages. Uploads are permanent versioned releases; fixing a bad release normally means
shipping a new version rather than overwriting it.

Public versions currently use `X.Y.Z` (for example, `0.1.0`). Every successful public release is
paired with its matching `vX.Y.Z` GitHub tag and release; a dry run never creates either. Only
the PyPI job has identity-token permission. Only the separate GitHub release job has repository
write permission; it cannot upload to PyPI.

### Recovering a partial release

PyPI and GitHub are separate services, so this is not an atomic transaction: PyPI can succeed
while GitHub pairing fails. The workflow then fails and reports the partial release. Do not
announce completion until both sides have been verified.

If publishing succeeded but the GitHub job failed, inspect its error and select **Re-run failed
jobs** on the same run, within the build artifact's seven-day retention period. Do not start
another publishing run, rerun all jobs, republish the version, move a tag, or delete existing
assets. The pairing job checks PyPI again, verifies any existing release assets byte-for-byte,
and uploads only missing assets. A conflicting tag, release, or file is an error requiring
inspection, not permission to overwrite it. If the run's validated artifact has expired, stop
and recover the exact original files with independent hash verification before any manual
recovery; rebuilding the same version is not a substitute for the original artifact.
