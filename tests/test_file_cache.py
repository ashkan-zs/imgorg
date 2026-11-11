import os
import sqlite3
from unittest.mock import patch

import pytest

import organize_images as app
from persistence import DATABASE_NAME, Persistence


def signature(path):
    status = os.stat(path)
    return status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns


def test_cache_first_hash_then_reuses_without_reading(tmp_path):
    path = tmp_path / 'photo.bin'
    path.write_bytes(b'first bytes')
    store = Persistence(str(tmp_path))
    with patch.object(app, 'calculate_hash', wraps=app.calculate_hash) as hasher:
        first = app.calculate_cached_hash(path, store)
        assert hasher.call_count == 1
        assert app.calculate_cached_hash(path, store) == first
        assert hasher.call_count == 1
    assert store.connection.execute('SELECT sha256 FROM file_cache').fetchone() == (first,)
    store.close()


@pytest.mark.parametrize('change', ['size', 'mtime', 'replace'])
def test_cache_invalidates_changed_file_signature(tmp_path, change):
    path = tmp_path / 'photo.bin'
    path.write_bytes(b'first')
    store = Persistence(str(tmp_path))
    old = app.calculate_cached_hash(path, store)
    if change == 'size':
        path.write_bytes(b'changed size')
    elif change == 'mtime':
        path.write_bytes(b'other')
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 2_000_000_000))
    else:
        replacement = tmp_path / 'replacement'
        replacement.write_bytes(b'other')
        os.replace(replacement, path)
    with patch.object(app, 'calculate_hash', wraps=app.calculate_hash) as hasher:
        new = app.calculate_cached_hash(path, store)
    assert hasher.call_count == 1
    assert new == app.calculate_hash(path)
    if change != 'mtime':
        assert new != old
    store.close()


def test_same_size_timestamp_change_invalidates_and_rename_is_new_key(tmp_path):
    path = tmp_path / 'a.bin'
    path.write_bytes(b'first')
    store = Persistence(str(tmp_path))
    first = app.calculate_cached_hash(path, store)
    old = path.stat()
    path.write_bytes(b'other')
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 3_000_000_000))
    with patch.object(app, 'calculate_hash', wraps=app.calculate_hash) as hasher:
        changed = app.calculate_cached_hash(path, store)
        assert hasher.call_count == 1
        path.rename(tmp_path / 'b.bin')
        app.calculate_cached_hash(tmp_path / 'b.bin', store)
        assert hasher.call_count == 2
    assert changed != first
    store.close()


def test_device_or_inode_mismatch_invalidates_cache_signature(tmp_path):
    path = tmp_path / 'photo.bin'
    path.write_bytes(b'bytes')
    store = Persistence(str(tmp_path))
    digest = app.calculate_cached_hash(path, store)
    current = signature(path)
    assert store.cached_hash(path, current) == digest
    assert store.cached_hash(path, (current[0] + 1, *current[1:])) is None
    assert store.cached_hash(path, (current[0], current[1] + 1, *current[2:])) is None
    store.close()


def test_changed_during_hash_is_neither_used_nor_cached(tmp_path):
    path = tmp_path / 'photo.bin'
    path.write_bytes(b'before')
    store = Persistence(str(tmp_path))
    original = app.calculate_hash

    def mutate_during_hash(target):
        digest = original(target)
        path.write_bytes(b'after!')
        return digest

    with patch.object(app, 'calculate_hash', side_effect=mutate_during_hash):
        with pytest.raises(OSError, match='changed while hashing'):
            app.calculate_cached_hash(path, store)
    assert store.connection.execute('SELECT count(*) FROM file_cache').fetchone() == (0,)
    store.close()


def test_dry_run_reads_existing_cache_but_does_not_write(tmp_path):
    path = tmp_path / 'photo.bin'
    path.write_bytes(b'existing')
    store = Persistence(str(tmp_path))
    app.calculate_cached_hash(path, store)
    store.close()
    before = (tmp_path / DATABASE_NAME).read_bytes()
    store = Persistence(str(tmp_path), dry_run=True)
    with patch.object(app, 'calculate_hash', wraps=app.calculate_hash) as hasher:
        app.calculate_cached_hash(path, store, dry_run=True)
        assert hasher.call_count == 0
        path.write_bytes(b'changed')
        app.calculate_cached_hash(path, store, dry_run=True)
        assert hasher.call_count == 1
    store.close()
    assert (tmp_path / DATABASE_NAME).read_bytes() == before


