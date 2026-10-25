
import hashlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import organize_images


SCRIPT = Path(__file__).resolve().parent.parent / "organize_images.py"


def test_identical_content_different_names(tmp_path):
    """Files with identical content should be detected as duplicates."""
    first = tmp_path / "photo1.jpg"
    second = tmp_path / "photo2.jpg"

    first.write_bytes(b"same image content")
    second.write_bytes(b"same image content")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--src", str(tmp_path)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr

    organized = list((tmp_path / "organized-images").rglob("*.jpg"))
    duplicates = list((tmp_path / "duplicate-files").rglob("*.jpg"))

    assert len(organized) == 1
    assert len(duplicates) == 1

    assert organized[0].read_bytes() == b"same image content"
    assert duplicates[0].read_bytes() == b"same image content"


def test_failed_move_does_not_register_hash(tmp_path):
    """A failed move must not add the file hash to the index."""
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"unique image content")

    destination = tmp_path / "organized-images"
    expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    organize_images.hash_keys.clear()

    with patch.object(
        organize_images,
        "move_without_overwriting",
        side_effect=OSError("Simulated move failure"),
    ):
        with pytest.raises(OSError, match="Simulated move failure"):
            organize_images.process_file(
                source,
                destination,
                "2026/October",
            )

    assert expected_hash not in organize_images.hash_keys
    assert source.exists()
