"""Pair a validated PyPI upload with an immutable GitHub tag and matching assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class ReleaseError(ValueError):
    """A release could not be verified without changing existing public data."""


def github(
    endpoint: str,
    *,
    payload: dict[str, object] | None = None,
    upload: Path | None = None,
    download: bool = False,
) -> dict[str, object] | bytes | None:
    command = ["gh", "api", endpoint]
    body = None
    if payload is not None:
        command += ["--method", "POST", "--input", "-"]
        body = json.dumps(payload).encode()
    if upload is not None:
        command += [
            "--method",
            "POST",
            "--header",
            "Content-Type: application/octet-stream",
            "--input",
            str(upload),
        ]
    if download:
        command += ["--header", "Accept: application/octet-stream"]
    result = subprocess.run(command, input=body, capture_output=True, check=False, timeout=120)
    if result.returncode:
        if payload is None and upload is None and b"(HTTP 404)" in result.stderr:
            return None
        raise ReleaseError("GitHub request failed; no existing tag or asset was overwritten")
    if download:
        return result.stdout
    try:
        value = json.loads(result.stdout)
    except (ValueError, UnicodeError) as exc:
        raise ReleaseError("GitHub returned an invalid response") from exc
    if not isinstance(value, dict):
        raise ReleaseError("GitHub returned an unexpected response")
    return value


def github_object(endpoint: str, **kwargs: object) -> dict[str, object] | None:
    value = github(endpoint, **kwargs)
    if value is not None and not isinstance(value, dict):
        raise ReleaseError("GitHub returned an unexpected response")
    return value


def validate_target(repository: str, version: str, sha: str) -> str:
    if (
        not REPOSITORY.fullmatch(repository)
        or not VERSION.fullmatch(version)
        or not SHA.fullmatch(sha)
    ):
        raise ReleaseError("Expected owner/repository, an X.Y.Z version, and a full commit SHA")
    return f"v{version}"


def check_tag(repository: str, version: str, sha: str) -> bool:
    tag = validate_target(repository, version, sha)
    ref = github_object(f"repos/{repository}/git/ref/tags/{tag}")
    if ref is None:
        return False
    target = ref.get("object")
    for _ in range(10):
        if not isinstance(target, dict) or not SHA.fullmatch(str(target.get("sha", ""))):
            raise ReleaseError("Existing release tag has an invalid target")
        if target.get("type") == "commit":
            if target["sha"] != sha:
                raise ReleaseError(
                    "Existing release tag points to a different commit; refusing release"
                )
            return True
        if target.get("type") != "tag":
            break
        annotated = github_object(f"repos/{repository}/git/tags/{target['sha']}")
        target = annotated.get("object") if annotated else None
    raise ReleaseError("Existing release tag does not resolve to the intended commit")


def distributions(directory: Path, version: str) -> dict[str, str]:
    expected = {f"spiritboard-{version}-py3-none-any.whl", f"spiritboard-{version}.tar.gz"}
    files = list(directory.iterdir())
    if {path.name for path in files} != expected or any(
        not path.is_file() or path.is_symlink() for path in files
    ):
        raise ReleaseError("Expected only the validated Spiritboard wheel and source archive")
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(files)}


def pypi_receipt(version: str) -> dict[str, object] | None:
    url = f"https://pypi.org/pypi/spiritboard/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            data = response.read(2_000_001)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise ReleaseError("Could not verify the PyPI upload") from exc
    except OSError as exc:
        raise ReleaseError("Could not verify the PyPI upload") from exc
    if len(data) > 2_000_000:
        raise ReleaseError("PyPI returned an oversized receipt")
    try:
        receipt = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise ReleaseError("PyPI returned an invalid receipt") from exc
    if not isinstance(receipt, dict):
        raise ReleaseError("PyPI returned an invalid receipt")
    return receipt


def verify_pypi(version: str, hashes: dict[str, str], *, attempts: int = 12) -> None:
    for attempt in range(attempts):
        receipt = pypi_receipt(version)
        if receipt is not None:
            info, urls = receipt.get("info"), receipt.get("urls")
            if (
                not isinstance(info, dict)
                or info.get("name") != "spiritboard"
                or info.get("version") != version
            ):
                raise ReleaseError("PyPI receipt does not match the requested version")
            if not isinstance(urls, list):
                raise ReleaseError("PyPI returned invalid distribution receipts")
            published: dict[str, str] = {}
            for item in urls:
                if not isinstance(item, dict) or not isinstance(item.get("digests"), dict):
                    raise ReleaseError("PyPI returned invalid distribution digests")
                name, digest = item.get("filename"), item["digests"].get("sha256")
                if not isinstance(name, str) or not isinstance(digest, str) or name in published:
                    raise ReleaseError("PyPI returned invalid distribution digests")
                published[name] = digest
            if any(
                name not in hashes or digest != hashes[name] for name, digest in published.items()
            ):
                raise ReleaseError("PyPI files differ from the validated release artifacts")
            if published == hashes:
                return
        if attempt + 1 < attempts:
            time.sleep(10)
    raise ReleaseError("PyPI upload receipts are incomplete; rerun the failed GitHub release job")


def release_body(repository: str, version: str, sha: str) -> str:
    return (
        f"Spiritboard {version}\n\n"
        f"PyPI: https://pypi.org/project/spiritboard/{version}/\n"
        f"Source: https://github.com/{repository}/commit/{sha}\n\n"
        "The attached wheel and source archive are the exact files verified on PyPI.\n"
        "SHA256SUMS records their SHA-256 digests.\n"
    )


def verify_asset(repository: str, asset: dict[str, object], name: str, digest: str) -> None:
    identifier = asset.get("id")
    if not isinstance(identifier, int) or isinstance(identifier, bool):
        raise ReleaseError("GitHub returned an invalid release asset")
    data = github(f"repos/{repository}/releases/assets/{identifier}", download=True)
    if not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != digest:
        raise ReleaseError(f"Existing GitHub asset {name} differs; refusing to overwrite it")


def validate_release(
    repository: str,
    version: str,
    sha: str,
    value: dict[str, object] | None,
    hashes: dict[str, str],
) -> dict[str, dict[str, object]]:
    if (
        value is None
        or value.get("tag_name") != f"v{version}"
        or value.get("body") != release_body(repository, version, sha)
        or value.get("draft") is not False
        or value.get("prerelease") is not False
        or not isinstance(value.get("id"), int)
    ):
        raise ReleaseError(
            "Existing GitHub release metadata conflicts; inspect it without overwriting"
        )
    assets = value.get("assets")
    if not isinstance(assets, list) or len(assets) > 3:
        raise ReleaseError("Unexpected GitHub release assets; inspect without overwriting")
    existing: dict[str, dict[str, object]] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            raise ReleaseError("GitHub returned an invalid release asset")
        name = asset.get("name")
        if not isinstance(name, str) or name not in hashes or name in existing:
            raise ReleaseError("Unexpected GitHub release assets; inspect without overwriting")
        existing[name] = asset
    # Check every existing asset before uploading missing ones; never replace a public file.
    for name, asset in existing.items():
        verify_asset(repository, asset, name, hashes[name])
    return existing


def checksum_bytes(hashes: dict[str, str]) -> bytes:
    return "".join(f"{digest}  {name}\n" for name, digest in sorted(hashes.items())).encode()


def preflight(repository: str, version: str, sha: str, directory: Path) -> None:
    tag = validate_target(repository, version, sha)
    hashes = distributions(directory, version)
    check_tag(repository, version, sha)
    hashes["SHA256SUMS"] = hashlib.sha256(checksum_bytes(hashes)).hexdigest()
    value = github_object(f"repos/{repository}/releases/tags/{tag}")
    if value is not None:
        validate_release(repository, version, sha, value, hashes)


def pair_release(repository: str, version: str, sha: str, directory: Path) -> None:
    tag = validate_target(repository, version, sha)
    preflight(repository, version, sha, directory)
    hashes = distributions(directory, version)
    verify_pypi(version, hashes)
    if not check_tag(repository, version, sha):
        github_object(
            f"repos/{repository}/git/refs", payload={"ref": f"refs/tags/{tag}", "sha": sha}
        )
    if not check_tag(repository, version, sha):
        raise ReleaseError("Release tag disappeared; refusing to create a release")
    checksum_data = checksum_bytes(hashes)
    hashes["SHA256SUMS"] = hashlib.sha256(checksum_data).hexdigest()
    value = github_object(f"repos/{repository}/releases/tags/{tag}")
    if value is None:
        value = github_object(
            f"repos/{repository}/releases",
            payload={
                "tag_name": tag,
                "target_commitish": sha,
                "name": f"Spiritboard {version}",
                "body": release_body(repository, version, sha),
                "draft": False,
                "prerelease": False,
            },
        )
    existing = validate_release(repository, version, sha, value, hashes)
    with tempfile.TemporaryDirectory(prefix="spiritboard-release-") as temporary:
        checksum = Path(temporary) / "SHA256SUMS"
        checksum.write_bytes(checksum_data)
        for name in sorted(hashes):
            if name not in existing:
                endpoint = (
                    f"https://uploads.github.com/repos/{repository}/releases/{value['id']}/assets"
                    f"?name={urllib.parse.quote(name, safe='')}"
                )
                asset = github_object(
                    endpoint, upload=checksum if name == checksum.name else directory / name
                )
                if asset is None:
                    raise ReleaseError("GitHub did not confirm the release asset upload")
                verify_asset(repository, asset, name, hashes[name])
    print(f"Paired PyPI {version} with https://github.com/{repository}/releases/tag/{tag} at {sha}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "pair"))
    parser.add_argument("--repository", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            preflight(args.repository, args.version, args.sha, args.dist)
            print("Existing tag, release, and assets verified; nothing was changed")
        else:
            pair_release(args.repository, args.version, args.sha, args.dist)
    except (ReleaseError, OSError, subprocess.TimeoutExpired) as exc:
        message = str(exc) if isinstance(exc, ReleaseError) else "Release operation failed"
        parser.exit(1, f"{message}\n")


if __name__ == "__main__":
    main()
