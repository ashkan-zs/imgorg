## Organize Image 
A script for organizing images and find duplicates

Create and activate a virtual environment, then install the dependencies:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

`pillow-heif` is required for HEIC/HEIF support. JPEG, TIFF, and HEIC/HEIF
photos use EXIF `DateTimeOriginal` when available; otherwise, the script uses
the filesystem modification time.

Run the script with `python organize_images.py --src /path/to/photos`.
Add `--dry-run` to preview changes.

To run the tests:

```sh
python -m pip install pytest
python -m pytest -v
```
