import pytest
import requests


@pytest.fixture(autouse=True)
def no_static_gtfs_download(monkeypatch):
    """Offline tools refresh routes at start; tests never reach the network."""
    def refuse(*args, **kwargs):
        raise requests.ConnectionError("network disabled in tests")

    monkeypatch.setattr("recorder.routes.requests.get", refuse)
