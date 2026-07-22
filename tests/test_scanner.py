import sqlite3
import struct
import sys
import tempfile
import time
import unittest
import unittest.mock
import zlib
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Database
import grouping
import hashing
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


def _make_decompression_bomb_png(path: Path, width: int = 60000, height: int = 60000) -> None:
    """Fabrique un PNG dont l'en-tete IHDR revendique des dimensions
    demesurees (60000x60000 par defaut) alors que le fichier reste minuscule
    - reproduit fidelement un en-tete corrompu ou piege (audit du
    2026-07-22, C1) : on part d'un vrai petit PNG valide puis on ecrase
    uniquement les 8 octets width/height de son chunk IHDR (en recalculant
    le CRC32 du chunk pour que Pillow accepte toujours le fichier comme un
    PNG bien forme et tente de le decoder)."""
    real = path.with_suffix(".tmp.png")
    Image.new("RGB", (4, 4), "red").save(real, format="PNG")
    data = bytearray(real.read_bytes())
    real.unlink()

    assert data[12:16] == b"IHDR"
    struct.pack_into(">II", data, 16, width, height)
    chunk_type_and_data = bytes(data[12:12 + 4 + 13])
    new_crc = zlib.crc32(chunk_type_and_data) & 0xFFFFFFFF
    struct.pack_into(">I", data, 12 + 4 + 13, new_crc)
    path.write_bytes(bytes(data))


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

    def test_decompression_bomb_does_not_abort_the_whole_scan(self):
        # Verrouille C1 (audit du 2026-07-22) : avant le correctif, seules
        # (UnidentifiedImageError, OSError, ValueError) etaient interceptees
        # autour du decodage - PIL.Image.DecompressionBombError herite
        # directement d'Exception et remontait donc brute, faisant avorter
        # tout scan_folder() (aucun commit du lot en cours) pour un seul
        # fichier a l'en-tete corrompu/piege. Ici, 2 photos valides
        # entourent un PNG dont l'en-tete revendique 60000x60000 pixels : le
        # scan doit traiter les 2 photos valides et journaliser la bombe
        # comme une simple erreur par fichier, sans exception non geree.
        _make_photo(self.photos_dir / "avant.jpg")
        _make_photo(self.photos_dir / "apres.jpg")
        bomb = self.photos_dir / "piege.png"
        _make_decompression_bomb_png(bomb)

        result = scanner.scan_folder(self.photos_dir, self.db)

        self.assertEqual(result.total_found, 3)
        self.assertEqual(result.scanned, 2)
        self.assertEqual(len(result.errors), 1)
        self.assertIn(str(bomb), result.errors[0][0])
        self.assertIn("DecompressionBomb", result.errors[0][1])
        self.assertEqual(self.db.count_photos(), 2)

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

    # -- commits groupes pendant le scan (optimisation trouvee a l'audit) ----------

    def test_interrupted_scan_does_not_lose_photos_already_committed_in_a_partial_batch(self):
        # bug potentiel introduit par le groupement des commits (moins de
        # fsync disque pendant un gros scan) : si une interruption survient
        # AVANT d'atteindre _COMMIT_BATCH_SIZE upserts, le commit final
        # inconditionnel apres la boucle doit quand meme valider ce lot
        # partiel - sinon un "Arreter" en tout debut de scan perdrait le
        # travail deja fait au lieu de simplement s'arreter proprement.
        _make_photo(self.photos_dir / "a.jpg")
        _make_photo(self.photos_dir / "b.jpg")
        _make_photo(self.photos_dir / "c.jpg")

        processed = {"count": 0}

        def track_progress(done, total, path):
            processed["count"] = done

        def stop_after_two():
            return processed["count"] >= 2

        result = scanner.scan_folder(
            self.photos_dir, self.db, progress_callback=track_progress, should_stop=stop_after_two,
        )

        self.assertEqual(result.scanned, 2)
        self.assertEqual(
            self.db.count_photos(), 2,
            "les 2 photos deja traitees avant l'arret doivent rester commitees, pas seulement en memoire",
        )

    def test_scan_commits_in_batches_not_once_per_photo(self):
        _make_photo(self.photos_dir / "a.jpg")
        _make_photo(self.photos_dir / "b.jpg")
        _make_photo(self.photos_dir / "c.jpg")

        commit_calls = {"count": 0}
        original_commit = self.db.commit

        def counting_commit():
            commit_calls["count"] += 1
            original_commit()

        with unittest.mock.patch.object(scanner, "_COMMIT_BATCH_SIZE", 2):
            with unittest.mock.patch.object(self.db, "commit", side_effect=counting_commit):
                result = scanner.scan_folder(self.photos_dir, self.db)

        self.assertEqual(result.scanned, 3)
        self.assertEqual(self.db.count_photos(), 3)
        # 3 photos, lots de 2 : un commit groupe apres la 2e photo, puis le
        # commit final inconditionnel pour la 3e restante - 2 commits au
        # total, jamais 3 (un par photo, l'ancien comportement).
        self.assertEqual(commit_calls["count"], 2)

    def test_unhandled_exception_mid_scan_does_not_lose_pending_uncommitted_batch(self):
        # Verrouille C4 : une authentique erreur disque/SQLite (pas une
        # erreur d'image, deja geree separement, cf. les tests ci-dessus sur
        # les fichiers illisibles) survenant PENDANT la boucle - ici
        # simulee en faisant lever db.upsert_photo lui-meme - ne doit pas
        # faire perdre les photos deja upsertees avec commit=False depuis le
        # dernier commit groupe. Avant le correctif, le seul `db.commit()`
        # inconditionnel se trouvait APRES la boucle et n'etait donc jamais
        # atteint quand une exception se propageait depuis l'interieur de la
        # boucle : le lot en cours de commit groupe (jusqu'a
        # _COMMIT_BATCH_SIZE photos) restait dans une transaction jamais
        # validee et disparaissait a la fermeture de la connexion.
        _make_photo(self.photos_dir / "a.jpg")
        _make_photo(self.photos_dir / "b.jpg")
        _make_photo(self.photos_dir / "c.jpg")

        original_upsert = self.db.upsert_photo
        calls = {"count": 0}

        def failing_on_third_call(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 3:
                raise sqlite3.OperationalError("disk I/O error")
            return original_upsert(*args, **kwargs)

        # _COMMIT_BATCH_SIZE reste a sa valeur par defaut (200) ici : la
        # 3e photo tombe donc AVANT tout commit groupe intermediaire, ce qui
        # isole precisement le cas non couvert par le commit final normal.
        with unittest.mock.patch.object(self.db, "upsert_photo", side_effect=failing_on_third_call):
            with self.assertRaises(sqlite3.OperationalError):
                scanner.scan_folder(self.photos_dir, self.db)

        self.assertEqual(
            self.db.count_photos(), 2,
            "les 2 upserts reussis avant l'exception doivent rester commites, pas perdus",
        )

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

    # -- support des chemins longs (correctif de l'audit, M1) ----------------------

    def test_scan_routes_file_access_through_long_path(self):
        # Verrouille le cablage reel de M1 au niveau du scanner (pas
        # seulement au niveau des fonctions pures de hashing.py, deja
        # verrouillees separement dans tests/test_hashing.py) : os.stat()
        # doit passer par hashing.long_path(path), pas par path.stat()
        # directement - seul moyen pour un chemin depassant MAX_PATH d'etre
        # reellement lu plutot que de simplement echouer proprement.
        _make_photo(self.photos_dir / "a.jpg")
        calls = []
        real_long_path = hashing.long_path

        def spy(path):
            calls.append(str(path))
            return real_long_path(path)

        with unittest.mock.patch.object(hashing, "long_path", side_effect=spy):
            result = scanner.scan_folder(self.photos_dir, self.db)

        self.assertEqual(result.scanned, 1)
        self.assertIn(str(self.photos_dir / "a.jpg"), calls)


if __name__ == "__main__":
    unittest.main()
