import errno
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import organize_images as organizer
from test_output import add_file, run_cli, snapshot_files, make_similar_photos, assert_report_links


@pytest.mark.parametrize('location', ['default', 'inside', 'outside'])
def test_copy_preserves_originals_and_reuses_completed_files(tmp_path, location):
    source = tmp_path / 'source'
    originals = [add_file(source / name, name.encode()) for name in ['photo.jpg', 'video.mp4', 'notes.txt']]
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in originals}
    output = {'default': source, 'inside': source / 'sorted', 'outside': tmp_path / 'sorted'}[location]
    args = [] if location == 'default' else ['--output', output]
    first = run_cli(source, '--copy', *args)
    after = snapshot_files(output)
    second = run_cli(source, '--copy', *args)
    assert 'Organized:    2' in first.stdout
    assert 'Needs review: 1' in second.stdout
    assert 'Total:        3' in second.stdout
    assert 'ALREADY COPIED' in second.stderr
    assert set(snapshot_files(output)) == set(after)
    for original, expected in before.items():
        assert (original.read_bytes(), original.stat().st_mtime_ns) == expected
        destinations = list((output / ('needs-review' if original.suffix == '.txt' else 'organized-images')).rglob(original.name))
        assert len(destinations) == 1
        assert (destinations[0].read_bytes(), destinations[0].stat().st_mtime_ns) == expected
        assert not os.path.samefile(original, destinations[0])


def test_different_sources_duplicate_and_collision_policy(tmp_path):
    source = tmp_path / 'source'
    output = tmp_path / 'sorted'
    add_file(source / 'one' / 'same.jpg', b'one')
    add_file(source / 'two' / 'same.jpg', b'two')
    add_file(source / 'three' / 'copy.jpg', b'one')
    run_cli(source, '--copy', '--output', output)
    assert len(list((output / 'organized-images').rglob('*.jpg'))) == 2
    assert len(list((output / 'duplicate-files').rglob('*.jpg'))) == 1
    again = run_cli(source, '--copy', '--output', output)
    assert 'Duplicates:   1' in again.stdout
    assert len(list((output / 'duplicate-files').rglob('*.jpg'))) == 1
    another = tmp_path / 'another'
    add_file(another / 'copy.jpg', b'one')
    run_cli(another, '--copy', '--output', output)
    assert len(list((output / 'duplicate-files').rglob('*.jpg'))) == 2


@pytest.mark.parametrize('error', [errno.ENOSPC, errno.EACCES])
def test_partial_copy_failure_leaves_no_outputs_or_hashes(tmp_path, error):
    source = add_file(tmp_path / 'source.jpg')
    output = tmp_path / 'output'
    output.mkdir()
    records = organizer.CopyRecords(str(output))
    stats = {'organized': 0, 'review': 0, 'duplicates': 0, 'errors': 0}
    def fail(src, dst):
        Path(dst).write_bytes(b'partial')
        assert not (output / source.name).exists()
        raise OSError(error, 'simulated failure')
    with patch.object(organizer, 'hash_keys', set()), patch.object(organizer.shutil, 'copy2', side_effect=fail):
        with pytest.raises(OSError):
            organizer.process_file(source, str(output), '2024/January', stats, 'organized', copy_records=records)
        assert not organizer.hash_keys
    assert list(output.iterdir()) == []
    assert source.read_bytes() == b'photo contents'
    assert stats['organized'] == 0


def test_copy_publication_and_alias_collisions(tmp_path):
    source = add_file(tmp_path / 'photo.jpg')
    output = tmp_path / 'output'
    output.mkdir()
    (output / source.name).symlink_to(source)
    result = Path(organizer.copy_without_overwriting(source, output))
    assert result.name == 'photo_1.jpg'
    assert result.read_bytes() == source.read_bytes()
    result.write_bytes(b'changed destination')
    assert source.read_bytes() == b'photo contents'
    assert not list(output.glob('.copy-*'))


@pytest.mark.parametrize('dry_run', [False, True])
def test_copy_similarity_and_dry_run(tmp_path, dry_run):
    source = tmp_path / 'source'
    output = source / 'sorted'
    make_similar_photos(source)
    add_file(source / 'duplicate.jpg', (source / 'one' / 'photo.jpg').read_bytes())
    before = snapshot_files(source)
    args = ['--dry-run'] if dry_run else []
    result = run_cli(source, '--copy', '--find-similar', '--output', output, *args)
    for name, value in before.items():
        assert snapshot_files(source)[name] == value
    assert 'Images analyzed: 2' in result.stdout
    if dry_run:
        assert snapshot_files(source) == before
        assert not output.exists()
        assert 'COPY:' in result.stderr and 'DUPLICATE COPY:' in result.stderr
    else:
        report = assert_report_links(output)
        again = run_cli(source, '--copy', '--find-similar', '--output', output)
        assert 'Images analyzed: 2' in again.stdout
        assert report['groups'] == assert_report_links(output)['groups']


