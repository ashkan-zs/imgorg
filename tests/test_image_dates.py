import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image
from PIL.ExifTags import Base, IFD
from pillow_heif import from_pillow

import organize_images


SCRIPT = Path(__file__).resolve().parent.parent / "organize_images.py"
CAPTURE_DATE = "2020:07:14 12:34:56"
MTIME = datetime(2024, 1, 15, 12).timestamp()


@pytest.fixture
def make_photo(tmp_path):
    def create(extension, date_original=CAPTURE_DATE):
        photo = tmp_path / f"photo{extension}"
        exif = Image.Exif()
        # The generic timestamp must not override DateTimeOriginal.
        exif[Base.DateTime] = "2022:03:01 00:00:00"
        if date_original is not None:
            exif[IFD.Exif] = {Base.DateTimeOriginal: date_original}

        with Image.new("RGB", (16, 16), "blue") as image:
            if extension.lower() in {".heic", ".heif"}:
                # Encode real HEIF data independently of Pillow's opener.
                from_pillow(image).save(photo, exif=exif.tobytes())
            else:
                image.save(photo, exif=exif)

        os.utime(photo, (MTIME, MTIME))
        return photo

    return create


def test_oversized_image_falls_back_to_mtime(tmp_path):
    photo = tmp_path / "oversized.tiff"
    photo.write_bytes(b"mocked oversized image")
    os.utime(photo, (MTIME, MTIME))
    pixel_limit = Image.MAX_IMAGE_PIXELS

    with patch.object(
        organize_images.Image,
        "open",
        side_effect=Image.DecompressionBombError("Image size exceeds limit"),
    ) as open_image:
        assert organize_images.get_image_date(photo) == datetime.fromtimestamp(
            MTIME
        ).strftime(organize_images.dir_format)

    open_image.assert_called_once_with(photo)
    assert Image.MAX_IMAGE_PIXELS == pixel_limit


@pytest.mark.parametrize("extension", [".jpg", ".heic", ".heif", ".HEIC", ".HEIF"])
def test_capture_date_is_used(make_photo, extension, monkeypatch):
    photo = make_photo(extension)
    # Parsing EXIF must remain independent of the output directory format.
    monkeypatch.setattr(organize_images, "dir_format", "%Y/%m/%d")

    with Image.open(photo) as image:
        if extension.lower() in {".heic", ".heif"}:
            assert image.format == "HEIF"
            image.load()
        assert image.getexif().get_ifd(IFD.Exif)[Base.DateTimeOriginal] == CAPTURE_DATE

    assert organize_images.get_image_date(photo) == "2020/07/14"


@pytest.mark.parametrize("extension", [".jpg", ".heic", ".heif"])
@pytest.mark.parametrize("date_original", [None, "not a date"])
def test_missing_or_invalid_capture_date_uses_mtime(make_photo, extension, date_original):
    photo = make_photo(extension, date_original)

    assert organize_images.get_image_date(photo) == datetime.fromtimestamp(
        MTIME
    ).strftime(organize_images.dir_format)


@pytest.mark.parametrize("extension", [".heic", ".heif"])
def test_heif_capture_date_organizes_file(make_photo, extension):
    photo = make_photo(extension)
    original = photo.read_bytes()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--src", str(photo.parent)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    capture_directory = datetime(2020, 7, 14).strftime(organize_images.dir_format)
    destination = photo.parent / "organized-images" / capture_directory / photo.name
    assert destination.read_bytes() == original
    assert not photo.exists()
    assert "Organized:    1" in result.stdout
    assert "Errors:       0" in result.stdout
