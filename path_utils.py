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
