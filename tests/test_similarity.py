import json
import os
import random
import shutil
import subprocess
import sys
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

import pytest
from PIL import Image, ImageDraw
from pillow_heif import from_pillow

import organize_images
import similarity


SCRIPT = Path(__file__).resolve().parent.parent / 'organize_images.py'


@pytest.fixture
def pattern():
    with Image.new('RGB', (96, 64), '#eef2f5') as image:
        draw = ImageDraw.Draw(image)
        draw.rectangle((3, 5, 35, 56), fill='#142e50')
        draw.ellipse((40, 7, 90, 49), fill='#e86d24')
        draw.polygon([(20, 62), (50, 28), (82, 60)], fill='#348153')
        yield image


def candidate(path):
    return {'path': path, 'original_name': path.name}


def save_image(image, path, **kwargs):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() in {'.heic', '.heif'}:
        from_pillow(image).save(path, **kwargs)
    else:
        image.save(path, **kwargs)
    os.utime(path, (1705320000, 1705320000))
    return candidate(path)


def run_cli(source, *args):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), '--src', str(source), *args],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def read_report(source):
    return json.loads((source / 'similar-images' / 'report.json').read_text())


def snapshot_tree(source):
    return {
        path.relative_to(source).as_posix(): (
            path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode
        ) if path.is_file() else None
        for path in source.rglob('*')
    }


def test_jpeg_compression_and_resizing_match_but_different_image_does_not(tmp_path, pattern):
    images = [
        save_image(pattern, tmp_path / 'high.jpg', quality=95),
        save_image(pattern, tmp_path / 'low.jpg', quality=40),
    ]
    with pattern.resize((192, 128)) as larger:
        images.append(save_image(larger, tmp_path / 'resized.png'))
    with Image.frombytes('RGB', (96, 64), random.Random(42).randbytes(96 * 64 * 3)) as other:
        images.append(save_image(other, tmp_path / 'different.png'))

    report = similarity.find_similar(images, tmp_path)

    assert report['total_images_analyzed'] == 4
    assert report['total_similarity_groups'] == 1
    group = report['groups'][0]
    assert group['reference']['path'] == 'resized.png'
    assert {member['path'] for member in group['members']} == {'high.jpg', 'low.jpg', 'resized.png'}
    assert all(member['distance'] <= 5 for member in group['members'])
    assert all(len(member['phash']) == 16 for member in group['members'])


def record(path, fingerprint, width=100, height=100):
    return {
        'path': path, 'original_name': path, 'width': width, 'height': height,
        'phash': f'{fingerprint:016x}',
    }


@pytest.mark.parametrize('threshold', [0, 1, 5, 63, 64])
def test_hamming_threshold_is_inclusive(threshold):
    reference = record('reference.png', 0, width=200)
    boundary = record('boundary.png', (1 << threshold) - 1)
    groups = similarity.group_images([boundary, reference], threshold)
    assert groups[0]['members'][1]['distance'] == threshold
    if threshold < 64:
        outside = record('outside.png', (1 << (threshold + 1)) - 1)
        assert similarity.group_images([outside, reference], threshold) == []


def test_non_transitive_grouping_and_stable_reference():
    a = record('a.png', 0, width=300)
    b = record('b.png', 0b111, width=200)
    c = record('c.png', 0b111111)
    expected = similarity.group_images([a, b, c], 3)
    assert expected == similarity.group_images([c, b, a, b], 3)
    assert len(expected) == 1
    assert expected[0]['id'] == 'group-0001'
    assert expected[0]['reference']['path'] == 'a.png'
    assert [member['path'] for member in expected[0]['members']] == ['a.png', 'b.png']
    assert expected[0]['members'][1]['distance'] == 3


def test_equal_resolution_uses_path_tie_break_and_stable_group_ids():
    images = [record('b.png', 0), record('a.png', 0),
              record('d.png', 0xffff), record('c.png', 0xffff)]
    groups = similarity.group_images(images, 0)
    assert [group['id'] for group in groups] == ['group-0001', 'group-0002']
    assert [group['reference']['path'] for group in groups] == ['a.png', 'c.png']
    assert groups == similarity.group_images(reversed(images), 0)


