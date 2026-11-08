"""Organized files by date and detect exact duplicate file using SHA-256."""

import argparse
import errno
import hashlib
import logging as log
import os
import shutil
import time
import sqlite3
import stat
import tempfile
import ctypes
import sys
from datetime import datetime
from PIL import Image, UnidentifiedImageError
from PIL.ExifTags import Base, IFD
from pillow_heif import register_heif_opener

from path_utils import is_copy_staging, is_staging_name, path_is_within, same_directory_entry, same_path
from persistence import DATABASE_NAME, DATABASE_FILES, Persistence, is_database_artifact


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


def validate_destination_path(destination, root):
    if not path_is_within(destination, root):
        raise OSError(f'Destination escapes generated output directory: {destination}')


def move_to_duplicate_folder(path, date, expected_hash=None, before_publish=None):
    duplicate_path = os.path.join(duplicate_dir, date)
    if not os.path.isdir(duplicate_path):
        os.makedirs(duplicate_path)
    move_without_overwriting(path, duplicate_path, expected_hash=expected_hash, before_publish=before_publish)


def move_without_overwriting(source, destination_dir, expected_hash=None, before_publish=None):
    name = os.path.basename(source)
    stem, extension = os.path.splitext(name)
    suffix = 0

    while True:
        candidate_name = name if suffix == 0 else f'{stem}_{suffix}{extension}'
        destination = os.path.join(destination_dir, candidate_name)
        if before_publish is not None:
            if os.path.lexists(destination):
                suffix += 1
                continue
            before_publish(destination, os.stat(source, follow_symlinks=False))
        try:
            # A hard link creates the destination only if it does not exist.
            os.link(source, destination, follow_symlinks=False)
        except FileExistsError:
            suffix += 1
            continue
        except OSError as err:
            if err.errno not in (errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP):
                raise
            if not os.path.islink(source):
                return move_via_staging(source, destination_dir, expected_hash=expected_hash, before_publish=before_publish)
            try:
                os.symlink(os.readlink(source), destination)
            except FileExistsError:
                suffix += 1
                continue

        # Keep a complete destination if source removal fails. Removing it here
        # could delete a different file if the destination was replaced meanwhile.
        os.unlink(source)
        return destination


def rename_without_overwriting(source, destination):
    """Use a native exclusive rename; never emulate it with check-then-rename."""
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin' and hasattr(library, 'renamex_np'):
        function = library.renamex_np
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        arguments = (os.fsencode(source), os.fsencode(destination), 4)  # RENAME_EXCL
    elif sys.platform.startswith('linux') and hasattr(library, 'renameat2'):
        function = library.renameat2
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        arguments = (-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise OSError(errno.EOPNOTSUPP, 'Atomic exclusive rename unavailable')
    function.restype = ctypes.c_int
    if function(*arguments) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)



