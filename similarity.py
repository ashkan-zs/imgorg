"""Read-only perceptual image analysis and local similarity reports."""

import html
import json
import logging as log
import os
import tempfile
import warnings
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import imagehash
from PIL import Image, ImageOps


REPORT_DIRECTORY = 'similar-images'
SCAN_IGNORE_DIRS = {REPORT_DIRECTORY, '.git', '.venv', '__pycache__', '.pytest_cache'}
SCAN_IGNORE_FILES = {'.sha256_hashes.txt', 'report.json', 'report.html', '.DS_Store'}
VIDEO_EXTENSIONS = {'.mov', '.mp4', '.m4v', '.avi', '.mkv', '.webm', '.mpeg', '.mpg', '.3gp'}


def scan_images(source):
    """Yield source paths, including old organized archives but never reports."""
    def scan_error(err):
        log.warning('[SIMILARITY] Cannot scan directory: %s', err)

    for root, dirs, files in os.walk(source, onerror=scan_error):
        dirs[:] = sorted(
            name for name in dirs
            if name not in SCAN_IGNORE_DIRS
            and not os.path.islink(os.path.join(root, name))
        )
        for name in sorted(files):
            path = os.path.join(root, name)
            if name not in SCAN_IGNORE_FILES and not os.path.islink(path):
                yield {'path': path, 'original_name': name}


def group_images(images, threshold):
    """Greedily group by direct distance to a highest-resolution reference.

    Groups are disjoint. Members need not match each other, and qualifying
    pairs across groups may be omitted. No transitive edges are followed.
    """
    remaining = sorted(
        {image['path']: image for image in images}.values(),
        key=lambda image: (
            -(image['width'] * image['height']), image['path']
        )
    )
    groups = []
    while remaining:
        reference = remaining[0]
        members = [{**reference, 'distance': 0}]
        unmatched = []
        reference_hash = int(reference['phash'], 16)
        for image in remaining[1:]:
            distance = (reference_hash ^ int(image['phash'], 16)).bit_count()
            if distance <= threshold:
                members.append({**image, 'distance': distance})
            else:
                unmatched.append(image)
        if len(members) >= 2:
            groups.append({
                'id': f'group-{len(groups) + 1:04d}',
                'reference': members[0],
                'members': members,
            })
        remaining = unmatched
    return groups


def find_similar(candidates, source, threshold=5):
    """Hash each distinct successful path once; failures are similarity warnings."""
    if not 0 <= threshold <= 64:
        raise ValueError('Similarity threshold must be between 0 and 64')

    source = Path(source).absolute()
    images = []
    seen = set()
    for candidate in candidates:
        path = Path(candidate['path']).absolute()
        if path in seen:
            continue
        seen.add(path)
        if path.suffix.lower() in VIDEO_EXTENSIONS:
            continue
        try:
            relative = path.relative_to(source).as_posix()
            if path.is_symlink():
                raise OSError('Symbolic links are excluded')
            if not path.is_file():
                raise OSError('Not a regular file or no longer available')
            with warnings.catch_warnings():
                # Skip even warning-sized images before decoding their pixels.
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    ImageOps.exif_transpose(image, in_place=True)
                    fingerprint = imagehash.phash(image, hash_size=8)
                    width, height = image.size
            images.append({
                'path': relative,
                'original_name': candidate['original_name'],
                'width': width,
                'height': height,
                'phash': str(fingerprint),
            })
        except Exception as err:
            # Third-party decoders can raise their own exception types.
            log.warning('[SIMILARITY] Skipping %s: %s', path, err)

    groups = group_images(images, threshold)
    return {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'algorithm': 'phash',
        'hash_size': 8,
        'threshold': threshold,
        'path_base': 'source directory; paths use forward slashes',
        'total_images_analyzed': len(images),
        'total_similarity_groups': len(groups),
        'groups': groups,
    }


