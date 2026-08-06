from __future__ import annotations

import hashlib
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class GitError(Exception):
    """Raised when a git operation fails."""


def _repo_key(url: str, username: str | None) -> str:
    """Build a filesystem-safe, stable directory name for a repo."""
    raw = f"{url}|{username or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _repo_dir(hass: HomeAssistant, url: str, username: str | None = None) -> Path:
    """Return the persistent local clone path for a repo URL."""
    base = Path(hass.config.path(".storage", DOMAIN, "repos"))
    return base / _repo_key(url, username)


def _authed_url(url: str, username: str | None, password: str | None) -> str:
    """Inject basic-auth credentials into a URL, if provided."""
    if not username and not password:
        return url

    parts = urlsplit(url)
    netloc = parts.netloc
    if username and password:
        netloc = f"{username}:{password}@{netloc}"
    elif username:
        netloc = f"{username}@{netloc}"

    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _scrub(url: str) -> str:
    """Strip credentials from a URL before it hits the logs."""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _run_git(args: list[str], cwd: Path | None = None) -> str:
    """Run a git command, raising GitError with scrubbed output on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as err:
        raise GitError(err.stderr.strip() or str(err)) from err
    except subprocess.TimeoutExpired as err:
        raise GitError(f"git command timed out: {' '.join(args)}") from err

    return result.stdout.strip()


def _is_git_repo(path: Path) -> bool:
    return (path / ".git").is_dir()


def _is_shallow_repo(path: Path) -> bool:
    return (path / ".git" / "shallow").is_file()


def _clone(auth_url: str, dest: Path, reference: str | None, shallow: bool) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["clone"]
    if shallow:
        args += ["--depth", "1"]
    if reference:
        args += ["--branch", reference]
    args += [auth_url, str(dest)]
    _run_git(args)


def _update(auth_url: str, dest: Path, reference: str | None, shallow: bool) -> None:
    _run_git(["remote", "set-url", "origin", auth_url], cwd=dest)

    if not shallow and _is_shallow_repo(dest):
        _run_git(["fetch", "--unshallow", "origin"], cwd=dest)

    ref = reference or "HEAD"

    if shallow:
        try:
            _run_git(["fetch", "--depth", "1", "origin", ref], cwd=dest)
            _run_git(["reset", "--hard", "FETCH_HEAD"], cwd=dest)
        except GitError:
            _LOGGER.debug("Shallow fetch of %s failed, retrying unshallowed", ref)
            _run_git(["fetch", "--unshallow", "origin"], cwd=dest)
            target = reference or "origin/HEAD"
            _run_git(["reset", "--hard", target], cwd=dest)
    else:
        _run_git(["fetch", "origin", ref], cwd=dest)
        _run_git(["reset", "--hard", "FETCH_HEAD"], cwd=dest)


def _sync(url: str, dest: Path, reference: str | None, username: str | None, password: str | None, shallow: bool) -> None:
    auth_url = _authed_url(url, username, password)

    try:
        if _is_git_repo(dest):
            if shallow and not _is_shallow_repo(dest):
                import shutil

                shutil.rmtree(dest)
                _clone(auth_url, dest, reference, shallow)
            else:
                _update(auth_url, dest, reference, shallow)
        else:
            if dest.exists():
                import shutil

                shutil.rmtree(dest)
            _clone(auth_url, dest, reference, shallow)
    except GitError:
        _LOGGER.exception("Git operation failed for %s", _scrub(url))
        raise


async def sync_repo(
    hass: HomeAssistant,
    url: str,
    reference: str | None = None,
    username: str | None = None,
    password: str | None = None,
    shallow: bool = True,
) -> Path:
    """Clone a repo if it doesn't exist locally yet, otherwise update it in place."""
    dest = _repo_dir(hass, url, username)
    _LOGGER.debug("Syncing %s -> %s (shallow=%s)", _scrub(url), dest, shallow)

    await hass.async_add_executor_job(_sync, url, dest, reference, username, password, shallow)
    return dest


def _last_commit_time(repo_path: Path, path: str | None) -> datetime | None:
    args = ["log", "-1", "--format=%ct"]
    if path:
        args += ["--", path]

    try:
        output = _run_git(args, cwd=repo_path)
    except GitError:
        _LOGGER.exception("Failed to read commit time for %s (path=%s)", repo_path, path)
        return None

    if not output:
        return None

    return datetime.fromtimestamp(int(output), tz=timezone.utc)


async def get_last_commit_time(
    hass: HomeAssistant,
    repo_path: Path,
    path: str | None = None,
) -> datetime | None:
    """Get the last commit time for a repo, or for a single path within it."""
    return await hass.async_add_executor_job(_last_commit_time, repo_path, path)


def _last_commit_hash(repo_path: Path, path: str | None) -> str | None:
    args = ["log", "-1", "--format=%H"]
    if path:
        args += ["--", path]

    try:
        output = _run_git(args, cwd=repo_path)
    except GitError:
        _LOGGER.exception("Failed to read commit hash for %s (path=%s)", repo_path, path)
        return None

    return output or None


async def get_last_commit_hash(
    hass: HomeAssistant,
    repo_path: Path,
    path: str | None = None,
) -> str | None:
    """Get the last commit hash for a repo, or for a single path within it."""
    return await hass.async_add_executor_job(_last_commit_hash, repo_path, path)