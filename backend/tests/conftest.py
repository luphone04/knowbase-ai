"""Deterministic test configuration, applied before the app is imported.

Every setting except DATABASE_URL is forced, so tests behave the same on a
laptop, in the api container (whose .env holds real values, including the
Gemini key), and in CI. Integration tests derive their own throwaway database
from DATABASE_URL; see tests/integration/conftest.py.
"""

import os

TEST_SECRET = "test-only-secret-0123456789abcdef0123456789"

TEST_ENVIRONMENT = {
    "APP_ENV": "test",
    "APP_SECRET_KEY": TEST_SECRET,
    "JWT_SECRET": TEST_SECRET + "-jwt",
    "ACCESS_TOKEN_EXPIRE_MINUTES": "480",
    "LOG_LEVEL": "INFO",
    "COOKIE_SECURE": "false",
    "ALLOWED_HOSTS": "localhost,127.0.0.1,testserver",
    "FRONTEND_ORIGIN": "http://localhost:3000",
    "GEMINI_API_KEY": "",
    "AUTH_RATE_LIMIT_WINDOW_SECONDS": "900",
    "LOGIN_RATE_LIMIT_PER_IP": "20",
    "LOGIN_FAILURE_LIMIT_PER_EMAIL": "5",
    "REGISTER_RATE_LIMIT_PER_IP": "5",
}

os.environ.update(TEST_ENVIRONMENT)
# Unit tests never connect; integration tests need a real value from the environment.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://unused:unused@127.0.0.1:1/unused")