def move_via_staging(source, destination_dir, expected_hash=None, before_publish=None):
    """Copy and verify before exclusive publication; retain the source until then."""
    def source_identity():
        status = os.stat(source, follow_symlinks=False)
        if not stat.S_ISREG(status.st_mode):
            raise OSError(f'Move source must be a regular file: {source}')
        return (status.st_dev, status.st_ino, status.st_size,
                status.st_mtime_ns, status.st_ctime_ns)

    identity = source_identity()
    if expected_hash is None:
        expected_hash = calculate_hash(source)
    descriptor, temporary = tempfile.mkstemp(prefix='.copy-', dir=destination_dir)
    try:
        status = os.fstat(descriptor)
        temporary_identity = (status.st_dev, status.st_ino)
    finally:
        os.close(descriptor)
    try:
        shutil.copy2(source, temporary)
        status = os.stat(temporary, follow_symlinks=False)
        if (status.st_dev, status.st_ino) != temporary_identity:
            raise OSError('Staged file was replaced during copying')
        if calculate_hash(temporary) != expected_hash or source_identity() != identity:
            raise OSError('Source changed during copying')

        stem, extension = os.path.splitext(os.path.basename(source))
        suffix = 0
        while True:
            name = f'{stem}_{suffix}{extension}' if suffix else f'{stem}{extension}'
            destination = os.path.join(destination_dir, name)
            if before_publish is not None:
                if os.path.lexists(destination):
                    suffix += 1
                    continue
                before_publish(destination, os.stat(temporary))
            try:
                try:
                    os.link(temporary, destination)
                except OSError as err:
                    if err.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV):
                        raise
                    rename_without_overwriting(temporary, destination)
                break
            except FileExistsError:
                suffix += 1

        # A failed unlink must never remove a destination that another process
        # may have replaced. A complete extra copy is safer than deleting it.
        if source_identity() != identity:
            raise OSError('Source changed before removal')
        os.unlink(source)
        return destination
    finally:
        try:
            status = os.stat(temporary, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if (status.st_dev, status.st_ino) == temporary_identity:
                os.unlink(temporary)


def copy_without_overwriting(source, destination_dir, expected_hash=None, before_publish=None):
    if os.path.islink(source) or not os.path.isfile(source):
        raise OSError(f'Copy source must be a regular file: {source}')
    descriptor, temporary = tempfile.mkstemp(prefix='.copy-', dir=destination_dir)
    os.close(descriptor)
    try:
        shutil.copy2(source, temporary)
        if expected_hash is not None and calculate_hash(temporary) != expected_hash:
            raise OSError('Source changed during copying')
        stem, extension = os.path.splitext(os.path.basename(source))
        suffix = 0
        while True:
            name = f'{stem}_{suffix}{extension}' if suffix else f'{stem}{extension}'
            destination = os.path.join(destination_dir, name)
            if before_publish is not None:
                before_publish(destination, os.stat(temporary))
            try:
                # Publish the completed temporary inode exclusively, never the source inode.
                try:
                    os.link(temporary, destination)
                except OSError as err:
                    if err.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV):
                        raise
                    rename_without_overwriting(temporary, destination)
                return destination
            except FileExistsError:
                suffix += 1
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class CopyRecords:
    def __init__(self, output, store):
        self.output = output
        self.store = store
        self.records = store.records()

    def source_key(self, source):
        key = os.path.realpath(source)
        if key in self.records:
            return key
        return next((stored for stored in self.records
                     if same_directory_entry(source, stored)), key)

    def previous(self, source, digest):
        key = self.source_key(source)
        record = self.records.get(key)
        if not isinstance(record, dict) or record.get('hash') != digest:
            return None
        destination = os.path.join(self.output, record.get('destination', ''))
        category = record.get('category')
        roots = {'organized': organized_images, 'review': need_review, 'duplicates': duplicate}
        if category not in roots or not path_is_within(destination, os.path.join(self.output, roots[category])):
            return None
        if os.path.islink(destination) or not os.path.isfile(destination) or same_path(source, destination):
            return None
        identity = record.get('identity')
        status = os.stat(destination)
        if identity is not None and identity != [status.st_dev, status.st_ino]:
            return None
        if calculate_hash(destination) != digest:
            return None
        return destination, category

    def remember(self, source, digest, destination, category, identity=None):
        key = self.source_key(source)
        relative = os.path.relpath(destination, self.output)
        device = None if identity is None else identity.st_dev
        inode = None if identity is None else identity.st_ino
        # Commit the intent before attempting exclusive filesystem publication.
        self.store.save_intent(key, digest, relative, category, device, inode)
        self.records[key] = {'hash': digest, 'destination': relative, 'category': category,
                             'identity': None if identity is None else [device, inode]}



class MoveRecords:
    def __init__(self, output, store):
        self.output = output
        self.store = store
        self.records = store.moves()

    def verify(self, source, record):
        digest, relative, category, device, inode, state = record
        roots = {'organized': organized_images, 'review': need_review, 'duplicates': duplicate}
        if category not in roots:
            return None
        destination = os.path.join(self.output, relative)
        if not path_is_within(destination, os.path.join(self.output, roots[category])):
            return None
        if os.path.islink(destination) or not os.path.isfile(destination):
            return None
        status = os.stat(destination)
        if (status.st_dev, status.st_ino) != (device, inode) or os.path.realpath(source) == os.path.realpath(destination):
            return None
        if calculate_hash(destination) != digest:
            return None
        return destination

    def recover(self, dry_run):
        verified_sources = {}
        for source, record in self.records.items():
            if record[5] == 'complete':
                continue
            try:
                destination = self.verify(source, record)
            except OSError as err:
                log.warning('Cannot verify move intent for %s: %s', source, err)
                continue
            if destination is None:
                log.warning('Unresolved move intent for %s: destination unverified', source)
                continue
            digest = record[0]
            hash_keys.add(digest)
            if os.path.exists(source):
                try:
                    if not os.path.islink(source) and calculate_hash(source) == digest:
                        verified_sources[os.path.realpath(source)] = digest
                except OSError as err:
                    log.warning('Cannot verify move source %s: %s', source, err)
            elif not dry_run:
                self.store.finish_move(source, digest)
            if not dry_run and os.path.exists(source):
                self.store.add_hashes([digest])
        return verified_sources

    def remember(self, source, digest, destination, category, identity):
        key = os.path.realpath(source)
        relative = os.path.relpath(destination, self.output)
        self.store.save_move_intent(key, digest, relative, category, identity)
        self.records[key] = (digest, relative, category, identity.st_dev, identity.st_ino, 'pending')

    def complete(self, source, digest):
        key = os.path.realpath(source)
        record = self.records[key]
        if self.verify(key, record) is None:
            raise OSError('Published move destination failed verification')
        self.store.finish_move(key, digest)
        self.records[key] = (*record[:5], 'complete')


