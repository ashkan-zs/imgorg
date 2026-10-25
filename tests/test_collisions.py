
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parent.parent / "organize_images.py"


class CollisionTests(unittest.TestCase):
    def run_organizer(self, root):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--src", str(root)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            result.stdout + result.stderr,
        )

    def add_file(self, root, folder, name, content):
        directory = root / folder
        directory.mkdir()
        source = directory / name
        source.write_bytes(content)
        os.utime(source, (1704067200, 1704067200))

    def test_image_name_collisions_keep_every_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            for folder, content in [
                ("one", b"one"),
                ("two", b"two"),
                ("three", b"three"),
            ]:
                self.add_file(root, folder, "same.jpg", content)

            self.run_organizer(root)

            files = list((root / "organized-images").rglob("*.jpg"))

            self.assertEqual(
                {file.read_bytes() for file in files},
                {b"one", b"two", b"three"},
            )
            self.assertEqual(len(files), 3)
            self.assertEqual(len({file.name for file in files}), 3)

    def test_review_name_collisions_keep_every_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            self.add_file(root, "one", "same.txt", b"one")
            self.add_file(root, "two", "same.txt", b"two")

            self.run_organizer(root)

            files = list((root / "needs-review").glob("same*.txt"))

            self.assertEqual(
                {file.read_bytes() for file in files},
                {b"one", b"two"},
            )
            self.assertEqual(len(files), 2)

    def test_existing_destination_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            destination_dir = (
                root / "organized-images" / "2024" / "January"
            )
            destination_dir.mkdir(parents=True)

            (destination_dir / "same.jpg").write_bytes(b"existing")
            self.add_file(root, "incoming", "same.jpg", b"incoming")

            self.run_organizer(root)

            self.assertEqual(
                (destination_dir / "same.jpg").read_bytes(),
                b"existing",
            )
            self.assertEqual(
                (destination_dir / "same_1.jpg").read_bytes(),
                b"incoming",
            )

    def test_duplicate_name_collisions_keep_every_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            for folder in ("one", "two", "three"):
                self.add_file(root, folder, "same.jpg", b"copy")

            self.run_organizer(root)

            files = list((root / "duplicate-files").rglob("*.jpg"))

            self.assertEqual(len(files), 2)
            self.assertEqual(len({file.name for file in files}), 2)
            self.assertTrue(
                all(file.read_bytes() == b"copy" for file in files)
            )


if __name__ == "__main__":
    unittest.main()
