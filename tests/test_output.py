import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

import pytest
from PIL import Image, ImageDraw
from PIL.ExifTags import Base, IFD
from pillow_heif import from_pillow

import organize_images
import similarity


SCRIPT = Path(__file__).resolve().parent.parent / 'organize_images.py'
MTIME = datetime(2024, 1, 15, 12).timestamp()
DATE_DIRECTORY = datetime.fromtimestamp(MTIME).strftime(organize_images.dir_format)


def add_file(path, content=b'photo contents'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (MTIME, MTIME))
    return path


def run_cli(source, *args, cwd=None):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), '--src', str(source), *map(str, args)],
        cwd=cwd, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def snapshot_files(root):
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob('*') if path.is_file()
    }


def make_similar_photos(source):
    with Image.new('RGB', (96, 64), '#eef2f5') as image:
        draw = ImageDraw.Draw(image)
        draw.rectangle((3, 5, 35, 56), fill='#142e50')
        draw.ellipse((40, 7, 90, 49), fill='#e86d24')
        draw.polygon([(20, 62), (50, 28), (82, 60)], fill='#348153')
        for folder, quality in [('one', 95), ('two', 40)]:
            photo = source / folder / 'photo.jpg'
            photo.parent.mkdir(parents=True)
            image.save(photo, quality=quality)
            os.utime(photo, (MTIME, MTIME))


@pytest.mark.parametrize('location', ['default', 'equal', 'outside', 'inside', 'deep-inside', 'parent'])
def test_output_layout_and_default_compatibility(tmp_path, location):
    source = tmp_path / 'photos'
    photo = add_file(source / 'incoming' / 'photo.jpg')
    notes = add_file(source / 'notes.txt', b'notes')
    original = photo.read_bytes(), photo.stat().st_mtime_ns
    output = {
        'default': source, 'equal': source, 'outside': tmp_path / 'sorted',
        'inside': source / 'sorted', 'deep-inside': source / 'nested' / 'sorted',
        'parent': tmp_path,
    }[location]
    args = [] if location == 'default' else ['--output', output]

    result = run_cli(source, *args)

    destination = output / 'organized-images' / DATE_DIRECTORY / photo.name
    assert (destination.read_bytes(), destination.stat().st_mtime_ns) == original
    assert (output / 'needs-review' / notes.name).read_bytes() == b'notes'
    assert (output / 'duplicate-files').is_dir()
    assert not (output / 'similar-images').exists()
    assert (output / organize_images.DATABASE_NAME).is_file()
    assert not photo.exists() and not notes.exists()
    assert 'Organized:    1' in result.stdout
    assert 'Needs review: 1' in result.stdout
    assert 'Errors:       0' in result.stdout
    if output != source:
        assert not (source / '.sha256_hashes.txt').exists()
        assert not (source / 'organized-images').exists()


def test_relative_paths_are_normalized_against_working_directory(tmp_path):
    source = tmp_path / 'photos'
    add_file(source / 'photo.jpg')
    run_cli('./photos/../photos', '--output', './unused/../sorted/nested', cwd=tmp_path)
    assert (tmp_path / 'sorted' / 'nested' / 'organized-images' / DATE_DIRECTORY / 'photo.jpg').is_file()
    assert not (tmp_path / 'unused').exists()


def test_home_expansion_and_symlink_aliases_use_canonical_paths(tmp_path, monkeypatch):
    source = tmp_path / 'photos'
    add_file(source / 'photo.jpg')
    output = source / 'sorted'
    output.mkdir()
    add_file(output / 'do-not-scan.jpg', b'old output')
    (tmp_path / 'source-alias').symlink_to(source, target_is_directory=True)
    (tmp_path / 'output-alias').symlink_to(output, target_is_directory=True)
    monkeypatch.setenv('HOME', str(tmp_path))
    result = run_cli('~/source-alias', '--output', '~/output-alias')
    assert 'Total:        1' in result.stdout
    assert (output / 'do-not-scan.jpg').read_bytes() == b'old output'
    assert (output / 'organized-images' / DATE_DIRECTORY / 'photo.jpg').is_file()


@pytest.mark.parametrize('custom', [False, True])
def test_generated_outputs_and_preexisting_output_folders_are_not_rescanned(tmp_path, custom):
    source = tmp_path / 'photos'
    add_file(source / 'incoming.jpg')
    old = {}
    for name in ['organized-images', 'duplicate-files', 'needs-review', 'similar-images']:
        path = add_file(source / 'old' / name / 'old.jpg', name.encode())
        old[path] = path.read_bytes(), path.stat().st_mtime_ns
    output = source / 'nested' / 'sorted' if custom else source
    if custom:
        path = add_file(output / 'existing.jpg', b'existing output-root file')
        old[path] = path.read_bytes(), path.stat().st_mtime_ns
    args = ['--output', output] if custom else []

    first = run_cli(source, *args)
    second = run_cli(source, *args)

    assert 'Total:        1' in first.stdout
    assert 'Total:        0' in second.stdout
    for path, expected in old.items():
        assert (path.read_bytes(), path.stat().st_mtime_ns) == expected