@pytest.mark.parametrize('extension', ['.png', '.tiff', '.heic', '.heif', '.webp', '.bmp'])
def test_supported_formats_are_hashed_without_skips(tmp_path, pattern, extension):
    image = save_image(pattern, tmp_path / f'photo{extension}')
    with patch.object(similarity.imagehash, 'phash', wraps=similarity.imagehash.phash) as phash:
        report = similarity.find_similar([image, image], tmp_path)
    assert report['total_images_analyzed'] == 1
    assert report['groups'] == []
    assert phash.call_count == 1
    assert phash.call_args.kwargs == {'hash_size': 8}


def test_malformed_missing_files_and_videos_do_not_crash(tmp_path, caplog):
    malformed = tmp_path / 'bad.jpg'
    malformed.write_bytes(b'not an image')
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'video')
    missing = tmp_path / 'missing.png'
    with patch.object(similarity.Image, 'open', wraps=Image.open) as opened:
        report = similarity.find_similar(
            [candidate(malformed), candidate(video), candidate(missing)], tmp_path
        )
    assert report['total_images_analyzed'] == 0
    assert opened.call_count == 1
    assert '[SIMILARITY] Skipping' in caplog.text


@pytest.mark.parametrize('error', [
    PermissionError('unreadable'),
    Image.DecompressionBombError('too large'),
    Image.DecompressionBombWarning('large image warning'),
])
def test_decoder_errors_preserve_pixel_protection(tmp_path, error, caplog):
    pixel_limit = Image.MAX_IMAGE_PIXELS
    path = tmp_path / 'photo.jpg'
    path.write_bytes(b'placeholder for mocked decoder')
    with patch.object(similarity.Image, 'open', side_effect=error):
        report = similarity.find_similar([candidate(path)], tmp_path)
    assert report['total_images_analyzed'] == 0
    assert Image.MAX_IMAGE_PIXELS == pixel_limit
    assert '[SIMILARITY]' in caplog.text


@pytest.mark.parametrize('mode', ['--find-similar', '--find-similar-only'])
def test_dry_run_is_read_only_and_previews_groups(tmp_path, pattern, mode):
    save_image(pattern, tmp_path / 'nested' / 'first.jpg', quality=95)
    save_image(pattern, tmp_path / 'other' / 'second.jpg', quality=40)
    database = tmp_path / '.sha256_hashes.txt'
    database.write_text('existing hash database\n')
    before = snapshot_tree(tmp_path)

    result = run_cli(tmp_path, mode, '--dry-run')

    assert snapshot_tree(tmp_path) == before
    assert 'Similarity groups: 1' in result.stdout
    assert 'reference' in result.stdout
    assert 'distance' in result.stdout
    assert not (tmp_path / 'similar-images').exists()


def test_without_flag_does_not_analyze_or_generate_reports(tmp_path, pattern, monkeypatch):
    save_image(pattern, tmp_path / 'photo.jpg')
    monkeypatch.setattr(sys, 'argv', [str(SCRIPT), '--src', str(tmp_path)])
    with patch.object(similarity, 'find_similar', side_effect=AssertionError('not opted in')):
        organize_images.main()
    assert not (tmp_path / 'similar-images').exists()


def test_combined_mode_reports_final_collision_paths_and_preserves_summary(tmp_path, pattern):
    first = tmp_path / 'a' / 'same.jpg'
    second = tmp_path / 'b' / 'same.jpg'
    exact = tmp_path / 'c' / 'exact-copy.jpg'
    save_image(pattern, first, quality=95)
    save_image(pattern, second, quality=40)
    exact.parent.mkdir()
    shutil.copyfile(first, exact)
    originals = {first.read_bytes(), second.read_bytes()}
    # A pre-existing output must neither be scanned nor overwritten.
    old = tmp_path / 'organized-images' / '2024' / 'January' / 'same.jpg'
    save_image(pattern, old, quality=80)
    old_bytes = old.read_bytes()

    result = run_cli(tmp_path, '--find-similar')
    report = read_report(tmp_path)

    assert report['total_images_analyzed'] == 2
    assert report['total_similarity_groups'] == 1
    members = report['groups'][0]['members']
    assert {Path(member['path']).name for member in members} == {'same_1.jpg', 'same_2.jpg'}
    assert {member['original_name'] for member in members} == {'same.jpg'}
    assert {(tmp_path / member['path']).read_bytes() for member in members} == originals
    assert old.read_bytes() == old_bytes
    assert len(list((tmp_path / 'duplicate-files').rglob('*.jpg'))) == 1
    assert 'Organized:    2' in result.stdout
    assert 'Duplicates:   1' in result.stdout
    assert 'Errors:       0' in result.stdout
    assert 'Total:        3' in result.stdout


