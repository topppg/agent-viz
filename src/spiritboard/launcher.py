"""Private local state and one dashboard process per data directory."""

from __future__ import annotations

import errno
import json
import os
import socket
import stat
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

_STARTUP_WAIT = 5.0
_REPLACE_ATTEMPTS = 20
_POLL_INTERVAL = 0.1
_PROBE_TIMEOUT = 0.5
_MAX_METADATA = 8192


class LaunchError(Exception):
    """A launch failure whose message is safe to show without local details."""


def running_url(directory: Path) -> str:
    """URL of the verified dashboard that owns ``directory``; never creates the directory."""
    if not (Path(directory) / "instance.lock").is_file():
        raise LaunchError("No dashboard is running with this data directory. Start one first.")
    with Instance(directory, {}) as instance:
        if instance.owner:
            raise LaunchError("No dashboard is running with this data directory. Start one first.")
        return instance.existing_url(match_settings=False)


def default_data_dir(*, demo: bool = False) -> Path:
    """Return the platform's user-data directory, independent of the working directory."""
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        configured = os.environ.get("LOCALAPPDATA", "")
        root = Path(configured) if configured else Path.home() / "AppData" / "Local"
        if not root.is_absolute():
            root = Path.home() / "AppData" / "Local"
    else:
        configured = os.environ.get("XDG_DATA_HOME", "")
        root = Path(configured) if configured else Path.home() / ".local" / "share"
        if not root.is_absolute():
            root = Path.home() / ".local" / "share"
    result = root / "spiritboard"
    return result / "demo" if demo else result


def _private_directory(directory: Path) -> None:
    missing: list[Path] = []
    parent = directory
    while True:
        try:
            parent.lstat()
            break
        except FileNotFoundError:
            missing.append(parent)
            parent = parent.parent
    # pathlib's parents=True ignores mode for parents, so create every missing level ourselves.
    for path in reversed(missing):
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    actual = directory.lstat()
    if not stat.S_ISDIR(actual.st_mode):
        raise LaunchError("The dashboard data directory must not be a symbolic link.")
    if os.name == "posix" and actual.st_mode & 0o077:
        raise LaunchError(
            "Dashboard data needs a private directory with owner-only permissions. "
            "Choose another --data-dir or secure the existing directory first."
        )


def _regular_descriptor(path: Path, *, create: bool = False) -> int:
    flags = os.O_RDWR | os.O_CREAT if create else os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        before = path.lstat() if path.exists() or path.is_symlink() else None
        if before is not None and not stat.S_ISREG(before.st_mode):
            raise LaunchError("Dashboard state must use regular files, not links or special files.")
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise LaunchError("Dashboard state could not be opened safely.") from None
    try:
        opened = os.fstat(descriptor)
        after = path.lstat()
    except OSError:
        os.close(descriptor)
        raise LaunchError("Dashboard state could not be opened safely.") from None
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or opened.st_nlink != 1
        or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
        or (before is not None and (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino))
        or (os.name == "posix" and opened.st_mode & 0o077)
    ):
        os.close(descriptor)
        raise LaunchError("Dashboard state must be private, regular files.")
    return descriptor


def _lock(descriptor: int, *, release: bool = False) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            operation = msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK
            msvcrt.locking(descriptor, operation, 1)
        else:
            import fcntl

            operation = fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(descriptor, operation)
    except OSError as error:
        if not release and error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return False
        raise LaunchError("The dashboard instance lock could not be used safely.") from None
    return True


