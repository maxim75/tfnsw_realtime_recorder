"""Upload completed window files to S3-compatible storage."""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import S3Config
from .windows import SYDNEY_TZ

log = logging.getLogger(__name__)

_PART = re.compile(r"^(?P<base>.*?)(?:_p(?P<part>\d+))?\.csv\.gz$")


def object_key(prefix: str, path: Path) -> str:
    """``{prefix}/feed={feed_dir}/date={YYYY-MM-DD}/{name}`` for a file in ``complete/<feed_dir>/``."""
    feed_dir = path.parent.name
    date = path.name[len(feed_dir) + 1 :][:10]
    key = f"feed={feed_dir}/date={date}/{path.name}"
    return f"{prefix}/{key}" if prefix else key


def next_part_key(key: str) -> str:
    match = _PART.match(key)
    part = int(match["part"] or 1) + 1
    return f"{match['base']}_p{part}.csv.gz"


def make_s3_client(s3: S3Config):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=s3.endpoint_url,
        region_name=s3.region,
        aws_access_key_id=s3.access_key_id,
        aws_secret_access_key=s3.secret_access_key,
        config=Config(
            # boto3 >= 1.36 adds CRC checksum headers that some S3-compatible stores reject.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 3},
        ),
    )


class Uploader:
    """Queue + worker thread. A file is deleted locally only after a successful upload."""

    def __init__(
        self,
        client,
        bucket: str | None,
        prefix: str,
        data_dir: Path,
        *,
        backoff_initial: float = 5.0,
        backoff_max: float = 300.0,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._prefix = prefix
        self._data_dir = data_dir
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._queue: queue.Queue[Path] = queue.Queue()
        self._stop = threading.Event()
        self._deadline = 0.0
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._uploaded_total = 0
        self._last_uploaded_key: str | None = None
        self._last_uploaded_at: datetime | None = None
        self._last_error: str | None = None
        self._last_error_at: datetime | None = None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @property
    def alive(self) -> bool:
        return not self.enabled or (self._thread is not None and self._thread.is_alive())

    def enqueue(self, path: Path) -> None:
        if self.enabled:
            self._queue.put(path)

    def start(self) -> None:
        if not self.enabled:
            log.warning("S3 not configured: completed files stay in %s", self._data_dir / "complete")
            return
        for path in sorted(self._data_dir.glob("complete/*/*.csv.gz")):
            self._queue.put(path)
        self._thread = threading.Thread(target=self._run, name="uploader", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 20.0) -> None:
        """Upload what is queued for up to *timeout* seconds, then stop."""
        self._deadline = time.monotonic() + timeout
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout + 1)

    def upload(self, path: Path) -> bool:
        from botocore.exceptions import ClientError

        try:
            key = object_key(self._prefix, path)
            while self._exists(key, ClientError):
                key = next_part_key(key)
            self._client.upload_file(
                str(path), self._bucket, key, ExtraArgs={"ContentType": "application/gzip"}
            )
        except Exception as exc:  # boto3/botocore raise many unrelated types
            log.warning("Upload of %s failed: %s", path.name, exc)
            with self._lock:
                self._last_error = str(exc)
                self._last_error_at = datetime.now(tz=SYDNEY_TZ)
            return False
        path.unlink(missing_ok=True)
        log.info("Uploaded %s", key)
        with self._lock:
            self._uploaded_total += 1
            self._last_uploaded_key = key
            self._last_uploaded_at = datetime.now(tz=SYDNEY_TZ)
        return True

    def _exists(self, key: str, client_error: type[Exception]) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except client_error as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def _run(self) -> None:
        backoff = self._backoff_initial
        while True:
            if self._stop.is_set() and (self._queue.empty() or time.monotonic() > self._deadline):
                return
            try:
                path = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if not path.exists():
                continue
            if self.upload(path):
                backoff = self._backoff_initial
                continue
            self._queue.put(path)
            if self._stop.is_set():
                return  # shutting down: leave it for the next start
            self._stop.wait(backoff)
            backoff = min(backoff * 2, self._backoff_max)

    def status(self) -> dict:
        pending = (
            self._queue.qsize()
            if self.enabled
            else len(list(self._data_dir.glob("complete/*/*.csv.gz")))
        )
        with self._lock:
            return {
                "s3_configured": self.enabled,
                "bucket": self._bucket,
                "prefix": self._prefix,
                "pending": pending,
                "uploaded_total": self._uploaded_total,
                "last_uploaded_key": self._last_uploaded_key,
                "last_uploaded_at": self._last_uploaded_at,
                "last_error": self._last_error,
                "last_error_at": self._last_error_at,
            }
