import sys
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Database
import scanner

_TAG_DATETIME_ORIGINAL = 36867


def _make_photo(path: Path, color=(120, 40, 200), taken_at: str = None) -> None:
    img = Image.new("RGB", (32, 32), color)
    if taken_at:
        exif = img.getexif()
        exif[_TAG_DATETIME_ORIGINAL] = taken_at
        img.save(path, exif=exif)
    else:
        img.save(path)


class TestScanFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.photos_dir = self.tmp / "photos"
        self.photos_dir.mkdir()
        self.db = Database(self.tmp / "photos.sqlite")

    def tearDown(self):
        self.db.close()

    def test_scans_nested_folders_and_extracts_exif_date(self):
        (self.photos_dir / "vacances").mkdir()
        _make_photo(self.photos_dir / "a.jpg", taken_at="2024:07:14 10:00:00")
        _make_photo(self.photos_dir / "vacances" / "b.jpg")

        result = scanner.scan_folder(self.photos_dir, self.db)

        self.assertEqual(result.total_found, 2)
        self.assertEqual(result.scanned, 2)
        self.assertEqual(result.skipped_unchanged, 0)
        self.assertEqual(self.db.count_photos(), 2)
        row_a = self.db.get_photo_by_path(str(self.photos_dir / "a.jpg"))
        self.assertTrue(row_a["taken_at"].startswith("2024-07-14"))
        row_b = self.db.get_photo_by_path(str(self.photos_dir / "vacances" / "b.jpg"))
        self.assertIsNone(row_b["taken_at"])

    def test_rescan_without_changes_skips_everything(self):
        _make_photo(self.photos_dir / "a.jpg")
        scanner.scan_folder(self.photos_dir, self.db)

        result = scanner.scan_folder(self.photos_dir, self.db)
        self.assertEqual(result.scanned, 0)
        self.assertEqual(result.skipped_unchanged, 1)

    def test_modified_file_gets_rescanned(self):
        path = self.photos_dir / "a.jpg"
        _make_photo(path, color=(10, 10, 10))
        scanner.scan_folder(self.photos_dir, self.db)
        first_sha = self.db.get_photo_by_path(str(path))["sha256"]

        time.sleep(0.01)
        _make_photo(path, color=(250, 250, 250))
        result = scanner.scan_folder(self.photos_dir, self.db)

        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.skipped_unchanged, 0)
        second_sha = self.db.get_photo_by_path(str(path))["sha256"]
        self.assertNotEqual(first_sha, second_sha)

    def test_corrupt_image_is_skipped_not_fatal(self):
        _make_photo(self.photos_dir / "bonne.jpg")
        bogus = self.photos_dir / "corrompue.jpg"
        bogus.write_bytes(b"pas une image")

        result = scanner.scan_folder(self.photos_dir, self.db)

        self.assertEqual(result.total_found, 2)
        self.assertEqual(result.scanned, 1)
        self.assertEqual(len(result.errors), 1)
        self.assertIn(str(bogus), result.errors[0][0])
        self.assertEqual(self.db.count_photos(), 1)

    def test_deleted_file_is_pruned_from_index(self):
        path_a = self.photos_dir / "a.jpg"
        path_b = self.photos_dir / "b.jpg"
        _make_photo(path_a)
        _make_photo(path_b)
        scanner.scan_folder(self.photos_dir, self.db)
        self.assertEqual(self.db.count_photos(), 2)

        path_b.unlink()
        result = scanner.scan_folder(self.photos_dir, self.db)

        self.assertEqual(result.pruned, 1)
        self.assertEqual(self.db.count_photos(), 1)
        self.assertIsNotNone(self.db.get_photo_by_path(str(path_a)))
        self.assertIsNone(self.db.get_photo_by_path(str(path_b)))

    def test_progress_callback_invoked_for_every_file(self):
        _make_photo(self.photos_dir / "a.jpg")
        _make_photo(self.photos_dir / "b.jpg")
        calls = []
        scanner.scan_folder(self.photos_dir, self.db, progress_callback=lambda done, total, p: calls.append((done, total)))
        self.assertEqual(calls, [(1, 2), (2, 2)])

    def test_should_stop_halts_scan_early(self):
        _make_photo(self.photos_dir / "a.jpg")
        _make_photo(self.photos_dir / "b.jpg")
        _make_photo(self.photos_dir / "c.jpg")
        result = scanner.scan_folder(self.photos_dir, self.db, should_stop=lambda: True)
        self.assertEqual(result.scanned, 0)
        self.assertEqual(self.db.count_photos(), 0)

    def test_rating_survives_rescan_of_unchanged_file(self):
        path = self.photos_dir / "a.jpg"
        _make_photo(path)
        scanner.scan_folder(self.photos_dir, self.db)
        photo_id = self.db.get_photo_by_path(str(path))["id"]
        self.db.set_rating(photo_id, 5)

        scanner.scan_folder(self.photos_dir, self.db)
        self.assertEqual(self.db.get_photo(photo_id)["rating"], 5)


if __name__ == "__main__":
    unittest.main()
