import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import organize_images as organizer
from persistence import DATABASE_FILES, DATABASE_NAME, Persistence
from test_output import add_file, run_cli, snapshot_files


def test_store_round_trip_and_uniqueness(tmp_path):
    digest = 'a' * 64
    store = Persistence(str(tmp_path))
    assert store.hashes() == set()
    store.close()
    store = Persistence(str(tmp_path))
    assert store.hashes() == set()
    store.add_hashes([digest, digest])
    with pytest.raises(sqlite3.IntegrityError):
        store.add_hashes(['invalid'])
    assert store.hashes() == {digest}
    store.save_intent('/source', digest, 'organized-images/a.jpg', 'organized')
    assert store.records()['/source']['identity'] is None
    store.save_intent('/source', digest, 'organized-images/b.jpg', 'organized', 3, 7)
    store.close()
    store = Persistence(str(tmp_path))
    assert store.hashes() == {digest}
    assert store.records()['/source'] == {'hash': digest, 'destination': 'organized-images/b.jpg',
                                           'category': 'organized', 'identity': [3, 7]}
    store.close()


def test_intents_are_committed_before_each_publication_attempt(tmp_path):
    source = add_file(tmp_path / 'source.jpg')
    output = tmp_path / 'output'
    output.mkdir()
    add_file(output / 'source.jpg', b'existing')
    store = Persistence(str(output))
    records = organizer.CopyRecords(str(output), store)
    real_link = os.link
    attempts = []

    def publish(src, dst):
        with sqlite3.connect(output / DATABASE_NAME) as other:
            row = other.execute('SELECT destination, device, inode FROM copy_intents').fetchone()
        assert row == (os.fsencode(os.path.relpath(dst, output)),
                       'id:' + str(os.stat(src).st_dev), 'id:' + str(os.stat(src).st_ino))
        attempts.append(Path(dst).name)
        return real_link(src, dst)

    with patch.object(organizer.os, 'link', side_effect=publish):
        destination = organizer.copy_without_overwriting(
            source, output, organizer.calculate_hash(source),
            lambda dst, identity: records.remember(source, organizer.calculate_hash(source), dst, 'organized', identity))
    store.close()
    assert attempts == ['source.jpg', 'source_1.jpg']
    assert Path(destination).read_bytes() == source.read_bytes()


def test_interrupted_before_publication_retries_without_adopting_existing_file(tmp_path):
    source = tmp_path / 'source'
    original = add_file(source / 'photo.jpg')
    output = tmp_path / 'output'
    script = '''
import os
import organize_images as app
def stop(*args, **kwargs):
    os._exit(77)
app.os.link = stop
app.main()
'''
    result = subprocess.run([sys.executable, '-c', script, '--src', str(source), '--output', str(output), '--copy'])
    assert result.returncode == 77
    with sqlite3.connect(output / DATABASE_NAME) as db:
        relative, = db.execute('SELECT destination FROM copy_intents').fetchone()
    existing = add_file(output / os.fsdecode(relative), original.read_bytes())
    result = run_cli(source, '--copy', '--output', output)
    assert 'ALREADY COPIED' not in result.stderr
    assert existing.read_bytes() == original.read_bytes()
    assert len(list(existing.parent.glob('photo*.jpg'))) == 2


def test_dry_run_absent_and_existing_database_never_writes(tmp_path):
    source = tmp_path / 'source'
    add_file(source / 'photo.jpg')
    output = tmp_path / 'output'
    run_cli(source, '--copy', '--output', output, '--dry-run')
    assert not output.exists()
    run_cli(source, '--copy', '--output', output)
    before = snapshot_files(tmp_path)
    result = run_cli(source, '--copy', '--output', output, '--dry-run')
    assert 'ALREADY COPIED' in result.stderr
    assert snapshot_files(tmp_path) == before
    assert not any((output / name).exists() for name in DATABASE_FILES[1:])


