"""Deterministic test configuration, applied before the app is imported.

Every setting except DATABASE_URL is forced, so tests behave the same on a
laptop, in the api container (whose .env holds real values, including the
Gemini key), and in CI. Integration tests derive their own throwaway database
from DATABASE_URL; see tests/integration/conftest.py.
"""

import atexit
import os
import shutil
import tempfile

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
    "GEMINI_CHAT_MODEL": "gemini-3.5-flash-lite",
    "GEMINI_THINKING_LEVEL": "low",
    "GEMINI_MAX_OUTPUT_TOKENS": "1200",
    "GEMINI_REQUEST_TIMEOUT_SECONDS": "90",
    "GEMINI_MAX_RETRIES": "3",
    "GEMINI_GENERATION_MAX_RETRIES": "0",
    "GEMINI_RETRY_BASE_DELAY_SECONDS": "1",
    "GEMINI_RETRY_MAX_DELAY_SECONDS": "20",
    "DAILY_CHAT_REQUEST_LIMIT": "30",
    "CHAT_CONTEXT_MAX_CHARS": "12000",
    "CHAT_HISTORY_MESSAGES": "6",
    "DAILY_EVALUATION_RUN_LIMIT": "3",
    "DAILY_EVALUATION_GENERATION_LIMIT": "120",
    "EVALUATION_QUESTION_INTERVAL_SECONDS": "6",
    "WORKER_POLL_SECONDS": "5",
    "AUTH_RATE_LIMIT_WINDOW_SECONDS": "900",
    "LOGIN_RATE_LIMIT_PER_IP": "20",
    "LOGIN_FAILURE_LIMIT_PER_EMAIL": "5",
    "REGISTER_RATE_LIMIT_PER_IP": "5",
    "GEMINI_EMBEDDING_MODEL": "gemini-embedding-001",
    "EMBEDDING_DIMENSIONS": "768",
    "GEMINI_EMBEDDING_BATCH_SIZE": "8",
    "GEMINI_CALL_DEADLINE_SECONDS": "120",
    "MAX_UPLOAD_MB": "10",
    "MIN_DOCUMENT_CHARS": "50",
    "MAX_PDF_PAGES": "500",
    "MAX_DOCUMENT_CHARS": "2000000",
    "MAX_DOCUMENT_CHUNKS": "1500",
    "CHUNK_TARGET_CHARS": "1800",
    "CHUNK_MAX_CHARS": "2400",
    "CHUNK_OVERLAP_CHARS": "250",
    "DAILY_EMBEDDING_TEXT_LIMIT": "300",
    "INGESTION_WORKSPACE_CHUNKERS": "{}",
    "RETRIEVAL_CANDIDATES": "10",
    "RRF_K": "60",
    "RETRIEVAL_TOP_N": "5",
    "DAILY_RETRIEVAL_REQUEST_LIMIT": "100",
    "DAILY_QUERY_EMBEDDING_LIMIT": "500",
    "JOB_MAX_ATTEMPTS": "3",
    "JOB_LEASE_SECONDS": "120",
    "JOB_HEARTBEAT_SECONDS": "30",
    "JOB_RETRY_BASE_SECONDS": "30",
    "JOB_MAX_RUNTIME_SECONDS": "900",
    "PARSE_TIMEOUT_SECONDS": "120",
    "PARSE_MEMORY_LIMIT_MB": "1536",
}

os.environ.update(TEST_ENVIRONMENT)
# Uploads go to a private temporary directory, never the real uploads volume.
TEST_UPLOAD_DIR = tempfile.mkdtemp(prefix="knowbase-test-uploads-")
os.environ["UPLOAD_DIR"] = TEST_UPLOAD_DIR
atexit.register(shutil.rmtree, TEST_UPLOAD_DIR, ignore_errors=True)
# Unit tests never connect; integration tests need a real value from the environment.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://unused:unused@127.0.0.1:1/unused")
