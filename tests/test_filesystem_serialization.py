import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import organize_images as app
from persistence import Persistence, DATABASE_NAME
from test_output import add_file, run_cli
from test_move_recovery import interrupt


def test_large_identities_cache_and_intents(tmp_path):
    store = Persistence(str(tmp_path))
    device, inode = 2**80 + 7, 2**64 - 1
    signature = (device, inode, 10, 20, 30)
    digest = 'a' * 64
    path = str(tmp_path / 'source')
    store.save_cached_hash(path, signature, digest)
    assert store.cached_hash(path, signature) == digest
    assert store.cached_hash(path, (device, inode + 1, 10, 20, 30)) is None
    store.save_intent(path, digest, 'needs-review/file', 'review', device, inode)
    assert store.records()[path]['identity'] == [device, inode]
    identity = SimpleNamespace(st_dev=device, st_ino=inode)
    store.save_move_intent(path, digest, 'needs-review/file', 'review', identity)
    assert store.moves()[path][3:5] == (device, inode)
    for table in ('file_cache', 'copy_intents', 'move_intents'):
        assert store.connection.execute(f'SELECT device, inode FROM {table}').fetchone() == (
            'id:' + str(device), 'id:' + str(inode))
    store.close()


def test_large_identity_recovery_verifies_destination(tmp_path):
    output = tmp_path / 'output'
    destination = add_file(output / 'needs-review' / 'file', b'contents')
    source = str(tmp_path / 'missing')
    digest = app.calculate_hash(destination)
    device, inode = 2**90 + 3, 2**80 + 5
    store = Persistence(str(output))
    identity = SimpleNamespace(st_dev=device, st_ino=inode)
    store.save_intent(source, digest, 'needs-review/file', 'review', device, inode)
    store.save_move_intent(source, digest, 'needs-review/file', 'review', identity)
    real_stat = os.stat

    def destination_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        expected = real_stat(destination, follow_symlinks=False)
        if (result.st_dev, result.st_ino) == (expected.st_dev, expected.st_ino):
            return SimpleNamespace(st_dev=device, st_ino=inode, st_mode=result.st_mode)
        return result

    with patch.object(app.os, 'stat', side_effect=destination_stat):
        previous = app.CopyRecords(str(output), store).previous(source, digest)
        assert previous is not None
        previous_path, previous_category = previous
        assert previous_category == 'review'
        # The record path is rebuilt with os.path.join while the fixture path
        # comes from pathlib; Windows may spell equivalent paths differently.
        # samefile verifies both resolve to the same filesystem entry.
        assert os.path.samefile(previous_path, destination)
        app.MoveRecords(str(output), store).recover(False)
    assert store.moves()[source][-1] == 'complete'
    assert digest in store.hashes()
    assert destination.read_bytes() == b'contents'
    store.close()


def test_legacy_text_keys_integer_identities_are_updated_in_place(tmp_path):
    path = str(tmp_path / 'source')
    digest = 'a' * 64
    store = Persistence(str(tmp_path))
    db = store.connection
    db.execute('INSERT INTO file_cache VALUES (?, ?, ?, ?, ?, ?, ?)', (path, 3, 4, 5, 6, 7, digest))
    db.execute('INSERT INTO copy_intents VALUES (?, ?, ?, ?, ?, ?)', (path, digest, 'needs-review/file', 'review', 3, 4))
    db.execute('INSERT INTO move_intents VALUES (?, ?, ?, ?, ?, ?, ?)', (path, digest, 'needs-review/file', 'review', 3, 4, 'pending'))
    store.close()
    before = (tmp_path / DATABASE_NAME).read_bytes()
    store = Persistence(str(tmp_path), dry_run=True)
    assert store.cached_hash(path, (3, 4, 5, 6, 7)) == digest
    assert store.records()[path]['identity'] == [3, 4]
    assert store.moves()[path][3:5] == (3, 4)
    store.close()
    assert (tmp_path / DATABASE_NAME).read_bytes() == before
    store = Persistence(str(tmp_path))
    store.save_cached_hash(path, (3, 4, 5, 6, 7), digest)
    store.save_intent(path, digest, 'needs-review/new', 'review', 3, 4)
    store.finish_move(path, digest)
    for table in ('file_cache', 'copy_intents', 'move_intents'):
        assert store.connection.execute(f'SELECT count(*) FROM {table}').fetchone() == (1,)
    store.close()


