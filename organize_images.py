"""Organized files by date and detect exact duplicate file using SHA-256."""

import argparse
import errno
import hashlib
import logging as log
import os
import shutil
import time
from datetime import datetime


log.getLogger().setLevel(log.INFO)


# Organized extensions
organized_extensions = ['heic', 'jpg', 'jpeg', 'png', 'gif', 'tiff', 'tif', 'cr2', 'psd', 'mov', 'mp4']

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

# Ignored files and folders
ignore_dirs = ['.git', '.venv', organized_images, need_review, duplicate ]
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


def get_image_date(path):
    ctime = time.ctime(os.path.getmtime(path))
    image_date = datetime.strptime(ctime, '%a %b %d %H:%M:%S %Y')
    image_date = image_date.strftime(dir_format)

    return image_date


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


def process_file(file_path, destination_dir, file_date):
    file_hash = calculate_hash(file_path)

    if file_hash in hash_keys:
        move_to_duplicate_folder(file_path, file_date)
        return

    os.makedirs(destination_dir, exist_ok=True)
    move_without_overwriting(file_path, destination_dir)
    hash_keys.add(file_hash)


def file_dates(path):
    load_hash_file()

    for root, dirs, files in os.walk(path):
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in ignore_dirs
            and not os.path.islink(os.path.join(root, directory))
        ]

        for filename in files:
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

                else:
                    destination_dir = need_review_dir

                process_file(file_path, destination_dir, image_date)

            except Exception as err:
                log.error("Failed to process %s: %s", file_path, err)


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

    args = parser.parse_args()

    path = os.path.abspath(args.src)

    if not os.path.isdir(path):
        parser.error(f"Source directory does not exist: {path}")

    duplicate_dir = os.path.join(path, duplicate)
    need_review_dir = os.path.join(path, need_review)
    data_file = os.path.join(path, '.sha256_hashes.txt')

    os.makedirs(duplicate_dir, exist_ok=True)
    os.makedirs(need_review_dir, exist_ok=True)

    file_dates(path)
    save_hash_file()


if __name__ == "__main__":
    main()