def test_copy_skips_source_symlinks_and_rejects_similarity_only(tmp_path):
    source = tmp_path / 'source'
    original = add_file(source / 'photo.jpg')
    (source / 'alias.jpg').symlink_to(original)
    result = run_cli(source, '--copy')
    assert 'Total:        1' in result.stdout
    assert original.exists()
    with patch('sys.argv', ['organize_images.py', '--src', str(source), '--copy', '--find-similar-only']):
        with pytest.raises(SystemExit) as error:
            organizer.main()
    assert error.value.code == 2


def test_missing_or_modified_destination_is_not_reused(tmp_path):
    source = tmp_path / 'source'
    output = tmp_path / 'output'
    add_file(source / 'photo.jpg')
    run_cli(source, '--copy', '--output', output)
    destination = next((output / 'organized-images').rglob('*.jpg'))
    destination.write_bytes(b'different content')
    result = run_cli(source, '--copy', '--output', output)
    assert destination.read_bytes() == b'different content'
    assert 'Duplicates:   1' in result.stdout
    assert len(list((output / 'duplicate-files').rglob('*.jpg'))) == 1


@pytest.mark.parametrize('failure', ['publication', 'records'])
def test_failed_finalization_does_not_register_copy(tmp_path, failure):
    source = add_file(tmp_path / 'source.jpg')
    output = tmp_path / 'output'
    output.mkdir()
    records = organizer.CopyRecords(str(output))
    stats = {'organized': 0, 'review': 0, 'duplicates': 0, 'errors': 0}
    target = patch.object(organizer.os, 'link', side_effect=PermissionError('denied')) if failure == 'publication' else patch.object(records, 'remember', side_effect=PermissionError('denied'))
    with patch.object(organizer, 'hash_keys', set()), target:
        with pytest.raises(PermissionError):
            organizer.process_file(source, str(output), '2024/January', stats, 'organized', copy_records=records)
        assert not organizer.hash_keys
    assert not list(output.glob('*.jpg'))
    assert not list(output.glob('.copy-*'))
    assert records.previous(source, organizer.calculate_hash(source)) is None
    assert source.read_bytes() == b'photo contents'


def test_copy_never_overwrites_existing_content(tmp_path):
    source = add_file(tmp_path / 'photo.jpg', b'new')
    output = tmp_path / 'output'
    existing = add_file(output / 'photo.jpg', b'existing')
    destination = Path(organizer.copy_without_overwriting(source, output))
    assert destination.name == 'photo_1.jpg'
    assert existing.read_bytes() == b'existing'
    assert destination.read_bytes() == b'new'
    assert source.read_bytes() == b'new'


def test_distinct_hardlinked_sources_remain_duplicates(tmp_path):
    source = tmp_path / 'source'
    first = add_file(source / 'first.jpg')
    os.link(first, source / 'second.jpg')
    output = tmp_path / 'output'
    result = run_cli(source, '--copy', '--output', output)
    assert 'Duplicates:   1' in result.stdout
    assert len(list((output / 'duplicate-files').rglob('*.jpg'))) == 1


def test_no_hardlink_fallback_is_exclusive_and_complete(tmp_path):
    source = add_file(tmp_path / 'photo.jpg', b'complete contents')
    output = tmp_path / 'output'
    existing = add_file(output / source.name, b'user file')
    with patch.object(organizer.os, 'link', side_effect=OSError(errno.EOPNOTSUPP, 'no links')):
        result = Path(organizer.copy_without_overwriting(source, output))
    assert result.name == 'photo_1.jpg'
    assert result.read_bytes() == source.read_bytes()
    assert existing.read_bytes() == b'user file'
    assert not list(output.glob('.copy-*'))


def test_no_safe_publication_primitive_fails_closed(tmp_path):
    source = add_file(tmp_path / 'photo.jpg')
    output = tmp_path / 'output'
    output.mkdir()
    with patch.object(organizer.os, 'link', side_effect=OSError(errno.EOPNOTSUPP, 'no links')), patch.object(organizer, 'rename_without_overwriting', side_effect=OSError(errno.EOPNOTSUPP, 'no exclusive rename')):
        with pytest.raises(OSError):
            organizer.copy_without_overwriting(source, output)
    assert list(output.iterdir()) == []
    assert source.read_bytes() == b'photo contents'


