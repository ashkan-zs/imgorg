import errno
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import organize_images as organizer
import similarity
from test_output import add_file, run_cli


def cross_device_link(source, real_link):
    def link(src, dst, *args, **kwargs):
        if os.fspath(src) == os.fspath(source):
            raise OSError(errno.EXDEV, 'cross-device')
        return real_link(src, dst, *args, **kwargs)
    return link


def test_source_read_failure_never_deletes_existing_destination(tmp_path):
    source = add_file(tmp_path / 'source' / 'photo.jpg')
    existing = add_file(tmp_path / 'output' / 'photo.jpg', b'unrelated')
    real_open = open
    def fail_source(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(source):
            raise PermissionError('source read failed')
        return real_open(path, *args, **kwargs)
    with patch.object(organizer.os, 'link', side_effect=OSError(errno.EXDEV, 'cross-device')), \
         patch('builtins.open', side_effect=fail_source):
        with pytest.raises(PermissionError):
            organizer.move_without_overwriting(source, existing.parent)
    assert existing.read_bytes() == b'unrelated'
    assert source.read_bytes() == b'photo contents'
    assert list(existing.parent.iterdir()) == [existing]


def test_fallback_publishes_complete_copy_and_preserves_collisions_and_metadata(tmp_path):
    source = add_file(tmp_path / 'source' / 'photo.jpg', b'complete')
    source.chmod(0o640)
    expected = source.stat()
    existing = add_file(tmp_path / 'output' / 'photo.jpg', b'unrelated')
    existing2 = add_file(existing.parent / 'photo_1.jpg', b'other')
    real_link = os.link
    with patch.object(organizer.os, 'link', side_effect=cross_device_link(source, real_link)):
        result = Path(organizer.move_without_overwriting(source, existing.parent))
    assert result.name == 'photo_2.jpg'
    assert result.read_bytes() == b'complete'
    assert result.stat().st_mtime_ns == expected.st_mtime_ns
    assert result.stat().st_mode & 0o777 == expected.st_mode & 0o777
    assert existing.read_bytes() == b'unrelated' and existing2.read_bytes() == b'other'
    assert not source.exists()
    assert not list(existing.parent.glob('.copy-*'))


@pytest.mark.parametrize('error', [errno.EACCES, errno.ENOSPC, errno.EIO])
def test_staging_failure_preserves_source_and_existing_destination(tmp_path, error):
    source = add_file(tmp_path / 'source' / 'photo.jpg')
    existing = add_file(tmp_path / 'output' / 'photo.jpg', b'unrelated')
    def fail(src, dst):
        Path(dst).write_bytes(b'partial')
        raise OSError(error, 'copy failed')
    with patch.object(organizer.os, 'link', side_effect=OSError(errno.EXDEV, 'cross-device')), \
         patch.object(organizer.shutil, 'copy2', side_effect=fail):
        with pytest.raises(OSError):
            organizer.move_without_overwriting(source, existing.parent)
    assert source.read_bytes() == b'photo contents'
    assert existing.read_bytes() == b'unrelated'
    assert list(existing.parent.iterdir()) == [existing]


def test_staged_content_is_verified_before_publication(tmp_path):
    source = add_file(tmp_path / 'source' / 'photo.jpg')
    output = tmp_path / 'output'
    output.mkdir()
    def corrupt(src, dst):
        Path(dst).write_bytes(b'bad copy')
    with patch.object(organizer.os, 'link', side_effect=OSError(errno.EXDEV, 'cross-device')), \
         patch.object(organizer.shutil, 'copy2', side_effect=corrupt):
        with pytest.raises(OSError, match='Source changed'):
            organizer.move_without_overwriting(source, output)
    assert source.read_bytes() == b'photo contents'
    assert not list(output.iterdir())


def test_failed_publication_keeps_source_and_existing_file(tmp_path):
    source = add_file(tmp_path / 'source' / 'photo.jpg')
    existing = add_file(tmp_path / 'output' / 'photo.jpg', b'unrelated')
    def fail(src, dst, *args, **kwargs):
        if os.fspath(src) == os.fspath(source):
            raise OSError(errno.EXDEV, 'cross-device')
        raise OSError(errno.EACCES, 'publication denied')
    with patch.object(organizer.os, 'link', side_effect=fail):
        with pytest.raises(OSError, match='publication denied'):
            organizer.move_without_overwriting(source, existing.parent)
    assert source.read_bytes() == b'photo contents'
    assert existing.read_bytes() == b'unrelated'
    assert list(existing.parent.iterdir()) == [existing]


def test_no_exclusive_publication_fails_safely(tmp_path):
    source = add_file(tmp_path / 'source' / 'photo.jpg')
    existing = add_file(tmp_path / 'output' / 'photo.jpg', b'unrelated')
    with patch.object(organizer.os, 'link', side_effect=OSError(errno.EXDEV, 'no links')), \
         patch.object(organizer, 'rename_without_overwriting', side_effect=OSError(errno.EOPNOTSUPP, 'no rename')):
        with pytest.raises(OSError):
            organizer.move_without_overwriting(source, existing.parent)
    assert source.read_bytes() == b'photo contents'
    assert existing.read_bytes() == b'unrelated'
    assert list(existing.parent.iterdir()) == [existing]


def test_native_exclusive_rename_preserves_existing_destination(tmp_path):
    source = add_file(tmp_path / 'staged.jpg')
    destination = tmp_path / 'published.jpg'
    organizer.rename_without_overwriting(source, destination)
    assert destination.read_bytes() == b'photo contents'
    assert not source.exists()

    second_source = add_file(tmp_path / 'second.jpg', b'second contents')
    with pytest.raises(FileExistsError):
        organizer.rename_without_overwriting(second_source, destination)
    assert second_source.read_bytes() == b'second contents'
    assert destination.read_bytes() == b'photo contents'


@pytest.mark.parametrize('fallback', [False, True])
def test_source_unlink_failure_keeps_complete_destination(tmp_path, fallback):
    source = add_file(tmp_path / 'source' / 'photo.jpg')
    output = tmp_path / 'output'
    output.mkdir()
    real_link, real_unlink = os.link, os.unlink
    def unlink(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(source):
            raise PermissionError('source unlink denied')
        return real_unlink(path, *args, **kwargs)
    link = cross_device_link(source, real_link) if fallback else real_link
    with patch.object(organizer.os, 'link', side_effect=link), patch.object(organizer.os, 'unlink', side_effect=unlink):
        with pytest.raises(PermissionError):
            organizer.move_without_overwriting(source, output)
    assert source.read_bytes() == b'photo contents'
    assert (output / source.name).read_bytes() == b'photo contents'
    assert not list(output.glob('.copy-*'))


def test_same_filesystem_move_refuses_replaced_source_after_link(tmp_path):
    source = add_file(tmp_path / 'source' / 'photo.jpg', b'original')
    output = tmp_path / 'output'
    output.mkdir()
    replacement = add_file(tmp_path / 'replacement.jpg', b'unrelated')
    real_link = os.link

    def replace_after_link(src, dst, **kwargs):
        real_link(src, dst, **kwargs)
        os.replace(replacement, source)

    with patch.object(organizer.os, 'link', side_effect=replace_after_link):
        with pytest.raises(OSError, match='changed before removal'):
            organizer.move_without_overwriting(source, output)
    assert source.read_bytes() == b'unrelated'
    assert (output / 'photo.jpg').read_bytes() == b'original'


def test_same_filesystem_move_and_collision_still_remove_original(tmp_path):
    source = add_file(tmp_path / 'source' / 'photo.jpg', b'original')
    output = tmp_path / 'output'
    existing = add_file(output / 'photo.jpg', b'existing')
    destination = Path(organizer.move_without_overwriting(source, output,
                       expected_hash=organizer.calculate_hash(source)))
    assert destination.name == 'photo_1.jpg'
    assert destination.read_bytes() == b'original'
    assert existing.read_bytes() == b'existing'
    assert not source.exists()


def test_interrupted_staging_never_publishes_partial_and_is_not_scanned(tmp_path):
    source = tmp_path / 'source'
    original = add_file(source / 'photo.jpg', b'complete photo contents')
    output = tmp_path / 'output'
    script = '''
import errno, os
import organize_images as app
app.os.link = lambda *args, **kwargs: (_ for _ in ()).throw(OSError(errno.EXDEV, 'cross-device'))
def crash(source, destination):
    with open(destination, 'wb') as file:
        file.write(b'partial')
        file.flush()
        os._exit(77)
app.shutil.copy2 = crash
app.main()
'''
    result = subprocess.run([sys.executable, '-c', script, '--src', str(source), '--output', str(output)], capture_output=True)
    assert result.returncode == 77, result.stderr
    assert original.read_bytes() == b'complete photo contents'
    assert not list((output / 'organized-images').rglob('photo.jpg'))
    leftovers = list((output / 'organized-images').rglob('.copy-*'))
    assert len(leftovers) == 1 and leftovers[0].read_bytes() == b'partial'
    assert list(similarity.scan_images(output)) == []
    assert list(similarity.scan_images(leftovers[0].parent)) == []
    archive_preview = run_cli(leftovers[0].parent, '--output', tmp_path / 'other-output', '--dry-run')
    assert 'Total:        0' in archive_preview.stdout
    user_file = add_file(source / 'nested' / '.copy-abcdefgh', b'legitimate')
    preview = run_cli(source, '--output', output, '--dry-run')
    assert 'Total:        2' in preview.stdout
    result = run_cli(source, '--output', output)
    assert 'Total:        2' in result.stdout
    assert (output / 'needs-review' / user_file.name).read_bytes() == b'legitimate'
    assert len(list((output / 'organized-images').rglob('photo.jpg'))) == 1
    assert leftovers[0].read_bytes() == b'partial'


def test_replaced_staging_path_is_not_deleted(tmp_path):
    source = add_file(tmp_path / 'source' / 'photo.jpg')
    output = tmp_path / 'output'
    output.mkdir()
    replacement = add_file(tmp_path / 'replacement', b'unrelated')
    staged = []
    def replace(src, dst):
        staged.append(Path(dst))
        os.replace(replacement, dst)
    with patch.object(organizer.os, 'link', side_effect=OSError(errno.EXDEV, 'cross-device')), \
         patch.object(organizer.shutil, 'copy2', side_effect=replace):
        with pytest.raises(OSError, match='Staged file was replaced'):
            organizer.move_without_overwriting(source, output)
    assert source.read_bytes() == b'photo contents'
    assert staged[0].read_bytes() == b'unrelated'
    assert not (output / source.name).exists()


def test_source_changed_during_copy_is_not_removed(tmp_path):
    source = add_file(tmp_path / 'source' / 'photo.jpg')
    output = tmp_path / 'output'
    output.mkdir()
    real_copy = organizer.shutil.copy2
    def mutate(src, dst):
        real_copy(src, dst)
        Path(src).write_bytes(b'changed')
    with patch.object(organizer.os, 'link', side_effect=OSError(errno.EXDEV, 'cross-device')), \
         patch.object(organizer.shutil, 'copy2', side_effect=mutate):
        with pytest.raises(OSError, match='Source changed'):
            organizer.move_without_overwriting(source, output)
    assert source.read_bytes() == b'changed'
    assert not list(output.iterdir())


def test_native_exclusive_rename_when_staged_hardlink_is_unavailable(tmp_path):
    source = add_file(tmp_path / 'source' / 'photo.jpg')
    existing = add_file(tmp_path / 'output' / 'photo.jpg', b'unrelated')
    with patch.object(organizer.os, 'link', side_effect=OSError(errno.EOPNOTSUPP, 'no links')):
        destination = Path(organizer.move_without_overwriting(source, existing.parent))
    assert destination.name == 'photo_1.jpg'
    assert destination.read_bytes() == b'photo contents'
    assert existing.read_bytes() == b'unrelated'
    assert not source.exists()


@pytest.mark.parametrize('error', [errno.EACCES, errno.ENOSPC])
def test_staging_creation_failure_preserves_existing_files(tmp_path, error):
    source = add_file(tmp_path / 'source' / 'photo.jpg')
    existing = add_file(tmp_path / 'output' / 'photo.jpg', b'unrelated')
    with patch.object(organizer.os, 'link', side_effect=OSError(errno.EXDEV, 'cross-device')), \
         patch.object(organizer.tempfile, 'mkstemp', side_effect=OSError(error, 'staging failed')):
        with pytest.raises(OSError):
            organizer.move_without_overwriting(source, existing.parent)
    assert source.read_bytes() == b'photo contents'
    assert existing.read_bytes() == b'unrelated'
    assert list(existing.parent.iterdir()) == [existing]


def test_unlink_failure_does_not_remove_replaced_destination(tmp_path):
    source = add_file(tmp_path / 'source' / 'photo.jpg')
    output = tmp_path / 'output'
    output.mkdir()
    replacement = add_file(tmp_path / 'replacement', b'unrelated replacement')
    real_link, real_unlink = os.link, os.unlink
    def unlink(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(source):
            os.replace(replacement, output / 'photo.jpg')
            raise PermissionError('source unlink denied')
        return real_unlink(path, *args, **kwargs)
    with patch.object(organizer.os, 'link', side_effect=cross_device_link(source, real_link)), \
         patch.object(organizer.os, 'unlink', side_effect=unlink):
        with pytest.raises(PermissionError):
            organizer.move_without_overwriting(source, output)
    assert source.read_bytes() == b'photo contents'
    assert (output / 'photo.jpg').read_bytes() == b'unrelated replacement'


def test_fallback_rejects_source_changed_after_classification(tmp_path):
    source = add_file(tmp_path / 'source' / 'photo.jpg', b'original')
    output = tmp_path / 'output'
    output.mkdir()
    digest = organizer.calculate_hash(source)
    source.write_bytes(b'changed before copying')
    with patch.object(organizer.os, 'link', side_effect=OSError(errno.EXDEV, 'cross-device')):
        with pytest.raises(OSError, match='Source changed'):
            organizer.move_without_overwriting(source, output, expected_hash=digest)
    assert source.read_bytes() == b'changed before copying'
    assert not list(output.iterdir())
