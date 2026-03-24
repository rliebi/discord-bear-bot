import os
import errno
import fcntl
import time
import logging
from typing import Optional

# Keep a module-level reference so the lock isn't garbage-collected
_lock_fp: Optional[object] = None

logger = logging.getLogger("bearbot.singleton")


def setup_singleton_lock(lock_name: str = "bot.lock") -> None:
    """
    Acquire an exclusive file lock inside DATA_DIR to ensure only one instance runs
    against the same data directory (and typically the same token) on the same host.

    Behavior:
    - Set ALLOW_MULTI_INSTANCE=true to bypass this guard.
    - By default, this function will WAIT and retry until the lock can be acquired.
      You can disable waiting by setting SINGLETON_WAIT=false to make it exit fast.
    Env vars:
      - DATA_DIR: base directory for the lock file (default: /data)
      - SINGLETON_WAIT: true/false (default: true) — wait/retry when the lock is held
      - SINGLETON_WAIT_INTERVAL: seconds between retries (default: 5)
    """
    global _lock_fp

    data_dir = os.environ.get("DATA_DIR", "/data")
    os.makedirs(data_dir, exist_ok=True)
    lock_path = os.path.join(data_dir, lock_name)

    wait_env = os.environ.get("SINGLETON_WAIT", "true").strip().lower() in {"1", "true", "yes"}
    try:
        interval = int(os.environ.get("SINGLETON_WAIT_INTERVAL", "5"))
        if interval <= 0:
            interval = 5
    except Exception:
        interval = 5

    while True:
        # Open (or create) the lock file each attempt to avoid stale fds
        fp = open(lock_path, "a+")
        try:
            # Try to acquire an exclusive non-blocking lock
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            fp.close()
            if e.errno in (errno.EACCES, errno.EAGAIN):
                if wait_env:
                    logger.info(
                        f"Singleton lock at {lock_path} is held by another instance; waiting {interval}s to retry..."
                    )
                    time.sleep(interval)
                    continue
                else:
                    raise RuntimeError(
                        f"Could not acquire singleton lock at {lock_path}. Another instance may be running."
                    ) from e
            else:
                # Unexpected error acquiring the lock
                raise

        # If we reach here, we have the lock
        try:
            fp.seek(0)
            fp.truncate()
            fp.write(str(os.getpid()))
            fp.flush()
            os.fsync(fp.fileno())
        except Exception:
            # Non-fatal; continue holding the lock
            pass

        _lock_fp = fp
        logger.info(f"Singleton lock acquired at {lock_path} (pid={os.getpid()}).")
        break