def test_dry_run_without_database_does_not_create_one(tmp_path):
    path = tmp_path / 'photo.bin'
    path.write_bytes(b'bytes')
    store = Persistence(str(tmp_path), dry_run=True)
    assert app.calculate_cached_hash(path, store, dry_run=True) == app.calculate_hash(path)
    assert not (tmp_path / DATABASE_NAME).exists()


def test_existing_database_gets_cache_table_without_losing_rows(tmp_path):
    db = tmp_path / DATABASE_NAME
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE exact_hashes (digest TEXT PRIMARY KEY NOT NULL CHECK(length(digest)=64))')
        connection.execute('INSERT INTO exact_hashes VALUES (?)', ('a' * 64,))
    store = Persistence(str(tmp_path))
    assert store.hashes() == {'a' * 64}
    assert store.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='file_cache'").fetchone()
    store.close()


def test_legacy_database_dry_run_treats_missing_cache_table_as_empty(tmp_path):
    db = tmp_path / DATABASE_NAME
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE exact_hashes (digest TEXT PRIMARY KEY NOT NULL CHECK(length(digest)=64))')
        connection.execute('''CREATE TABLE copy_intents (
            source TEXT PRIMARY KEY NOT NULL, digest TEXT NOT NULL, destination TEXT NOT NULL,
            category TEXT NOT NULL, device INTEGER, inode INTEGER)''')
        connection.execute('''CREATE TABLE move_intents (
            source TEXT PRIMARY KEY NOT NULL, digest TEXT NOT NULL, destination TEXT NOT NULL,
            category TEXT NOT NULL, device INTEGER NOT NULL, inode INTEGER NOT NULL, state TEXT NOT NULL)''')
        connection.execute('INSERT INTO exact_hashes VALUES (?)', ('a' * 64,))
    path = tmp_path / 'photo.bin'
    path.write_bytes(b'legacy cache miss')
    original_db = db.read_bytes()
    store = Persistence(str(tmp_path), dry_run=True)
    with patch.object(app, 'calculate_hash', wraps=app.calculate_hash) as hasher:
        digest = app.calculate_cached_hash(path, store, dry_run=True)
        assert hasher.call_count == 1
    store.close()
    assert digest == app.calculate_hash(path)
    assert db.read_bytes() == original_db
    assert not any((tmp_path / (DATABASE_NAME + suffix)).exists()
                   for suffix in ('-journal', '-wal', '-shm'))


def test_invalid_cached_digest_is_recalculated_and_replaced_on_write(tmp_path):
    path = tmp_path / 'photo.bin'
    path.write_bytes(b'correct contents')
    store = Persistence(str(tmp_path))
    signature_now = signature(path)
    store.save_cached_hash(path, signature_now, 'z' * 64)
    with patch.object(app, 'calculate_hash', wraps=app.calculate_hash) as hasher:
        digest = app.calculate_cached_hash(path, store)
        assert hasher.call_count == 1
    assert digest == app.calculate_hash(path)
    assert store.cached_hash(path, signature_now) == digest
    store.close()


def test_invalid_cached_digest_is_recalculated_without_dry_run_write(tmp_path):
    path = tmp_path / 'photo.bin'
    path.write_bytes(b'correct contents')
    store = Persistence(str(tmp_path))
    store.save_cached_hash(path, signature(path), 'z' * 64)
    store.close()
    before = (tmp_path / DATABASE_NAME).read_bytes()
    store = Persistence(str(tmp_path), dry_run=True)
    with patch.object(app, 'calculate_hash', wraps=app.calculate_hash) as hasher:
        digest = app.calculate_cached_hash(path, store, dry_run=True)
        assert hasher.call_count == 1
    store.close()
    assert digest == app.calculate_hash(path)
    assert (tmp_path / DATABASE_NAME).read_bytes() == before


def test_cache_alias_lookup_uses_directory_entry_identity(tmp_path):
    path = tmp_path / 'photo.bin'
    path.write_bytes(b'bytes')
    alias = tmp_path / 'alias.bin'
    try:
        alias.symlink_to(path)
    except OSError:
        pytest.skip('Symlinks unavailable')
    store = Persistence(str(tmp_path))
    digest = app.calculate_cached_hash(path, store)
    assert store.cached_hash(alias, signature(alias)) == digest
    store.close()


def test_deleted_file_does_not_use_stale_cache(tmp_path):
    path = tmp_path / 'photo.bin'
    path.write_bytes(b'bytes')
    store = Persistence(str(tmp_path))
    app.calculate_cached_hash(path, store)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        app.calculate_cached_hash(path, store)
    store.close()
