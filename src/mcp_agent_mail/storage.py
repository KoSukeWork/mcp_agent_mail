"""Managed mailbox attachments, projections, locks, and local notifications."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import importlib
import json
import logging
import os
import random
import re
import sys
import threading as _threading
import time
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Sequence, TypeVar

from filelock import SoftFileLock, Timeout
from PIL import Image

from .config import Settings

_logger = logging.getLogger(__name__)
_IMAGE_PATTERN = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<path>[^)]+)\)")


@dataclass(slots=True)
class MailboxStorage:
    settings: Settings
    slug: str
    # Project-specific managed storage root
    root: Path
    # Path used for advisory file lock during archive writes
    lock_path: Path
    # Configured storage root used to resolve relative attachment paths
    repo_root: Path

    @property
    def attachments_dir(self) -> Path:
        return self.root / "attachments"

_PROCESS_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}
_PROCESS_LOCK_OWNERS: dict[tuple[int, str], int] = {}

# ---------------------------------------------------------------------------
# Lock-FD telemetry: track active AsyncFileLock instances for leak detection
# ---------------------------------------------------------------------------
# WeakValueDictionary (NOT a plain dict): the telemetry registry must not keep
# instances alive. A plain `dict[int, AsyncFileLock]` strong-refs every lock, so
# each refcount stays >= 1 and the `__del__` that pops the entry can never fire —
# entries accumulate unboundedly (observed 560 active_locks at idle, #244). With
# weak values the GC reclaims released locks normally and the entry drops
# automatically; `__del__`'s `pop(id(self), None)` becomes a harmless no-op.
# (CPython fires weakref callbacks synchronously on dealloc, before an id can be
# reused, so the id-keyed mapping has no stale-entry race.)
_ACTIVE_LOCK_INSTANCES: "weakref.WeakValueDictionary[int, AsyncFileLock]" = (
    weakref.WeakValueDictionary()
)  # id(lock) -> lock
_LOCK_INSTANCES_GUARD = _threading.Lock()
_LOCK_FD_LEAKED_TOTAL: int = 0  # monotonic counter of detected leaks


def get_lock_telemetry() -> dict[str, int]:
    """Return current lock-FD telemetry for monitoring."""
    with _LOCK_INSTANCES_GUARD:
        return {
            "active_locks": len(_ACTIVE_LOCK_INSTANCES),
            "leaked_total": _LOCK_FD_LEAKED_TOTAL,
        }


def get_fd_usage() -> tuple[int, int]:
    """Return open/maximum file-descriptor counts when the platform exposes them."""
    try:
        resource_module = importlib.import_module("resource")
        getrlimit = getattr(resource_module, "getrlimit", None)
        rlimit_nofile = getattr(resource_module, "RLIMIT_NOFILE", None)
        if not callable(getrlimit) or not isinstance(rlimit_nofile, int):
            return (-1, -1)
        soft_limit, _hard_limit = getrlimit(rlimit_nofile)
        fd_dir = Path("/dev/fd") if sys.platform == "darwin" else Path("/proc/self/fd")
        if fd_dir.exists():
            return (len(list(fd_dir.iterdir())), soft_limit)
        return (-1, soft_limit)
    except (ImportError, OSError, AttributeError):
        return (-1, -1)


# macOS ``fcntl`` command for resolving an open fd to its path. Entries under
# ``/dev/fd`` on Darwin are ``fdesc`` character devices (NOT symlinks), so
# ``os.readlink`` raises EINVAL there; ``fcntl(fd, F_GETPATH, buf)`` is the
# portable lookup. Python's ``fcntl`` module doesn't always expose F_GETPATH
# symbolically, so fall back to the stable value from ``<sys/fcntl.h>`` (50).
# MAXPATHLEN on macOS is 1024; lockfile paths inside the mail archive are far
# shorter, so a 1024-byte buffer never truncates a real lock path.
_DARWIN_MAXPATHLEN = 1024


def _resolve_fd_path(fd_num: int, proc_entry: Path) -> str | None:
    """Resolve an open file descriptor to its filesystem path.

    On macOS uses ``fcntl(F_GETPATH)`` (``/dev/fd`` entries are char devices,
    not symlinks); on Linux reads the ``/proc/self/fd/N`` symlink. Returns
    ``None`` when the lookup fails (fd closed concurrently, not path-backed,
    etc.).
    """
    try:
        if sys.platform == "darwin":
            import fcntl

            f_getpath = getattr(fcntl, "F_GETPATH", 50)
            raw = fcntl.fcntl(fd_num, f_getpath, b"\x00" * _DARWIN_MAXPATHLEN)
            return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        return str(proc_entry.readlink())
    except OSError:
        return None


def cleanup_leaked_lockfile_fds() -> int:
    """Scan the process's open FDs for ones pointing at deleted ``.lock`` files and close them.

    Returns the number of leaked FDs that were closed.

    Cross-platform: walks ``/dev/fd`` on macOS (fdesc fs — entries are
    character devices, not symlinks, so paths are resolved via
    ``fcntl(F_GETPATH)``) and ``/proc/self/fd`` on Linux. Symmetric with
    ``get_fd_usage`` above. The "deleted file" signal is
    ``os.fstat(fd).st_nlink == 0`` on both platforms — a still-open fd whose
    underlying inode has zero remaining hard links. That subsumes the
    Linux-only ``" (deleted)"`` readlink suffix the previous implementation
    relied on, and is the only signal that works on macOS at all (where the
    function was previously a silent no-op because it only checked
    ``/proc/self/fd``).

    Without this dispatch, the ``AsyncFileLock`` lockfile-fd leak (issue #116)
    accumulates unbounded on macOS until the process hits ``RLIMIT_NOFILE`` and
    every subsequent ``send_message``/``reply_message`` fails with EMFILE.
    """
    fd_dir = Path("/dev/fd") if sys.platform == "darwin" else Path("/proc/self/fd")
    if not fd_dir.exists():
        return 0
    closed = 0
    try:
        for entry in fd_dir.iterdir():
            try:
                fd_num = int(entry.name)
            except ValueError:
                continue
            path = _resolve_fd_path(fd_num, entry)
            # Cheap name filter before the fstat syscall: only ever reap fds
            # that look like our advisory lock files.  Anchor on the basename
            # so that unrelated paths containing ".lock" as a substring (e.g.
            # ``/tmp/.lockfile-fooXYZ``, ``/var/log/log.locked-archive``) are
            # never matched.  All AsyncFileLock paths this project creates end
            # with ``.lock`` (e.g. ``.archive.lock``, ``.commit.lock``,
            # ``<thread>.md.lock``).  The ``".lock." in base`` branch is
            # currently dead code — no production path uses that naming — but
            # is kept as a defensive guard against future ``.lock.<suffix>``
            # patterns (e.g. a hypothetical ``.archive.lock.bak`` backup file)
            # that might be introduced later without updating this filter.
            # On Linux, /proc/self/fd symlinks for deleted inodes carry a
            # " (deleted)" suffix — strip it before basename extraction so the
            # ".lock" ending is visible.
            if not path:
                continue
            clean_path = path.removesuffix(" (deleted)")
            base = Path(clean_path).name
            if not (base.endswith(".lock") or ".lock." in base):
                continue
            # Cross-platform "deleted" signal: a still-open fd whose inode has
            # zero remaining hard links. A live, on-disk lockfile has
            # st_nlink >= 1 and is left untouched.
            try:
                if os.fstat(fd_num).st_nlink != 0:
                    continue
            except OSError:
                continue
            try:
                os.close(fd_num)
                closed += 1
                _logger.warning(
                    "lockfile_fd.leaked_closed",
                    extra={"fd": fd_num, "target": path},
                )
            except OSError:
                pass
    except OSError:
        pass
    if closed:
        global _LOCK_FD_LEAKED_TOTAL
        with _LOCK_INSTANCES_GUARD:
            _LOCK_FD_LEAKED_TOTAL += closed
    return closed


class AsyncFileLock:
    """Async-friendly wrapper around SoftFileLock with metadata tracking and adaptive retries.

    Features:
    - Metadata tracking (.owner.json) enables stale lock detection
    - Process-level asyncio.Lock prevents re-entrant acquisition
    - Adaptive retry with exponential backoff on acquisition failure
    - Stale lock cleanup when owner process is dead or lock is too old

    Adaptive Timeout Strategy:
    - Initial attempt uses short timeout (10% of total)
    - Failed attempts trigger stale lock cleanup check
    - Subsequent attempts use progressively longer timeouts
    - This allows fast acquisition when lock is free while still handling
      edge cases like stale locks or slow I/O
    """

    def __init__(
        self,
        path: Path,
        *,
        timeout_seconds: float = 60.0,
        stale_timeout_seconds: float = 180.0,
        max_retries: int = 5,
    ) -> None:
        self._path = Path(path)
        # thread_local=False is REQUIRED for correctness. We drive acquire()
        # and release() through ``asyncio.to_thread`` (the default executor),
        # so a single logical lock lifetime is spread across *different* worker
        # threads. With filelock's default ``thread_local=True`` the underlying
        # fd + lock_counter live in a per-thread context: an acquire on worker
        # thread A and a release on worker thread B then disagree — thread B
        # sees ``is_locked == False`` and ``release()`` becomes a silent no-op,
        # leaving the fd open and the ``.lock``/``.commit.lock`` file on disk
        # forever. The next writer blocks on a lock held by the still-alive
        # server process and hangs indefinitely (issue #166; also feeds the
        # leaked-fd accumulation tracked by #116). A shared (process-wide)
        # context makes cross-thread acquire/release consistent and portable.
        self._lock = SoftFileLock(str(self._path), thread_local=False)
        self._timeout = float(timeout_seconds)
        self._stale_timeout = float(max(stale_timeout_seconds, 0.0))
        self._max_retries = max_retries
        self._pid = os.getpid()
        self._metadata_path = self._path.parent / f"{self._path.name}.owner.json"
        self._held = False
        self._lock_key = str(self._path.resolve())
        self._acquisition_start: float | None = None
        self._acquisition_attempts: int = 0
        self._loop_key: tuple[int, str] | None = None
        self._process_lock: asyncio.Lock | None = None
        self._process_lock_held = False
        # Register for telemetry
        with _LOCK_INSTANCES_GUARD:
            _ACTIVE_LOCK_INSTANCES[id(self)] = self

    def __del__(self) -> None:
        lock_instances_guard = globals().get("_LOCK_INSTANCES_GUARD")
        active_lock_instances = globals().get("_ACTIVE_LOCK_INSTANCES")
        if lock_instances_guard is None or active_lock_instances is None:
            return
        try:
            with lock_instances_guard:
                active_lock_instances.pop(id(self), None)
        except Exception:
            return

    def _force_close_fd(self) -> bool:
        """Force-close the underlying SoftFileLock file descriptor if still open.

        This is the last-resort safety net: if ``release()`` raised or was
        interrupted, the FD may still be open and pointing at a (possibly
        deleted) lock file.  We reach into the SoftFileLock internals to
        close it, preventing FD exhaustion.

        Returns True if an FD was actually closed, False otherwise.
        """
        fd = getattr(self._lock, "_context", None)
        if fd is None:
            return False
        lock_fd = getattr(fd, "lock_file_fd", None)
        if lock_fd is None:
            return False
        try:
            os.close(lock_fd)
            fd.lock_file_fd = None
            fd.lock_counter = 0
            _logger.warning(
                "lockfile_fd.force_closed",
                extra={"path": str(self._path), "fd": lock_fd},
            )
            global _LOCK_FD_LEAKED_TOTAL
            with _LOCK_INSTANCES_GUARD:
                _LOCK_FD_LEAKED_TOTAL += 1
            return True
        except OSError:
            # FD was already closed (e.g., by a concurrent cleanup)
            fd.lock_file_fd = None
            fd.lock_counter = 0
            return False

    def _release_strict(self) -> bool:
        """Release the lock, ensuring the FD is closed even on failure.

        Returns True if the lock was successfully released (FD closed).
        If ``release()`` raises, falls back to ``_force_close_fd()``.
        """
        try:
            self._lock.release()
            return True
        except Exception as exc:
            _logger.error(
                "lockfile_fd.release_failed",
                extra={"path": str(self._path), "error": str(exc)},
            )
            # Fallback: force-close the FD to prevent leak
            self._force_close_fd()
            return False

    async def __aenter__(self) -> None:
        """Acquire the file lock with adaptive retry and stale lock detection.

        Adaptive Retry Strategy:
        1. First attempt: Short timeout (10% of total) - fast path for uncontested locks
        2. On timeout: Check for stale locks and clean up if found
        3. Subsequent attempts: Exponential backoff with longer per-attempt timeouts
        4. Final attempt: Full remaining timeout

        This strategy optimizes for:
        - Fast acquisition when lock is free (common case)
        - Graceful handling of stale locks from crashed processes
        - Avoiding thundering herd with jittered backoff
        """
        self._acquisition_start = time.monotonic()
        self._acquisition_attempts = 0

        loop = asyncio.get_running_loop()
        self._loop_key = (id(loop), self._lock_key)
        process_lock = _PROCESS_LOCKS.get(self._loop_key)
        if process_lock is None:
            process_lock = asyncio.Lock()
            _PROCESS_LOCKS[self._loop_key] = process_lock
        current_task = asyncio.current_task()
        owner_id = _PROCESS_LOCK_OWNERS.get(self._loop_key)
        current_task_id = id(current_task) if current_task else id(self)
        if owner_id == current_task_id:
            raise RuntimeError(f"Re-entrant AsyncFileLock acquisition detected for {self._path}")
        self._process_lock = process_lock
        await self._process_lock.acquire()
        self._process_lock_held = True
        _PROCESS_LOCK_OWNERS[self._loop_key] = current_task_id
        try:
            total_timeout = self._timeout if self._timeout > 0 else 60.0
            remaining = total_timeout

            for attempt in range(self._max_retries + 1):
                self._acquisition_attempts = attempt + 1

                # Adaptive timeout per attempt:
                # - First attempt: 10% of total (fast path)
                # - Middle attempts: progressively longer
                # - Last attempt: all remaining time
                if attempt == 0:
                    per_attempt_timeout = min(total_timeout * 0.1, 5.0)  # 10%, max 5s
                elif attempt == self._max_retries:
                    per_attempt_timeout = remaining  # Use all remaining
                else:
                    # Exponential growth: 0.5s, 1s, 2s, 4s, ...
                    per_attempt_timeout = min(0.5 * (2 ** attempt), remaining)

                try:
                    if self._timeout <= 0:
                        await _to_thread(self._lock.acquire)
                    else:
                        await _to_thread(self._lock.acquire, per_attempt_timeout)
                    self._held = True
                    await _to_thread(self._write_metadata)

                    # Log successful acquisition if it took retries
                    if attempt > 0:
                        elapsed = time.monotonic() - self._acquisition_start
                        _logger.info(
                            "file_lock.acquired_after_retry",
                            extra={
                                "path": str(self._path),
                                "attempts": attempt + 1,
                                "elapsed_seconds": round(elapsed, 2),
                            },
                        )
                    return None

                except Timeout:
                    elapsed = time.monotonic() - self._acquisition_start
                    remaining = total_timeout - elapsed

                    if remaining <= 0 or attempt >= self._max_retries:
                        # Final attempt failed - try one last stale cleanup
                        cleaned = await _to_thread(self._cleanup_if_stale)
                        if cleaned:
                            # Stale lock was cleaned - try once more with short timeout
                            try:
                                await _to_thread(self._lock.acquire, 1.0)
                                self._held = True
                                await _to_thread(self._write_metadata)
                                _logger.info(
                                    "file_lock.acquired_after_stale_cleanup",
                                    extra={"path": str(self._path)},
                                )
                                return None
                            except Timeout:
                                pass  # Fall through to timeout error
                        raise TimeoutError(
                            f"Timed out acquiring lock {self._path} after {elapsed:.2f}s "
                            f"({attempt + 1} attempts). No stale owner detected."
                        ) from None

                    # Check for stale lock before retrying
                    cleaned = await _to_thread(self._cleanup_if_stale)
                    if cleaned:
                        _logger.info(
                            "file_lock.stale_cleaned",
                            extra={"path": str(self._path), "attempt": attempt + 1},
                        )
                        # Don't add backoff delay - immediately retry after cleanup
                        continue

                    # Add jittered backoff before retry (0.05s to 0.5s)
                    backoff = min(0.05 * (2 ** attempt), 0.5)
                    jitter = backoff * 0.25 * (2 * random.random() - 1)
                    await asyncio.sleep(backoff + jitter)

        except BaseException:
            # Best-effort cleanup on any failure (including cancellation) to avoid leaking
            # lock file handles and process-level locks.
            if self._held:
                release_ok = False
                task = asyncio.create_task(_to_thread(self._release_strict))
                try:
                    release_ok = await asyncio.shield(task)
                except BaseException:
                    with contextlib.suppress(Exception):
                        release_ok = await task
                if not release_ok:
                    # release_strict already force-closed the FD; force-close
                    # again as a safety net (idempotent)
                    await _to_thread(self._force_close_fd)
                self._held = False
                # Only unlink files if we confirmed the FD is closed (release
                # succeeded or was force-closed).  This prevents unlinking a
                # lock path while another process may have legitimately
                # acquired it in between.
                for cleanup_coro in (
                    _to_thread(self._metadata_path.unlink, missing_ok=True),
                    _to_thread(self._path.unlink, missing_ok=True),
                ):
                    task = asyncio.create_task(cleanup_coro)
                    try:
                        await asyncio.shield(task)
                    except BaseException:
                        with contextlib.suppress(Exception):
                            await task

            if self._loop_key is not None:
                _PROCESS_LOCK_OWNERS.pop(self._loop_key, None)
            if self._process_lock_held and self._process_lock:
                self._process_lock.release()
                self._process_lock_held = False
            if (
                self._loop_key is not None
                and self._process_lock
                and not self._process_lock.locked()
            ):
                _PROCESS_LOCKS.pop(self._loop_key, None)
            self._process_lock = None
            raise

    def _cleanup_if_stale(self) -> bool:
        """Remove lock and metadata when the lock is stale.

        A lock is considered stale if EITHER:
        1. Owner metadata proves the owning process no longer exists, OR
        2. The lock age exceeds the stale timeout

        Missing owner metadata by itself is not enough to declare the lock stale,
        because there is a small window between acquiring the lock file and
        writing the sidecar metadata.
        """
        if not self._path.exists():
            return False
        now = time.time()
        metadata: dict[str, Any] = {}
        if self._metadata_path.exists():
            try:
                metadata = json.loads(self._metadata_path.read_text(encoding="utf-8"))
            except Exception:
                metadata = {}
        pid_val = metadata.get("pid")
        pid_int: int | None = None
        if pid_val is not None:
            with contextlib.suppress(Exception):
                pid_int = int(pid_val)
        owner_alive: bool | None = None
        if pid_int is not None:
            owner_alive = self._pid_alive(pid_int)
        created_ts = metadata.get("created_ts")
        age = None
        if isinstance(created_ts, (int, float)):
            age = now - float(created_ts)
        else:
            with contextlib.suppress(Exception):
                age = now - self._path.stat().st_mtime

        # Lock is stale if owner metadata proves the owner is gone OR if the
        # lock file itself has aged beyond the configured stale timeout.
        is_stale = False
        if owner_alive is False or (self._stale_timeout > 0 and isinstance(age, (int, float)) and age >= self._stale_timeout):
            is_stale = True

        if not is_stale:
            return False

        # Clean up stale lock
        with contextlib.suppress(Exception):
            self._path.unlink(missing_ok=True)
        with contextlib.suppress(Exception):
            self._metadata_path.unlink(missing_ok=True)
        return True

    def _write_metadata(self) -> None:
        payload = {
            "pid": self._pid,
            "created_ts": time.time(),
        }
        self._metadata_path.write_text(json.dumps(payload), encoding="utf-8")
        return None

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object) -> None:
        if self._held:
            # Step 1: Release the lock (closes FD + unlinks lock file internally).
            # Use _release_strict so that if release() raises, the FD is still
            # force-closed, preventing the leaked-FD exhaustion bug (#116).
            release_ok = False
            release_task = asyncio.create_task(_to_thread(self._release_strict))
            try:
                release_ok = await asyncio.shield(release_task)
            except BaseException:
                with contextlib.suppress(Exception):
                    release_ok = await release_task
            if not release_ok:
                # Last resort: ensure FD is closed even if everything above failed
                await _to_thread(self._force_close_fd)

            # Step 2: Windows needs a short delay after close before unlink
            if sys.platform == "win32":
                await asyncio.sleep(0.01)

            # Step 3: Clean up metadata file.  The lock file itself is already
            # unlinked by SoftFileLock._release(); we only need to remove the
            # metadata sidecar.  Redundant unlink of self._path is safe (missing_ok).
            for cleanup_path in (self._metadata_path, self._path):
                task = asyncio.create_task(
                    _to_thread(cleanup_path.unlink, missing_ok=True)
                )
                try:
                    await asyncio.shield(task)
                except BaseException:
                    with contextlib.suppress(Exception):
                        await task
            self._held = False

        # Clean up process-level locks
        if self._loop_key is not None:
            _PROCESS_LOCK_OWNERS.pop(self._loop_key, None)
        if self._process_lock_held and self._process_lock:
            self._process_lock.release()
            self._process_lock_held = False
        if (
            self._loop_key is not None
            and self._process_lock
            and not self._process_lock.locked()
        ):
            _PROCESS_LOCKS.pop(self._loop_key, None)
        self._process_lock = None
        self._loop_key = None
        return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Check if a process with the given PID is alive (cross-platform)."""
        if pid <= 0:
            return False

        # Try psutil first if available (most reliable cross-platform method)
        try:
            import psutil
            return bool(psutil.pid_exists(pid))
        except ImportError:
            pass

        # Platform-specific fallbacks
        if sys.platform == 'win32':
            # Windows: Use ctypes to call OpenProcess
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                SYNCHRONIZE = 0x00100000
                # Try to open the process with minimal permissions
                handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
                if handle:
                    kernel32.CloseHandle(handle)
                    return True
                return False
            except Exception:
                # If ctypes fails, assume process doesn't exist
                return False
        else:
            # Unix/Linux/macOS: Use os.kill(pid, 0)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                # Process exists but we don't have permission to signal it
                return True
            except OSError:
                return False
            return True


@asynccontextmanager
async def archive_write_lock(archive: MailboxStorage, *, timeout_seconds: float = 60.0) -> AsyncIterator[None]:
    """Context manager for safely mutating archive surfaces.

    The lock is released in a ``finally`` no matter how the body terminates —
    normal return, exception, or task cancellation/timeout. Acquisition is kept
    inside the same ``try`` so that a cancellation delivered at the await
    boundary *after* ``__aenter__`` returns (but before the body runs) still
    routes through release; ``__aexit__`` is a no-op when nothing was acquired
    (``_held`` stays False and no process lock is held), so the unconditional
    finally is safe. This guarantees ``.archive.lock`` is never left wedged on
    disk after an interrupted write (issue #166).
    """
    lock = AsyncFileLock(archive.lock_path, timeout_seconds=timeout_seconds)
    exc_type: type[BaseException] | None = None
    exc: BaseException | None = None
    tb: object | None = None
    try:
        await lock.__aenter__()
        yield
    except BaseException as raised:
        exc_type = type(raised)
        exc = raised
        tb = raised.__traceback__
        raise
    finally:
        # Ensure lock release even under task cancellation (Python 3.14: CancelledError is BaseException).
        task = asyncio.create_task(lock.__aexit__(exc_type, exc, tb))
        try:
            await asyncio.shield(task)
        except BaseException:
            with contextlib.suppress(Exception):
                await task


T = TypeVar('T')

async def _to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(func, *args, **kwargs)


def _expanduser_resolve_path(path: Path) -> Path:
    return path.expanduser().resolve()


def collect_lock_status(settings: Settings, project_slug: str | None = None) -> dict[str, Any]:
    """Return metadata for managed mailbox locks without traversing legacy storage."""

    root = Path(settings.storage.root).expanduser().resolve()
    scan_roots = (
        [root / "mailboxes" / project_slug]
        if project_slug
        else [root / ".mailbox-locks", root / "mailboxes"]
    )
    locks: list[dict[str, Any]] = []
    summary = {"total": 0, "active": 0, "stale": 0, "metadata_missing": 0}

    for scan_root in scan_roots:
        if not scan_root.exists():
            continue
        now = time.time()
        for lock_path in sorted(scan_root.rglob("*.lock"), key=lambda p: str(p)):
            lock_path.relative_to(root)
            metadata_path = lock_path.parent / f"{lock_path.name}.owner.json"
            if not lock_path.exists():
                continue
            metadata_present = metadata_path.exists()
            if lock_path.name != ".archive.lock" and not metadata_present:
                continue

            info: dict[str, Any] = {
                "path": str(lock_path),
                "metadata_path": str(metadata_path) if metadata_present else None,
                "status": "held",
                "metadata_present": metadata_present,
                "category": "archive" if lock_path.name == ".archive.lock" else "custom",
            }

            with contextlib.suppress(Exception):
                stat = lock_path.stat()
                info["size"] = stat.st_size
                info["modified_ts"] = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()

            metadata: dict[str, Any] = {}
            if metadata_present:
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except Exception:
                    metadata = {}
            info["metadata"] = metadata

            pid_val = metadata.get("pid")
            pid_int: int | None = None
            if pid_val is not None:
                with contextlib.suppress(Exception):
                    pid_int = int(pid_val)
            info["owner_pid"] = pid_int
            owner_alive: bool | None = None
            if pid_int is not None:
                owner_alive = AsyncFileLock._pid_alive(pid_int)
            info["owner_alive"] = owner_alive

            created_ts = metadata.get("created_ts") if isinstance(metadata, dict) else None
            if isinstance(created_ts, (int, float)):
                info["created_ts"] = datetime.fromtimestamp(created_ts, tz=timezone.utc).isoformat()
                info["age_seconds"] = max(0.0, now - float(created_ts))
            else:
                info["created_ts"] = None
                with contextlib.suppress(Exception):
                    info["age_seconds"] = max(0.0, now - lock_path.stat().st_mtime)
                if "age_seconds" not in info:
                    info["age_seconds"] = None

            stale_threshold = AsyncFileLock(lock_path)._stale_timeout
            info["stale_timeout_seconds"] = stale_threshold
            age_val = info.get("age_seconds")
            # Lock is stale if owner metadata proves the owner is gone OR if the
            # lock file age exceeds the configured stale timeout.
            is_stale = False
            if owner_alive is False or (stale_threshold > 0 and isinstance(age_val, (int, float)) and age_val >= stale_threshold):
                is_stale = True
            info["stale_suspected"] = is_stale

            summary["total"] += 1

            if is_stale:
                summary["stale"] += 1
            elif info["owner_alive"] is True:
                summary["active"] += 1
            if not metadata_present:
                summary["metadata_missing"] += 1

            locks.append(info)

    return {"locks": locks, "summary": summary}


async def ensure_mailbox_storage(settings: Settings, slug: str) -> MailboxStorage:
    """Open attachment/projection storage without initializing or opening Git."""
    if not slug or Path(slug).name != slug or slug in {".", ".."} or "/" in slug or "\\" in slug:
        raise ValueError("Invalid mailbox storage slug")
    root = Path(settings.storage.root).expanduser().resolve()
    project_root = root / "mailboxes" / slug
    guarded = (root / "mailboxes", project_root, root / ".mailbox-locks", root / ".mailbox-locks" / f"{slug}.lock")
    if any(path.is_symlink() or path.is_junction() for path in guarded) or not project_root.resolve().is_relative_to(root / "mailboxes"):
        raise ValueError("Mailbox storage must remain inside the configured storage root")
    await asyncio.to_thread(project_root.mkdir, parents=True, exist_ok=True)
    lock_root = root / ".mailbox-locks"
    await asyncio.to_thread(lock_root.mkdir, parents=True, exist_ok=True)
    return MailboxStorage(settings=settings, slug=slug, root=project_root,
                          lock_path=lock_root / f"{slug}.lock", repo_root=root)


async def write_file_reservation_records(
    archive: MailboxStorage,
    file_reservations: Sequence[dict[str, object]],
) -> None:
    if not file_reservations:
        return
    for file_reservation in file_reservations:
        path_pattern = str(file_reservation.get("path_pattern") or file_reservation.get("path") or "").strip()
        if not path_pattern:
            raise ValueError("File reservation record must include 'path_pattern'.")
        normalized_file_reservation = dict(file_reservation)
        normalized_file_reservation["path_pattern"] = path_pattern
        normalized_file_reservation.pop("path", None)
        digest = hashlib.sha1(path_pattern.encode("utf-8"), usedforsecurity=False).hexdigest()
        # Legacy path: digest of path_pattern (kept to avoid stale artifacts in existing installs)
        legacy_path = archive.root / "file_reservations" / f"{digest}.json"
        await _write_json(legacy_path, normalized_file_reservation)

        # Stable per-reservation artifact to avoid collisions across shared reservations
        reservation_id = normalized_file_reservation.get("id")
        id_token = str(reservation_id).strip() if reservation_id is not None else ""
        if id_token.isdigit():
            id_path = archive.root / "file_reservations" / f"id-{id_token}.json"
            await _write_json(id_path, normalized_file_reservation)


async def write_file_reservation_record(archive: MailboxStorage, file_reservation: dict[str, object]) -> None:
    await write_file_reservation_records(archive, [file_reservation])


def _resolve_archive_relative_path(archive: MailboxStorage, raw_path: str) -> Path:
    """Resolve a relative path safely inside the project archive root.

    Rejects directory traversal and ensures the resolved path stays within
    the project's archive root (defense-in-depth against symlink escapes).
    """
    normalized = (raw_path or "").strip().replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or normalized.startswith("..")
        or "/../" in normalized
        or normalized.endswith("/..")
        or normalized == ".."
    ):
        raise ValueError("Invalid path: directory traversal not allowed")

    safe_rel = normalized.lstrip("/")
    root = archive.root.resolve()
    candidate = (archive.root / safe_rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Invalid path: directory traversal not allowed") from exc
    return candidate


async def process_attachments(
    archive: MailboxStorage,
    body_md: str,
    attachment_paths: Iterable[str] | None,
    convert_markdown: bool,
    *,
    embed_policy: str = "auto",
) -> tuple[str, list[dict[str, object]], list[str]]:
    attachments_meta: list[dict[str, object]] = []
    commit_paths: list[str] = []
    updated_body = body_md
    # Respect explicit convert_markdown decision; embed_policy ("inline"/"file") forces conversion
    should_convert = convert_markdown or embed_policy in {"inline", "file"}
    if should_convert:
        updated_body = await _convert_markdown_images(
            archive, body_md, attachments_meta, commit_paths, embed_policy=embed_policy
        )
    else:
        # Even when not converting, surface inline data-uri images in attachments meta for visibility
        if "data:image" in body_md:
            for m in _IMAGE_PATTERN.finditer(body_md):
                raw_path = m.group("path")
                if raw_path.startswith("data:"):
                    try:
                        header = raw_path.split(",", 1)[0]
                        media_type = "image/webp"
                        if ";" in header:
                            mt = header[5:].split(";", 1)[0]
                            if mt:
                                media_type = mt
                        attachments_meta.append({"type": "inline", "media_type": media_type})
                    except Exception:
                        attachments_meta.append({"type": "inline"})
    if attachment_paths:
        for path in attachment_paths:
            p = Path(path)
            if p.is_absolute():
                if not archive.settings.storage.allow_absolute_attachment_paths:
                    raise ValueError(
                        "Absolute attachment paths are disabled. Set ALLOW_ABSOLUTE_ATTACHMENT_PATHS=true to enable."
                    )
                resolved = await _to_thread(_expanduser_resolve_path, p)
            else:
                resolved = _resolve_archive_relative_path(archive, path)
            meta, rel_path = await _store_image(archive, resolved, embed_policy=embed_policy)
            attachments_meta.append(meta)
            if rel_path:
                commit_paths.append(rel_path)
    return updated_body, attachments_meta, commit_paths


async def _convert_markdown_images(
    archive: MailboxStorage,
    body_md: str,
    meta: list[dict[str, object]],
    commit_paths: list[str],
    *,
    embed_policy: str = "auto",
) -> str:
    matches = list(_IMAGE_PATTERN.finditer(body_md))
    if not matches:
        return body_md
    result_parts: list[str] = []
    last_idx = 0
    for match in matches:
        path_start, path_end = match.span("path")
        result_parts.append(body_md[last_idx:path_start])
        raw_path = match.group("path")
        normalized_path = raw_path.strip()
        if raw_path.startswith("data:"):
            # Preserve inline data URI and record minimal metadata so callers can assert inline behavior
            try:
                header = normalized_path.split(",", 1)[0]
                media_type = "image/webp"
                if ";" in header:
                    mt = header[5:].split(";", 1)[0]
                    if mt:
                        media_type = mt
                meta.append({
                    "type": "inline",
                    "media_type": media_type,
                })
            except Exception:
                meta.append({"type": "inline"})
            result_parts.append(raw_path)
            last_idx = path_end
            continue
        file_path = Path(normalized_path)
        if file_path.is_absolute():
            if not archive.settings.storage.allow_absolute_attachment_paths:
                result_parts.append(raw_path)
                last_idx = path_end
                continue
            file_path = await _to_thread(_expanduser_resolve_path, file_path)
        else:
            try:
                file_path = _resolve_archive_relative_path(archive, normalized_path)
            except ValueError:
                result_parts.append(raw_path)
                last_idx = path_end
                continue
        if not file_path.is_file():
            result_parts.append(raw_path)
            last_idx = path_end
            continue
        attachment_meta, rel_path = await _store_image(archive, file_path, embed_policy=embed_policy)
        replacement_value: str
        if attachment_meta["type"] == "inline":
            replacement_value = f"data:image/webp;base64,{attachment_meta['data_base64']}"
        else:
            replacement_value = str(attachment_meta["path"])
        leading_ws_len = len(raw_path) - len(raw_path.lstrip())
        trailing_ws_len = len(raw_path) - len(raw_path.rstrip())
        leading_ws = raw_path[:leading_ws_len] if leading_ws_len else ""
        trailing_ws = raw_path[len(raw_path) - trailing_ws_len :] if trailing_ws_len else ""
        result_parts.append(f"{leading_ws}{replacement_value}{trailing_ws}")
        meta.append(attachment_meta)
        if rel_path:
            commit_paths.append(rel_path)
        last_idx = path_end
    result_parts.append(body_md[last_idx:])
    return "".join(result_parts)


async def _store_image(archive: MailboxStorage, path: Path, *, embed_policy: str = "auto") -> tuple[dict[str, object], str | None]:
    data = await _to_thread(path.read_bytes)

    # Open image and convert, properly closing the original to prevent file handle leaks
    def _open_and_convert(p: Path) -> Image.Image:
        with Image.open(p) as pil:
            return pil.convert("RGBA" if pil.mode in ("LA", "RGBA") else "RGB")

    img = await _to_thread(_open_and_convert, path)
    try:
        width, height = img.size
        buffer_path = archive.attachments_dir
        await _to_thread(buffer_path.mkdir, parents=True, exist_ok=True)
        # Use SHA256 for new content-addressable writes.  SHA256 is collision-
        # resistant in the cryptographic sense and avoids the theoretical
        # SHAttered (2017) chosen-prefix collision risk present in SHA1.
        # Legacy blobs already on disk keep their 40-char SHA1 filenames; the
        # digest field in returned metadata uses the field name ``"sha1"`` for
        # backward-compat with all existing consumers (the field now carries
        # a 64-char SHA256 hex string for new content, 40-char SHA1 for legacy
        # content already present on disk — both are opaque keys to callers).
        digest = hashlib.sha256(data).hexdigest()
        target_dir = buffer_path / digest[:2]
        await _to_thread(target_dir.mkdir, parents=True, exist_ok=True)
        target_path = target_dir / f"{digest}.webp"
        # Optionally store original alongside (in originals/)
        original_rel: str | None = None
        if archive.settings.storage.keep_original_images:
            originals_dir = archive.root / "attachments" / "originals" / digest[:2]
            await _to_thread(originals_dir.mkdir, parents=True, exist_ok=True)
            orig_ext = path.suffix.lower().lstrip(".") or "bin"
            orig_path = originals_dir / f"{digest}.{orig_ext}"
            if not orig_path.exists():
                await _to_thread(orig_path.write_bytes, data)
            original_rel = orig_path.relative_to(archive.repo_root).as_posix()
        if not target_path.exists():
            await _save_webp(img, target_path)
        new_bytes = await _to_thread(target_path.read_bytes)
        rel_path = target_path.relative_to(archive.repo_root).as_posix()
        # Update per-attachment manifest with metadata
        try:
            manifest_dir = archive.root / "attachments" / "_manifests"
            await _to_thread(manifest_dir.mkdir, parents=True, exist_ok=True)
            manifest_path = manifest_dir / f"{digest}.json"
            manifest_payload = {
                "sha1": digest,
                "webp_path": rel_path,
                "bytes_webp": len(new_bytes),
                "width": width,
                "height": height,
                "original_path": original_rel,
                "bytes_original": len(data),
                "original_ext": path.suffix.lower(),
            }
            await _write_json(manifest_path, manifest_payload)
            await _append_attachment_audit(
                archive,
                digest,
                {
                    "event": "stored",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "webp_path": rel_path,
                    "bytes_webp": len(new_bytes),
                    "original_path": original_rel,
                    "bytes_original": len(data),
                    "ext": path.suffix.lower(),
                },
            )
        except Exception:
            pass

        should_inline = False
        if embed_policy == "inline":
            should_inline = True
        elif embed_policy == "file":
            should_inline = False
        else:
            should_inline = len(new_bytes) <= archive.settings.storage.inline_image_max_bytes
        if should_inline:
            encoded = base64.b64encode(new_bytes).decode("ascii")
            return {
                "type": "inline",
                "media_type": "image/webp",
                "bytes": len(new_bytes),
                "width": width,
                "height": height,
                "sha1": digest,
                "data_base64": encoded,
            }, rel_path
        meta: dict[str, object] = {
            "type": "file",
            "media_type": "image/webp",
            "bytes": len(new_bytes),
            "path": rel_path,
            "width": width,
            "height": height,
            "sha1": digest,
        }
        if original_rel:
            meta["original_path"] = original_rel
        return meta, rel_path
    finally:
        # Close the converted image to prevent file handle leaks
        img.close()


async def _save_webp(img: Image.Image, path: Path) -> None:
    await _to_thread(img.save, path, format="WEBP", method=6, quality=80)


async def _write_text(path: Path, content: str) -> None:
    await _to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    await _to_thread(path.write_text, content, encoding="utf-8")


async def _write_json(path: Path, payload: dict[str, object]) -> None:
    content = json.dumps(payload, indent=2, sort_keys=True)
    await _write_text(path, content + "\n")


async def _append_attachment_audit(archive: MailboxStorage, digest: str, event: dict[str, object]) -> None:
    """Append a single JSON line audit record for an attachment digest.

    Creates attachments/_audit/<digest>.log if missing. Best-effort; failures are ignored.
    The digest is a SHA256 hex string for new content (64 chars) or a legacy SHA1
    hex string (40 chars) for content written by older versions of this code.
    """
    try:
        audit_dir = archive.root / "attachments" / "_audit"
        await _to_thread(audit_dir.mkdir, parents=True, exist_ok=True)
        audit_path = audit_dir / f"{digest}.log"

        def _append_line() -> None:
            line = json.dumps(event, sort_keys=True)
            with audit_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

        await _to_thread(_append_line)
    except Exception:
        pass


# ==================================================================================


# =============================================================================
# -------------------------------------------------------------------------------------------------
# Push Notifications: Signal file approach for local deployments
# -------------------------------------------------------------------------------------------------
# When enabled, write a signal file when a message is delivered to an agent's inbox.
# Agents can watch these files using inotify/FSEvents/kqueue for instant notifications
# without polling. This is useful for local multi-agent workflows.
#
# Signal file path: {signals_dir}/projects/{project_slug}/agents/{agent_name}.signal
# Signal file contents: JSON with message metadata (id, from, subject, importance, timestamp)

# Debounce tracking: (project_slug, agent_name) -> last_signal_time
_SIGNAL_DEBOUNCE: dict[tuple[str, str], float] = {}


async def emit_notification_signal(
    settings: Settings,
    project_slug: str,
    agent_name: str,
    message_metadata: dict[str, Any] | None = None,
) -> bool:
    """Emit a notification signal for an agent in a project.

    This creates/updates a signal file that agents can watch for incoming messages.
    The signal file contains metadata about the notification for context.

    Args:
        settings: Application settings (must have notifications enabled)
        project_slug: Project identifier
        agent_name: Target agent name
        message_metadata: Optional dict with message info (id, from, subject, importance)

    Returns:
        True if signal was emitted, False if notifications disabled or debounced
    """
    if not settings.notifications.enabled:
        return False

    # Debounce check: skip if we signaled this agent recently
    debounce_key = (project_slug, agent_name)
    debounce_ms = settings.notifications.debounce_ms
    now_ms = time.time() * 1000

    last_signal = _SIGNAL_DEBOUNCE.get(debounce_key, 0)
    if now_ms - last_signal < debounce_ms:
        return False  # Too soon, skip

    _SIGNAL_DEBOUNCE[debounce_key] = now_ms

    # Build signal file path
    signals_dir = await _to_thread(_expanduser_resolve_path, Path(settings.notifications.signals_dir))
    signal_path = signals_dir / "projects" / project_slug / "agents" / f"{agent_name}.signal"

    # Prepare signal content
    signal_data: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "project": project_slug,
        "agent": agent_name,
    }


    if settings.notifications.include_metadata and message_metadata:
        signal_data["message"] = {
            "id": message_metadata.get("id"),
            "from": message_metadata.get("from"),
            "subject": message_metadata.get("subject"),
            "importance": message_metadata.get("importance", "normal"),
        }

    # Write signal file
    def _write_signal() -> None:
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        signal_path.write_text(json.dumps(signal_data, indent=2), encoding="utf-8")

    try:
        await _to_thread(_write_signal)
        return True
    except Exception:
        # Signal emission is best-effort; don't fail message delivery
        return False


