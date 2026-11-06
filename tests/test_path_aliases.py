import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

import organize_images
import similarity
from path_utils import path_is_within, same_path


SCRIPT = Path(__file__).resolve().parent.parent / 'organize_images.py'


@pytest.fixture
def case_sensitive(tmp_path):
    (tmp_path / 'case-probe').mkdir()
    return not (tmp_path / 'CASE-PROBE').exists()


@pytest.fixture
def case_insensitive(case_sensitive):
    if case_sensitive:
        pytest.skip('Temporary filesystem is case-sensitive')


def photo(path, color='blue'):
    path.parent.mkdir(parents=True, exist_ok=True)
    with Image.new('RGB', (16, 16), color) as image:
        image.save(path)
    return path


def snapshot(root):
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        if path.is_file() else None
        for path in root.rglob('*')
    }


def run_cli(source, output, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), '--src', str(source), '--output', str(output), *args],
        capture_output=True, text=True,
    )


@pytest.mark.parametrize('location', ['inside', 'nested', 'equal', 'outside'])
@pytest.mark.parametrize('mode', ['organize', 'combined', 'similarity-only'])
def test_case_alias_output_keeps_existing_files_untouched(tmp_path, case_insensitive, location, mode):
    source = tmp_path / 'photos'
    incoming = photo(source / 'incoming.png')
    output, alias = {
        'inside': (source / 'sorted', source / 'SORTED'),
        'nested': (source / 'nested' / 'sorted', source / 'NESTED' / 'SORTED'),
        'equal': (source, tmp_path / 'PHOTOS'),
        'outside': (tmp_path / 'sorted', tmp_path / 'SORTED'),
    }[location]
    # When source equals output, loose files are input; reports are excluded.
    keep = photo((output / 'similar-images' if location == 'equal' else output) / 'keep.png', 'red')
    original = keep.read_bytes(), keep.stat().st_mtime_ns
    args = {'organize': [], 'combined': ['--find-similar'],
            'similarity-only': ['--find-similar-only']}[mode]

    result = run_cli(source, alias, *args)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (keep.read_bytes(), keep.stat().st_mtime_ns) == original
    if mode == 'similarity-only':
        assert incoming.exists()
        assert 'Images analyzed: 1' in result.stdout
    else:
        assert not incoming.exists()
        assert 'Organized:    1' in result.stdout
        assert 'Total:        1' in result.stdout
        assert 'Errors:       0' in result.stdout
        if mode == 'combined':
            assert 'Images analyzed: 1' in result.stdout


def test_similarity_scanner_excludes_case_alias_directly(tmp_path, case_insensitive):
    source = tmp_path / 'photos'
    incoming = photo(source / 'incoming.png')
    output = source / 'nested' / 'sorted'
    photo(output / 'keep.png')
    before = snapshot(source)

    candidates = list(similarity.scan_images(source, excluded_dirs=[source / 'NESTED' / 'SORTED']))

    assert [Path(item['path']) for item in candidates] == [incoming]
    assert snapshot(source) == before


@pytest.mark.parametrize('mode', [[], ['--find-similar'], ['--find-similar-only']])
def test_case_alias_exclusion_during_dry_run(tmp_path, case_insensitive, mode):
    source = tmp_path / 'photos'
    photo(source / 'incoming.png')
    photo(source / 'sorted' / 'keep.png', 'red')
    before = snapshot(tmp_path)

    result = run_cli(source, source / 'SORTED', '--dry-run', *mode)

    assert result.returncode == 0, result.stdout + result.stderr
    assert snapshot(tmp_path) == before
    if mode:
        assert 'Images analyzed: 1' in result.stdout
    if mode != ['--find-similar-only']:
        assert 'Total:        1' in result.stdout


@pytest.mark.parametrize('name, args', [
    ('organized-images', []), ('duplicate-files', []), ('needs-review', []),
    ('similar-images', ['--find-similar-only']),
])
def test_case_alias_cannot_bypass_source_overlap_check(tmp_path, case_insensitive, name, args):
    output = tmp_path / 'sorted'
    source = output / name / 'archive'
    photo(source / 'incoming.png')
    before = snapshot(tmp_path)

    result = run_cli(source, tmp_path / 'SORTED', '--dry-run', *args)

    assert result.returncode == 2
    assert 'Source must not be inside a generated output directory' in result.stderr
    assert snapshot(tmp_path) == before


