import os
import sqlite3
from pathlib import Path

import pytest

import organize_images as app
from test_output import add_file, run_cli, snapshot_files


def records(output):
    with sqlite3.connect(output / app.DATABASE_NAME) as db:
        return db.execute('SELECT source, destination FROM copy_intents ORDER BY source').fetchall()


@pytest.mark.parametrize('alias_type', ['case', 'symlink'])
def test_repeat_copy_reuses_source_alias(tmp_path, alias_type):
    source = tmp_path / 'photos'
    original = add_file(source / 'IMG_001.jpg', b'photo')
    output = tmp_path / 'output'
    first = run_cli(source, '--copy', '--output', output)
    assert 'Organized:    1' in first.stdout
    if alias_type == 'case':
        if not (tmp_path / 'PHOTOS').exists():
            pytest.skip('Temporary filesystem is case-sensitive')
        alias = tmp_path / 'PHOTOS'
    else:
        alias = tmp_path / 'source-alias'
        alias.symlink_to(source, target_is_directory=True)
    before = snapshot_files(output)
    preview = run_cli(alias, '--copy', '--output', output, '--dry-run')
    assert 'ALREADY COPIED' in preview.stderr
    assert snapshot_files(output) == before
    repeated = run_cli(alias, '--copy', '--output', output)
    assert 'ALREADY COPIED' in repeated.stderr
    assert original.read_bytes() == b'photo'
    assert len(records(output)) == 1
    assert len(list((output / 'organized-images').rglob('*.jpg'))) == 1
    assert not list((output / 'duplicate-files').rglob('*.jpg'))


def test_distinct_hardlinks_keep_distinct_records(tmp_path):
    source = tmp_path / 'source'
    first = add_file(source / 'a.jpg', b'photo')
    os.link(first, source / 'b.jpg')
    output = tmp_path / 'output'
    result = run_cli(source, '--copy', '--output', output)
    assert 'Duplicates:   1' in result.stdout
    assert len(records(output)) == 2


def test_case_distinct_hardlinks_are_not_aliases_on_case_sensitive_fs(tmp_path):
    source = tmp_path / 'source'
    first = add_file(source / 'a.jpg', b'photo')
    if (source / 'A.jpg').exists():
        pytest.skip('Temporary filesystem is case-insensitive')
    os.link(first, source / 'A.jpg')
    output = tmp_path / 'output'
    run_cli(source, '--copy', '--output', output)
    assert len(records(output)) == 2


def test_source_relocation_does_not_adopt_old_record(tmp_path):
    source = tmp_path / 'source'
    original = add_file(source / 'photo.jpg', b'photo')
    output = tmp_path / 'output'
    run_cli(source, '--copy', '--output', output)
    renamed = source / 'renamed.jpg'
    original.rename(renamed)
    result = run_cli(source, '--copy', '--output', output)
    assert 'ALREADY COPIED' not in result.stderr
    assert 'Duplicates:   1' in result.stdout
    assert renamed.read_bytes() == b'photo'
    assert len(records(output)) == 2


def test_output_relocation_reuses_verified_copy_on_same_filesystem(tmp_path):
    source = tmp_path / 'source'
    add_file(source / 'photo.jpg', b'photo')
    output = tmp_path / 'output'
    run_cli(source, '--copy', '--output', output)
    moved = tmp_path / 'moved-output'
    output.rename(moved)
    result = run_cli(source, '--copy', '--output', moved)
    assert 'ALREADY COPIED' in result.stderr
    assert len(records(moved)) == 1
    assert len(list((moved / 'organized-images').rglob('*.jpg'))) == 1


def test_separate_outputs_have_separate_records(tmp_path):
    source = tmp_path / 'source'
    add_file(source / 'photo.jpg', b'photo')
    for output in (tmp_path / 'one', tmp_path / 'two'):
        result = run_cli(source, '--copy', '--output', output)
        assert 'Organized:    1' in result.stdout
        assert len(records(output)) == 1


def test_alias_does_not_reuse_replaced_destination(tmp_path):
    source = tmp_path / 'source'
    original = add_file(source / 'photo.jpg', b'photo')
    output = tmp_path / 'output'
    run_cli(source, '--copy', '--output', output)
    destination = next((output / 'organized-images').rglob('photo.jpg'))
    replacement = add_file(tmp_path / 'replacement.jpg', b'photo')
    os.replace(replacement, destination)
    alias = tmp_path / 'source-alias'
    alias.symlink_to(source, target_is_directory=True)
    result = run_cli(alias, '--copy', '--output', output)
    assert 'ALREADY COPIED' not in result.stderr
    assert destination.read_bytes() == b'photo'
    assert 'Duplicates:   1' in result.stdout
    assert original.read_bytes() == b'photo'
    assert len(records(output)) == 1
    assert len(list((output / 'duplicate-files').rglob('*.jpg'))) == 1