def _validated_url(value: object) -> tuple[str, int]:
    if not isinstance(value, str):
        raise LaunchError("The running dashboard has invalid connection details.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise LaunchError("The running dashboard has invalid connection details.") from None
    if (
        parsed.scheme != "http"
        or port is None
        or not 1 <= port <= 65535
        or parsed.netloc != f"127.0.0.1:{port}"
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise LaunchError("The running dashboard must use a local loopback address.")
    return f"http://127.0.0.1:{port}", port


def _health_matches(port: int, instance_id: str, timeout: float) -> bool:
    # Read a bounded HTTP response with one absolute deadline, even if a peer trickles bytes.
    deadline = time.monotonic() + timeout
    received = bytearray()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as connection:
            connection.sendall(
                f"GET /api/health HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n"
                "Connection: close\r\n\r\n".encode("ascii")
            )
            while len(received) <= _MAX_METADATA:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                connection.settimeout(remaining)
                chunk = connection.recv(min(4096, _MAX_METADATA + 1 - len(received)))
                if not chunk:
                    break
                received.extend(chunk)
        header, separator, body = bytes(received).partition(b"\r\n\r\n")
        status = header.split(b"\r\n", 1)[0].split(b" ", 2)
        if (
            len(received) > _MAX_METADATA
            or not separator
            or len(status) < 2
            or status[0] not in (b"HTTP/1.0", b"HTTP/1.1")
            or status[1] != b"200"
        ):
            return False
        health = json.loads(body)
        return (
            isinstance(health, dict)
            and health.get("ok") is True
            and health.get("service") == "spiritboard"
            and health.get("instance_id") == instance_id
        )
    except (OSError, ValueError, UnicodeError):
        return False


class Instance:
    """Hold a kernel lock, or locate the verified process that already holds it."""

    def __init__(self, directory: Path, settings: dict[str, object]):
        self.directory = Path(directory)
        try:
            self._settings = json.dumps(settings, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError):
            raise LaunchError("Dashboard options cannot be stored safely.") from None
        self.owner = False
        self._descriptor: int | None = None
        self._instance_id: str | None = None

    def __enter__(self) -> Instance:
        if self._descriptor is not None:
            raise LaunchError("The dashboard instance lock is already in use.")
        self._instance_id = None
        try:
            _private_directory(self.directory)
            self._descriptor = _regular_descriptor(self.directory / "instance.lock", create=True)
            self.owner = _lock(self._descriptor)
            return self
        except OSError:
            self.__exit__(None, None, None)
            raise LaunchError("The dashboard data directory could not be opened safely.") from None
        except LaunchError:
            self.__exit__(None, None, None)
            raise

    def _metadata(self) -> dict[str, object]:
        descriptor = _regular_descriptor(self.directory / "instance.json")
        try:
            raw = os.read(descriptor, _MAX_METADATA + 1)
        finally:
            os.close(descriptor)
        if len(raw) > _MAX_METADATA:
            raise LaunchError("The running dashboard has invalid connection details.")
        try:
            metadata = json.loads(raw)
            if not isinstance(metadata, dict) or metadata.get("version") != 1:
                raise ValueError
            if metadata.get("service") != "spiritboard":
                raise ValueError
            instance_id = metadata.get("instance_id")
            if not isinstance(instance_id, str):
                raise ValueError
            uuid.UUID(instance_id)
            return metadata
        except (ValueError, TypeError, UnicodeError):
            raise LaunchError("The running dashboard has invalid connection details.") from None

    def publish(self, url: str, instance_id: str) -> None:
        """Publish connection details only after the local server has bound its port."""
        if not self.owner or self._descriptor is None:
            raise LaunchError("Only the dashboard lock owner may publish connection details.")
        url, _ = _validated_url(url)
        try:
            uuid.UUID(instance_id)
            payload = json.dumps(
                {
                    "version": 1,
                    "service": "spiritboard",
                    "url": url,
                    "instance_id": instance_id,
                    "settings": json.loads(self._settings),
                },
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
            if len(payload) > _MAX_METADATA:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise LaunchError("Dashboard connection details cannot be stored safely.") from None
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".instance-", dir=self.directory)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(_REPLACE_ATTEMPTS):
                try:
                    os.replace(temporary, self.directory / "instance.json")
                    break
                except PermissionError:
                    if attempt == _REPLACE_ATTEMPTS - 1:
                        raise
                    time.sleep(_POLL_INTERVAL)
            self._instance_id = instance_id
        except OSError:
            raise LaunchError("Dashboard connection details could not be stored safely.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def existing_url(self, *, match_settings: bool = True, port: int | None = None) -> str:
        """Return an existing dashboard only after matching its private instance identity."""
        if self._descriptor is None or self.owner:
            raise LaunchError("There is no other dashboard instance to reuse.")
        deadline = time.monotonic() + _STARTUP_WAIT
        while time.monotonic() < deadline:
            try:
                metadata = self._metadata()
                url, instance_port = _validated_url(metadata.get("url"))
                instance_id = str(metadata["instance_id"])
                settings = json.dumps(metadata.get("settings"), sort_keys=True, allow_nan=False)
                timeout = min(_PROBE_TIMEOUT, max(0.001, deadline - time.monotonic()))
                verified = _health_matches(instance_port, instance_id, timeout)
            except (LaunchError, OSError, ValueError, TypeError):
                verified = False
            if verified:
                if port is not None and port != instance_port:
                    raise LaunchError(
                        f"This data directory's dashboard runs on port {instance_port}, "
                        f"not {port}. Use that port, stop it first, or choose another --data-dir."
                    )
                if match_settings and settings != self._settings:
                    raise LaunchError(
                        "A dashboard already uses this data directory with different options. "
                        "Use its existing options, stop it first, or choose another --data-dir."
                    )
                return url
            time.sleep(min(_POLL_INTERVAL, max(0, deadline - time.monotonic())))
        raise LaunchError(
            "Another dashboard owns this data directory but is not ready or could not be verified. "
            "Try again shortly, or choose another --data-dir."
        )

    def __exit__(self, *_exc: object) -> None:
        if self._descriptor is None:
            return
        try:
            if self.owner and self._instance_id is not None:
                try:
                    if self._metadata().get("instance_id") == self._instance_id:
                        (self.directory / "instance.json").unlink()
                except (LaunchError, OSError):
                    pass
            if self.owner:
                _lock(self._descriptor, release=True)
        finally:
            os.close(self._descriptor)
            self._descriptor = None
            self.owner = False