def test_containment_handles_missing_children_of_case_aliases(tmp_path, case_insensitive):
    root = tmp_path / 'sorted'
    root.mkdir()
    alias = tmp_path / 'SORTED'
    before = snapshot(tmp_path)

    assert same_path(root, alias)
    assert same_path(root / 'new', alias / 'new')
    assert path_is_within(alias / 'new' / 'date', root / 'new')
    organize_images.validate_destination_path(alias / 'new' / 'date', root / 'new')
    assert not path_is_within(tmp_path / 'elsewhere', alias)
    assert snapshot(tmp_path) == before


def test_case_sensitive_directories_are_not_conflated(tmp_path, case_sensitive):
    if not case_sensitive:
        pytest.skip('Temporary filesystem is case-insensitive')
    source = tmp_path / 'photos'
    lower = photo(source / 'sorted' / 'incoming.png')
    upper = source / 'SORTED'
    keep = photo(upper / 'keep.png', 'red')
    original = keep.read_bytes(), keep.stat().st_mtime_ns

    assert not same_path(lower.parent, upper)
    assert not same_path(lower.parent / 'new', upper / 'new')
    assert not path_is_within(lower, upper)
    candidates = list(similarity.scan_images(source, excluded_dirs=[upper]))
    assert [Path(item['path']) for item in candidates] == [lower]
    result = run_cli(source, upper)

    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Total:        1' in result.stdout
    assert not lower.exists()
    assert (keep.read_bytes(), keep.stat().st_mtime_ns) == original


def test_symlink_aliases_are_compared_without_traversing_source_links(tmp_path):
    source = tmp_path / 'photos'
    incoming = photo(source / 'incoming.png')
    output = source / 'nested' / 'sorted'
    keep = photo(output / 'keep.png', 'red')
    original = keep.read_bytes(), keep.stat().st_mtime_ns
    alias = tmp_path / 'output-alias'
    alias.symlink_to(output, target_is_directory=True)
    outside = tmp_path / 'outside'
    photo(outside / 'do-not-follow.png')
    (source / 'outside-alias').symlink_to(outside, target_is_directory=True)
    (source / 'cycle').symlink_to(source, target_is_directory=True)

    assert same_path(output, alias)
    assert path_is_within(alias / 'missing' / 'date', output)
    assert [Path(item['path']) for item in similarity.scan_images(source, [alias])] == [incoming]
    result = run_cli(source, alias)

    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Total:        1' in result.stdout
    assert (keep.read_bytes(), keep.stat().st_mtime_ns) == original
    assert (outside / 'do-not-follow.png').exists()


@pytest.mark.parametrize('mode', [[], ['--find-similar'], ['--find-similar-only']])
def test_missing_output_is_not_created_for_path_comparisons(tmp_path, mode):
    source = tmp_path / 'photos'
    incoming = photo(source / 'incoming.png')
    output = source / 'new' / 'nested' / 'sorted'
    before = snapshot(tmp_path)

    assert same_path(output, output)
    assert not same_path(source, output)
    assert path_is_within(output / 'date', output)
    assert not path_is_within(source, output)
    assert [Path(item['path']) for item in similarity.scan_images(source, [output])] == [incoming]
    result = run_cli(source, output, '--dry-run', *mode)

    assert result.returncode == 0, result.stdout + result.stderr
    assert snapshot(tmp_path) == before


def test_destination_containment_rejects_a_symlink_escape(tmp_path):
    root = tmp_path / 'sorted'
    root.mkdir()
    outside = tmp_path / 'elsewhere'
    outside.mkdir()
    (root / 'escape').symlink_to(outside, target_is_directory=True)

    assert not path_is_within(root / 'escape' / 'new', root)
    with pytest.raises(OSError, match='Destination escapes'):
        organize_images.validate_destination_path(root / 'escape' / 'new', root)
