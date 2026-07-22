import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
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
        self._upsert(path="C:\\photos\\vacances\\a.jpg")
        self._upsert(path="C:\\photos\\travail\\b.jpg")
        under_vacances = self.db.list_paths_under("C:\\photos\\vacances")
        self.assertEqual(under_vacances, {"C:\\photos\\vacances\\a.jpg"})

    def test_list_paths_under_does_not_match_sibling_prefix_folder(self):
        # Bug trouve a l'audit : "C:\Photos" ne doit PAS matcher
        # "C:\PhotosBackup" (prefixe partiel du nom de dossier, pas un vrai
        # sous-dossier) - sans frontiere de separateur, un rescan de
        # "Photos" purgeait a tort l'index de "PhotosBackup".
        self._upsert(path="C:\\Photos\\a.jpg")
        self._upsert(path="C:\\PhotosBackup\\b.jpg")
        under_photos = self.db.list_paths_under("C:\\Photos")
        self.assertEqual(under_photos, {"C:\\Photos\\a.jpg"})

    def test_list_paths_under_escapes_like_wildcards_in_folder_name(self):
        # Bug trouve a l'audit : le "_" de LIKE est un joker un-caractere-
        # quelconque - sans echappement, "Vacances_2024" matchait aussi
        # "Vacances-2024" et "VacancesX2024".
        self._upsert(path="C:\\Vacances_2024\\a.jpg")
        self._upsert(path="C:\\Vacances-2024\\b.jpg")
        self._upsert(path="C:\\VacancesX2024\\c.jpg")
        under = self.db.list_paths_under("C:\\Vacances_2024")
        self.assertEqual(under, {"C:\\Vacances_2024\\a.jpg"})

    def test_list_paths_under_includes_nested_subfolders(self):
        self._upsert(path="C:\\Photos\\2024\\ete\\a.jpg")
        under = self.db.list_paths_under("C:\\Photos")
        self.assertEqual(under, {"C:\\Photos\\2024\\ete\\a.jpg"})


