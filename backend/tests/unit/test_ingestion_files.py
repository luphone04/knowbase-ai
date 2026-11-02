"""Upload validation: extension allow-list, real signatures, encoding, size, names."""

import hashlib

import pytest

from app.ingestion import files
from app.ingestion.files import UploadRejected, sanitize_filename, validate_upload
from tests.fakes.documents import make_docx, make_pdf, make_zip

MB = 1024 * 1024


def reject(name: str, data: bytes, max_bytes: int = 10 * MB) -> UploadRejected:
    with pytest.raises(UploadRejected) as excinfo:
        validate_upload(name, data, max_bytes=max_bytes)
    return excinfo.value


@pytest.mark.parametrize(
    ("name", "data", "file_type"),
    [
        ("policy.pdf", make_pdf([["Returns within 30 days."]]), "pdf"),
        ("guide.docx", make_docx([("paragraph", "Hello")]), "docx"),
        ("notes.txt", b"Plain text notes.", "txt"),
        ("readme.md", b"# Title\n\nBody", "md"),
        ("UPPER.PDF", make_pdf([["x"]]), "pdf"),  # extension check is case-insensitive
    ],
)
def test_supported_files_are_accepted(name: str, data: bytes, file_type: str) -> None:
    upload = validate_upload(name, data, max_bytes=10 * MB)
    assert upload.file_type == file_type
    assert upload.original_filename == name
    assert upload.sha256 == hashlib.sha256(data).hexdigest()
    assert upload.size == len(data)


@pytest.mark.parametrize(
    "name", ["script.py", "page.html", "archive.zip", "old.doc", "noext", "x.pdf.exe"]
)
def test_unsupported_types_are_rejected(name: str) -> None:
    error = reject(name, b"content")
    assert (error.status_code, error.code) == (415, "unsupported_file_type")


def test_empty_file_is_rejected() -> None:
    assert reject("empty.txt", b"").code == "empty_file"


def test_oversized_file_is_rejected() -> None:
    error = reject("big.txt", b"a" * (MB + 1), max_bytes=MB)
    assert (error.status_code, error.code) == (413, "file_too_large")


def test_file_at_exactly_the_limit_is_accepted() -> None:
    assert validate_upload("ok.txt", b"a" * MB, max_bytes=MB).size == MB


# --- signature / content checks --------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("fake.pdf", b"This is just text pretending to be a PDF."),
        ("fake.docx", b"plain text, not a zip"),
        ("fake.docx", make_zip({"hello.txt": b"zip but not word"})),
        (
            "legacy.docx",
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100,
        ),  # OLE / .doc / encrypted
        ("pdf-as.txt", make_pdf([["x"]])),
        ("zip-as.md", make_zip({"a": b"b"})),
        ("binary.txt", b"text with a \x00 byte"),
        ("elf.txt", b"\x7fELF\x02\x01\x01"),
    ],
)
def test_content_must_match_the_extension(name: str, data: bytes) -> None:
    error = reject(name, data)
    assert error.status_code == 422
    assert error.code in ("file_content_mismatch", "invalid_text_encoding")


def test_macro_enabled_word_files_are_rejected() -> None:
    data = make_zip(
        {
            "[Content_Types].xml": b"application/vnd.ms-word.document.macroEnabled.main+xml",
            "word/document.xml": b"<w:document/>",
        }
    )
    assert "macro" in reject("macro.docx", data).message


def test_docx_zip_bomb_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files, "MAX_DOCX_UNCOMPRESSED_BYTES", 1000)
    data = make_docx([("paragraph", "x" * 5000)])
    assert "expands to more than" in reject("bomb.docx", data).message


def test_invalid_utf8_is_rejected_with_position() -> None:
    error = reject("latin1.txt", "café".encode("latin-1"))
    assert error.code == "invalid_text_encoding"
    assert "position 3" in error.message


def test_utf8_bom_is_accepted() -> None:
    assert validate_upload("bom.md", "﻿# Title".encode(), max_bytes=MB).file_type == "md"


def test_pdf_header_may_follow_leading_bytes() -> None:
    assert validate_upload("lead.pdf", b"\n\n" + make_pdf([["x"]]), max_bytes=MB).file_type == "pdf"


# --- file names ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd.txt", "passwd.txt"),
        ("..\\..\\windows\\win.ini.txt", "win.ini.txt"),
        ("/absolute/path/report.pdf", "report.pdf"),
        ("report‮fdp.txt", "reportfdp.txt"),  # right-to-left override stripped
        ("tab\tand\nnewline.md", "tabandnewline.md"),
        ("  spaced.txt  ", "spaced.txt"),
    ],
)
def test_filenames_are_reduced_to_a_safe_display_name(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", ".", "..", "../", "dir/"])
def test_names_without_a_file_part_are_rejected(raw: str | None) -> None:
    with pytest.raises(UploadRejected):
        sanitize_filename(raw)


def test_very_long_names_keep_their_extension() -> None:
    name = sanitize_filename("a" * 500 + ".pdf")
    assert len(name) == files.MAX_FILENAME_LENGTH and name.endswith(".pdf")
