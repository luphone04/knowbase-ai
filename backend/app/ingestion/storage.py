"""Uploaded files on the shared `uploads_data` volume (api and worker mount it at UPLOAD_DIR).

Storage keys are generated here, from UUIDs only:
`<workspace_id>/<source_id>/<random hex>.<type>`. The uploader's file name
never touches the filesystem. Every key is checked against that exact pattern,
and its resolved path must stay inside the upload root. Keys and paths are
internal and never returned by the API.
"""

import logging
import os
import re
import secrets
import shutil
import uuid
from pathlib import Path

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_KEY_PATTERN = re.compile(rf"^{_UUID}/{_UUID}/[0-9a-f]{{32}}\.(pdf|docx|txt|md)$")


logger = logging.getLogger(__name__)


class InvalidStorageKey(ValueError):
    pass


class StoredFileMissing(FileNotFoundError):
    pass


class FileStorage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    @staticmethod
    def new_key(workspace_id: uuid.UUID, source_id: uuid.UUID, file_type: str) -> str:
        return f"{workspace_id}/{source_id}/{secrets.token_hex(16)}.{file_type}"

    def path_for(self, key: str) -> Path:
        if not _KEY_PATTERN.fullmatch(key):
            raise InvalidStorageKey("malformed storage key")
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):  # defence in depth; the pattern already forbids it
            raise InvalidStorageKey("storage key escapes the upload root")
        return path

    def _source_dir(self, workspace_id: uuid.UUID, source_id: uuid.UUID) -> Path:
        path = (self.root / str(workspace_id) / str(source_id)).resolve()
        if not path.is_relative_to(self.root):
            raise InvalidStorageKey("source directory escapes the upload root")
        return path

    def save(self, key: str, data: bytes) -> None:
        """Write atomically: a crash leaves either no file or the complete file."""
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        temporary = path.with_name(f".{secrets.token_hex(8)}.partial")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def read(self, key: str) -> bytes:
        try:
            return self.path_for(key).read_bytes()
        except FileNotFoundError as exc:
            raise StoredFileMissing(key) from exc

    def exists(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def delete(self, key: str) -> None:
        self.path_for(key).unlink(missing_ok=True)

    def delete_keys(self, keys: set[str]) -> int:
        """Remove exactly these stored files (each key validated). Returns how many existed.

        Callers pass only keys known to be superseded. Nothing else in the folder is
        touched, so an upload still being written (or not yet committed) is safe.
        """
        removed = 0
        for key in keys:
            path = self.path_for(key)
            if path.is_file():
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def delete_source(self, workspace_id: uuid.UUID, source_id: uuid.UUID) -> bool:
        """Remove the source's folder. False (and logged) if something could not be removed."""
        failures: list[str] = []
        directory = self._source_dir(workspace_id, source_id)
        if directory.exists():
            shutil.rmtree(directory, onexc=lambda _f, path, exc: failures.append(f"{path}: {exc}"))
        for failure in failures:
            logger.error("Could not remove stored upload %s", failure)
        return not failures
