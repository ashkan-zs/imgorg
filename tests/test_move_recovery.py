import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import organize_images as app
from test_output import add_file, run_cli

SCRIPT = Path(app.__file__)


@pytest.mark.parametrize('alias_type', ['original', 'case', 'symlink'])
@pytest.mark.parametrize('mode', ['move', 'dry-run', 'copy'])
def test_retained_move_source_survives_restart_alias(tmp_path, alias_type, mode):
    source = tmp_path / 'photos'
    original = add_file(source / 'photo.jpg', b'retained contents')
    output = tmp_path / 'output'
    interrupt(source, output, 'after_publish')
    destination = next((output / 'organized-images').rglob('photo.jpg'))
    alias = source
    if alias_type == 'case':
        alias = tmp_path / 'PHOTOS'
        if not alias.exists():
            pytest.skip('Temporary filesystem is case-sensitive')
    elif alias_type == 'symlink':
        alias = tmp_path / 'alias'
        alias.symlink_to(source, target_is_directory=True)
    before = (output / app.DATABASE_NAME).read_bytes()
    args = {'move': [], 'dry-run': ['--dry-run'], 'copy': ['--copy']}[mode]
    run_cli(alias, '--output', output, *args)
    assert original.read_bytes() == b'retained contents'
    assert destination.read_bytes() == b'retained contents'
    if mode != 'copy':
        assert not list((output / 'duplicate-files').rglob('*.jpg'))
    if mode == 'dry-run':
        assert (output / app.DATABASE_NAME).read_bytes() == before
    with sqlite3.connect(output / app.DATABASE_NAME) as db:
        assert db.execute('SELECT state FROM move_intents').fetchone() == ('pending',)


def test_recovered_source_guard_does_not_suppress_distinct_hardlink(tmp_path):
    source = tmp_path / 'photos'
    retained = add_file(source / 'a.jpg', b'contents')
    output = tmp_path / 'output'
    interrupt(source, output, 'after_publish')
    other = source / 'b.jpg'
    os.link(retained, other)
    run_cli(source, '--output', output)
    assert retained.read_bytes() == b'contents'
    assert not other.exists()
    duplicates = list((output / 'duplicate-files').rglob('*.jpg'))
    assert len(duplicates) == 1
    assert duplicates[0].name == 'b.jpg'
    assert duplicates[0].read_bytes() == b'contents'


def interrupt(source, output, point):
    script = '''
import os, sys
import organize_images as app
point = sys.argv.pop()
if point == 'after_intent':
    app.os.link = lambda *args, **kwargs: os._exit(77)
elif point == 'after_publish':
    original = app.os.link
    def publish(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(77)
    app.os.link = publish
elif point == 'after_unlink':
    original = app.os.unlink
    def unlink(path, *args, **kwargs):
        original(path, *args, **kwargs)
        os._exit(77)
    app.os.unlink = unlink
app.main()
'''
    result = subprocess.run([sys.executable, '-c', script, '--src', str(source),
                             '--output', str(output), point], capture_output=True)
    assert result.returncode == 77, result.stderr


@pytest.mark.parametrize('point', ['after_intent', 'after_publish', 'after_unlink'])
def test_interruption_and_recovery_before_new_source(tmp_path, point):
    source = tmp_path / 'source'
    output = tmp_path / 'output'
    original = add_file(source / 'z.jpg', b'same')
    interrupt(source, output, point)
    add_file(source / 'a.jpg', b'same')
    preview = run_cli(source, '--output', output, '--dry-run')
    if point == 'after_intent':
        assert 'Duplicates:   1' in preview.stdout  # both live sources match in memory
    else:
        assert 'Duplicates:   1' in preview.stdout
    result = run_cli(source, '--output', output)
    if point == 'after_intent':
        assert len(list((output / 'organized-images').rglob('*.jpg'))) == 1
    else:
        assert len(list((output / 'organized-images').rglob('*.jpg'))) == 1
        assert (output / 'organized-images').exists()
        assert 'Duplicates:   1' in result.stdout
    if point == 'after_publish':
        assert original.exists()  # Ambiguous source is retained for review.
    with sqlite3.connect(output / app.DATABASE_NAME) as db:
        assert db.execute('SELECT count(*) FROM exact_hashes').fetchone()[0] == 1


