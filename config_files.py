"""Publish a dated current file atomically, optionally retaining older versions."""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import re
import stat
import tempfile

__all__ = ["archive_enabled", "publish_file"]


def archive_enabled(value="0"):
    """Accept the explicit archive toggle; omitted settings default to disabled."""
    if isinstance(value, bool):
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip() in {"0", "1"}:
        return value.strip() == "1"
    raise ValueError("LITELLM_FREE_ARCHIVE must be 0 or 1")


def _sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _version_file(path, content, directory):
    while True:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = directory / (path.stem + "_" + stamp + path.suffix)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            continue
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        _sync_directory(directory)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def _managed_target(path):
    """Only our relative, dated regular files beside this link are disposable."""
    if not path.is_symlink():
        return None
    relative = Path(os.readlink(path))
    pattern = re.escape(path.stem) + r"_\d{8}T\d{12}Z" + re.escape(path.suffix)
    if relative.parent != Path(".") or not re.fullmatch(pattern, relative.name):
        return None
    target = path.parent / relative
    try:
        return target if stat.S_ISREG(target.lstat().st_mode) else None
    except FileNotFoundError:
        return None


def _retain_previous(path, managed):
    directory = path.parent / "archiv"
    if directory.is_symlink():
        raise ValueError("Archive directory must not be a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if managed:
        retained = directory / managed.name
        try:
            os.link(managed, retained, follow_symlinks=False)
        except FileExistsError:
            # Preserve an existing archive entry even if its name happens to collide.
            return _version_file(path, managed.read_bytes(), directory)
        try:
            _sync_directory(directory)
        except BaseException:
            retained.unlink(missing_ok=True)
            raise
        return retained
    return _version_file(path, path.read_bytes(), directory)


def publish_file(path, text, force=False, archive=False):
    """Publish a relative latest symlink; retain older owned files only on request."""
    path = Path(path)
    archive = archive_enabled(archive)
    if not isinstance(text, str):
        raise TypeError("Published content must be text")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(path.parent / ("." + path.name + ".publish.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        exists = path.exists() or path.is_symlink()
        if exists and not force:
            raise ValueError("Output exists; choose another file or use --force")
        if path.is_dir():
            raise ValueError("Output path is a directory")
        content = text.encode("utf-8")
        managed = _managed_target(path)
        if managed and managed.read_bytes() == content:
            return managed
        if path.exists() and not path.is_symlink() and not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("Output path must be a regular file or symlink")
        retained = target = None
        switched = False
        try:
            if archive and (managed or (path.exists() and not path.is_symlink())):
                retained = _retain_previous(path, managed)
            target = _version_file(path, content, path.parent)
            with tempfile.TemporaryDirectory(prefix=".publish-", dir=path.parent) as temporary:
                link = Path(temporary) / "current"
                link.symlink_to(target.name)
                os.replace(link, path)
                switched = True
            _sync_directory(path.parent)
        except BaseException:
            if not switched:
                for created in (target, retained):
                    if created is not None:
                        created.unlink(missing_ok=True)
            raise
        if managed:
            managed.unlink()
            _sync_directory(path.parent)
        return target
