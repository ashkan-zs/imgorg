"""Output-scoped SQLite storage for exact hashes and copy intents."""

import os
import re
import sqlite3
import stat
from pathlib import Path

from path_utils import same_directory_entry, same_path

DATABASE_NAME = '.organize-image.sqlite3'
DATABASE_FILES = tuple(DATABASE_NAME + suffix for suffix in ('', '-journal', '-wal', '-shm'))


def encode_identity(value):
    # INTEGER affinity would convert a plain large decimal string to lossy REAL.
    # A prefix keeps decimal identities as TEXT even in existing INTEGER columns.
    return None if value is None else 'id:' + str(value)


def decode_identity(value):
    if value is None or isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value[3:] if value.startswith('id:') else value)
    raise sqlite3.DataError('Filesystem identity must be an integer or decimal TEXT')


def validate_database_files(output):
    """Reject aliases that could let SQLite write through another directory entry."""
    for name in DATABASE_FILES:
        path = os.path.join(output, name)
        try:
            status = os.lstat(path)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(status.st_mode):
            raise ValueError(f'SQLite database artifact must be a regular, non-symlink file: {path}')
        if status.st_nlink > 1:
            raise ValueError(f'SQLite database artifact has multiple hard links; remove the alias before running: {path}')


def is_database_artifact(path, root, source, output):
    """Identify database files only at the source or output directory root."""
    return any(
        same_path(root, directory)
        and any(same_path(path, os.path.join(directory, name)) for name in DATABASE_FILES)
        for directory in (source, output)
    )


