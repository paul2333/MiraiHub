"""Atomic, revisioned QIMEI snapshots; account/domain validation belongs to callers.

Readers never create directories or locks. Writers coordinate through a stable
sibling lock and replace one complete snapshot, never a file's contents in place.
Use a directory mount on a local filesystem; individual file mounts cannot be
atomically replaced. Writer preflight syncs existing directory ancestry too, so a
retry cannot skip an entry left by an earlier failed directory sync. Windows
cannot normally open directories for fsync, so only
that explicitly unsupported operation is omitted there. The caller must reload
after SnapshotCommitUncertain before deciding whether an operation needs retrying.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import tempfile
import threading
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None


class SnapshotError(RuntimeError):
    """Snapshot storage or structural validation failed; no payload in messages."""


class SnapshotConflict(SnapshotError):
    """The expected revision does not identify the current snapshot."""


class SnapshotCommitUncertain(SnapshotError):
    """Replacement occurred, but its directory durability was not confirmed."""


class _LockState:
    def __init__(self):
        self.mutex = threading.RLock()
        self.local = threading.local()


_lock_states = {}
_lock_states_guard = threading.Lock()


def _canonical(path) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        raise SnapshotError("snapshot path resolution failed") from None


def _io_message(operation, exc) -> str:
    return "snapshot %s failed (errno=%s)" % (operation, getattr(exc, "errno", None))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-finite JSON number")


def _validate(value):
    if not isinstance(value, dict):
        raise SnapshotError("snapshot must be an object")
    if set(value) - {"schema", "revision", "table", "pool", "metadata"}:
        raise SnapshotError("snapshot contains unsupported fields")
    if type(value.get("schema")) is not int or value["schema"] != 1:
        raise SnapshotError("snapshot schema must be integer 1")
    if type(value.get("revision")) is not int or value["revision"] < 1:
        raise SnapshotError("snapshot revision must be a positive integer")
    if not isinstance(value.get("table"), dict) or not isinstance(value.get("pool"), dict):
        raise SnapshotError("snapshot table and pool must be objects")
    if "metadata" in value and not isinstance(value["metadata"], dict):
        raise SnapshotError("snapshot metadata must be an object")
    return value


def read_snapshot(path) -> dict:
    """Read one complete revision strictly, without locks or filesystem writes."""
    target = _canonical(path)
    try:
        with target.open("r", encoding="utf-8") as stream:
            value = json.load(stream, object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant)
    except FileNotFoundError:
        raise SnapshotError("snapshot is missing") from None
    except OSError as exc:
        raise SnapshotError(_io_message("read", exc)) from None
    except (ValueError, UnicodeError, RecursionError):
        raise SnapshotError("snapshot contains invalid JSON") from None
    return _validate(value)


def _ensure_directory(directory: Path) -> None:
    missing = []
    current = directory
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise SnapshotError("snapshot filesystem root is unavailable")
        current = parent
    for item in reversed(missing):
        try:
            item.mkdir(mode=0o700)
        except FileExistsError:
            if not item.is_dir():
                raise
        _fsync_directory(item)
        _fsync_directory(item.parent)
    current = directory
    while True:
        _fsync_directory(current)
        parent = current.parent
        if parent == current:
            break
        current = parent


def _acquire_os_lock(fd):
    if fcntl is not None:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                return
            except InterruptedError:
                continue
    if msvcrt is None:
        raise SnapshotError("snapshot OS locking is unavailable")
    # Windows byte-range locks may extend beyond EOF; initialization would race
    # with a different handle locking this byte before an unlocked write.
    while True:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            time.sleep(0.05)


def _release_os_lock(fd):
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    elif msvcrt is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def snapshot_lock(path):
    """Serialize writers across threads/processes, with same-thread reentrancy."""
    target = _canonical(path)
    key = os.path.normcase(str(target))
    with _lock_states_guard:
        state = _lock_states.setdefault(key, _LockState())
    with state.mutex:
        depth = getattr(state.local, "depth", 0)
        if depth:
            state.local.depth = depth + 1
            try:
                yield target
            finally:
                state.local.depth = depth
            return
        fd = None
        locked = False
        try:
            _ensure_directory(target.parent)
            fd = os.open(target.with_name(".%s.lock" % target.name),
                         os.O_CREAT | os.O_RDWR, 0o600)
            _acquire_os_lock(fd)
            locked = True
            state.local.depth = 1
            yield target
        except OSError as exc:
            raise SnapshotError(_io_message("lock", exc)) from None
        finally:
            state.local.depth = 0
            if fd is not None:
                try:
                    if locked:
                        _release_os_lock(fd)
                finally:
                    os.close(fd)


def _fsync_directory(directory):
    try:
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        # Windows does not support opening a directory with os.open for fsync.
        # Its CRT can report ENOENT for an existing drive root; never ignore
        # that code for a directory which really disappeared or is missing.
        if os.name == "nt":
            if exc.errno in (errno.EACCES, errno.EPERM, errno.EINVAL,
                             errno.EISDIR, errno.ENOTSUP):
                return
            if exc.errno == errno.ENOENT and Path(directory).is_dir():
                return
        raise
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_snapshot(path, *, table, pool, expected_revision, metadata=None) -> dict:
    """Compare-and-replace one revision, preserving omitted existing metadata."""
    if type(expected_revision) is not int or expected_revision < 0:
        raise SnapshotError("expected revision must be a nonnegative integer")
    if not isinstance(table, dict) or not isinstance(pool, dict):
        raise SnapshotError("snapshot table and pool must be objects")
    if metadata is not None and not isinstance(metadata, dict):
        raise SnapshotError("snapshot metadata must be an object")
    with snapshot_lock(path) as target:
        current = None
        if expected_revision == 0:
            if target.exists():
                raise SnapshotConflict("snapshot already exists; creation refused")
        else:
            if not target.exists():
                raise SnapshotConflict("snapshot is missing; update refused")
            current = read_snapshot(target)
            if current["revision"] != expected_revision:
                raise SnapshotConflict("snapshot revision changed; reload before retry")
        payload = {"schema": 1, "revision": expected_revision + 1,
                   "table": table, "pool": pool}
        if metadata is not None:
            payload["metadata"] = metadata
        elif current is not None and "metadata" in current:
            payload["metadata"] = current["metadata"]
        try:
            serialized = json.dumps(payload, indent=2, sort_keys=True,
                                    ensure_ascii=False, allow_nan=False) + "\n"
            serialized.encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise SnapshotError("snapshot payload is not JSON serializable") from None
        temporary = None
        fd = None
        replaced = False
        try:
            fd, name = tempfile.mkstemp(prefix=".%s.tmp." % target.name,
                                         dir=target.parent)
            temporary = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                fd = None
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            replaced = True
            _fsync_directory(target.parent)
        except OSError as exc:
            if replaced:
                raise SnapshotCommitUncertain(
                    "snapshot revision %d replaced; directory durability uncertain; "
                    "reload before retry" % payload["revision"]) from None
            raise SnapshotError(_io_message("write before replacement", exc)) from None
        finally:
            if fd is not None:
                os.close(fd)
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
        return json.loads(serialized)
