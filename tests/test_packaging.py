import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_installed_console_script_runs_outside_repository(tmp_path):
    work = tmp_path / "outside"
    work.mkdir()
    command = Path(sys.executable).parent / "imgorg"
    assert command.is_file(), "Install the project before running packaging tests"
    help_result = subprocess.run(
        [str(command), "--help"], cwd=work, capture_output=True, text=True
    )
    assert help_result.returncode == 0, help_result.stdout + help_result.stderr
    assert "--src" in help_result.stdout
    assert "--copy" in help_result.stdout
    assert "--dry-run" in help_result.stdout

    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "photo.jpg").write_bytes(b"fixture")
    output = tmp_path / "organized"
    dry_run = subprocess.run(
        [
            str(command),
            "--src",
            str(photos),
            "--output",
            str(output),
            "--copy",
            "--dry-run",
        ],
        cwd=work,
        capture_output=True,
        text=True,
    )
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr
    assert (photos / "photo.jpg").read_bytes() == b"fixture"
    assert not output.exists()


def test_direct_script_execution_still_supports_help(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "organize_images.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--similar-threshold" in result.stdout
