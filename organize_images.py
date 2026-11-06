"""Organized files by date and detect exact duplicate file using SHA-256."""

import argparse
import errno
import hashlib
import logging as log
import os
import shutil
import time
from datetime import datetime
from PIL import Image, UnidentifiedImageError
from PIL.ExifTags import Base, IFD
from pillow_heif import register_heif_opener


register_heif_opener()

log.getLogger().setLevel(log.INFO)


# Organized extensions
organized_extensions = ['heic', 'heif', 'jpg', 'jpeg', 'png', 'gif', 'tiff', 'tif', 'cr2', 'psd', 'mov', 'mp4']

''' Directory structure for moving images
    %Y: 4 digit
    %y: 2 digit
    %B: full name month
    %b
    %d: day 2digit
'''
dir_format = '%Y/%B'

# Create required folders
organized_images = 'organized-images'
need_review = 'needs-review'
duplicate = 'duplicate-files'
similar = 'similar-images'

# Ignored files and folders
ignore_dirs = ['.git', '.venv', organized_images, need_review, duplicate, similar ]
ignore_files = ['.gitignore', '.sha256_hashes.txt']

# File for saving hashes
hash_keys = set()

def load_hash_file():
    try:
        with open(data_file, 'r') as file:
            for line in file:
                file_hash = line.strip()

                # SHA-256 hashes have 64 hex characters
                if len(file_hash) == 64:
                    hash_keys.add(file_hash)

    except IOError:
        pass


def save_hash_file():
    with open(data_file, 'w') as file:
        for file_hash in sorted(hash_keys):
            file.write(f'{file_hash}\n')


def should_organized(file_name):
    splits = file_name.split('.')

    if splits[-1].lower() in organized_extensions:
        return True
    return False


def calculate_hash(file_path):
    sha256 = hashlib.sha256()

    with open(file_path, 'rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            sha256.update(chunk)

        return sha256.hexdigest()


def get_image_date(file_path):
    extension = os.path.splitext(file_path)[1].lower()

    if extension in {".jpg", ".jpeg", ".tif", ".tiff", ".heic", ".heif"}:
        try:
            with Image.open(file_path) as image:
                exif = image.getexif()
                date_original = exif.get_ifd(IFD.Exif).get(
                    Base.DateTimeOriginal
                )

                if date_original:
                    date = datetime.strptime(
                        date_original,
                        "%Y:%m:%d %H:%M:%S"
                    )
                    return date.strftime(dir_format)

        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            Image.DecompressionBombError,
            UnidentifiedImageError
        ):
            log.debug(
                "EXIF date unavailable for %s; using mtime",
                file_path
            )

    modified_date = datetime.fromtimestamp(
        os.path.getmtime(file_path)
    )
    return modified_date.strftime(dir_format)


def move_to_duplicate_folder(path, date):
    duplicate_path = os.path.join(duplicate_dir, date)
    if not os.path.isdir(duplicate_path):
        os.makedirs(duplicate_path)
    move_without_overwriting(path, duplicate_path)


def move_without_overwriting(source, destination_dir):
    name = os.path.basename(source)
    stem, extension = os.path.splitext(name)
    suffix = 0

    while True:
        candidate_name = name if suffix == 0 else f'{stem}_{suffix}{extension}'
        destination = os.path.join(destination_dir, candidate_name)
        try:
            # A hard link creates the destination only if it does not exist.
            os.link(source, destination, follow_symlinks=False)
        except FileExistsError:
            suffix += 1
            continue
        except OSError as err:
            if err.errno not in (errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP):
                raise
            try:
                if os.path.islink(source):
                    os.symlink(os.readlink(source), destination)
                else:
                    with open(source, 'rb') as source_file:
                        with open(destination, 'xb') as destination_file:
                            shutil.copyfileobj(source_file, destination_file)
                    shutil.copystat(source, destination)
            except FileExistsError:
                suffix += 1
                continue
            except Exception:
                if os.path.lexists(destination):
                    os.unlink(destination)
                raise

        try:
            os.unlink(source)
        except OSError:
            try:
                os.unlink(destination)
            except OSError:
                log.exception("Failed to roll back destination: %s", destination)
            raise
        return destination


