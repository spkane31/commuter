"""Private source snapshots and single-host process coordination."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path


def content_version(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, allow_nan=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


@contextmanager
def process_lock(path: Path):
    """Reject overlapping operations; locks are released automatically on a crash."""

    import fcntl  # This application deploys to POSIX hosts (Linux Pi/macOS).

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(
                "A Commuter operation is already running; retry later"
            ) from exc
        yield
    finally:
        os.close(descriptor)


class SourceCache:
    """Cache raw Strava data in an explicitly owned directory, never worksheet cells."""

    marker = ".commuter-training-cache"

    def __init__(self, root: Path) -> None:
        self.root = root

    def load(self, athlete_id: int, activity_id: int) -> dict[str, object] | None:
        path = self.root / str(athlete_id) / f"{activity_id}.json"
        if not path.exists():
            return None
        with path.open() as source:
            payload = json.load(source)
        if not isinstance(payload, dict):
            raise ValueError(f"Invalid cached activity {activity_id}")
        return payload

    def save(
        self, athlete_id: int, activity_id: int, payload: dict[str, object]
    ) -> None:
        encoded = json.dumps(payload, allow_nan=False, sort_keys=True)
        if self.root.is_symlink():
            raise ValueError("Refusing to write a symlinked cache")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not (self.root / self.marker).exists():
            if any(self.root.iterdir()):
                raise ValueError(
                    "Training cache must be an empty or application-owned directory"
                )
            (self.root / self.marker).touch(mode=0o600)
        self.root.chmod(0o700)
        directory = self.root / str(athlete_id)
        if directory.is_symlink():
            raise ValueError("Refusing to write a symlinked cache")
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        fd, temporary = tempfile.mkstemp(dir=directory, prefix=".snapshot-")
        try:
            with os.fdopen(fd, "w") as target:
                target.write(encoded)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, directory / f"{activity_id}.json")
        finally:
            Path(temporary).unlink(missing_ok=True)

    def activity_ids(self, athlete_id: int) -> list[int]:
        return sorted(
            int(p.stem)
            for p in (self.root / str(athlete_id)).glob("*.json")
            if p.stem.isdigit()
        )

    def remove(self, athlete_id: int | None = None) -> None:
        if not self.root.exists():
            return
        if self.root.is_symlink() or not (self.root / self.marker).is_file():
            raise ValueError("Refusing to remove an unowned training cache")
        target = self.root if athlete_id is None else self.root / str(athlete_id)
        if target.is_symlink():
            raise ValueError("Refusing to remove a symlinked cache")
        if target.exists():
            shutil.rmtree(target)