def process_file(file_path, destination_dir, file_date, stats=None, category=None, dry_run=False, copy_records=None, move_records=None):
    if stats is None:
        stats = {
            "organized": 0,
            "review": 0,
            "duplicates": 0,
            "errors": 0,
        }

    file_hash = calculate_hash(file_path)

    if copy_records is not None:
        previous = copy_records.previous(file_path, file_hash)
        if previous is not None:
            destination, previous_category = previous
            stats[previous_category] += 1
            hash_keys.add(file_hash)
            log.info('%sALREADY COPIED: %s -> %s', '[DRY RUN] ' if dry_run else '', file_path, destination)
            return None if previous_category == 'duplicates' else (file_path if dry_run else destination)
        duplicate_copy = file_hash in hash_keys
        target = os.path.join(duplicate_dir, file_date) if duplicate_copy else destination_dir
        if duplicate_copy:
            validate_destination_path(target, duplicate_dir)
        copy_category = 'duplicates' if duplicate_copy else category
        if dry_run:
            log.info('[DRY RUN] %sCOPY: %s -> %s', 'DUPLICATE ' if duplicate_copy else '', file_path, target)
            destination = file_path
        else:
            os.makedirs(target, exist_ok=True)
            destination = copy_without_overwriting(
                file_path, target, expected_hash=file_hash,
                before_publish=lambda destination, identity: copy_records.remember(
                    file_path, file_hash, destination, copy_category, identity
                )
            )
        hash_keys.add(file_hash)
        stats[copy_category] += 1
        return None if duplicate_copy else destination

    if file_hash in hash_keys:
        validate_destination_path(os.path.join(duplicate_dir, file_date), duplicate_dir)
        if dry_run:
            log.info("[DRY RUN] DUPLICATE: %s -> %s",
                     file_path, os.path.join(duplicate_dir, file_date))
        else:
            move_to_duplicate_folder(file_path, file_date, expected_hash=file_hash,
                    before_publish=lambda dest, identity: move_records.remember(file_path, file_hash, dest, 'duplicates', identity) if move_records else None)
            if move_records is not None:
                move_records.complete(file_path, file_hash)

        stats['duplicates'] += 1
        return

    if dry_run:
        log.info("[DRY RUN] MOVE: %s -> %s", file_path, destination_dir)
        destination = file_path
    else:
        os.makedirs(destination_dir, exist_ok=True)
        destination = move_without_overwriting(file_path, destination_dir, expected_hash=file_hash,
            before_publish=lambda dest, identity: move_records.remember(file_path, file_hash, dest, category, identity) if move_records else None)
        if move_records is not None:
            move_records.complete(file_path, file_hash)

    hash_keys.add(file_hash)

    if category is not None:
        stats[category] += 1

    return destination


def file_dates(path, dry_run=False, similarity_paths=None, output=None, copy_records=None, move_records=None, recovered_sources=None):
    if copy_records is not None:
        for source, record in copy_records.records.items():
            if not isinstance(record, dict) or not isinstance(record.get('hash'), str):
                continue
            digest = record['hash']
            try:
                if copy_records.previous(source, digest) is not None:
                    hash_keys.add(digest)
            except (OSError, ValueError, TypeError) as err:
                log.warning('Cannot verify copy record for %s: %s', source, err)
    output = os.path.realpath(output if output is not None else path)

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
            and not same_path(os.path.join(root, directory), output)
        )

        for filename in sorted(files):
            if (is_copy_staging(root, filename, DATABASE_NAME)
                or is_database_artifact(os.path.join(root, filename), root, path, output)
                or filename in ignore_files
                or (same_path(root, path) and filename == '.copy_records.json')
                or (
                same_path(root, output)
                and (same_path(os.path.join(root, filename), os.path.join(output, '.copy_records.json'))
                     or is_staging_name(filename, ('.copy-records-', '.hashes-')))
                )):
                continue

            file_path = os.path.join(root, filename)
            if recovered_sources and os.path.realpath(file_path) in recovered_sources:
                if calculate_hash(file_path) == recovered_sources[os.path.realpath(file_path)]:
                    log.warning('Move already published; source retained for review: %s', file_path)
                    continue

            if os.path.islink(file_path):
                log.warning("Skipping symbolic link: %s", file_path)
                continue

            try:
                image_date = get_image_date(file_path)

                if should_organized(filename):
                    destination_root = os.path.join(output, organized_images)
                    destination_dir = os.path.join(destination_root, image_date)
                    category = "organized"
                else:
                    destination_root = need_review_dir
                    destination_dir = need_review_dir
                    category = "review"

                validate_destination_path(destination_dir, destination_root)
                destination = process_file(file_path, destination_dir, image_date, stats=stats, category=category, dry_run=dry_run, copy_records=copy_records, move_records=move_records)
                if similarity_paths is not None and destination is not None:
                    similarity_paths.append({
                        'path': destination,
                        'original_name': filename,
                    })

            except sqlite3.Error:
                raise
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


