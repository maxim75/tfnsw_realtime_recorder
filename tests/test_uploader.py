import time
from pathlib import Path

from botocore.exceptions import ClientError

from recorder.uploader import Uploader, next_part_key, object_key


class FakeS3:
    def __init__(self, existing=(), fail_times=0):
        self.objects = {k: b"" for k in existing}
        self.fail_times = fail_times
        self.extra_args = []

    def head_object(self, Bucket, Key):
        if Key in self.objects:
            return {}
        raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None):
        if self.fail_times:
            self.fail_times -= 1
            raise ClientError({"Error": {"Code": "500", "Message": "boom"}}, "PutObject")
        self.objects[Key] = Path(Filename).read_bytes()
        self.extra_args.append(ExtraArgs)


def make_file(data_dir, name="buses_2026-10-16T12-10+1100.csv.gz", feed_dir="buses"):
    path = data_dir / "complete" / feed_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")
    return path


KEY = "vehiclepos/feed=buses/date=2026-10-16/buses_2026-10-16T12-10+1100.csv.gz"


def test_object_key(tmp_path):
    assert object_key("vehiclepos", make_file(tmp_path)) == KEY
    path = make_file(tmp_path, "ferries_MFF_2026-10-16T12-10+1100_p2.csv.gz", "ferries_MFF")
    assert object_key("", path) == (
        "feed=ferries_MFF/date=2026-10-16/ferries_MFF_2026-10-16T12-10+1100_p2.csv.gz"
    )


def test_next_part_key():
    assert next_part_key("a/b_2026-10-16T12-10+1100.csv.gz") == "a/b_2026-10-16T12-10+1100_p2.csv.gz"
    assert next_part_key("a/b_2026-10-16T12-10+1100_p2.csv.gz") == "a/b_2026-10-16T12-10+1100_p3.csv.gz"


def test_upload_success_deletes_local_file(tmp_path):
    s3 = FakeS3()
    up = Uploader(s3, "bucket", "vehiclepos", tmp_path)
    path = make_file(tmp_path)
    assert up.upload(path)
    assert s3.objects[KEY] == b"data"
    assert s3.extra_args == [{"ContentType": "application/gzip"}]
    assert not path.exists()
    st = up.status()
    assert st["uploaded_total"] == 1 and st["last_uploaded_key"] == KEY


def test_existing_object_is_not_overwritten(tmp_path):
    s3 = FakeS3(existing=[KEY])
    up = Uploader(s3, "bucket", "vehiclepos", tmp_path)
    assert up.upload(make_file(tmp_path))
    assert s3.objects[KEY] == b""
    assert s3.objects[KEY.replace(".csv.gz", "_p2.csv.gz")] == b"data"


def test_failure_keeps_file_and_records_error(tmp_path):
    up = Uploader(FakeS3(fail_times=1), "bucket", "vehiclepos", tmp_path)
    path = make_file(tmp_path)
    assert not up.upload(path)
    assert path.exists()
    assert "boom" in up.status()["last_error"]


def test_worker_retries_until_s3_recovers(tmp_path):
    s3 = FakeS3(fail_times=2)
    up = Uploader(s3, "bucket", "vehiclepos", tmp_path, backoff_initial=0.01, backoff_max=0.02)
    path = make_file(tmp_path)
    up.start()  # startup scan finds the file in complete/
    deadline = time.monotonic() + 5
    while KEY not in s3.objects and time.monotonic() < deadline:
        time.sleep(0.01)
    up.stop(timeout=1)
    assert KEY in s3.objects
    assert not path.exists()
    assert up.status()["pending"] == 0


def test_stop_drains_queue(tmp_path):
    s3 = FakeS3()
    up = Uploader(s3, "bucket", "vehiclepos", tmp_path)
    up.start()
    up.enqueue(make_file(tmp_path))
    up.stop(timeout=2)
    assert KEY in s3.objects
    assert not up.alive


def test_disabled_uploader_keeps_files(tmp_path):
    up = Uploader(None, None, "vehiclepos", tmp_path)
    path = make_file(tmp_path)
    up.start()
    up.enqueue(path)
    up.stop()
    assert path.exists()
    st = up.status()
    assert st["s3_configured"] is False and st["pending"] == 1
    assert up.alive  # disabled is healthy


def test_unlink_failure_keeps_thread_alive_and_does_not_reupload(tmp_path, monkeypatch):
    s3 = FakeS3()
    up = Uploader(s3, "bucket", "vehiclepos", tmp_path, backoff_initial=0.01, backoff_max=0.02)
    first = make_file(tmp_path)
    second = make_file(tmp_path, "buses_2026-10-16T12-20+1100.csv.gz")
    real_unlink = Path.unlink
    unlink_calls = []

    def flaky_unlink(self, *a, **k):
        if self == first:
            unlink_calls.append(self)
            raise OSError(30, "read-only file system")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    up.start()
    key2 = KEY.replace("12-10", "12-20")
    deadline = time.monotonic() + 5
    while key2 not in s3.objects and time.monotonic() < deadline:
        time.sleep(0.01)
    assert key2 in s3.objects and up.alive
    up.enqueue(first)  # e.g. picked up again; must be skipped
    time.sleep(0.8)
    up.stop(timeout=1)
    assert KEY in s3.objects and not any("_p2" in k for k in s3.objects)
    assert first.exists() and not second.exists()
    assert "read-only" in up.status()["last_error"]


def test_loop_body_exception_does_not_kill_thread(tmp_path):
    s3 = FakeS3()
    up = Uploader(s3, "bucket", "vehiclepos", tmp_path, backoff_initial=0.01, backoff_max=0.02)
    calls = []
    real = up.upload

    def flaky(path):
        calls.append(path)
        if len(calls) == 1:
            raise RuntimeError("unexpected")
        return real(path)

    up.upload = flaky
    path = make_file(tmp_path)
    up.start()
    deadline = time.monotonic() + 5
    while KEY not in s3.objects and time.monotonic() < deadline:
        time.sleep(0.01)
    up.stop(timeout=1)
    assert KEY in s3.objects
    assert "unexpected" in up.status()["last_error"]


def test_unexpected_thread_exit_calls_on_fatal(tmp_path):
    fatal = []
    up = Uploader(FakeS3(), "bucket", "vehiclepos", tmp_path, on_fatal=lambda: fatal.append(1))

    def boom(path):
        raise RuntimeError("unexpected")

    up.upload = boom
    up._lock = None  # the guarded handler itself fails, so the error escapes the loop
    make_file(tmp_path)
    up.start()
    deadline = time.monotonic() + 5
    while not fatal and time.monotonic() < deadline:
        time.sleep(0.01)
    assert fatal == [1]


def test_normal_stop_does_not_call_on_fatal(tmp_path):
    fatal = []
    up = Uploader(FakeS3(), "bucket", "vehiclepos", tmp_path, on_fatal=lambda: fatal.append(1))
    up.start()
    up.stop(timeout=1)
    assert fatal == []