def process_file(file_path, destination_dir, file_date, stats=None, category=None, dry_run=False):
    if stats is None:
        stats = {
            "organized": 0,
            "review": 0,
            "duplicates": 0,
            "errors": 0,
        }

    file_hash = calculate_hash(file_path)

    if file_hash in hash_keys:
        if dry_run:
            log.info("[DRY RUN] DUPLICATE: %s -> %s",
                     file_path, os.path.join(duplicate_dir, file_date))
        else:
            move_to_duplicate_folder(file_path, file_date)

        stats['duplicates'] += 1
        return

    if dry_run:
        log.info("[DRY RUN] MOVE: %s -> %s", file_path, destination_dir)
        destination = file_path
    else:
        os.makedirs(destination_dir, exist_ok=True)
        destination = move_without_overwriting(file_path, destination_dir)

    hash_keys.add(file_hash)

    if category is not None:
        stats[category] += 1

    return destination


def file_dates(path, dry_run=False, similarity_paths=None):
    load_hash_file()

    stats = {
        "organized": 0,
        "review": 0,
        "duplicates": 0,
        "errors": 0,
    }

    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(
            directory
            for directory in dirs
            if directory not in ignore_dirs
            and not os.path.islink(os.path.join(root, directory))
        )

        for filename in sorted(files):
            if filename in ignore_files:
                continue

            file_path = os.path.join(root, filename)

            if os.path.islink(file_path):
                log.warning("Skipping symbolic link: %s", file_path)
                continue

            try:
                image_date = get_image_date(file_path)

                if should_organized(filename):
                    destination_dir = os.path.join(path, organized_images, image_date)
                    category = "organized"
                else:
                    destination_dir = need_review_dir
                    category = "review"

                destination = process_file(file_path, destination_dir, image_date, stats=stats, category=category, dry_run=dry_run)
                if similarity_paths is not None and destination is not None:
                    similarity_paths.append({
                        'path': destination,
                        'original_name': filename,
                    })

            except Exception as err:
                stats["errors"] += 1
                log.error("Failed to process %s: %s", file_path, err)

    return stats


def print_summary(stats, dry_run=False):
    title = "Dry Run Summary" if dry_run else "Summary"

    print(f"\n--- {title} ---")
    print(f"Organized:    {stats['organized']}")
    print(f"Needs review: {stats['review']}")
    print(f"Duplicates:   {stats['duplicates']}")
    print(f"Errors:       {stats['errors']}")
    print(f"Total:        {sum(stats.values())}")


def main():
    global path, duplicate_dir, need_review_dir, data_file

    parser = argparse.ArgumentParser(
        description='This is a script for organizing photos by date'
    )
    parser.add_argument(
        '-s',
        '--src',
        help='the directory you want to organize',
        nargs='?',
        default=os.getcwd()
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Preview file operations without making changes'
    )
    parser.add_argument(
        '--find-similar',
        action='store_true',
        help='Report perceptually similar images without acting on matches'
    )
    parser.add_argument(
        '--find-similar-only',
        action='store_true',
        help='Scan an existing archive without organizing or relocating files'
    )
    parser.add_argument(
        '--similar-threshold',
        type=int,
        default=5,
        help='Maximum 64-bit pHash Hamming distance, from 0 to 64 (default: 5)'
    )

    args = parser.parse_args()

    if not 0 <= args.similar_threshold <= 64:
        parser.error('--similar-threshold must be between 0 and 64')

    path = os.path.abspath(args.src)

    if not os.path.isdir(path):
        parser.error(f"Source directory does not exist: {path}")

    if args.find_similar or args.find_similar_only:
        try:
            from similarity import run_similarity, scan_images
        except ImportError as err:
            parser.error(
                f'Similarity dependencies unavailable ({err}); '
                'install them with python -m pip install -r requirements.txt'
            )

    if args.find_similar_only:
        run_similarity(
            scan_images(path), path, args.similar_threshold, dry_run=args.dry_run
        )
        return

    duplicate_dir = os.path.join(path, duplicate)
    need_review_dir = os.path.join(path, need_review)
    data_file = os.path.join(path, '.sha256_hashes.txt')

    if not args.dry_run:
        os.makedirs(duplicate_dir, exist_ok=True)
        os.makedirs(need_review_dir, exist_ok=True)

    similarity_paths = [] if args.find_similar else None
    stats = file_dates(path, dry_run=args.dry_run, similarity_paths=similarity_paths)

    if not args.dry_run:
        save_hash_file()

    print_summary(stats, dry_run=args.dry_run)

    if args.find_similar:
        run_similarity(
            similarity_paths, path, args.similar_threshold, dry_run=args.dry_run
        )


if __name__ == "__main__":
    main()