async def clear_notification_signal(
    settings: Settings,
    project_slug: str,
    agent_name: str,
) -> bool:
    """Clear notification signal for an agent (called when inbox is read).

    This removes the signal file to indicate the agent has acknowledged notifications.

    Args:
        settings: Application settings
        project_slug: Project identifier
        agent_name: Target agent name

    Returns:
        True if signal was cleared, False if file didn't exist or error
    """
    if not settings.notifications.enabled:
        return False

    signals_dir = await _to_thread(_expanduser_resolve_path, Path(settings.notifications.signals_dir))
    signal_path = signals_dir / "projects" / project_slug / "agents" / f"{agent_name}.signal"

    def _clear_signal() -> bool:
        if signal_path.exists():
            signal_path.unlink()
            return True
        return False

    try:
        return await _to_thread(_clear_signal)
    except Exception:
        return False


def list_pending_signals(settings: Settings, project_slug: str | None = None) -> list[dict[str, Any]]:
    """List all pending notification signals.

    Args:
        settings: Application settings
        project_slug: Optional filter by project

    Returns:
        List of signal info dicts with project, agent, and metadata
    """
    if not settings.notifications.enabled:
        return []

    signals_dir = Path(settings.notifications.signals_dir).expanduser().resolve()
    if not signals_dir.exists():
        return []

    results: list[dict[str, Any]] = []
    projects_dir = signals_dir / "projects"

    if not projects_dir.exists():
        return []

    project_dirs = [projects_dir / project_slug] if project_slug else list(projects_dir.iterdir())

    for proj_dir in project_dirs:
        if not proj_dir.is_dir():
            continue
        agents_dir = proj_dir / "agents"
        if not agents_dir.exists():
            continue

        for signal_file in agents_dir.glob("*.signal"):
            try:
                data = json.loads(signal_file.read_text(encoding="utf-8"))
                results.append(data)
            except Exception:
                # Corrupted signal file; include minimal info
                results.append({
                    "project": proj_dir.name,
                    "agent": signal_file.stem,
                    "error": "Failed to parse signal file",
                })

    return results
