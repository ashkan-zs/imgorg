"""Filesystem-aware path comparisons without creating missing directories."""

import os
import re


def is_staging_name(name, prefixes):
    """Match the complete names produced by tempfile.mkstemp."""
    return any(re.fullmatch(re.escape(prefix) + r'[a-z0-9_]{8}', name) for prefix in prefixes)


def same_path(first, second):
    first = os.path.realpath(first)
    second = os.path.realpath(second)
    while True:
        try:
            return os.path.samefile(first, second)
        except OSError:
            if first == second:
                return True
            # Missing paths have no identity yet. Peel off identical suffixes
            # to compare existing parents, preserving case in missing names.
            first_parent, first_name = os.path.split(first)
            second_parent, second_name = os.path.split(second)
            if (
                first_name != second_name
                or first_parent == first
                or second_parent == second
            ):
                return False
            first, second = first_parent, second_parent


def path_is_within(path, directory):
    """Include directory itself, comparing each resolved ancestor by identity."""
    path = os.path.realpath(path)
    directory = os.path.realpath(directory)
    while True:
        if same_path(path, directory):
            return True
        parent = os.path.dirname(path)
        if parent == path:
            return False
        path = parent


def is_copy_staging(root, name, database_name):
    """Recognize temporary copies only inside a known output's generated trees."""
    if not is_staging_name(name, ('.copy-',)):
        return False
    ancestor = os.path.realpath(root)
    while True:
        if (os.path.isfile(os.path.join(ancestor, database_name))
                or all(os.path.isdir(os.path.join(ancestor, directory))
                       for directory in ('duplicate-files', 'needs-review'))):
            if any(path_is_within(root, os.path.join(ancestor, directory))
                   for directory in ('organized-images', 'duplicate-files', 'needs-review')):
                return True
        parent = os.path.dirname(ancestor)
        if parent == ancestor:
            return False
        ancestor = parent


def same_directory_entry(first, second):
    """Match aliases of one existing directory entry, not separate hard links."""
    first = os.path.realpath(first)
    second = os.path.realpath(second)
    if not same_path(first, second):
        return False
    first_parent, first_name = os.path.split(first)
    second_parent, second_name = os.path.split(second)
    if not same_path(first_parent, second_parent):
        return False
    if first_name == second_name:
        return True
    if first_name.casefold() != second_name.casefold():
        return False
    # If two distinct spellings exist as directory entries, they may be
    # separate hard links on a case-sensitive filesystem.
    try:
        names = os.listdir(first_parent)
    except OSError:
        return False
    return sum(name.casefold() == first_name.casefold() for name in names) == 1