def render_html(report):
    """Render escaped text and URL-encoded, source-relative local links."""
    parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" '
        'content="default-src \'none\'; img-src \'self\' file:; '
        'style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
        '<title>Similar images</title>',
        '<style>body{font-family:system-ui;margin:2rem;background:#f5f5f5}'
        'section{margin:2rem 0} .images{display:flex;flex-wrap:wrap;gap:1rem}'
        'figure{background:white;padding:1rem;margin:0;max-width:320px;overflow-wrap:anywhere}'
        'img{width:280px;height:200px;object-fit:contain} .reference{border:2px solid #246}'
        '</style></head><body><h1>Similar images</h1>',
        f'<p>Analyzed images: {report["total_images_analyzed"]}; '
        f'similarity groups: {report["total_similarity_groups"]}; '
        f'pHash threshold: {report["threshold"]} / 64.</p>',
        '<p>References are chosen by pixel count, then path. Resolution does not '
        'establish the original or best-quality image. Distances are to the reference; '
        'other members may not match each other.</p>',
        '<p>Previews use existing files. If your browser cannot display a format '
        '(such as HEIC/HEIF), use the file link with a compatible viewer.</p>',
    ]
    if not report['groups']:
        parts.append('<p>No similar image groups found.</p>')
    for group in report['groups']:
        parts.append(f'<section><h2>{html.escape(group["id"])}</h2><div class="images">')
        for member in group['members']:
            # The report lives one directory below the source. Encoding prevents
            # filenames containing #, ?, quotes, percent signs or : becoming URLs.
            url = html.escape('../' + quote(member['path'], safe='/'), quote=True)
            name = html.escape(member['original_name'])
            path = html.escape(member['path'])
            reference = member['path'] == group['reference']['path']
            label = 'Reference' if reference else 'Similar to reference'
            css = ' class="reference"' if reference else ''
            parts.append(
                f'<figure{css}><strong>{label}</strong><br>'
                f'<a href="{url}"><img src="{url}" loading="lazy" '
                f'alt="Preview unavailable: {name}"></a>'
                f'<figcaption>{name}<br>{member["width"]} × {member["height"]} pixels; '
                f'distance: {member["distance"]}<br>{path}<br>'
                f'<a href="{url}">Open original file</a></figcaption></figure>'
            )
        parts.append('</div></section>')
    parts.append('</body></html>')
    return '\n'.join(parts)


def write_reports(report, source):
    """Replace reports atomically; never follow existing report symlinks."""
    directory = Path(source) / REPORT_DIRECTORY
    if directory.is_symlink():
        raise OSError(f'Report directory is a symbolic link: {directory}')
    directory.mkdir(exist_ok=True)
    outputs = {
        'report.json': json.dumps(report, indent=2, ensure_ascii=True) + '\n',
        'report.html': render_html(report),
    }
    for name, content in outputs.items():
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', dir=directory, delete=False
            ) as file:
                temporary = file.name
                file.write(content)
            # Replacing a symlink/hard link replaces its entry, not its target.
            os.replace(temporary, directory / name)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)


def print_similarity_summary(report, dry_run=False):
    title = 'Dry Run Similarity Summary' if dry_run else 'Similarity Summary'
    print(f'\n--- {title} ---')
    print(f'Images analyzed: {report["total_images_analyzed"]}')
    print(f'Similarity groups: {report["total_similarity_groups"]}')
    for group in report['groups']:
        print(f'{group["id"]}: reference {group["reference"]["path"]!r}')
        for member in group['members']:
            print(f'  {member["path"]!r} (distance {member["distance"]})')


def run_similarity(candidates, source, threshold=5, dry_run=False):
    report = find_similar(candidates, source, threshold)
    print_similarity_summary(report, dry_run=dry_run)
    if not dry_run:
        try:
            write_reports(report, source)
        except OSError as err:
            log.warning('[SIMILARITY] Could not write reports: %s', err)
    return report
