## Organize Image

Organize images by date, detect exact duplicates, and optionally report visually
similar images for human review.

Create and activate a virtual environment, then install the dependencies:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Install all requirements, including Pillow, `pillow-heif`, and `ImageHash`.
ImageHash installs its numerical dependencies automatically.
JPEG, TIFF, and HEIC/HEIF
photos use EXIF `DateTimeOriginal` when available; otherwise, the script uses
the filesystem modification time.

Run the script with `python organize_images.py --src /path/to/photos`.
Add `--dry-run` to preview changes.

### Output directory

By default, outputs and `.sha256_hashes.txt` stay under `--src`, as before.
Use `--output PATH` to choose a separate output root:

```sh
python organize_images.py --src ~/Pictures/Unsorted --output ~/Pictures/Sorted
python organize_images.py --src ./photos --output ./sorted --dry-run
```

Organized files go into `OUTPUT/organized-images/`, exact duplicates into
`OUTPUT/duplicate-files/`, and other files into `OUTPUT/needs-review/`.
`OUTPUT/similar-images/` is created only when a similarity mode is requested.
Files are moved using the existing collision-safe transfers; `--output` does
not enable copying. Relative paths are resolved against the working directory;
`~`, `..`, and directory aliases are normalized.

The hash database lives at `OUTPUT/.sha256_hashes.txt`, so repeat runs and
different sources targeting the same output share that library's duplicate
history. A database at a different source location is left untouched and is
not automatically migrated or merged. Keep using the same output root to retain
its history. Similarity-only mode neither reads nor writes either database.

Output may equal the source, be outside it, or be nested inside it. A distinct
output subtree inside the source is excluded entirely from input scanning,
including pre-existing files there. Normal organization also continues to skip
the standard generated directory names anywhere in the source. Sources located
inside one of the configured generated destinations are rejected, as are
symlinks or files occupying generated directory paths. Choose a different
output root for those cases. Symbolic-link hash databases are also rejected.
Date folders cannot redirect transfers outside their generated directory.
Invalid paths and startup permission failures are reported without a traceback.
The run is not transactional: a later transfer or database-save failure does
not undo earlier successful moves. Dry-run creates no output directories,
reports, or persistent files.

### Similarity detection

Similarity detection is disabled by default. Organize new photos and then
analyze the successfully processed images:

```sh
python organize_images.py --src ./photos --find-similar
python organize_images.py --src ./photos --output ./sorted --find-similar
```

Scan an existing archive recursively without running file organization:

```sh
python organize_images.py --src ./organized-images --find-similar-only
python organize_images.py --src ./organized-images --find-similar-only --similar-threshold 5
python organize_images.py --src ./organized-images --find-similar-only --dry-run
python organize_images.py --src ./organized-images --output ./reports --find-similar-only
```

`--find-similar-only` takes precedence if both similarity flags are provided.
It scans images across nested folders, including previously organized archives,
without reading or writing the SHA-256 database or creating organization folders.
It writes only the two similarity reports. Files inside existing `similar-images/`
directories, development/cache directories, hash databases, and report artifacts
are excluded; symbolic links are not followed. Image-containing folders such as
`organized-images`, `duplicate-files`, and `needs-review` are included in this mode.
With `--output`, only the report destination changes; the archive images stay
in place. A distinct report-output subtree nested inside the archive is excluded
from the scan. With both `--output` and `--dry-run`, neither similarity mode
creates the output path, and previews use the original source paths.

To preview combined organization and similarity analysis without any writes:

```sh
python organize_images.py --src ./photos --find-similar --dry-run
```

Both dry-run modes compute hashes and print groups, reference paths, and distances
in memory. They create no directories, reports, thumbnails, or hash database files.

### Exact duplicates and perceptual matches

Normal organization continues to use SHA-256 to detect byte-for-byte duplicates
and move them into `duplicate-files/`. This behavior does not depend on similarity
detection. In combined mode these exact duplicates are excluded from similarity
analysis; their counts remain in the normal organization summary.