def validate_output_path(source, output, directories, check_database=True):
    ancestor = output
    while not os.path.lexists(ancestor):
        ancestor = os.path.dirname(ancestor)
    if not os.path.isdir(ancestor):
        raise ValueError(f'Output path is not a directory: {ancestor}')

    for name in directories:
        destination = os.path.join(output, name)
        if os.path.islink(destination):
            raise ValueError(f'Generated output directory is a symbolic link: {destination}')
        if os.path.lexists(destination) and not os.path.isdir(destination):
            raise ValueError(f'Generated output path is not a directory: {destination}')
        if path_is_within(source, destination):
            raise ValueError(f'Source must not be inside a generated output directory: {destination}')

    if check_database:
        for name in DATABASE_FILES:
            database = os.path.join(output, name)
            if os.path.lexists(database) and (os.path.islink(database) or not os.path.isfile(database)):
                raise ValueError(f'SQLite database artifact must be a regular file: {database}')


def main():
    global path, duplicate_dir, need_review_dir

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
        '--output',
        metavar='PATH',
        help='Output root for organized files and reports (default: source directory)'
    )
    parser.add_argument('--copy', action='store_true', help='Copy files and preserve originals')
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

    if args.copy and args.find_similar_only:
        parser.error('--copy cannot be combined with --find-similar-only')

    if not 0 <= args.similar_threshold <= 64:
        parser.error('--similar-threshold must be between 0 and 64')

    if args.output == '':
        parser.error('--output must not be empty')

    try:
        path = os.path.realpath(os.path.expanduser(args.src))
        output = os.path.realpath(os.path.expanduser(args.output)) if args.output is not None else path
    except (OSError, ValueError) as err:
        parser.error(f'Invalid source or output path: {err}')

    if not os.path.isdir(path):
        parser.error(f"Source directory does not exist: {path}")

    directories = [similar] if args.find_similar_only else [organized_images, duplicate, need_review]
    if args.find_similar and not args.find_similar_only:
        directories.append(similar)
    try:
        validate_output_path(path, output, directories, check_database=not args.find_similar_only)
    except (OSError, ValueError) as err:
        parser.error(f'Invalid output path: {err}')

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
            scan_images(path, excluded_dirs=[output] if not same_path(output, path) else [], output=output),
            path, args.similar_threshold, dry_run=args.dry_run,
            output=output if args.output is not None else None
        )
        return

    duplicate_dir = os.path.join(output, duplicate)
    need_review_dir = os.path.join(output, need_review)
    if not args.dry_run:
        try:
            os.makedirs(duplicate_dir, exist_ok=True)
            os.makedirs(need_review_dir, exist_ok=True)
        except OSError as err:
            parser.exit(1, f'error: Cannot prepare output directory: {err}\n')

    store = None
    hash_keys.clear()
    similarity_paths = [] if args.find_similar else None
    try:
        store = Persistence(output, dry_run=args.dry_run)
        hash_keys.update(store.hashes())
        copy_records = CopyRecords(output, store) if args.copy else None
        move_records = None if args.copy else MoveRecords(output, store)
        recovered_sources = None if args.copy else move_records.recover(args.dry_run)
        stats = file_dates(path, dry_run=args.dry_run, similarity_paths=similarity_paths,
                           output=output, copy_records=copy_records, move_records=move_records,
                           recovered_sources=recovered_sources)
        if not args.dry_run:
            store.add_hashes(hash_keys)
    except (OSError, sqlite3.Error) as err:
        parser.exit(1, f'error: SQLite persistence failure: {err}\n')
    finally:
        if store is not None:
            store.close()

    print_summary(stats, dry_run=args.dry_run)

    if args.find_similar:
        run_similarity(
            similarity_paths, path if args.dry_run else output,
            args.similar_threshold, dry_run=args.dry_run,
            output=output if args.output is not None else None
        )


if __name__ == "__main__":
    main()
