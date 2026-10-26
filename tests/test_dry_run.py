
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / "organize_images.py"


def test_dry_run_does_not_modify_files(tmp_path):
    photo = tmp_path / "photo.jpg"
    duplicate = tmp_path / "photo-copy.jpg"
    notes = tmp_path / "notes.txt"

    photo.write_bytes(b"same image")
    duplicate.write_bytes(b"same image")
    notes.write_text("some notes")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--src",
            str(tmp_path),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr

    # Original files must remain untouched.
    assert photo.read_bytes() == b"same image"
    assert duplicate.read_bytes() == b"same image"
    assert notes.read_text() == "some notes"

    # No output directories or hash file should be created.
    assert not (tmp_path / "organized-images").exists()
    assert not (tmp_path / "duplicate-files").exists()
    assert not (tmp_path / "needs-review").exists()
    assert not (tmp_path / ".sha256_hashes.txt").exists()

    # The preview should report planned operations.
    output = result.stdout + result.stderr
    assert "[DRY RUN]" in output
    assert "DUPLICATE" in output
