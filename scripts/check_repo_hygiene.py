"""Fail if secrets or local data are in the git index (tracked or staged).

Runs locally before every push and in CI, through check_repo_hygiene.sh.

- Everything is read from the index, which is exactly what a commit contains,
  not from the working tree.
- File names come NUL-separated (`git ls-files -z`), so quotes, spaces, or
  newlines in a name can't hide a file.
- Binary files are scanned as bytes, and zip-based files (.docx, .xlsx, .zip)
  are opened and their members scanned too.
- Findings name the file, line, and kind of secret, never the matched value.

Standard library only, Python 3.9+.
"""

import io
import re
import subprocess
import sys
import zipfile

FORBIDDEN_PATH = re.compile(
    r"(^|/)\.env(\..+)?$"
    r"|(^|/)(node_modules|\.venv[^/]*|venv|uploads|postgres_data|\.next)/"
    r"|\.(pem|key|p12|sqlite3|dump|log|jar)$"  # .jar: curl/httpie session cookie jars
    r"|(^|/)cookies?[^/]*\.txt$"
)
ALLOWED_PATH = re.compile(r"(^|/)\.env\.example$")

SECRETS = {
    "Google API key": rb"AIza[0-9A-Za-z_-]{35}|AQ\.[0-9A-Za-z_-]{40,}",
    "GitHub token": rb"gh[pousr]_[A-Za-z0-9]{36,}",
    "private key": rb"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "session token (JWT)": rb"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    "cookie jar": rb"# (Netscape )?HTTP Cookie File",
    "secret in a NEXT_PUBLIC_* variable": rb"NEXT_PUBLIC_[A-Z_]*(GEMINI|SECRET|API_KEY)",
}
PATTERNS = [(kind, re.compile(pattern)) for kind, pattern in SECRETS.items()]
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024  # scan at most this much unpacked data per archive


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def index_files() -> list[str]:
    """Regular files and executables in the index (not symlinks or submodules)."""
    entries = git("ls-files", "--cached", "--stage", "-z").split(b"\0")
    files = []
    for entry in entries:
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        mode = meta.split(b" ")[0]
        if mode in (b"100644", b"100755"):
            files.append(path.decode("utf-8", "surrogateescape"))
    return files


def staged_content(path: str) -> bytes:
    return git("cat-file", "blob", f":{path}")


def findings(label: str, data: bytes) -> list[str]:
    found = []
    for kind, pattern in PATTERNS:
        for match in pattern.finditer(data):
            line = data.count(b"\n", 0, match.start()) + 1
            found.append(f"{label}:{line}: {kind}")
    if data.startswith(b"PK\x03\x04"):
        found += archive_findings(label, data)
    return found


def archive_findings(label: str, data: bytes) -> list[str]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return []
    found, budget = [], MAX_ARCHIVE_BYTES
    for member in archive.infolist():
        if member.is_dir():
            continue
        if member.file_size > budget:
            found.append(f"{label}!{member.filename}: too large to scan; check it by hand")
            break
        budget -= member.file_size
        try:
            content = archive.read(member)
        except (RuntimeError, NotImplementedError, zipfile.BadZipFile):
            found.append(f"{label}!{member.filename}: encrypted or unreadable; check it by hand")
            continue
        found += findings(f"{label}!{member.filename}", content)
    return found


def main() -> int:
    files = index_files()
    failed = False

    forbidden = [f for f in files if FORBIDDEN_PATH.search(f) and not ALLOWED_PATH.search(f)]
    if forbidden:
        print("FAIL: forbidden files are tracked or staged:")
        for path in forbidden:
            print(f"  {path!r}")
        failed = True

    secrets = [hit for path in files for hit in findings(path, staged_content(path))]
    if secrets:
        print("FAIL: secret-looking content found (values not shown):")
        for hit in secrets:
            print(f"  {hit}")
        failed = True

    # The committed template must not carry a real key.
    if ".env.example" in files and re.search(
        rb"^GEMINI_API_KEY=.+", staged_content(".env.example"), re.MULTILINE
    ):
        print("FAIL: .env.example has a non-empty GEMINI_API_KEY.")
        failed = True

    if failed:
        return 1
    print("OK: no secrets or local data tracked or staged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