def test_unicode_paths_round_trip_through_sqlite(tmp_path):
    source = str(tmp_path / '写真' / 'café.jpg')
    destination = 'organized-images/夏.jpg'
    store = Persistence(str(tmp_path))
    signature = (11, 22, 33, 44, 55)
    digest = 'c' * 64
    store.save_cached_hash(source, signature, digest)
    store.save_intent(source, digest, destination, 'organized', 11, 22)
    store.save_move_intent(source, digest, destination, 'organized',
                           SimpleNamespace(st_dev=11, st_ino=22))
    assert store.cached_hash(source, signature) == digest
    assert store.records()[source]['destination'] == destination
    assert store.moves()[source][1] == destination
    store.close()


def byte_paths(tmp_path):
    try:
        source = tmp_path / os.fsdecode(b'source-\xff')
        output = tmp_path / os.fsdecode(b'output-\xfe')
        path = source / os.fsdecode(b'photo-\xfd.jpg')
        add_file(path, b'contents')
        output.mkdir()
    except (OSError, UnicodeError):
        pytest.skip('Filesystem cannot create non-UTF-8 names')
    return source, output, path


def test_surrogateescaped_paths_bind_losslessly_without_filesystem_support(tmp_path):
    try:
        source = os.fsdecode(b'path-\xff')
        destination = os.fsdecode(b'needs-review/destination-\xfe.jpg')
    except UnicodeError:
        pytest.skip('Windows paths use Unicode and do not support POSIX surrogateescape names')
    source = str(tmp_path / source)
    store = Persistence(str(tmp_path))
    signature = (2**80, 2**90, 1, 2, 3)
    digest = 'a' * 64
    store.save_cached_hash(source, signature, digest)
    store.save_cached_hash(source, signature, 'b' * 64)
    assert store.cached_hash(source, signature) == 'b' * 64
    store.save_intent(source, digest, destination, 'review', *signature[:2])
    assert store.records()[source]['destination'] == destination
    store.save_move_intent(source, digest, destination, 'review',
                           SimpleNamespace(st_dev=signature[0], st_ino=signature[1]))
    assert store.moves()[source][1] == destination
    store.finish_move(source, digest)
    assert store.moves()[source][-1] == 'complete'
    assert store.connection.execute('SELECT source, destination FROM copy_intents').fetchone() == (
        os.fsencode(source), os.fsencode(destination))
    assert store.connection.execute('SELECT count(*) FROM file_cache').fetchone() == (1,)
    store.close()
    before = (tmp_path / DATABASE_NAME).read_bytes()
    store = Persistence(str(tmp_path), dry_run=True)
    assert store.cached_hash(source, signature) == 'b' * 64
    assert store.records()[source]['destination'] == destination
    store.close()
    assert (tmp_path / DATABASE_NAME).read_bytes() == before


def test_non_utf8_paths_cache_copy_and_dry_run(tmp_path):
    source, output, path = byte_paths(tmp_path)
    run_cli(source, '--copy', '--output', output)
    store = Persistence(str(output), dry_run=True)
    assert store.cached_hash(path, app.file_signature(path)) == app.calculate_hash(path)
    assert str(path) in store.records()
    store.close()
    before = (output / DATABASE_NAME).read_bytes()
    run_cli(source, '--copy', '--output', output, '--dry-run')
    assert (output / DATABASE_NAME).read_bytes() == before
    result = run_cli(source, '--copy', '--output', output)
    assert 'ALREADY COPIED' in result.stderr
    assert path.read_bytes() == b'contents'
    assert len(list((output / 'organized-images').rglob('*.jpg'))) == 1


def test_non_utf8_move_recovery(tmp_path):
    source, output, path = byte_paths(tmp_path)
    interrupt(source, output, 'after_unlink')
    assert not path.exists()
    incoming = add_file(source / os.fsdecode(b'incoming-\xfc.jpg'), b'contents')
    result = run_cli(source, '--copy', '--output', output)
    assert 'Duplicates:   1' in result.stdout
    assert incoming.read_bytes() == b'contents'
    store = Persistence(str(output), dry_run=True)
    assert store.moves()[str(path)][-1] == 'complete'
    store.close()
    assert next((output / 'organized-images').rglob('*.jpg')).read_bytes() == b'contents'