def test_repeated_runs_and_sources_share_the_output_hash_database(tmp_path):
    source = tmp_path / 'photos'
    output = source / 'sorted'
    first = add_file(source / 'first.jpg')
    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    # A source index from a different library must not seed the new output.
    old_database = add_file(source / '.sha256_hashes.txt', (digest + '\n').encode())
    old_snapshot = old_database.read_bytes(), old_database.stat().st_mtime_ns

    initial = run_cli(source, '--output', output)
    add_file(source / 'copy.jpg')
    repeated = run_cli(source, '--output', output)
    another_source = tmp_path / 'other-photos'
    add_file(another_source / 'copy.jpg')
    another = run_cli(another_source, '--output', output)

    assert 'Organized:    1' in initial.stdout
    assert 'Duplicates:   1' in repeated.stdout
    assert 'Duplicates:   1' in another.stdout
    assert 'Errors:       0' in repeated.stdout + another.stdout
    with sqlite3.connect(output / organize_images.DATABASE_NAME) as db:
        assert db.execute('SELECT digest FROM exact_hashes').fetchall() == [(digest,)]
    assert (old_database.read_bytes(), old_database.stat().st_mtime_ns) == old_snapshot
    duplicates = output / 'duplicate-files' / DATE_DIRECTORY
    assert {path.name for path in duplicates.iterdir()} == {'copy.jpg', 'copy_1.jpg'}


@pytest.mark.parametrize('extension', ['.jpg', '.heif'])
def test_custom_output_preserves_capture_dates_and_image_bytes(tmp_path, extension):
    source = tmp_path / 'photos'
    source.mkdir()
    output = tmp_path / 'sorted'
    photo = source / f'photo{extension}'
    exif = Image.Exif()
    exif[IFD.Exif] = {Base.DateTimeOriginal: '2020:07:14 12:34:56'}
    with Image.new('RGB', (16, 16), 'blue') as image:
        if extension == '.heif':
            from_pillow(image).save(photo, exif=exif.tobytes())
        else:
            image.save(photo, exif=exif)
    os.utime(photo, (MTIME, MTIME))
    original = photo.read_bytes(), photo.stat().st_mtime_ns

    run_cli(source, '--output', output)

    date = datetime(2020, 7, 14).strftime(organize_images.dir_format)
    destination = output / 'organized-images' / date / photo.name
    assert (destination.read_bytes(), destination.stat().st_mtime_ns) == original


def test_custom_output_collisions_preserve_all_files(tmp_path):
    source = tmp_path / 'photos'
    output = tmp_path / 'sorted'
    add_file(source / 'one' / 'same.jpg', b'first')
    add_file(source / 'two' / 'same.jpg', b'second')
    existing = add_file(output / 'organized-images' / DATE_DIRECTORY / 'same.jpg', b'existing')

    run_cli(source, '--output', output)

    assert existing.read_bytes() == b'existing'
    assert (existing.parent / 'same_1.jpg').read_bytes() == b'first'
    assert (existing.parent / 'same_2.jpg').read_bytes() == b'second'


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        self.urls.extend(value for key, value in attrs if key in {'href', 'src'})


def assert_report_links(output):
    directory = output / 'similar-images'
    parser = Links()
    parser.feed((directory / 'report.html').read_text())
    assert parser.urls
    assert all((directory / unquote(url)).is_file() for url in parser.urls)
    return json.loads((directory / 'report.json').read_text())


@pytest.mark.parametrize('location', ['outside', 'inside', 'equal'])
def test_combined_similarity_uses_final_custom_destinations(tmp_path, location):
    source = tmp_path / 'photos'
    make_similar_photos(source)
    add_file(source / 'three' / 'copy.jpg', (source / 'one' / 'photo.jpg').read_bytes())
    output = {'outside': tmp_path / 'sorted', 'inside': source / 'sorted', 'equal': source}[location]
    add_file(output / 'organized-images' / DATE_DIRECTORY / 'photo.jpg', b'existing')

    result = run_cli(source, '--output', output, '--find-similar')
    report = assert_report_links(output)

    assert report['total_images_analyzed'] == 2
    assert report['total_similarity_groups'] == 1
    assert 'output directory' in report['path_base']
    members = report['groups'][0]['members']
    assert {Path(member['path']).name for member in members} == {'photo_1.jpg', 'photo_2.jpg'}
    assert all((output / member['path']).is_file() for member in members)
    assert 'Organized:    2' in result.stdout
    assert 'Duplicates:   1' in result.stdout
    assert 'Errors:       0' in result.stdout


