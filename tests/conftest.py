import pytest

from jobscout import feeds


@pytest.fixture(autouse=True)
def no_live_feeds(monkeypatch):
    """Tests never download the real GitHub job lists; tests that need them fake them."""
    monkeypatch.setattr(feeds, "simplify_listings", lambda: [])
