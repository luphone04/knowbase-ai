"""scripts/check_repo_hygiene.py against throwaway git repositories (audit M11).

Fake secrets are assembled at run time, so this file never contains one
itself. Skipped where the scripts/ folder or git isn't available (inside the
backend image).
"""

import io
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_repo_hygiene.py"
FAKE_GOOGLE_KEY = "AIza" + "Q" * 35
FAKE_JWT = ".".join(["eyJ" + "a" * 20, "eyJ" + "b" * 30, "c" * 43])

pytestmark = pytest.mark.skipif(
    not SCRIPT.is_file() or shutil.which("git") is None,
    reason="needs the repository's scripts/ folder and git",
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "README.md").write_text("A clean project.\n")
    stage(tmp_path, "README.md")
    return tmp_path


def stage(repo: Path, *paths: str) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "-f", "--", *paths], check=True)


def check(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=repo, capture_output=True, text=True, check=False
    )


def test_a_clean_index_passes(repo: Path) -> None:
    result = check(repo)
    assert result.returncode == 0, result.stdout
    assert "OK" in result.stdout


@pytest.mark.parametrize(
    "name", ["alice.jar", "cookies.txt", ".env", "backend/.env.local", 'we"ird dir/.env']
)
def test_forbidden_files_fail_whatever_their_name(repo: Path, name: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n")
    stage(repo, name)
    result = check(repo)
    assert result.returncode == 1
    assert "forbidden files" in result.stdout


def test_a_session_token_is_caught_and_never_printed(repo: Path) -> None:
    (repo / "notes.md").write_text(f"login cookie: access_token={FAKE_JWT}\n")
    stage(repo, "notes.md")
    result = check(repo)
    assert result.returncode == 1
    assert "notes.md:1: session token (JWT)" in result.stdout
    assert FAKE_JWT not in result.stdout and FAKE_JWT[:12] not in result.stdout


def test_a_quote_in_a_file_name_does_not_skip_its_content(repo: Path) -> None:
    (repo / "it's notes.md").write_text(f"key = {FAKE_GOOGLE_KEY}\n")
    stage(repo, "it's notes.md")
    result = check(repo)
    assert result.returncode == 1
    assert "it's notes.md:1: Google API key" in result.stdout


def test_the_staged_version_is_checked_not_the_working_tree(repo: Path) -> None:
    (repo / "config.md").write_text(f"key = {FAKE_GOOGLE_KEY}\n")
    stage(repo, "config.md")
    (repo / "config.md").write_text("key = (removed, but not re-staged)\n")
    result = check(repo)
    assert result.returncode == 1
    assert "config.md:1: Google API key" in result.stdout


def test_a_secret_inside_a_compressed_document_is_caught(repo: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", f"<w:t>{FAKE_GOOGLE_KEY}</w:t>" * 50)
    (repo / "report.docx").write_bytes(buffer.getvalue())
    stage(repo, "report.docx")
    result = check(repo)
    assert result.returncode == 1
    assert "report.docx!word/document.xml:1: Google API key" in result.stdout


def test_a_real_key_in_the_env_template_fails(repo: Path) -> None:
    (repo / ".env.example").write_text("GEMINI_API_KEY=not-empty\n")
    stage(repo, ".env.example")
    result = check(repo)
    assert result.returncode == 1
    assert "non-empty GEMINI_API_KEY" in result.stdout