@pytest.mark.parametrize('location', ['outside', 'inside', 'equal', 'parent'])
def test_similarity_only_custom_output_is_read_only_and_links_to_source(tmp_path, location):
    source = tmp_path / 'archive'
    make_similar_photos(source / 'organized-images' / '2020')
    add_file(source / '.sha256_hashes.txt', b'leave this database alone')
    output = {'outside': tmp_path / 'reports', 'inside': source / 'reports',
              'equal': source, 'parent': tmp_path}[location]
    if output != source:
        add_file(output / '.sha256_hashes.txt', b'output database also unchanged')
    if location == 'inside':
        make_similar_photos(output / 'do-not-scan')
    before = snapshot_files(source)
    output_database = (output / '.sha256_hashes.txt').read_bytes()

    result = run_cli(source, '--output', output, '--find-similar-only')
    report = assert_report_links(output)

    assert report['total_images_analyzed'] == 2
    assert report['total_similarity_groups'] == 1
    assert 'output directory' in report['path_base']
    for name, content in before.items():
        assert snapshot_files(source)[name] == content
    assert (output / '.sha256_hashes.txt').read_bytes() == output_database
    assert not (output / 'duplicate-files').exists()
    assert not (output / 'needs-review').exists()
    assert '--- Summary ---' not in result.stdout


@pytest.mark.parametrize('mode', [[], ['--find-similar'], ['--find-similar-only']])
@pytest.mark.parametrize('inside', [False, True])
def test_custom_output_dry_run_has_no_persistent_output(tmp_path, mode, inside):
    source = tmp_path / 'photos'
    make_similar_photos(source)
    output = source / 'new' / 'sorted' if inside else tmp_path / 'new' / 'sorted'
    before = snapshot_files(tmp_path)
    directories = {path for path in tmp_path.rglob('*') if path.is_dir()}

    result = run_cli(source, '--output', output, '--dry-run', *mode)

    assert snapshot_files(tmp_path) == before
    assert {path for path in tmp_path.rglob('*') if path.is_dir()} == directories
    assert not output.exists()
    if mode:
        assert 'Similarity groups: 1' in result.stdout
        assert 'one/photo.jpg' in result.stdout


@pytest.mark.parametrize('mode', [[], ['--find-similar-only']])
@pytest.mark.parametrize('invalid', ['file', 'file-parent'])
def test_invalid_output_path_fails_before_processing(tmp_path, mode, invalid):
    source = tmp_path / 'photos'
    photo = add_file(source / 'photo.jpg')
    blocker = add_file(tmp_path / 'blocker', b'not a directory')
    output = blocker if invalid == 'file' else blocker / 'child'
    result = subprocess.run(
        [sys.executable, str(SCRIPT), '--src', str(source), '--output', str(output), *mode],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert 'Invalid output path' in result.stderr
    assert 'Traceback' not in result.stderr
    assert photo.exists()
    assert blocker.read_bytes() == b'not a directory'


@pytest.mark.parametrize('mode, directory', [([], 'organized-images'), ([], 'duplicate-files'),
                                           ([], 'needs-review'), (['--find-similar-only'], 'similar-images')])
def test_source_inside_a_generated_destination_is_rejected(tmp_path, mode, directory):
    output = tmp_path / 'sorted'
    source = output / directory / 'archive'
    photo = add_file(source / 'photo.jpg')
    before = snapshot_files(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), '--src', str(source), '--output', str(output), *mode],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert 'Source must not be inside' in result.stderr
    assert photo.exists()
    assert snapshot_files(tmp_path) == before


@pytest.mark.parametrize('kind', ['file', 'symlink', 'database-symlink'])
def test_invalid_output_artifacts_are_rejected_before_moves(tmp_path, kind):
    source = tmp_path / 'photos'
    photo = add_file(source / 'photo.jpg')
    output = tmp_path / 'sorted'
    output.mkdir()
    if kind == 'file':
        add_file(output / 'organized-images', b'not a directory')
    elif kind == 'symlink':
        (output / 'organized-images').symlink_to(source, target_is_directory=True)
    else:
        (output / organize_images.DATABASE_NAME).symlink_to(photo)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), '--src', str(source), '--output', str(output)],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert 'Invalid output path' in result.stderr
    assert photo.read_bytes() == b'photo contents'


