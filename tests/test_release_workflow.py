"""Release pairing is tested against synthetic GitHub and PyPI responses only."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("paired_release", ROOT / "scripts/paired_release.py")
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
REPOSITORY = "example/spiritboard"
VERSION = "0.1.0"
SHA = "a" * 40
OTHER_SHA = "b" * 40


class FakeGitHub:
    def __init__(self) -> None:
        self.tag: dict[str, str] | None = None
        self.annotated: dict[str, dict[str, str]] = {}
        self.release: dict[str, object] | None = None
        self.assets: dict[int, bytes] = {}
        self.calls: list[tuple[str, dict[str, object] | None, Path | None]] = []
        self.fail_upload = False

    def __call__(
        self,
        endpoint: str,
        *,
        payload: dict[str, object] | None = None,
        upload: Path | None = None,
        download: bool = False,
    ) -> dict[str, object] | bytes | None:
        self.calls.append((endpoint, payload, upload))
        if download:
            return self.assets[int(endpoint.rsplit("/", 1)[-1])]
        if endpoint.endswith("/git/ref/tags/v0.1.0"):
            return {"object": self.tag} if self.tag is not None else None
        if "/git/tags/" in endpoint:
            return {"object": self.annotated[endpoint.rsplit("/", 1)[-1]]}
        if endpoint.endswith("/git/refs"):
            assert payload == {"ref": "refs/tags/v0.1.0", "sha": SHA}
            self.tag = {"type": "commit", "sha": SHA}
            return {"object": self.tag}
        if endpoint.endswith("/releases/tags/v0.1.0"):
            return copy.deepcopy(self.release)
        if endpoint.endswith("/releases"):
            assert payload is not None and payload["target_commitish"] == SHA
            self.release = {**payload, "id": 17, "assets": []}
            return copy.deepcopy(self.release)
        if upload is not None:
            if self.fail_upload:
                raise release.ReleaseError("Synthetic asset upload failure")
            assert self.release is not None
            identifier = len(self.assets) + 1
            name = urllib.parse.parse_qs(urllib.parse.urlsplit(endpoint).query)["name"][0]
            asset = {"name": name, "id": identifier}
            self.assets[identifier] = upload.read_bytes()
            self.release["assets"].append(asset)
            return asset
        raise AssertionError(f"Unexpected GitHub endpoint: {endpoint}")

    @property
    def writes(self) -> list[tuple[str, dict[str, object] | None, Path | None]]:
        return [call for call in self.calls if call[1] is not None or call[2] is not None]


@pytest.fixture
def artifacts(tmp_path: Path) -> Path:
    directory = tmp_path / "dist"
    directory.mkdir()
    (directory / "spiritboard-0.1.0-py3-none-any.whl").write_bytes(b"synthetic wheel")
    (directory / "spiritboard-0.1.0.tar.gz").write_bytes(b"synthetic source archive")
    return directory


def receipt(directory: Path) -> dict[str, object]:
    return {
        "info": {"name": "spiritboard", "version": VERSION},
        "urls": [
            {
                "filename": path.name,
                "digests": {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
            }
            for path in sorted(directory.iterdir())
        ],
    }


@pytest.fixture
def services(monkeypatch: pytest.MonkeyPatch, artifacts: Path) -> FakeGitHub:
    github = FakeGitHub()
    monkeypatch.setattr(release, "github", github)
    monkeypatch.setattr(release, "pypi_receipt", lambda version: receipt(artifacts))
    monkeypatch.setattr(release.time, "sleep", lambda duration: None)
    return github


@pytest.mark.parametrize("annotated", [False, True])
def test_preflight_resolves_tags_without_writing(services: FakeGitHub, annotated: bool) -> None:
    assert release.check_tag(REPOSITORY, VERSION, SHA) is False
    services.tag = {
        "type": "tag" if annotated else "commit",
        "sha": OTHER_SHA if annotated else SHA,
    }
    services.annotated[OTHER_SHA] = {"type": "commit", "sha": SHA}
    assert release.check_tag(REPOSITORY, VERSION, SHA) is True
    assert services.writes == []


@pytest.mark.parametrize("annotated", [False, True])
def test_conflicting_tag_stops_before_any_write_or_receipt(
    services: FakeGitHub, artifacts: Path, monkeypatch: pytest.MonkeyPatch, annotated: bool
) -> None:
    services.tag = {"type": "tag" if annotated else "commit", "sha": OTHER_SHA}
    services.annotated[OTHER_SHA] = {"type": "commit", "sha": OTHER_SHA}
    monkeypatch.setattr(release, "pypi_receipt", lambda version: pytest.fail("Must not query PyPI"))
    with pytest.raises(release.ReleaseError, match="different commit"):
        release.pair_release(REPOSITORY, VERSION, SHA, artifacts)
    assert services.writes == []


@pytest.mark.parametrize(
    "version", ["0.1.0; echo no", "$(echo no)", "v0.1.0", "01.1.0", "0.1.0/refs/main", "0.1"]
)
def test_unsafe_or_unsupported_version_rejected(services: FakeGitHub, version: str) -> None:
    with pytest.raises(release.ReleaseError, match="X.Y.Z"):
        release.check_tag(REPOSITORY, version, SHA)
    assert services.calls == []


def test_pairing_creates_exact_tag_release_and_matching_artifacts(
    services: FakeGitHub, artifacts: Path
) -> None:
    release.pair_release(REPOSITORY, VERSION, SHA, artifacts)
    assert services.tag == {"type": "commit", "sha": SHA}
    assert services.release is not None
    assert services.release["target_commitish"] == SHA
    body = services.release["body"]
    assert f"https://github.com/{REPOSITORY}/commit/{SHA}" in body
    assert "https://pypi.org/project/spiritboard/0.1.0/" in body
    attached = {asset["name"]: services.assets[asset["id"]] for asset in services.release["assets"]}
    for path in artifacts.iterdir():
        assert attached[path.name] == path.read_bytes()
    assert set(attached) == {path.name for path in artifacts.iterdir()} | {"SHA256SUMS"}
    assert (
        attached["SHA256SUMS"]
        == "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in sorted(artifacts.iterdir())
        ).encode()
    )
    assert len(list(artifacts.iterdir())) == 2


def test_rerun_only_verifies_existing_assets(services: FakeGitHub, artifacts: Path) -> None:
    release.pair_release(REPOSITORY, VERSION, SHA, artifacts)
    services.calls.clear()
    release.pair_release(REPOSITORY, VERSION, SHA, artifacts)
    assert services.writes == []


def test_partial_github_failure_can_retry_without_republishing_pypi(
    services: FakeGitHub, artifacts: Path
) -> None:
    services.fail_upload = True
    with pytest.raises(release.ReleaseError, match="Synthetic asset"):
        release.pair_release(REPOSITORY, VERSION, SHA, artifacts)
    assert services.release is not None
    services.fail_upload = False
    services.calls.clear()
    release.pair_release(REPOSITORY, VERSION, SHA, artifacts)
    assert len(services.writes) == 3
    assert all(call[2] is not None for call in services.writes)


def test_missing_assets_only_are_uploaded(services: FakeGitHub, artifacts: Path) -> None:
    release.pair_release(REPOSITORY, VERSION, SHA, artifacts)
    services.release["assets"].pop()
    services.calls.clear()
    release.pair_release(REPOSITORY, VERSION, SHA, artifacts)
    assert len(services.writes) == 1
    assert services.writes[0][2] is not None


def test_conflicting_asset_is_not_overwritten(services: FakeGitHub, artifacts: Path) -> None:
    release.pair_release(REPOSITORY, VERSION, SHA, artifacts)
    services.assets[1] = b"not the validated checksum file"
    services.calls.clear()
    with pytest.raises(release.ReleaseError, match="refusing to overwrite"):
        release.pair_release(REPOSITORY, VERSION, SHA, artifacts)
    assert services.writes == []


def test_conflicting_release_metadata_is_not_overwritten(
    services: FakeGitHub, artifacts: Path
) -> None:
    release.pair_release(REPOSITORY, VERSION, SHA, artifacts)
    services.release["body"] = "An unrelated release"
    services.calls.clear()
    with pytest.raises(release.ReleaseError, match="metadata conflicts"):
        release.pair_release(REPOSITORY, VERSION, SHA, artifacts)
    assert services.writes == []


@pytest.mark.parametrize("conflict", ["metadata", "asset"])
def test_preflight_rejects_release_conflicts_before_upload(
    services: FakeGitHub, artifacts: Path, conflict: str
) -> None:
    release.pair_release(REPOSITORY, VERSION, SHA, artifacts)
    if conflict == "metadata":
        services.release["body"] = "An unrelated release"
    else:
        services.assets[1] = b"an unrelated asset"
    services.calls.clear()
    with pytest.raises(release.ReleaseError):
        release.preflight(REPOSITORY, VERSION, SHA, artifacts)
    assert services.writes == []


def test_preflight_allows_matching_or_missing_release_without_writes(
    services: FakeGitHub, artifacts: Path
) -> None:
    release.preflight(REPOSITORY, VERSION, SHA, artifacts)
    assert services.writes == []
    release.pair_release(REPOSITORY, VERSION, SHA, artifacts)
    services.calls.clear()
    release.preflight(REPOSITORY, VERSION, SHA, artifacts)
    assert services.writes == []


@pytest.mark.parametrize("change", ["digest", "extra", "project", "version", "duplicate"])
def test_incorrect_pypi_receipt_prevents_public_tag_or_release(
    services: FakeGitHub, artifacts: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    value = receipt(artifacts)
    if change == "digest":
        value["urls"][0]["digests"]["sha256"] = "0" * 64
    elif change == "extra":
        value["urls"].append({"filename": "unreviewed.whl", "digests": {"sha256": "0" * 64}})
    elif change in {"project", "version"}:
        value["info"]["name" if change == "project" else "version"] = "different"
    else:
        value["urls"].append(value["urls"][0])
    monkeypatch.setattr(release, "pypi_receipt", lambda version: value)
    with pytest.raises(release.ReleaseError):
        release.pair_release(REPOSITORY, VERSION, SHA, artifacts)
    assert services.writes == []


def test_pypi_receipts_wait_for_both_files(
    artifacts: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete = receipt(artifacts)
    incomplete = copy.deepcopy(complete)
    incomplete["urls"].pop()
    responses = iter([None, incomplete, complete])
    sleeps: list[int] = []
    monkeypatch.setattr(release, "pypi_receipt", lambda version: next(responses))
    monkeypatch.setattr(release.time, "sleep", sleeps.append)
    release.verify_pypi(VERSION, release.distributions(artifacts, VERSION), attempts=3)
    assert sleeps == [10, 10]


def test_missing_pypi_receipts_fail_without_github_writes(
    services: FakeGitHub, artifacts: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release, "pypi_receipt", lambda version: None)
    with pytest.raises(release.ReleaseError, match="receipts are incomplete"):
        release.pair_release(REPOSITORY, VERSION, SHA, artifacts)
    assert services.writes == []


def test_unexpected_local_artifact_is_rejected(services: FakeGitHub, artifacts: Path) -> None:
    (artifacts / "extra.whl").write_bytes(b"unreviewed")
    with pytest.raises(release.ReleaseError, match="only the validated"):
        release.pair_release(REPOSITORY, VERSION, SHA, artifacts)
    assert services.calls == []


def test_github_client_uses_argument_array_and_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        captured.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, b'{"id": 17}', b"")

    monkeypatch.setattr(release.subprocess, "run", run)
    assert release.github("repos/example/spiritboard/releases", payload={"body": "$secret"}) == {
        "id": 17
    }
    command, kwargs = captured[0]
    assert command == [
        "gh",
        "api",
        "repos/example/spiritboard/releases",
        "--method",
        "POST",
        "--input",
        "-",
    ]
    assert json.loads(kwargs["input"]) == {"body": "$secret"}
    assert "shell" not in kwargs


def test_github_errors_are_not_mistaken_for_missing_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, b"", b"denied (HTTP 403)"
        ),
    )
    with pytest.raises(release.ReleaseError, match="GitHub request failed"):
        release.check_tag(REPOSITORY, VERSION, SHA)


def test_workflow_pairs_after_publish_with_separate_permissions() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    publish, pairing = workflow.split("\n  publish:\n", 1)[1].split("\n  github-release:\n", 1)
    assert "github.ref == 'refs/heads/main' && inputs.publish" in publish
    assert "github.ref == 'refs/heads/main' && inputs.publish" in pairing
    assert "contents: read" in publish and "id-token: write" in publish
    assert "contents: write" not in publish
    assert "needs: [build, publish]" in pairing
    assert "contents: write" in pairing and "id-token:" not in pairing
    assert "paired_release.py preflight" in publish
    assert publish.index("paired_release.py preflight") < publish.index("uv publish")
    assert "paired_release.py pair" in pairing
    assert "RELEASE_SHA: ${{ github.sha }}" in publish
    assert "RELEASE_SHA: ${{ github.sha }}" in pairing
    assert '--version "$RELEASE_VERSION" --sha "$RELEASE_SHA"' in pairing
    assert "name: spiritboard-dist" in publish and "name: spiritboard-dist" in pairing
    assert "uv publish" not in pairing
