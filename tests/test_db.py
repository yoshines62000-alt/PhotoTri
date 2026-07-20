import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Database, phash_from_sqlite, phash_to_sqlite


class TestPhashRoundTrip(unittest.TestCase):
    def test_round_trip_preserves_value(self):
        for value in (0, 1, 2**63 - 1, 2**63, 2**64 - 1, 0xFFFFFFFFFFFFFFFF, 0x8000000000000000):
            self.assertEqual(phash_from_sqlite(phash_to_sqlite(value)), value)

    def test_msb_set_value_does_not_overflow(self):
        # Une valeur avec le bit de poids fort a 1 (>= 2**63) declenchait
        # OverflowError avant correctif : struct.pack/unpack doit produire
        # un entier signe valide (dans -2**63 .. 2**63-1), pas lever.
        signed = phash_to_sqlite(2**64 - 1)
        self.assertTrue(-2**63 <= signed <= 2**63 - 1)


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = Database(self.tmp / "photos.sqlite")

    def tearDown(self):
        self.db.close()

    def _upsert(self, path="C:/photos/a.jpg", size=1000, mtime=1.0, phash=0, sha="sha-a", taken_at="2026-01-01"):
        return self.db.upsert_photo(path, size, mtime, 800, 600, sha, phash, taken_at)

    def test_upsert_then_read_back(self):
        photo_id = self._upsert()
        row = self.db.get_photo(photo_id)
        self.assertEqual(row["path"], "C:/photos/a.jpg")
        self.assertEqual(row["size"], 1000)
        self.assertEqual(row["width"], 800)
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["rating"], 0)

    def test_upsert_msb_set_hash_does_not_raise(self):
        # Reproduction directe du bug d'overflow avec une vraie insertion.
        photo_id = self._upsert(phash=2**64 - 1)
        row = self.db.get_photo(photo_id)
        self.assertEqual(phash_from_sqlite(row["phash"]), 2**64 - 1)

    def test_upsert_same_path_updates_in_place(self):
        id1 = self._upsert(size=1000, sha="sha-1")
        id2 = self._upsert(size=2000, sha="sha-2")
        self.assertEqual(id1, id2)
        self.assertEqual(self.db.count_photos(), 1)
        row = self.db.get_photo(id1)
        self.assertEqual(row["size"], 2000)
        self.assertEqual(row["sha256"], "sha-2")

    def test_rescan_preserves_rating_and_status(self):
        photo_id = self._upsert()
        self.db.set_rating(photo_id, 4)
        self.db.mark_moved(photo_id, "C:/revue/a.jpg")
        # Un nouveau scan du meme chemin (fichier modifie sur disque) ne
        # doit PAS effacer la note ni le statut deja attribues par
        # l'utilisateur - ce sont des annotations, pas des donnees derivees.
        self._upsert(size=9999, sha="sha-nouveau")
        row = self.db.get_photo(photo_id)
        self.assertEqual(row["rating"], 4)
        self.assertEqual(row["status"], "moved")
        self.assertEqual(row["moved_to"], "C:/revue/a.jpg")
        self.assertEqual(row["size"], 9999)

    def test_set_rating_out_of_range_raises(self):
        photo_id = self._upsert()
        with self.assertRaises(ValueError):
            self.db.set_rating(photo_id, 6)
        with self.assertRaises(ValueError):
            self.db.set_rating(photo_id, -1)

    def test_list_active_photos_excludes_moved(self):
        id1 = self._upsert(path="C:/photos/a.jpg")
        id2 = self._upsert(path="C:/photos/b.jpg")
        self.db.mark_moved(id2, "C:/revue/b.jpg")
        active = self.db.list_active_photos()
        self.assertEqual([r["id"] for r in active], [id1])

    def test_delete_by_paths_removes_entries(self):
        self._upsert(path="C:/photos/a.jpg")
        self._upsert(path="C:/photos/b.jpg")
        self.db.delete_by_paths(["C:/photos/a.jpg"])
        self.assertEqual(self.db.count_photos(), 1)
        self.assertIsNone(self.db.get_photo_by_path("C:/photos/a.jpg"))
        self.assertIsNotNone(self.db.get_photo_by_path("C:/photos/b.jpg"))

    def test_list_paths_under_prefix(self):
        self._upsert(path="C:/photos/vacances/a.jpg")
        self._upsert(path="C:/photos/travail/b.jpg")
        under_vacances = self.db.list_paths_under("C:/photos/vacances")
        self.assertEqual(under_vacances, {"C:/photos/vacances/a.jpg"})


if __name__ == "__main__":
    unittest.main()