class Persistence:
    def _path_key(self, table, column, path):
        """Use filesystem bytes for new paths, retaining legacy TEXT primary keys.

        Table/column names are internal constants; all path values are bound.
        """
        encoded = os.fsencode(path)
        row = self.connection.execute(
            f'SELECT {column} FROM {table} WHERE {column}=?', (encoded,)).fetchone()
        if row is not None:
            return row[0]
        decoded = os.fsdecode(encoded)
        try:
            decoded.encode('utf-8')
        except UnicodeEncodeError:
            return encoded
        row = self.connection.execute(
            f'SELECT {column} FROM {table} WHERE {column}=?', (decoded,)).fetchone()
        return row[0] if row is not None else encoded

    def __init__(self, output, dry_run=False):
        self.path = os.path.join(output, DATABASE_NAME)
        self.connection = None
        self.has_file_cache = False
        validate_database_files(output)
        if dry_run and not os.path.exists(self.path):
            return
        if dry_run:
            # Immutable mode cannot create sidecars or recover a journal. Refuse a
            # potentially incomplete snapshot instead of silently ignoring it.
            if any(os.path.exists(self.path + suffix) for suffix in ('-journal', '-wal')):
                raise sqlite3.OperationalError('Cannot read database with a pending journal or WAL in dry-run')
            uri = Path(self.path).absolute().as_uri() + '?mode=ro&immutable=1'
            self.connection = sqlite3.connect(uri, uri=True)
            # Older databases are intentionally not upgraded in immutable mode.
            # Querying sqlite_master distinguishes that case from unrelated SQL
            # errors, which must still propagate.
            self.has_file_cache = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='file_cache'"
            ).fetchone() is not None
        else:
            validate_database_files(output)
            self.connection = sqlite3.connect(self.path, isolation_level=None)
            try:
                # sqlite3 accepts a pathname, not an already validated file
                # descriptor. Recheck before the first SQL write; replacement
                # between this check and SQLite's use remains a race.
                validate_database_files(output)
                # Rollback journaling avoids persistent WAL/SHM files for this local CLI.
                self.connection.execute('PRAGMA journal_mode=DELETE')
                self.connection.execute('PRAGMA synchronous=FULL')
                self.connection.execute('BEGIN')
                self.connection.execute('CREATE TABLE IF NOT EXISTS exact_hashes (digest TEXT PRIMARY KEY NOT NULL CHECK(length(digest) = 64))')
                self.connection.execute('''CREATE TABLE IF NOT EXISTS copy_intents (
                    source TEXT PRIMARY KEY NOT NULL, digest TEXT NOT NULL CHECK(length(digest) = 64),
                    destination TEXT NOT NULL, category TEXT NOT NULL CHECK(category IN ('organized', 'review', 'duplicates')),
                    device INTEGER, inode INTEGER, CHECK((device IS NULL) = (inode IS NULL)))''')
                self.connection.execute('''CREATE TABLE IF NOT EXISTS file_cache (
                    path TEXT PRIMARY KEY NOT NULL, device INTEGER NOT NULL, inode INTEGER NOT NULL,
                    size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, ctime_ns INTEGER NOT NULL,
                    sha256 TEXT NOT NULL CHECK(length(sha256) = 64))''')
                self.has_file_cache = True
                self.connection.execute("""CREATE TABLE IF NOT EXISTS move_intents (
                    source TEXT PRIMARY KEY NOT NULL, digest TEXT NOT NULL CHECK(length(digest) = 64),
                    destination TEXT NOT NULL, category TEXT NOT NULL CHECK(category IN ('organized', 'review', 'duplicates')),
                    device INTEGER NOT NULL, inode INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending', 'complete')))""")
                self.connection.execute('COMMIT')
            except BaseException:
                self.connection.close()
                self.connection = None
                raise

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def hashes(self):
        if self.connection is None:
            return set()
        return {row[0] for row in self.connection.execute('SELECT digest FROM exact_hashes')}

    def records(self):
        if self.connection is None:
            return {}
        return {os.fsdecode(source): {'hash': digest, 'destination': os.fsdecode(destination), 'category': category,
                         'identity': None if device is None else [decode_identity(device), decode_identity(inode)]}
                for source, digest, destination, category, device, inode in self.connection.execute(
                    'SELECT source, digest, destination, category, device, inode FROM copy_intents')}

    def _transaction(self, sql, parameters):
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            self.connection.executemany(sql, parameters)
            self.connection.execute('COMMIT')
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute('ROLLBACK')
            raise

    def add_hashes(self, digests):
        self._transaction('INSERT INTO exact_hashes(digest) VALUES (?) ON CONFLICT(digest) DO NOTHING', ((digest,) for digest in digests))

    def cached_hash(self, path, signature):
        """Return a matching per-file digest, honoring filesystem path aliases."""
        if self.connection is None or not self.has_file_cache:
            return None
        absolute = os.path.abspath(os.fspath(path))
        key = os.path.realpath(absolute)
        key = self._path_key('file_cache', 'path', key)
        row = self.connection.execute(
            'SELECT device, inode, size, mtime_ns, ctime_ns, sha256 FROM file_cache WHERE path=?',
            (key,)).fetchone()
        if row is None and os.fsdecode(key) != absolute:
            # Case and symlink aliases are not safely recognized by string
            # normalization. Resolve only on a miss, and keep hard links distinct.
            for stored_path, device, inode, size, mtime_ns, ctime_ns, digest in self.connection.execute(
                    'SELECT path, device, inode, size, mtime_ns, ctime_ns, sha256 FROM file_cache'):
                if same_directory_entry(path, os.fsdecode(stored_path)):
                    row = (device, inode, size, mtime_ns, ctime_ns, digest)
                    key = stored_path
                    break
        stored_signature = None if row is None else (decode_identity(row[0]), decode_identity(row[1]), *row[2:5])
        digest = row[5] if stored_signature == signature else None
        if digest is not None and (
                not isinstance(digest, str)
                or re.fullmatch(r'[0-9a-fA-F]{64}', digest) is None):
            return None
        return digest

    def save_cached_hash(self, path, signature, digest):
        if self.connection is None or not self.has_file_cache:
            return
        key = os.path.realpath(os.path.abspath(os.fspath(path)))
        key = self._path_key('file_cache', 'path', key)
        self._transaction('''INSERT INTO file_cache(path, device, inode, size, mtime_ns, ctime_ns, sha256)
            VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET
            device=excluded.device, inode=excluded.inode, size=excluded.size,
            mtime_ns=excluded.mtime_ns, ctime_ns=excluded.ctime_ns, sha256=excluded.sha256''',
            [(key, encode_identity(signature[0]), encode_identity(signature[1]), *signature[2:], digest)])

    def save_intent(self, source, digest, destination, category, device=None, inode=None):
        source = self._path_key('copy_intents', 'source', source)
        self._transaction('''INSERT INTO copy_intents(source, digest, destination, category, device, inode)
            VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(source) DO UPDATE SET
            digest=excluded.digest, destination=excluded.destination, category=excluded.category,
            device=excluded.device, inode=excluded.inode''',
            [(source, digest, os.fsencode(destination), category, encode_identity(device), encode_identity(inode))])

    def moves(self):
        if self.connection is None:
            return {}
        return {os.fsdecode(source): (digest, os.fsdecode(destination), category, decode_identity(device), decode_identity(inode), state)
                for source, digest, destination, category, device, inode, state in self.connection.execute(
                    'SELECT source, digest, destination, category, device, inode, state FROM move_intents')}

    def save_move_intent(self, source, digest, destination, category, identity):
        source = self._path_key('move_intents', 'source', source)
        self._transaction("""INSERT INTO move_intents(source, digest, destination, category, device, inode, state)
            VALUES (?, ?, ?, ?, ?, ?, 'pending') ON CONFLICT(source) DO UPDATE SET
            digest=excluded.digest, destination=excluded.destination, category=excluded.category,
            device=excluded.device, inode=excluded.inode, state='pending'""",
            [(source, digest, os.fsencode(destination), category, encode_identity(identity.st_dev), encode_identity(identity.st_ino))])

    def finish_move(self, source, digest):
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            source = self._path_key('move_intents', 'source', source)
            self.connection.execute('INSERT INTO exact_hashes(digest) VALUES (?) ON CONFLICT(digest) DO NOTHING', (digest,))
            self.connection.execute("UPDATE move_intents SET state='complete' WHERE source=? AND digest=?", (source, digest))
            if self.connection.execute('SELECT changes()').fetchone()[0] != 1:
                raise sqlite3.IntegrityError('Move intent changed before completion')
            self.connection.execute('COMMIT')
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute('ROLLBACK')
            raise