def test_output_creation_permission_failure_is_reported_without_moving_files(tmp_path, monkeypatch, capsys):
    source = tmp_path / 'photos'
    photo = add_file(source / 'photo.jpg')
    output = tmp_path / 'sorted'
    monkeypatch.setattr(sys, 'argv', [str(SCRIPT), '--src', str(source), '--output', str(output)])
    with patch.object(organize_images.os, 'makedirs', side_effect=PermissionError('permission denied')):
        with pytest.raises(SystemExit) as error:
            organize_images.main()
    assert error.value.code == 1
    assert 'Cannot prepare output directory' in capsys.readouterr().err
    assert photo.exists()
    assert not output.exists()


def test_hash_database_save_failure_is_reported_gracefully(tmp_path, monkeypatch, capsys):
    source = tmp_path / 'photos'
    add_file(source / 'photo.jpg')
    output = tmp_path / 'sorted'
    monkeypatch.setattr(sys, 'argv', [str(SCRIPT), '--src', str(source), '--output', str(output)])
    with patch.object(organize_images.Persistence, 'add_hashes', side_effect=sqlite3.OperationalError('permission denied')):
        with pytest.raises(SystemExit) as error:
            organize_images.main()
    assert error.value.code == 1
    assert 'SQLite persistence failure' in capsys.readouterr().err


def test_custom_report_permission_failure_does_not_modify_archive(tmp_path, monkeypatch, caplog):
    source = tmp_path / 'archive'
    make_similar_photos(source)
    output = tmp_path / 'reports'
    before = snapshot_files(source)
    monkeypatch.setattr(sys, 'argv', [str(SCRIPT), '--src', str(source), '--output', str(output),
                                   '--find-similar-only'])
    with patch.object(similarity, 'write_reports', side_effect=PermissionError('permission denied')):
        organize_images.main()
    assert '[SIMILARITY] Could not write reports' in caplog.text
    assert snapshot_files(source) == before
    assert not output.exists()


def test_similarity_only_creates_only_reports_in_new_nested_output(tmp_path):
    source = tmp_path / 'archive'
    make_similar_photos(source)
    output = tmp_path / 'new' / 'nested' / 'reports'
    before = snapshot_files(source)

    run_cli(source, '--output', output, '--find-similar-only')

    assert snapshot_files(source) == before
    assert {path.relative_to(output).as_posix() for path in output.rglob('*')} == {
        'similar-images', 'similar-images/report.json', 'similar-images/report.html'
    }
    assert_report_links(output)


def test_unreadable_hash_database_stops_before_moves(tmp_path, monkeypatch, capsys):
    source = tmp_path / 'photos'
    photo = add_file(source / 'photo.jpg')
    output = tmp_path / 'sorted'
    monkeypatch.setattr(sys, 'argv', [str(SCRIPT), '--src', str(source), '--output', str(output)])
    with patch.object(organize_images.Persistence, 'hashes', side_effect=sqlite3.OperationalError('permission denied')):
        with pytest.raises(SystemExit) as error:
            organize_images.main()
    assert error.value.code == 1
    assert 'SQLite persistence failure' in capsys.readouterr().err
    assert photo.exists()


def test_empty_output_is_rejected(tmp_path):
    source = tmp_path / 'photos'
    photo = add_file(source / 'photo.jpg')
    result = subprocess.run(
        [sys.executable, str(SCRIPT), '--src', str(source), '--output', ''],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert '--output must not be empty' in result.stderr
    assert photo.exists()


@pytest.mark.parametrize('directory', ['organized-images', 'duplicate-files'])
def test_date_directory_symlink_cannot_redirect_moves_into_source(tmp_path, directory):
    source = tmp_path / 'photos'
    photo = add_file(source / 'photo.jpg')
    redirected = source / 'redirected'
    redirected.mkdir()
    output = tmp_path / 'sorted'
    date_directory = output / directory / DATE_DIRECTORY
    date_directory.parent.mkdir(parents=True)
    date_directory.symlink_to(redirected, target_is_directory=True)
    if directory == 'duplicate-files':
        digest = hashlib.sha256(photo.read_bytes()).hexdigest()
        store = organize_images.Persistence(str(output))
        store.add_hashes([digest])
        store.close()

    result = run_cli(source, '--output', output, '--find-similar')

    assert 'Destination escapes generated output directory' in result.stderr
    assert 'Errors:       1' in result.stdout
    assert 'Images analyzed: 0' in result.stdout
    assert photo.read_bytes() == b'photo contents'
    assert list(redirected.iterdir()) == []
