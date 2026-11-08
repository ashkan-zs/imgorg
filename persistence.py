"""Output-scoped SQLite storage for exact hashes and copy intents."""

import os
import sqlite3
from pathlib import Path

from path_utils import same_path

DATABASE_NAME = '.organize-image.sqlite3'
DATABASE_FILES = tuple(DATABASE_NAME + suffix for suffix in ('', '-journal', '-wal', '-shm'))


def is_database_artifact(path, root, source, output):
    """Identify database files only at the source or output directory root."""
    return any(
        same_path(root, directory)
        and any(same_path(path, os.path.join(directory, name)) for name in DATABASE_FILES)
        for directory in (source, output)
    )


class Persistence:
    def __init__(self, output, dry_run=False):
        self.path = os.path.join(output, DATABASE_NAME)
        self.connection = None
        if dry_run and not os.path.exists(self.path):
            return
        if dry_run:
            # Immutable mode cannot create sidecars or recover a journal. Refuse a
            # potentially incomplete snapshot instead of silently ignoring it.
            if any(os.path.exists(self.path + suffix) for suffix in ('-journal', '-wal')):
                raise sqlite3.OperationalError('Cannot read database with a pending journal or WAL in dry-run')
            uri = Path(self.path).absolute().as_uri() + '?mode=ro&immutable=1'
            self.connection = sqlite3.connect(uri, uri=True)
        else:
            self.connection = sqlite3.connect(self.path, isolation_level=None)
            try:
                # Rollback journaling avoids persistent WAL/SHM files for this local CLI.
                self.connection.execute('PRAGMA journal_mode=DELETE')
                self.connection.execute('PRAGMA synchronous=FULL')
                self.connection.execute('BEGIN')
                self.connection.execute('CREATE TABLE IF NOT EXISTS exact_hashes (digest TEXT PRIMARY KEY NOT NULL CHECK(length(digest) = 64))')
                self.connection.execute('''CREATE TABLE IF NOT EXISTS copy_intents (
                    source TEXT PRIMARY KEY NOT NULL, digest TEXT NOT NULL CHECK(length(digest) = 64),
                    destination TEXT NOT NULL, category TEXT NOT NULL CHECK(category IN ('organized', 'review', 'duplicates')),
                    device INTEGER, inode INTEGER, CHECK((device IS NULL) = (inode IS NULL)))''')
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
        return {source: {'hash': digest, 'destination': destination, 'category': category,
                         'identity': None if device is None else [device, inode]}
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

    def save_intent(self, source, digest, destination, category, device=None, inode=None):
        self._transaction('''INSERT INTO copy_intents(source, digest, destination, category, device, inode)
            VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(source) DO UPDATE SET
            digest=excluded.digest, destination=excluded.destination, category=excluded.category,
            device=excluded.device, inode=excluded.inode''',
            [(source, digest, destination, category, device, inode)])

    def moves(self):
        if self.connection is None:
            return {}
        return {source: (digest, destination, category, device, inode, state)
                for source, digest, destination, category, device, inode, state in self.connection.execute(
                    'SELECT source, digest, destination, category, device, inode, state FROM move_intents')}

    def save_move_intent(self, source, digest, destination, category, identity):
        self._transaction("""INSERT INTO move_intents(source, digest, destination, category, device, inode, state)
            VALUES (?, ?, ?, ?, ?, ?, 'pending') ON CONFLICT(source) DO UPDATE SET
            digest=excluded.digest, destination=excluded.destination, category=excluded.category,
            device=excluded.device, inode=excluded.inode, state='pending'""",
            [(source, digest, destination, category, identity.st_dev, identity.st_ino)])

    def finish_move(self, source, digest):
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            self.connection.execute('INSERT INTO exact_hashes(digest) VALUES (?) ON CONFLICT(digest) DO NOTHING', (digest,))
            self.connection.execute("UPDATE move_intents SET state='complete' WHERE source=? AND digest=?", (source, digest))
            if self.connection.execute('SELECT changes()').fetchone()[0] != 1:
                raise sqlite3.IntegrityError('Move intent changed before completion')
            self.connection.execute('COMMIT')
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute('ROLLBACK')
            raise
