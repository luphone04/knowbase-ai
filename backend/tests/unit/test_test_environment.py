"""The test configuration really pins every setting (audit: 9 were missing).

An unpinned setting takes its value from the environment, so tests in the
api container would run with the developer's .env values.
"""

from app.config import Settings
from tests.conftest import TEST_ENVIRONMENT

# Set separately in tests/conftest.py: a throwaway database, a private upload folder.
SET_ELSEWHERE = {"DATABASE_URL", "UPLOAD_DIR"}


def test_every_setting_is_pinned_for_tests() -> None:
    names = {name.upper() for name in Settings.model_fields}
    assert sorted(names - SET_ELSEWHERE - TEST_ENVIRONMENT.keys()) == []