def test_failed_move_never_becomes_a_similarity_record(tmp_path, pattern, monkeypatch):
    save_image(pattern, tmp_path / 'photo.jpg')
    monkeypatch.setattr(organize_images, 'data_file', str(tmp_path / '.sha256_hashes.txt'), raising=False)
    monkeypatch.setattr(organize_images, 'need_review_dir', str(tmp_path / 'needs-review'), raising=False)
    records = []
    with patch.object(organize_images, 'hash_keys', set()):
        with patch.object(organize_images, 'move_without_overwriting', side_effect=OSError('failed')):
            stats = organize_images.file_dates(str(tmp_path), similarity_paths=records)
    assert records == []
    assert stats == {'organized': 0, 'review': 0, 'duplicates': 0, 'errors': 1}
    assert (tmp_path / 'photo.jpg').exists()


class ReportLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for key, value in attrs:
            if key in {'href', 'src'}:
                self.links.append(value)


def test_json_html_contents_escaping_and_safe_nested_links(tmp_path, pattern):
    # Exercise URL syntax, HTML metacharacters, Unicode and directory names.
    name = 'odd "<script>alert(1)<& #?%é.jpg'
    first = save_image(pattern, tmp_path / 'nested #?' / name, quality=95)
    second = save_image(pattern, tmp_path / 'javascript:photos' / 'other.jpg', quality=40)
    report = similarity.find_similar([first, second], tmp_path)
    similarity.write_reports(report, tmp_path)
    loaded = read_report(tmp_path)
    content = (tmp_path / 'similar-images' / 'report.html').read_text()

    assert loaded == report
    assert loaded['algorithm'] == 'phash'
    assert loaded['hash_size'] == 8
    assert loaded['threshold'] == 5
    assert datetime.fromisoformat(loaded['generated_at']).tzinfo is not None
    assert loaded['total_images_analyzed'] == 2
    assert loaded['total_similarity_groups'] == 1
    assert 'source directory' in loaded['path_base']
    assert loaded['groups'][0]['reference']['distance'] == 0
    assert all(member['width'] == 96 and member['height'] == 64
               for member in loaded['groups'][0]['members'])
    assert '&lt;script&gt;' in content
    assert 'HEIC/HEIF' in content
    assert 'Open original file' in content
    assert 'distance:' in content
    parser = ReportLinks()
    parser.feed(content)
    assert 'script' not in parser.tags
    assert parser.links
    for link in parser.links:
        assert link.startswith('../')
        assert '#' not in link and '?' not in link and ':' not in link
        assert (tmp_path / 'similar-images' / unquote(link)).is_file()


def test_similarity_only_scans_previous_archives_without_organization(tmp_path, pattern, monkeypatch):
    source = tmp_path / 'organized-images'
    save_image(pattern, source / '2020' / 'July' / 'first.jpg', quality=95)
    save_image(pattern, source / '2025' / 'November' / 'second.jpg', quality=40)
    # These names must not be excluded in archive-only mode.
    save_image(pattern, source / 'needs-review' / 'third.bmp')
    save_image(pattern, source / 'duplicate-files' / 'fourth.png')
    (source / '.sha256_hashes.txt').write_text('do not modify\n')
    generated = source / 'similar-images'
    save_image(pattern, generated / 'must-not-scan.png')
    generated.joinpath('report.json').write_text('old report')
    before = snapshot_tree(source)
    monkeypatch.setattr(sys, 'argv', [str(SCRIPT), '--src', str(source),
                                   '--find-similar-only', '--find-similar'])

    with (
        patch.object(organize_images, 'file_dates', side_effect=AssertionError('organization')),
        patch.object(organize_images, 'process_file', side_effect=AssertionError('processing')),
        patch.object(organize_images, 'calculate_hash', side_effect=AssertionError('SHA-256')),
        patch.object(organize_images, 'move_without_overwriting', side_effect=AssertionError('move')),
        patch.object(organize_images, 'Persistence', side_effect=AssertionError('open database')),
    ):
        organize_images.main()

    report = read_report(source)
    assert report['total_images_analyzed'] == 4
    assert report['total_similarity_groups'] == 1
    assert len(report['groups'][0]['members']) == 4
    after = snapshot_tree(source)
    for name, value in before.items():
        if not name.startswith('similar-images/'):
            assert after[name] == value
    assert not (source / 'organized-images').exists()
    assert not any('must-not-scan' in member['path'] for member in report['groups'][0]['members'])


