import os
import uuid
from pathlib import Path

import pytest

from app.ingestion.storage import FileStorage, InvalidStorageKey, StoredFileMissing

WORKSPACE, SOURCE = uuid.uuid4(), uuid.uuid4()


@pytest.fixture
def storage(tmp_path: Path) -> FileStorage:
    return FileStorage(tmp_path / "uploads")


def test_keys_are_server_generated_uuids(storage: FileStorage) -> None:
    key = storage.new_key(WORKSPACE, SOURCE, "pdf")
    workspace, source, name = key.split("/")
    assert (workspace, source) == (str(WORKSPACE), str(SOURCE))
    assert len(name) == len("0" * 32 + ".pdf") and name.endswith(".pdf")
    assert storage.new_key(WORKSPACE, SOURCE, "pdf") != key


def test_save_read_and_delete_round_trip(storage: FileStorage) -> None:
    key = storage.new_key(WORKSPACE, SOURCE, "txt")
    storage.save(key, b"hello")
    assert storage.read(key) == b"hello"
    path = storage.path_for(key)
    assert path.is_relative_to(storage.root)
    assert oct(path.stat().st_mode & 0o777) == oct(0o640)  # not world-readable, not executable
    assert not any(p.name.endswith(".partial") for p in path.parent.iterdir())
    storage.delete(key)
    with pytest.raises(StoredFileMissing):
        storage.read(key)


@pytest.mark.parametrize(
    "key",
    [
        "../../etc/passwd",
        f"{WORKSPACE}/{SOURCE}/../../../etc/passwd",
        f"{WORKSPACE}/{SOURCE}/report.pdf",  # a user-style name is never a valid key
        f"{WORKSPACE}/{SOURCE}/{'a' * 32}.exe",
        f"/{WORKSPACE}/{SOURCE}/{'a' * 32}.pdf",
        f"{WORKSPACE}/{'a' * 32}.pdf",
        "",
    ],
)
def test_malformed_or_escaping_keys_are_refused(storage: FileStorage, key: str) -> None:
    with pytest.raises(InvalidStorageKey):
        storage.path_for(key)


def test_symlink_escape_is_refused(storage: FileStorage, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (storage.root / str(WORKSPACE)).mkdir(parents=True)
    os.symlink(outside, storage.root / str(WORKSPACE) / str(SOURCE))
    with pytest.raises(InvalidStorageKey):
        storage.path_for(f"{WORKSPACE}/{SOURCE}/{'a' * 32}.pdf")


def test_delete_others_keeps_only_listed_keys(storage: FileStorage) -> None:
    keys = [storage.new_key(WORKSPACE, SOURCE, "txt") for _ in range(3)]
    for key in keys:
        storage.save(key, b"x")
    assert storage.delete_others(WORKSPACE, SOURCE, {keys[1]}) == 2
    assert [storage.exists(k) for k in keys] == [False, True, False]


def test_delete_source_removes_its_directory_only(storage: FileStorage) -> None:
    mine = storage.new_key(WORKSPACE, SOURCE, "txt")
    other = storage.new_key(WORKSPACE, uuid.uuid4(), "txt")
    storage.save(mine, b"x")
    storage.save(other, b"y")
    storage.delete_source(WORKSPACE, SOURCE)
    assert not storage.exists(mine) and storage.exists(other)
