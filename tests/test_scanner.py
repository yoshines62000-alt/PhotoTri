import sys
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Database
import grouping
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

    def test_interrupted_rescan_does_not_prune_unseen_files(self):
        # Bug trouve a l'audit : un scan interrompu (bouton Arreter,
        # fermeture de la fenetre) n'a vu qu'une partie des fichiers reels -
        # traiter "en base mais pas vu" comme "disparu" purgeait a tort des
        # photos toujours bien presentes sur le disque, avec perte
        # silencieuse de leur note/statut deja attribue.
        _make_photo(self.photos_dir / "a.jpg")
        _make_photo(self.photos_dir / "b.jpg")
        _make_photo(self.photos_dir / "c.jpg")
        first = scanner.scan_folder(self.photos_dir, self.db)
        self.assertEqual(first.scanned, 3)
        self.assertEqual(self.db.count_photos(), 3)

        # Arret apres le tout premier fichier traite - les 2 autres ne sont
        # jamais "vus" durant ce second scan (should_stop est verifie au
        # DEBUT de chaque iteration, avant de traiter le fichier suivant).
        processed = {"count": 0}

        def track_progress(done, total, path):
            processed["count"] = done

        def stop_after_one():
            return processed["count"] >= 1

        second = scanner.scan_folder(
            self.photos_dir, self.db, progress_callback=track_progress, should_stop=stop_after_one,
        )

        self.assertEqual(second.pruned, 0)
        self.assertEqual(self.db.count_photos(), 3, "aucune photo reelle ne doit etre purgee sur un scan interrompu")
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            self.assertIsNotNone(self.db.get_photo_by_path(str(self.photos_dir / name)))

    def test_rating_survives_rescan_of_unchanged_file(self):
        path = self.photos_dir / "a.jpg"
        _make_photo(path)
        scanner.scan_folder(self.photos_dir, self.db)
        photo_id = self.db.get_photo_by_path(str(path))["id"]
        self.db.set_rating(photo_id, 5)

        scanner.scan_folder(self.photos_dir, self.db)
        self.assertEqual(self.db.get_photo(photo_id)["rating"], 5)

    # -- exclusion du dossier de revision (bug trouve a l'audit) -------------------

    def test_exclude_dir_is_skipped_when_inside_scanned_folder(self):
        # Le dossier de revision, meme place manuellement a l'interieur du
        # dossier scanne, ne doit jamais etre parcouru : c'est la defense en
        # profondeur qui protege l'utilisateur meme si le defaut (a cote du
        # dossier scanne, pas dedans) a ete change manuellement.
        review_dir = self.photos_dir / "PhotoTri_a_revoir"
        review_dir.mkdir()
        _make_photo(self.photos_dir / "a.jpg")
        _make_photo(review_dir / "b_range.jpg")

        result = scanner.scan_folder(self.photos_dir, self.db, exclude_dir=review_dir)

        self.assertEqual(result.total_found, 1)
        self.assertEqual(result.scanned, 1)
        self.assertEqual(self.db.count_photos(), 1)
        self.assertIsNotNone(self.db.get_photo_by_path(str(self.photos_dir / "a.jpg")))
        self.assertIsNone(self.db.get_photo_by_path(str(review_dir / "b_range.jpg")))

    def test_exclude_dir_nested_deeper_is_also_skipped(self):
        review_dir = self.photos_dir / "sous_dossier" / "PhotoTri_a_revoir"
        review_dir.mkdir(parents=True)
        _make_photo(self.photos_dir / "sous_dossier" / "a.jpg")
        _make_photo(review_dir / "b_range.jpg")

        result = scanner.scan_folder(self.photos_dir, self.db, exclude_dir=review_dir)

        self.assertEqual(result.total_found, 1)
        self.assertIsNone(self.db.get_photo_by_path(str(review_dir / "b_range.jpg")))

    def test_exclude_dir_outside_scanned_folder_has_no_effect(self):
        # Cas du nouveau defaut (a cote du dossier scanne) : exclude_dir
        # n'existe meme pas sous root, le parcours n'est pas affecte.
        review_dir = self.tmp / "photos_PhotoTri_a_revoir"
        _make_photo(self.photos_dir / "a.jpg")

        result = scanner.scan_folder(self.photos_dir, self.db, exclude_dir=review_dir)

        self.assertEqual(result.total_found, 1)
        self.assertEqual(result.scanned, 1)

    def test_exclude_dir_none_scans_everything_as_before(self):
        _make_photo(self.photos_dir / "a.jpg")
        result = scanner.scan_folder(self.photos_dir, self.db, exclude_dir=None)
        self.assertEqual(result.total_found, 1)
        self.assertEqual(result.scanned, 1)

    def test_moved_duplicate_does_not_reappear_after_rescan(self):
        # Reproduction complete de la sequence decrite a l'audit :
        # 1) scan initial avec 2 photos identiques -> 1 groupe exact
        # 2) deplacement non destructif d'un exemplaire vers le dossier de
        #    revision par defaut (a l'interieur du dossier scanne, comme
        #    c'etait le cas AVANT ce correctif)
        # 3) rescan normal (recommande par le README)
        # 4) regroupement : le doublon range ne doit PLUS reapparaitre.
        review_dir = self.photos_dir / "PhotoTri_a_revoir"
        original = self.photos_dir / "photo.jpg"
        duplicate = self.photos_dir / "photo_copie.jpg"
        _make_photo(original, color=(50, 100, 150))
        _make_photo(duplicate, color=(50, 100, 150))

        first_scan = scanner.scan_folder(self.photos_dir, self.db, exclude_dir=review_dir)
        self.assertEqual(first_scan.scanned, 2)
        photos = list(self.db.list_active_photos())
        groups = grouping.group_photos(photos)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].kind, "exact")
        self.assertEqual(len(groups[0].photo_ids), 2)

        # Rangement non destructif : deplacement physique + purge en base de
        # l'ancien chemin, exactement comme le fait _move_checked_photos.
        review_dir.mkdir(parents=True, exist_ok=True)
        moved_path = review_dir / duplicate.name
        duplicate.rename(moved_path)
        self.db.delete_by_paths([str(duplicate)])

        second_scan = scanner.scan_folder(self.photos_dir, self.db, exclude_dir=review_dir)

        # Le fichier deplace ne doit pas avoir ete reindexe comme photo active.
        self.assertIsNone(self.db.get_photo_by_path(str(moved_path)))
        self.assertIsNotNone(self.db.get_photo_by_path(str(original)))
        self.assertEqual(self.db.count_photos(), 1)

        photos_after = list(self.db.list_active_photos())
        groups_after = grouping.group_photos(photos_after)
        self.assertEqual(
            groups_after, [],
            "le doublon range ne doit pas reapparaitre dans un groupe apres rescan",
        )


if __name__ == "__main__":
    unittest.main()