def test_copy_mode_recovers_move_unlinked_before_finalization(tmp_path):
    source = tmp_path / 'source'
    output = tmp_path / 'output'
    original = add_file(source / 'moved.jpg', b'identical bytes')
    interrupt(source, output, 'after_unlink')
    published = next((output / 'organized-images').rglob('moved.jpg'))
    published_bytes = published.read_bytes()
    incoming = add_file(source / 'incoming.jpg', b'identical bytes')

    result = run_cli(source, '--copy', '--output', output)

    assert 'Duplicates:   1' in result.stdout
    assert not original.exists()
    assert incoming.read_bytes() == b'identical bytes'
    assert published.read_bytes() == published_bytes
    duplicate = next((output / 'duplicate-files').rglob('incoming.jpg'))
    assert duplicate.read_bytes() == b'identical bytes'
    with sqlite3.connect(output / app.DATABASE_NAME) as db:
        assert db.execute('SELECT state FROM move_intents').fetchone() == ('complete',)
        assert db.execute('SELECT digest FROM exact_hashes').fetchone() == (app.calculate_hash(published),)


def test_copy_mode_dry_run_does_not_finalize_pending_move(tmp_path):
    source = tmp_path / 'source'
    output = tmp_path / 'output'
    add_file(source / 'moved.jpg', b'original')
    interrupt(source, output, 'after_unlink')
    add_file(source / 'incoming.jpg', b'original')
    database = output / app.DATABASE_NAME
    before = database.read_bytes()

    result = run_cli(source, '--copy', '--output', output, '--dry-run')

    assert 'Duplicates:   1' in result.stdout
    assert database.read_bytes() == before
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT state FROM move_intents').fetchone() == ('pending',)


def test_copy_mode_does_not_complete_unverified_pending_move(tmp_path):
    source = tmp_path / 'source'
    output = tmp_path / 'output'
    add_file(source / 'moved.jpg', b'original')
    interrupt(source, output, 'after_unlink')
    destination = next((output / 'organized-images').rglob('moved.jpg'))
    destination.write_bytes(b'corrupt')
    incoming = add_file(source / 'incoming.jpg', b'original')

    result = run_cli(source, '--copy', '--output', output)

    assert 'Unresolved move intent' in result.stderr
    assert incoming.read_bytes() == b'original'
    with sqlite3.connect(output / app.DATABASE_NAME) as db:
        assert db.execute('SELECT state FROM move_intents').fetchone() == ('pending',)
    assert len(list((output / 'organized-images').rglob('incoming.jpg'))) == 1


def test_recovery_refuses_replaced_destination_and_preserves_source(tmp_path):
    source = tmp_path / 'source'
    output = tmp_path / 'output'
    original = add_file(source / 'z.jpg', b'original')
    interrupt(source, output, 'after_publish')
    destination = next((output / 'organized-images').rglob('z.jpg'))
    replacement = add_file(tmp_path / 'replacement.jpg', b'original')
    os.replace(replacement, destination)
    before = destination.read_bytes()
    result = run_cli(source, '--output', output, '--dry-run')
    assert original.exists() and destination.read_bytes() == before
    assert 'Unresolved move intent' in result.stderr


def test_recovery_after_source_replacement_keeps_both_files(tmp_path):
    source_root = tmp_path / 'source'
    source = add_file(source_root / 'photo.jpg', b'original')
    output = tmp_path / 'output'
    destination_dir = output / 'organized-images' / '2024' / 'January'
    destination_dir.mkdir(parents=True)
    replacement = add_file(tmp_path / 'replacement.jpg', b'unrelated')
    digest = app.calculate_hash(source)
    store = app.Persistence(str(output))
    records = app.MoveRecords(str(output), store)
    real_link = os.link

    def replace_after_link(src, dst, **kwargs):
        real_link(src, dst, **kwargs)
        os.replace(replacement, source)

    with patch.object(app.os, 'link', side_effect=replace_after_link):
        with pytest.raises(OSError, match='changed before removal'):
            app.move_without_overwriting(source, destination_dir, expected_hash=digest,
                before_publish=lambda dst, identity: records.remember(source, digest, dst, 'organized', identity))
    store.close()
    result = run_cli(source_root, '--output', output)
    assert result.returncode == 0
    assert not source.exists()
    assert (destination_dir / 'photo.jpg').read_bytes() == b'original'
    files = list((output / 'organized-images').rglob('photo*.jpg'))
    assert len(files) == 2
    assert {path.read_bytes() for path in files} == {b'original', b'unrelated'}
    with sqlite3.connect(output / app.DATABASE_NAME) as db:
        assert {row[0] for row in db.execute('SELECT digest FROM exact_hashes')} == {
            digest, app.calculate_hash(next(path for path in files if path.read_bytes() == b'unrelated'))}