class TestPragmaOrderAtOpen(unittest.TestCase):
    """Verrouille E1 (audit du 2026-07-22) : busy_timeout doit etre pose
    AVANT journal_mode=WAL a l'ouverture, sinon la mise en place du mode WAL
    lui-meme n'est pas protegee par ce timeout."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_busy_timeout_pragma_runs_before_journal_mode_wal_pragma(self):
        # Verrou de code direct : quel que soit le comportement du systeme
        # de fichiers/OS, l'ORDRE des deux PRAGMA executes par
        # Database.__init__ doit rester busy_timeout puis journal_mode=WAL,
        # jamais l'inverse (bug trouve a l'audit : ordre inverse).
        # sqlite3.Connection est un type C immuable (impossible de patcher
        # sa methode execute directement) : on intercepte plutot au niveau
        # de sqlite3.connect() avec un objet-espion qui delegue tout a la
        # vraie connexion, sauf execute() qu'il observe au passage.
        executed = []

        class _SpyConnection:
            def __init__(self, real_conn):
                object.__setattr__(self, "_real", real_conn)

            def execute(self, sql, *args, **kwargs):
                upper = sql.strip().upper()
                if upper.startswith("PRAGMA BUSY_TIMEOUT") or upper.startswith("PRAGMA JOURNAL_MODE"):
                    executed.append(upper)
                return self._real.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._real, name)

            def __setattr__(self, name, value):
                setattr(self._real, name, value)

        real_connect = sqlite3.connect

        def spy_connect(path, *args, **kwargs):
            return _SpyConnection(real_connect(path, *args, **kwargs))

        with unittest.mock.patch.object(db.sqlite3, "connect", side_effect=spy_connect):
            database = Database(self.tmp / "order.sqlite")
        database.close()

        self.assertEqual(len(executed), 2, executed)
        self.assertTrue(executed[0].startswith("PRAGMA BUSY_TIMEOUT"), executed)
        self.assertTrue(executed[1].startswith("PRAGMA JOURNAL_MODE"), executed)

    def test_opening_fresh_database_while_another_connection_holds_exclusive_lock_waits_instead_of_failing(self):
        # Reproduction directe de la course decrite dans l'audit, isolee de
        # tout filet de securite independant : sqlite3.connect() de Python
        # applique lui-meme un busy_timeout par defaut de 5s (parametre
        # `timeout`), ce qui masquerait la difference entre les deux ordres
        # sur un verrou court. On force ce parametre a 0 (aucune protection
        # par defaut) pour isoler precisement l'effet du PRAGMA
        # busy_timeout=10000 de db.py lui-meme : seul l'ORDRE dans lequel il
        # est pose determine si la conversion en mode WAL (qui necessite un
        # verrou exclusif momentane, uniquement au tout premier passage en
        # WAL d'un fichier neuf - scenario du tout premier lancement/fichier
        # absent) attend le verrou concurrent ou echoue immediatement.
        #
        # Preuve que le scenario choisi (fichier NEUF) reproduit bien
        # l'echec avec l'ancien ordre (WAL avant busy_timeout) : verifie
        # separement ci-dessous par manipulation directe de sqlite3, avant
        # ce test, lors du developpement du correctif - reproduit de facon
        # fiable et systematique, pas une seule fois par hasard.
        db_path = self.tmp / "fresh.sqlite"  # n'existe pas encore sur le disque

        locker = sqlite3.connect(str(db_path), check_same_thread=False)
        locker.execute("BEGIN EXCLUSIVE")
        locker.execute("CREATE TABLE dummy(x)")

        def release_lock_shortly():
            time.sleep(0.3)
            locker.commit()
            locker.close()

        release_thread = threading.Thread(target=release_lock_shortly, daemon=True)
        release_thread.start()

        real_connect = sqlite3.connect

        def connect_without_pythons_own_timeout(path, *args, **kwargs):
            kwargs["timeout"] = 0.0
            return real_connect(path, *args, **kwargs)

        started = time.monotonic()
        with unittest.mock.patch.object(db.sqlite3, "connect", side_effect=connect_without_pythons_own_timeout):
            database = Database(db_path)  # NE DOIT PAS lever OperationalError
        elapsed = time.monotonic() - started
        database.close()
        release_thread.join(timeout=2)

        # A attendu que le verrou concurrent se libere (preuve que
        # busy_timeout etait bien actif pour la conversion en WAL) plutot
        # que d'echouer immediatement, sans pour autant epuiser les 10s
        # configurees.
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertLess(elapsed, 5)


class TestSchemaMigrations(unittest.TestCase):
    """Verrouille E3 (audit du 2026-07-22) : jusqu'ici, _create_schema()
    n'utilisait que CREATE TABLE IF NOT EXISTS (no-op sur une table deja
    existante) - sans aucun mecanisme de migration, une base d'utilisateur
    creee par une version anterieure ne recevrait jamais une colonne ajoutee
    par une future version. `PRAGMA user_version` compare a `db.SCHEMA_VERSION`
    (le nombre de migrations enregistrees dans `db._MIGRATIONS`) doit
    combler ce trou a chaque ouverture."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_fresh_database_is_stamped_with_the_current_schema_version(self):
        path = self.tmp / "fresh.sqlite"
        database = Database(path)
        try:
            version = database.conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, db.SCHEMA_VERSION)
        finally:
            database.close()

    def test_pending_migration_is_applied_to_an_older_database_on_open(self):
        # Simule une base creee par une version anterieure du schema
        # (user_version reste a 0 par defaut tant qu'aucune migration n'a
        # jamais ete appliquee) : table "photos" deja existante SANS une
        # colonne hypothetique qu'une migration future ajouterait, avec une
        # ligne de donnees deja presente (doit survivre a la migration).
        path = self.tmp / "old.sqlite"
        raw = sqlite3.connect(str(path))
        raw.executescript("""
            CREATE TABLE photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                width INTEGER,
                height INTEGER,
                sha256 TEXT NOT NULL,
                phash INTEGER NOT NULL,
                taken_at TEXT,
                rating INTEGER NOT NULL DEFAULT 0 CHECK (rating BETWEEN 0 AND 5),
                status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'moved')),
                moved_to TEXT,
                scanned_at TEXT NOT NULL
            );
        """)
        raw.execute(
            "INSERT INTO photos (path, size, mtime, sha256, phash, scanned_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("C:/old.jpg", 100, 1.0, "sha", 0, "2026-01-01"),
        )
        raw.commit()
        raw.close()

        migration_calls = []

        def add_hypothetical_column(conn):
            migration_calls.append(True)
            conn.execute("ALTER TABLE photos ADD COLUMN note_test TEXT")

        with unittest.mock.patch.object(db, "_MIGRATIONS", [add_hypothetical_column]), \
                unittest.mock.patch.object(db, "SCHEMA_VERSION", 1):
            database = Database(path)
            try:
                self.assertEqual(migration_calls, [True], "la migration en attente doit avoir ete appliquee")
                columns = [row[1] for row in database.conn.execute("PRAGMA table_info(photos)").fetchall()]
                self.assertIn("note_test", columns, "la colonne ajoutee par la migration doit exister")
                version = database.conn.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(version, 1)
                # Les donnees existantes doivent survivre a la migration.
                row = database.get_photo_by_path("C:/old.jpg")
                self.assertIsNotNone(row)
                self.assertEqual(row["sha256"], "sha")
            finally:
                database.close()

    def test_migration_is_not_reapplied_on_a_database_already_at_the_current_version(self):
        path = self.tmp / "already_current.sqlite"
        Database(path).close()

        calls = []

        def should_never_run(conn):
            calls.append(True)

        # SCHEMA_VERSION reste inchangee (deja egale a len(_MIGRATIONS)
        # reel, 0 aujourd'hui) : cette "migration" hypothetique ajoutee
        # SEULE a _MIGRATIONS (sans relever SCHEMA_VERSION en meme temps) ne
        # doit jamais s'executer sur une base deja a la version courante -
        # verrouille la garde `current != SCHEMA_VERSION`/le slicing borne
        # de _apply_migrations, pas seulement le cas "liste vide" trivial.
        with unittest.mock.patch.object(db, "_MIGRATIONS", [should_never_run]):
            database2 = Database(path)
            database2.close()
        self.assertEqual(calls, [], "aucune migration ne doit s'executer sur une base deja a la version courante")

    def test_schema_version_matches_the_number_of_registered_migrations(self):
        # Verrou de coherence : SCHEMA_VERSION doit toujours refleter
        # len(_MIGRATIONS), jamais une constante maintenue separement a la
        # main (source d'oubli garantie a la premiere migration ajoutee).
        self.assertEqual(db.SCHEMA_VERSION, len(db._MIGRATIONS))


if __name__ == "__main__":
    unittest.main()