def test_dry_run_refuses_pending_journal_without_modifying_it(tmp_path):
    source = tmp_path / 'source'
    add_file(source / 'photo.jpg')
    output = tmp_path / 'output'
    run_cli(source, '--copy', '--output', output)
    add_file(output / (DATABASE_NAME + '-journal'), b'pending')
    before = snapshot_files(tmp_path)
    result = subprocess.run([sys.executable, str(Path(organizer.__file__)), '--src', str(source),
                             '--output', str(output), '--dry-run'], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'pending journal or WAL' in result.stderr
    assert snapshot_files(tmp_path) == before


@pytest.mark.parametrize('name', DATABASE_FILES)
def test_hardlinked_database_artifact_fails_before_output_mutation(tmp_path, name):
    source = tmp_path / 'source'
    original = add_file(source / 'photo.jpg', b'photo')
    output = tmp_path / 'output'
    output.mkdir()
    external = add_file(tmp_path / 'external', b'')
    os.link(external, output / name)
    before = snapshot_files(tmp_path)

    result = subprocess.run([sys.executable, str(Path(organizer.__file__)), '--src', str(source),
                             '--output', str(output)], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'multiple hard links' in result.stderr
    assert snapshot_files(tmp_path) == before
    assert original.read_bytes() == b'photo'
    assert not (output / 'needs-review').exists()
    assert not (output / 'duplicate-files').exists()


def test_persistence_rejects_hardlinked_database_when_opened_directly(tmp_path):
    external = add_file(tmp_path / 'external', b'')
    output = tmp_path / 'output'
    output.mkdir()
    os.link(external, output / DATABASE_NAME)
    with pytest.raises(ValueError, match='multiple hard links'):
        Persistence(str(output))
    assert external.read_bytes() == b''


def test_database_artifacts_excluded_only_at_output_root(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    for name in DATABASE_FILES:
        add_file(source / name, b'root artifact')
        add_file(source / 'nested' / name, ('nested ' + name).encode())
    # A missing database is needed for a valid dry-run, so use a separate output
    # alias in the scan helper and test the traversal with an existing valid store.
    for name in DATABASE_FILES:
        (source / name).unlink()
    store = Persistence(str(source))
    store.close()
    for name in DATABASE_FILES[1:]:
        add_file(source / name, b'root artifact')
    import similarity
    found = {Path(item['path']) for item in similarity.scan_images(source, output=source)}
    assert found == {source / 'nested' / name for name in DATABASE_FILES}
    for name in DATABASE_FILES[1:]:
        (source / name).unlink()
    result = run_cli(source, '--dry-run')
    assert f'Total:        {len(DATABASE_FILES)}' in result.stdout


def test_corrupt_database_stops_before_processing(tmp_path):
    source = tmp_path / 'source'
    original = add_file(source / 'photo.jpg')
    output = tmp_path / 'output'
    db = add_file(output / DATABASE_NAME, b'not sqlite')
    result = subprocess.run([sys.executable, str(Path(organizer.__file__)), '--src', str(source),
                             '--output', str(output)], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'SQLite persistence failure' in result.stderr
    assert original.exists()
    assert db.read_bytes() == b'not sqlite'


def test_legacy_files_are_untouched_and_not_imported(tmp_path):
    source = tmp_path / 'source'
    original = add_file(source / 'photo.jpg')
    digest = organizer.calculate_hash(original)
    old_hash = add_file(source / '.sha256_hashes.txt', (digest + '\n').encode())
    old_records = add_file(source / '.copy_records.json', b'{invalid json')
    output = tmp_path / 'output'
    output_hash = add_file(output / '.sha256_hashes.txt', (digest + '\n').encode())
    output_records = add_file(output / '.copy_records.json', b'{invalid json')
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in
              (old_hash, old_records, output_hash, output_records)}
    result = run_cli(source, '--copy', '--output', output)
    assert 'Organized:    1' in result.stdout
    assert 'Duplicates:   0' in result.stdout
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == data for p, data in before.items())


def test_intent_database_failure_stops_before_publication(tmp_path, monkeypatch, capsys):
    source = tmp_path / 'source'
    original = add_file(source / 'photo.jpg')
    output = tmp_path / 'output'
    monkeypatch.setattr(sys, 'argv', ['organize_images.py', '--src', str(source), '--output', str(output), '--copy'])
    with patch.object(Persistence, 'save_intent', side_effect=sqlite3.OperationalError('disk full')):
        with pytest.raises(SystemExit) as error:
            organizer.main()
    assert error.value.code == 1
    assert 'disk full' in capsys.readouterr().err
    assert original.exists()
    assert not list((output / 'organized-images').rglob('*.jpg'))
    with sqlite3.connect(output / DATABASE_NAME) as db:
        assert db.execute('SELECT count(*) FROM copy_intents').fetchone() == (0,)
        assert db.execute('SELECT count(*) FROM exact_hashes').fetchone() == (0,)


@pytest.mark.parametrize('mode', [[], ['--copy'], ['--dry-run'],
                                  ['--find-similar'], ['--find-similar-only']])
def test_source_root_database_artifacts_survive_separate_output(tmp_path, mode):
    import similarity

    source = tmp_path / 'source'
    source.mkdir()
    store = Persistence(str(source))
    store.add_hashes(['a' * 64])
    store.close()
    for name in DATABASE_FILES[1:]:
        add_file(source / name, ('source ' + name).encode())
    nested = [add_file(source / 'nested' / name, ('nested ' + name).encode())
              for name in DATABASE_FILES]
    protected = [source / name for name in DATABASE_FILES]
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino) for p in protected}
    output = tmp_path / 'output'

    candidates = {Path(item['path']) for item in similarity.scan_images(source, output=output)}
    assert candidates == set(nested)
    result = run_cli(source, '--output', output, *mode)

    assert all((p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino) == value
               for p, value in before.items())
    assert not any((output / 'needs-review' / name).exists() and
                   (output / 'needs-review' / name).read_bytes() == before[source / name][0]
                   for name in DATABASE_FILES)
    if '--find-similar-only' in mode or '--dry-run' in mode:
        assert all(p.exists() for p in nested)
    else:
        assert 'Needs review: 4' in result.stdout
        for p in nested:
            assert (output / 'needs-review' / p.name).read_bytes() == ('nested ' + p.name).encode()
            assert p.exists() == ('--copy' in mode)


def test_similarity_scanner_excludes_artifacts_at_both_roots(tmp_path):
    import similarity

    source = tmp_path / 'source'
    output = source / 'output'
    for directory in (source, output, source / 'nested'):
        for name in DATABASE_FILES:
            add_file(directory / name, (directory.name + name).encode())
    before = snapshot_files(source)
    candidates = {Path(item['path']) for item in similarity.scan_images(source, output=output)}
    assert candidates == {source / 'nested' / name for name in DATABASE_FILES}
    assert snapshot_files(source) == before
