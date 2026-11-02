"""Upload validation: what is accepted before anything is stored (SPEC.md §7.2).

The file's extension decides which format is expected, and the bytes must then
prove it: a PDF signature, a real Word document ZIP, or strict UTF-8 text.
The browser's MIME type is never trusted. Deep parsing of PDF and DOCX happens
in the worker, so a damaged file fails there with a clear job error, not here.
"""

import hashlib
import io
import unicodedata
import zipfile
from dataclasses import dataclass

from app.core.errors import AppError

FILE_TYPES_BY_EXTENSION = {".pdf": "pdf", ".docx": "docx", ".txt": "txt", ".md": "md"}
MAX_FILENAME_LENGTH = 200
# Zip-bomb guards for DOCX: limits on the unpacked size and entry count, not the upload size.
MAX_DOCX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_DOCX_ENTRIES = 5000

_BINARY_SIGNATURES = (b"%PDF-", b"PK\x03\x04", b"\xd0\xcf\x11\xe0", b"\x7fELF", b"MZ")


class UploadRejected(AppError):
    pass


def _reject(status: int, code: str, message: str) -> UploadRejected:
    return UploadRejected(status, code, message)


@dataclass(frozen=True)
class ValidatedUpload:
    original_filename: str
    file_type: str
    data: bytes
    sha256: str

    @property
    def size(self) -> int:
        return len(self.data)


def sanitize_filename(raw: str | None) -> str:
    """A display name only: never used as a filesystem path.

    Any directory part the client sent is dropped ("../../etc/passwd.txt"
    becomes "passwd.txt"), as are control and bidirectional-override
    characters.
    """
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = unicodedata.normalize("NFC", name)
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C").strip()
    if name in ("", ".", ".."):
        raise _reject(422, "invalid_filename", "The file needs a name.")
    if len(name) > MAX_FILENAME_LENGTH:
        stem, dot, extension = name.rpartition(".")
        keep = MAX_FILENAME_LENGTH - len(extension) - 1
        name = f"{stem[:keep]}.{extension}" if dot and keep > 0 else name[:MAX_FILENAME_LENGTH]
    return name


def file_type_for(filename: str) -> str:
    extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    file_type = FILE_TYPES_BY_EXTENSION.get(extension)
    if file_type is None:
        raise _reject(
            415,
            "unsupported_file_type",
            "Only PDF (.pdf), Word (.docx), text (.txt), and Markdown (.md) files are supported.",
        )
    return file_type


def validate_upload(raw_filename: str | None, data: bytes, *, max_bytes: int) -> ValidatedUpload:
    filename = sanitize_filename(raw_filename)
    file_type = file_type_for(filename)
    if not data:
        raise _reject(422, "empty_file", "The file is empty.")
    if len(data) > max_bytes:
        raise _reject(
            413,
            "file_too_large",
            f"The file is larger than the {max_bytes // (1024 * 1024)} MB limit.",
        )
    _check_content(file_type, data)
    return ValidatedUpload(
        original_filename=filename,
        file_type=file_type,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _mismatch(file_type: str, detail: str) -> UploadRejected:
    return _reject(422, "file_content_mismatch", f"This is not a valid .{file_type} file: {detail}")


def _check_content(file_type: str, data: bytes) -> None:
    if file_type == "pdf":
        if b"%PDF-" not in data[:1024]:
            raise _mismatch("pdf", "it has no PDF header.")
    elif file_type == "docx":
        _check_docx(data)
    else:
        _check_text(file_type, data)


def _check_docx(data: bytes) -> None:
    if data.startswith(b"\xd0\xcf\x11\xe0"):
        raise _mismatch("docx", "legacy .doc and password-protected Word files are not supported.")
    if not data.startswith(b"PK\x03\x04"):
        raise _mismatch("docx", "it is not a Word document.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_DOCX_ENTRIES:
                raise _mismatch("docx", "it contains too many parts.")
            if sum(entry.file_size for entry in entries) > MAX_DOCX_UNCOMPRESSED_BYTES:
                raise _mismatch("docx", "it expands to more than 100 MB.")
            names = {entry.filename for entry in entries}
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise _mismatch("docx", "it is a ZIP file but not a Word document.")
            content_types = archive.read("[Content_Types].xml")
    except zipfile.BadZipFile as exc:
        raise _mismatch("docx", "the file is damaged.") from exc
    if b"macroEnabled" in content_types:
        raise _mismatch("docx", "macro-enabled documents are not accepted.")
    if b"wordprocessingml.document.main+xml" not in content_types:
        raise _mismatch("docx", "it is not a Word document.")


def _check_text(file_type: str, data: bytes) -> None:
    if data.startswith(_BINARY_SIGNATURES) or b"\x00" in data:
        raise _mismatch(file_type, "it contains binary data.")
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _reject(
            422,
            "invalid_text_encoding",
            f"The file is not valid UTF-8 text (invalid byte at position {exc.start}).",
        ) from exc
