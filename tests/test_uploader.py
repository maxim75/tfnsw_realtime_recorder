import time
from pathlib import Path

from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import ClientError

from recorder.uploader import Uploader, next_part_key, object_key


class FakeS3:
    def __init__(self, existing=(), fail_times=0, deny_head=0, deny_put=0, fail_keys=(), deny_code="AccessDenied"):
        self.objects = {k: b"" for k in existing}
        self.fail_times = fail_times
        self.deny_head = deny_head  # next N head_object calls get a spurious 403
        self.deny_put = deny_put  # next N upload_file calls get a spurious AccessDenied
        self.fail_keys = set(fail_keys)  # keys whose upload always fails with 500
        self.deny_code = deny_code  # Hetzner sometimes sends an empty code with HTTP 403
        self.extra_args = []
        self.put_attempts = 0

    def head_object(self, Bucket, Key):
        if self.deny_head:
            self.deny_head -= 1
            raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")
        if Key in self.objects:
            return {}
        raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None):
        self.put_attempts += 1
        if self.deny_put:
            self.deny_put -= 1
            # boto3's transfer manager re-raises the ClientError like this (inside except)
            denial = ClientError(
                {"Error": {"Code": self.deny_code, "Message": ""},
                 "ResponseMetadata": {"HTTPStatusCode": 403}},
                "PutObject",
            )
            try:
                raise denial
            except ClientError as e:
                raise S3UploadFailedError(f"Failed to upload {Filename} to {Bucket}/{Key}: {e}")
        if self.fail_times or Key in self.fail_keys:
            self.fail_times = max(0, self.fail_times - 1)
            raise ClientError({"Error": {"Code": "500", "Message": "boom"}}, "PutObject")
        self.objects[Key] = Path(Filename).read_bytes()
        self.extra_args.append(ExtraArgs)


PREFIXES = {"sydney": "vehiclepos", "prague": "vehiclepos/city=prague"}


def make_file(data_dir, name="buses_2026-10-16T12-10+1100.csv.gz", feed_dir="buses", city="sydney"):
    path = data_dir / city / "complete" / feed_dir / name
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
    up = Uploader(s3, "bucket", PREFIXES, tmp_path)
    path = make_file(tmp_path)
    assert up.upload(path)
    assert s3.objects[KEY] == b"data"
    assert s3.extra_args == [{"ContentType": "application/gzip"}]
    assert not path.exists()
    st = up.status()
    assert st["uploaded_total"] == 1 and st["last_uploaded_key"] == KEY


def test_existing_object_is_not_overwritten(tmp_path):
    s3 = FakeS3(existing=[KEY])
    up = Uploader(s3, "bucket", PREFIXES, tmp_path)
    assert up.upload(make_file(tmp_path))
    assert s3.objects[KEY] == b""
    assert s3.objects[KEY.replace(".csv.gz", "_p2.csv.gz")] == b"data"


def test_failure_keeps_file_and_records_error(tmp_path):
    up = Uploader(FakeS3(fail_times=1), "bucket", PREFIXES, tmp_path)
    path = make_file(tmp_path)
    assert not up.upload(path)
    assert path.exists()
    assert "boom" in up.status()["last_error"]


def test_worker_retries_until_s3_recovers(tmp_path):
    s3 = FakeS3(fail_times=2)
    up = Uploader(s3, "bucket", PREFIXES, tmp_path, backoff_initial=0.01, backoff_max=0.02)
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
    up = Uploader(s3, "bucket", PREFIXES, tmp_path)
    up.start()
    up.enqueue(make_file(tmp_path))
    up.stop(timeout=2)
    assert KEY in s3.objects
    assert not up.alive


def test_disabled_uploader_keeps_files(tmp_path):
    up = Uploader(None, None, PREFIXES, tmp_path)
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
    up = Uploader(s3, "bucket", PREFIXES, tmp_path, backoff_initial=0.01, backoff_max=0.02)
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
    up = Uploader(s3, "bucket", PREFIXES, tmp_path, backoff_initial=0.01, backoff_max=0.02)
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
    up = Uploader(FakeS3(), "bucket", PREFIXES, tmp_path, on_fatal=lambda: fatal.append(1))

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
    up = Uploader(FakeS3(), "bucket", PREFIXES, tmp_path, on_fatal=lambda: fatal.append(1))
    up.start()
    up.stop(timeout=1)
    assert fatal == []


def test_spurious_403_on_head_is_retried_within_the_upload(tmp_path):
    s3 = FakeS3(deny_head=2)
    up = Uploader(s3, "bucket", PREFIXES, tmp_path, denial_retry_delay=0)
    assert up.upload(make_file(tmp_path))
    assert KEY in s3.objects  # not a _p2: the 403s were not taken as "exists"