def test_similarity_only_creates_only_reports_and_leaves_database_untouched(tmp_path, pattern):
    save_image(pattern, tmp_path / 'a.jpg', quality=95)
    save_image(pattern, tmp_path / 'nested' / 'b.jpg', quality=40)
    (tmp_path / '.sha256_hashes.txt').write_text('unchanged\n')
    before = snapshot_tree(tmp_path)
    run_cli(tmp_path, '--find-similar-only', '--similar-threshold', '5')
    after = snapshot_tree(tmp_path)
    assert {name: after[name] for name in before} == before
    assert set(after) - set(before) == {
        'similar-images', 'similar-images/report.json', 'similar-images/report.html'
    }


@pytest.mark.parametrize('mode', ['--find-similar', '--find-similar-only'])
def test_empty_source_writes_valid_empty_reports(tmp_path, mode):
    run_cli(tmp_path, mode)
    report = read_report(tmp_path)
    assert report['total_images_analyzed'] == 0
    assert report['total_similarity_groups'] == 0
    assert report['groups'] == []
    assert 'No similar image groups found' in (tmp_path / 'similar-images' / 'report.html').read_text()


@pytest.mark.parametrize('threshold', ['-1', '65', 'bad'])
def test_cli_rejects_invalid_threshold_before_writing(tmp_path, threshold):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), '--src', str(tmp_path),
         '--find-similar-only', '--similar-threshold', threshold],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert '--similar-threshold' in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_report_symlinks_cannot_overwrite_source_images(tmp_path, pattern):
    original = tmp_path / 'original.png'
    save_image(pattern, original)
    before = original.read_bytes()
    directory = tmp_path / 'similar-images'
    directory.symlink_to(tmp_path, target_is_directory=True)
    report = similarity.find_similar([], tmp_path)
    with pytest.raises(OSError, match='symbolic link'):
        similarity.write_reports(report, tmp_path)
    directory.unlink()
    directory.mkdir()
    (directory / 'report.json').symlink_to(original)
    os.link(original, directory / 'report.html')
    similarity.write_reports(report, tmp_path)
    assert original.read_bytes() == before
    assert not (directory / 'report.json').is_symlink()


def test_similarity_warning_does_not_increment_organization_errors(tmp_path):
    (tmp_path / 'bad.jpg').write_bytes(b'invalid image')
    result = run_cli(tmp_path, '--find-similar')
    assert 'Organized:    1' in result.stdout
    assert 'Errors:       0' in result.stdout
    assert '[SIMILARITY] Skipping' in result.stderr
    assert read_report(tmp_path)['total_images_analyzed'] == 0


def test_archive_scan_excludes_artifacts_symlinks_and_special_files(tmp_path, pattern):
    save_image(pattern, tmp_path / 'real.png')
    save_image(pattern, tmp_path / 'similar-images' / 'ignored.png')
    save_image(pattern, tmp_path / '.venv' / 'ignored.png')
    (tmp_path / 'link.png').symlink_to(tmp_path / 'real.png')
    (tmp_path / 'cycle').symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / '.sha256_hashes.txt').write_text('database')
    os.mkfifo(tmp_path / 'pipe.png')

    with patch.object(similarity.Image, 'open', wraps=Image.open) as opened:
        report = similarity.find_similar(similarity.scan_images(tmp_path), tmp_path)

    assert report['total_images_analyzed'] == 1
    assert opened.call_count == 1


def test_missing_dependencies_fail_before_organization(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', [str(SCRIPT), '--src', str(tmp_path), '--find-similar'])
    monkeypatch.setitem(sys.modules, 'similarity', None)
    with patch.object(organize_images, 'file_dates', side_effect=AssertionError('organization')):
        with pytest.raises(SystemExit) as error:
            organize_images.main()
    assert error.value.code == 2
    assert 'pip install -r requirements.txt' in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_report_permission_error_is_a_similarity_warning(tmp_path, caplog):
    with patch.object(similarity, 'write_reports', side_effect=PermissionError('read-only directory')):
        report = similarity.run_similarity([], tmp_path)
    assert report['total_images_analyzed'] == 0
    assert '[SIMILARITY] Could not write reports' in caplog.text
    assert list(tmp_path.iterdir()) == []