@pytest.mark.parametrize('fallback', [False, True])
def test_terminated_publication_is_reused_without_writing_in_dry_run(tmp_path, fallback):
    source = tmp_path / 'source'
    original = add_file(source / 'photo.jpg')
    output = tmp_path / 'output'
    script = '''
import errno, os, sys
import organize_images as app
original_link = app.os.link
original_rename = app.rename_without_overwriting
def publish(*args, **kwargs):
    if sys.argv.pop() == 'fallback':
        original_rename(*args, **kwargs)
    else:
        original_link(*args, **kwargs)
    os._exit(77)
if sys.argv[-1] == 'fallback':
    def unsupported(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, 'no links')
    app.os.link = unsupported
    app.rename_without_overwriting = publish
else:
    app.os.link = publish
mode = sys.argv.pop()
# Keep the mode outside argparse, but available to the publication hook.
def wrapped(*args, **kwargs):
    sys.argv.append(mode)
    return publish(*args, **kwargs)
if mode == 'fallback':
    app.rename_without_overwriting = wrapped
else:
    app.os.link = wrapped
app.main()
'''
    result = subprocess.run([sys.executable, '-c', script, '--src', str(source), '--output', str(output), '--copy', 'fallback' if fallback else 'link'], capture_output=True)
    assert result.returncode == 77, result.stderr
    destination = next((output / 'organized-images').rglob('photo.jpg'))
    before = snapshot_files(output)
    preview = run_cli(source, '--copy', '--output', output, '--dry-run')
    assert 'ALREADY COPIED' in preview.stderr
    assert snapshot_files(output) == before
    again = run_cli(source, '--copy', '--output', output)
    assert 'ALREADY COPIED' in again.stderr
    assert list((output / 'organized-images').rglob('photo*.jpg')) == [destination]
    assert not list((output / 'duplicate-files').rglob('*.jpg'))
    assert destination.read_bytes() == original.read_bytes()
    assert organizer.calculate_hash(original) in (output / '.sha256_hashes.txt').read_text()


def test_unpublished_intent_cannot_adopt_existing_same_content(tmp_path):
    source = add_file(tmp_path / 'photo.jpg')
    output = tmp_path / 'output'
    existing = add_file(output / 'organized-images' / 'photo.jpg')
    temporary = add_file(output / 'staged', source.read_bytes())
    records = organizer.CopyRecords(str(output))
    records.remember(source, organizer.calculate_hash(source), existing, 'organized', temporary.stat())
    assert records.previous(source, organizer.calculate_hash(source)) is None
    assert existing.read_bytes() == source.read_bytes()


def test_interrupted_staging_artifacts_are_not_inputs(tmp_path):
    import similarity
    source = tmp_path / 'source'
    original = add_file(source / 'photo.jpg')
    artifacts = [add_file(source / name) for name in ['.copy-abcd', '.copy-records-abcd', '.hashes-abcd']]
    assert list(similarity.scan_images(str(source))) == [{'path': str(original), 'original_name': original.name}]
    result = run_cli(source, '--copy')
    assert 'Total:        1' in result.stdout
    assert all(path.exists() for path in artifacts)


def test_recovery_hashes_are_loaded_before_new_sources(tmp_path):
    source = tmp_path / 'source'
    output = tmp_path / 'output'
    original = add_file(source / 'z.jpg')
    run_cli(source, '--copy', '--output', output)
    # Simulate termination after publication but before the hash database is saved.
    (output / '.sha256_hashes.txt').unlink()
    duplicate = add_file(source / 'a.jpg', original.read_bytes())
    before = snapshot_files(output)
    preview = run_cli(source, '--copy', '--output', output, '--dry-run')
    assert 'Duplicates:   1' in preview.stdout
    assert snapshot_files(output) == before
    result = run_cli(source, '--copy', '--output', output)
    assert 'Duplicates:   1' in result.stdout
    assert [p.name for p in (output / 'organized-images').rglob('*.jpg')] == ['z.jpg']
    assert [p.name for p in (output / 'duplicate-files').rglob('*.jpg')] == ['a.jpg']
    assert duplicate.read_bytes() == original.read_bytes()


@pytest.mark.parametrize('damage', ['missing', 'modified'])
def test_recovery_does_not_restore_unverified_destinations(tmp_path, damage):
    source = tmp_path / 'source'
    output = tmp_path / 'output'
    original = add_file(source / 'z.jpg')
    run_cli(source, '--copy', '--output', output)
    (output / '.sha256_hashes.txt').unlink()
    destination = next((output / 'organized-images').rglob('z.jpg'))
    if damage == 'missing':
        destination.unlink()
    else:
        destination.write_bytes(b'changed')
    add_file(source / 'a.jpg', original.read_bytes())
    result = run_cli(source, '--copy', '--output', output)
    assert 'Organized:    1' in result.stdout
    assert 'Duplicates:   1' in result.stdout
    if damage == 'modified':
        assert destination.read_bytes() == b'changed'


def test_recovery_restores_multiple_records_before_similarity(tmp_path):
    source = tmp_path / 'source'
    output = tmp_path / 'output'
    make_similar_photos(source)
    run_cli(source, '--copy', '--output', output)
    (output / '.sha256_hashes.txt').unlink()
    add_file(source / 'a.jpg', (source / 'one' / 'photo.jpg').read_bytes())
    add_file(source / 'b.jpg', (source / 'two' / 'photo.jpg').read_bytes())
    result = run_cli(source, '--copy', '--output', output, '--find-similar')
    assert 'Duplicates:   2' in result.stdout
    assert 'Images analyzed: 2' in result.stdout
    assert len(list((output / 'organized-images').rglob('*.jpg'))) == 2
    assert_report_links(output)