def test_spurious_access_denied_on_put_is_retried_within_the_upload(tmp_path):
    s3 = FakeS3(deny_put=2)
    up = Uploader(s3, "bucket", PREFIXES, tmp_path, denial_retry_delay=0)
    path = make_file(tmp_path)
    assert up.upload(path)
    assert s3.objects[KEY] == b"data" and s3.put_attempts == 3
    assert not path.exists()


def test_persistent_access_denied_gives_up_and_keeps_file(tmp_path):
    s3 = FakeS3(deny_put=100)
    up = Uploader(s3, "bucket", PREFIXES, tmp_path, denial_retry_delay=0)
    path = make_file(tmp_path)
    assert not up.upload(path)
    assert s3.put_attempts == 4  # first try + 3 quick retries
    assert path.exists()
    assert "AccessDenied" in up.status()["last_error"]


def test_stop_keeps_draining_after_a_failed_file(tmp_path):
    names = [f"buses_2026-10-16T12-{m}+1100.csv.gz" for m in ("00", "10", "20")]
    keys = [f"vehiclepos/feed=buses/date=2026-10-16/{n}" for n in names]
    s3 = FakeS3(fail_keys=[keys[0]])
    up = Uploader(s3, "bucket", PREFIXES, tmp_path, denial_retry_delay=0)
    paths = [make_file(tmp_path, n) for n in names]
    up.start()
    up.stop(timeout=2)
    assert keys[1] in s3.objects and keys[2] in s3.objects
    assert paths[0].exists()  # left for the next start
    assert not paths[1].exists() and not paths[2].exists()


def test_access_denied_with_empty_error_code_is_retried(tmp_path):
    s3 = FakeS3(deny_put=1, deny_code="")
    up = Uploader(s3, "bucket", PREFIXES, tmp_path, denial_retry_delay=0)
    assert up.upload(make_file(tmp_path))
    assert s3.put_attempts == 2


def test_other_put_errors_are_not_retried_quickly(tmp_path):
    s3 = FakeS3(fail_keys=[KEY])
    up = Uploader(s3, "bucket", PREFIXES, tmp_path, denial_retry_delay=0)
    assert not up.upload(make_file(tmp_path))
    assert s3.put_attempts == 1


PRAGUE_KEY = "vehiclepos/city=prague/feed=all/date=2026-10-16/all_2026-10-16T12-10+0200.csv.gz"


def test_each_city_uploads_under_its_own_prefix(tmp_path):
    s3 = FakeS3()
    up = Uploader(s3, "bucket", PREFIXES, tmp_path)
    assert up.upload(make_file(tmp_path, "all_2026-10-16T12-10+0200.csv.gz", "all", city="prague"))
    assert up.upload(make_file(tmp_path))
    assert set(s3.objects) == {PRAGUE_KEY, KEY}


def test_startup_scan_finds_every_city(tmp_path):
    s3 = FakeS3()
    up = Uploader(s3, "bucket", PREFIXES, tmp_path)
    make_file(tmp_path)
    make_file(tmp_path, "all_2026-10-16T12-10+0200.csv.gz", "all", city="prague")
    up.start()
    up.stop(timeout=2)
    assert set(s3.objects) == {PRAGUE_KEY, KEY}


def test_file_of_unknown_city_is_left_in_place(tmp_path):
    s3 = FakeS3()
    up = Uploader(s3, "bucket", PREFIXES, tmp_path, backoff_initial=0.01)
    stray = make_file(tmp_path, "all_2026-10-16T12-10+0200.csv.gz", "all", city="atlantis")
    assert not up.upload(stray)
    up.start()
    up.stop(timeout=1)
    assert stray.exists() and s3.objects == {} and s3.put_attempts == 0
    assert "atlantis" in up.status()["last_error"]
    assert up.status()["pending"] == 0  # not retried forever


def test_status_times_are_timezone_aware(tmp_path):
    up = Uploader(FakeS3(), "bucket", PREFIXES, tmp_path)
    up.upload(make_file(tmp_path))
    assert up.status()["last_uploaded_at"].utcoffset() is not None
    assert up.status()["prefixes"] == PREFIXES


def test_disabled_uploader_counts_pending_files_of_every_city(tmp_path):
    up = Uploader(None, None, PREFIXES, tmp_path)
    make_file(tmp_path)
    make_file(tmp_path, "all_2026-10-16T12-10+0200.csv.gz", "all", city="prague")
    assert up.status()["pending"] == 2