def test_finish_move_database_failure_keeps_recoverable_intent(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    original = add_file(source / 'z.jpg', b'original')
    output = tmp_path / 'output'
    monkeypatch.setattr(sys, 'argv', [str(SCRIPT), '--src', str(source), '--output', str(output)])
    with patch.object(app.Persistence, 'finish_move', side_effect=sqlite3.OperationalError('disk full')):
        with pytest.raises(SystemExit):
            app.main()
    assert not original.exists()
    with sqlite3.connect(output / app.DATABASE_NAME) as db:
        assert db.execute('SELECT state FROM move_intents').fetchone() == ('pending',)
    run_cli(source, '--output', output)
    with sqlite3.connect(output / app.DATABASE_NAME) as db:
        assert db.execute('SELECT state FROM move_intents').fetchone() == ('complete',)


def test_multiple_pending_moves_recover_before_traversal(tmp_path):
    source = tmp_path / 'source'
    output = tmp_path / 'output'
    first = add_file(source / 'z.jpg', b'first')
    interrupt(source, output, 'after_unlink')
    second = add_file(source / 'y.jpg', b'second')
    interrupt(source, output, 'after_unlink')
    add_file(source / 'a.jpg', b'first')
    add_file(source / 'b.jpg', b'second')
    result = run_cli(source, '--output', output)
    assert 'Duplicates:   2' in result.stdout
    assert not first.exists() and not second.exists()
    with sqlite3.connect(output / app.DATABASE_NAME) as db:
        assert db.execute("SELECT count(*) FROM move_intents WHERE state='complete'").fetchone()[0] >= 2


@pytest.mark.parametrize('damage', ['missing', 'modified'])
def test_unverified_pending_destination_does_not_restore_hash(tmp_path, damage):
    source = tmp_path / 'source'
    output = tmp_path / 'output'
    original = add_file(source / 'z.jpg', b'original')
    interrupt(source, output, 'after_publish')
    destination = next((output / 'organized-images').rglob('z.jpg'))
    if damage == 'missing':
        destination.unlink()
    else:
        destination.write_bytes(b'corrupt')
    add_file(source / 'a.jpg', b'original')
    preview = run_cli(source, '--output', output, '--dry-run')
    assert 'Unresolved move intent' in preview.stderr
    assert original.exists()
    if damage == 'modified':
        assert destination.read_bytes() == b'corrupt'


def test_recovery_is_idempotent(tmp_path):
    source = tmp_path / 'source'
    output = tmp_path / 'output'
    add_file(source / 'z.jpg', b'original')
    interrupt(source, output, 'after_unlink')
    run_cli(source, '--output', output)
    first = (output / app.DATABASE_NAME).read_bytes()
    run_cli(source, '--output', output)
    assert (output / app.DATABASE_NAME).read_bytes() == first


def test_two_pending_published_moves_recover_together(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    output = tmp_path / 'output'
    output.mkdir(exist_ok=True)
    store = app.Persistence(str(output))
    for name, content in [('z.jpg', b'first'), ('y.jpg', b'second')]:
        destination = add_file(output / 'organized-images' / '2024' / 'January' / name, content)
        store.save_move_intent(str(source / name), app.calculate_hash(destination),
                               str(destination.relative_to(output)), 'organized', destination.stat())
    store.close()
    add_file(source / 'a.jpg', b'first')
    add_file(source / 'b.jpg', b'second')
    result = run_cli(source, '--output', output)
    assert 'Duplicates:   2' in result.stdout
    with sqlite3.connect(output / app.DATABASE_NAME) as db:
        assert db.execute("SELECT count(*) FROM move_intents WHERE state='complete'").fetchone()[0] >= 2


def test_intent_commit_failure_prevents_publication(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    original = add_file(source / 'photo.jpg', b'original')
    output = tmp_path / 'output'
    monkeypatch.setattr(sys, 'argv', [str(SCRIPT), '--src', str(source), '--output', str(output)])
    with patch.object(app.Persistence, 'save_move_intent', side_effect=sqlite3.OperationalError('disk full')):
        with pytest.raises(SystemExit):
            app.main()
    assert original.read_bytes() == b'original'
    assert not list((output / 'organized-images').rglob('*.jpg'))
