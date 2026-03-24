import os
import errno
import fcntl
from typing import Optional

# Keep a module-level reference so the lock isn't garbage-collected
_lock_fp: Optional[object] = None


def setup_singleton_lock(lock_name: str = "bot.lock") -> None:
    """
    Acquire an exclusive, non-blocking file lock inside DATA_DIR to ensure only one instance runs
    against the same data directory (and typically the same token) on the same host.

    Set ALLOW_MULTI_INSTANCE=true to bypass this guard.
    """
    global _lock_fp

    data_dir = os.environ.get("DATA_DIR", "/data")
    os.makedirs(data_dir, exist_ok=True)
    lock_path = os.path.join(data_dir, lock_name)

    # Open (or create) the lock file
    fp = open(lock_path, "a+")

    try:
        # Try to acquire an exclusive non-blocking lock
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        fp.close()
        if e.errno in (errno.EACCES, errno.EAGAIN):
            raise RuntimeError(
                f"Could not acquire singleton lock at {lock_path}. Another instance may be running."
            ) from e
        raise

    # Write the current PID for observability
    try:
        fp.seek(0)
        fp.truncate()
        fp.write(str(os.getpid()))
        fp.flush()
        os.fsync(fp.fileno())
    except Exception:
        # Non-fatal; continue holding the lock
        pass

    # Store globally to keep the lock alive for process lifetime
    _lock_fp = fp