Perceptual hashing uses ImageHash's [`phash`](https://github.com/JohannesBuchner/imagehash)
with `hash_size=8`, producing 64 bits that summarize luminance structure. Hamming
distance counts differing bits: 0 means identical hashes, not necessarily identical
files. JPEG recompression and resizing often preserve a close hash; different
colors, simple images, crops, rotations, and other edits can produce false matches
or missed matches. This is a review aid, not proof of identity or quality.

The default maximum distance is **5**, inclusive. Set `--similar-threshold N` to
an integer from **0 to 64**. Lower values are stricter; higher values may group
unrelated images. Similarity-only mode does not calculate SHA-256, so identical
files can appear together in its perceptual groups.

Images are hashed once per distinct candidate path, with EXIF orientation applied
in memory. Pillow-readable image formats are supported, including JPEG, PNG, TIFF,
HEIC/HEIF, WebP, and BMP. Animated/multipage files use the first frame/page. Videos,
unsupported files, unreadable images, and decompression-bomb warnings/errors are
skipped; warnings use a separate `[SIMILARITY]` prefix and do not increase file
organization errors. Pillow's safety limits remain enabled.

### Groups and reports

Candidates are sorted by decreasing pixel count, with case-sensitive relative path
as the tie-breaker. The largest remaining image becomes a reference; all remaining
images within the threshold of that reference form a group. Assigned images are
removed before choosing the next reference. Singletons are omitted. Group IDs
(`group-0001`, etc.) and member order are deterministic for the same candidates,
hashes, dimensions, and threshold.

Every member is compared directly to its reference. Groups are disjoint, so there
are no transitive chains that add an image solely because it matches another
member. Members may not match one another, and some qualifying pairs across
different groups may not be reported. This greedy strategy uses up to quadratic
comparisons and keeps hashes in memory; large archives may take time. Highest
resolution is only a heuristic and does not identify an original or the
best-quality image.

Reports are written under `--output`, or under `--src` when `--output` is omitted:

- `similar-images/report.json`: UTC generation timestamp, algorithm, hash size,
  threshold, counts, references, member paths, original filenames, dimensions,
  hashes, and each member's distance to its reference (including 0 for the reference).
- `similar-images/report.html`: locally viewable groups, linked previews,
  reference labels, filenames, dimensions, and distances. No server, JavaScript,
  CDN, or internet connection is required.

JSON image paths use forward slashes and are relative to the output root when
`--output` is supplied, or to `--src` otherwise. The `path_base` field identifies
the convention. In similarity-only mode, paths can contain `..` to reach source
images outside the report-output root. HTML links are URL-encoded relative to
the report directory (`../` followed by that image path). Preserve the relative
locations of reports and images for links to work. Previews reference
existing images without copying or embedding them. Browsers without HEIC/HEIF
(or other format) support show fallback text and an original-file link for use
with a compatible viewer. No generated thumbnails are stored.

Similarity analysis never deletes, moves, renames, overwrites, or edits images.
Combined mode still performs the normal organization moves requested by the CLI;
it records actual destination paths, including collision suffixes, only after a
successful move. Existing generated directories are not rescanned in combined
mode. Similarity-only mode leaves source files, timestamps, metadata, and the hash
database untouched. Reading files may update access times according to the
filesystem. Existing reports are replaced on each non-dry run, including when no
matches exist; report-writing failures are logged separately. Reports describe a
single run, not a persistent index, and later external changes can invalidate links.

### Tests

GitHub Actions runs the full test suite on Ubuntu with Python 3.11, 3.12, 3.13,
3.14, and 3.15 on pushes to `main`, pull requests targeting `main`, and manual
runs. All five versions are required CI jobs. Python 3.15 has stable Linux builds
in `actions/setup-python` and compatible dependency wheels; it needs no prerelease
opt-in. Each job installs the declared dependencies, runs `pip check`, and executes
all tests, including HEIC/HEIF tests.

To run the same checks locally, activate your virtual environment first:

```sh
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install pytest
python -m pip check
python -m pytest -v
```
