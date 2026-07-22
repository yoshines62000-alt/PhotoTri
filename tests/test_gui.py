"""Tests d'interface utilisant un vrai `Tk()` (pas de mock du widget lui
meme) : necessaires pour verrouiller des bugs de mise en page qui ne se
manifestent que dans la geometrie reellement calculee par Tkinter (voir F1
dans l'audit du 2026-07-22), invisibles a la seule lecture du code. Ignores
proprement (`skipTest`) si aucun affichage n'est disponible pour ouvrir une
fenetre Tk (ex. environnement CI sans serveur graphique)."""

import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from tkinter import TclError, Tk
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui
import grouping


# Chemin de dossier "realiste mais long" : une arborescence OneDrive tout a
# fait ordinaire (annee + evenement + sous-dossier de journee), pas un cas
# extreme fabrique pour l'occasion - exactement le type de chemin qui a
# fait echouer la mise en page avant correctif (144 caracteres, capture
# `11_chemin_tres_long_overlap.png` de l'audit).
LONG_FOLDER_PATH = (
    r"C:\Users\utilisateur\OneDrive\Images\Photos de famille\2024"
    r"\Vacances_ete_a_la_montagne_avec_les_enfants_et_les_grands_parents"
    r"\Journee_du_15_aout"
)


class TestFolderLabelDoesNotHideActionButtons(unittest.TestCase):
    """Verrouille F1 : un chemin de dossier long ne doit jamais repousser
    les boutons "Analyser (scanner)" et "Arreter" hors de la zone visible de
    la fenetre, ni les rendre non-mappes (bug reproduit et mesure a l'audit
    du 2026-07-22 : ismapped=0, viewable=0, width=1 pour les deux boutons,
    a la taille de fenetre par defaut 1150x720, avec un chemin de 144
    caracteres)."""

    def setUp(self):
        try:
            self.root = Tk()
        except TclError as exc:
            self.skipTest(f"Pas d'affichage disponible pour un test Tk reel : {exc}")
        self.tmp_dir = Path(tempfile.mkdtemp())
        self._apps = []

    def tearDown(self):
        for app in self._apps:
            try:
                app.db.close()
            except Exception:
                pass
        try:
            self.root.destroy()
        except TclError:
            pass

    def _make_app(self) -> "gui.PhotoTriApp":
        """Construit une PhotoTriApp reelle sans jamais toucher au vrai
        %APPDATA%\\PhotoTri\\phototri.sqlite de l'utilisateur qui execute
        les tests - _data_dir() est redirige vers un dossier temporaire
        jetable, ferme dans tearDown. La verification de mise a jour (appel
        reseau vers l'API GitHub) est neutralisee : sans rapport avec la
        mise en page testee ici, elle rendrait le test tributaire du reseau."""
        with mock.patch.object(gui, "_data_dir", return_value=self.tmp_dir), \
             mock.patch.object(gui.update_checker, "start_update_check"):
            app = gui.PhotoTriApp(self.root)
        self._apps.append(app)
        return app

    def _right_edge_within_top_frame(self, app, widget) -> bool:
        """Calcule le bord droit de `widget` en coordonnees relatives a
        `top_frame` (en remontant la chaine de parents jusqu'a top_frame) et
        verifie qu'il ne depasse pas la largeur reellement allouee a
        top_frame - une comparaison via winfo_rootx() serait faussee par la
        position de la fenetre sur l'ecran (variable selon le gestionnaire
        de fenetres), alors que cette comparaison relative est fiable quel
        que soit l'emplacement de la fenetre."""
        x = 0
        node = widget
        while node != app.top_frame:
            x += node.winfo_x()
            node = node.nametowidget(node.winfo_parent())
        right_edge = x + widget.winfo_width()
        return right_edge <= app.top_frame.winfo_width()

    def test_default_window_size_keeps_scan_and_stop_buttons_usable(self):
        app = self._make_app()
        with mock.patch.object(gui.filedialog, "askdirectory", return_value=LONG_FOLDER_PATH):
            app._choose_folder()
        self.root.update_idletasks()
        self.root.update()

        for widget, name in ((app.scan_button, "Analyser"), (app.stop_button, "Arreter")):
            self.assertEqual(widget.winfo_ismapped(), 1, f"bouton {name} non mappe")
            self.assertGreater(widget.winfo_width(), 1, f"bouton {name} de largeur nulle")
            self.assertTrue(
                self._right_edge_within_top_frame(app, widget),
                f"bouton {name} deborde de la zone visible de la fenetre",
            )

    def test_folder_path_display_stays_truncated_but_full_path_preserved(self):
        app = self._make_app()
        with mock.patch.object(gui.filedialog, "askdirectory", return_value=LONG_FOLDER_PATH):
            app._choose_folder()
        self.root.update_idletasks()
        self.root.update()

        # Le texte AFFICHE peut etre tronque, mais le chemin complet doit
        # rester connu de l'application (utilise pour le scan lui-meme et
        # pour l'info-bulle), jamais perdu.
        self.assertEqual(app._folder_full_path, LONG_FOLDER_PATH)
        self.assertEqual(str(app.selected_folder), LONG_FOLDER_PATH)
        displayed = app.folder_label_var.get()
        self.assertLessEqual(len(displayed), len(LONG_FOLDER_PATH))

    def test_short_folder_path_is_not_truncated(self):
        app = self._make_app()
        short_path = r"C:\Photos"
        with mock.patch.object(gui.filedialog, "askdirectory", return_value=short_path):
            app._choose_folder()
        self.root.update_idletasks()
        self.root.update()
        self.assertEqual(app.folder_label_var.get(), short_path)


class TestConfidenceIndicatorInUI(unittest.TestCase):
    """Verrouille A2 (audit du 2026-07-22) : avant ce correctif, ni le
    Treeview des groupes ni les cartes-vignettes n'affichaient jamais le
    moindre signal de similarite/distance - impossible de distinguer un
    vrai quasi-doublon d'un faux positif sans comparer visuellement chaque
    carte. Insere des lignes directement en base (hachages CONTROLES, pas
    calcules depuis de vraies photos) pour obtenir des distances de Hamming
    predictibles, avec de vrais petits fichiers image pour que les
    vignettes des cartes se generent normalement."""

    def setUp(self):
        try:
            self.root = Tk()
        except TclError as exc:
            self.skipTest(f"Pas d'affichage disponible pour un test Tk reel : {exc}")
        self.tmp_dir = Path(tempfile.mkdtemp())
        self._apps = []

    def tearDown(self):
        for app in self._apps:
            try:
                app.db.close()
            except Exception:
                pass
        try:
            self.root.destroy()
        except TclError:
            pass

    def _make_app(self) -> "gui.PhotoTriApp":
        with mock.patch.object(gui, "_data_dir", return_value=self.tmp_dir), \
             mock.patch.object(gui.update_checker, "start_update_check"):
            app = gui.PhotoTriApp(self.root)
        self._apps.append(app)
        return app

    def _make_image_file(self, name: str, color=(120, 120, 120)) -> Path:
        path = self.tmp_dir / name
        Image.new("RGB", (32, 32), color).save(path)
        return path

    def _wait_grouping_idle(self, app, timeout=10):
        deadline = time.monotonic() + timeout
        while app._grouping_in_progress:
            self.root.update()
            time.sleep(0.01)
            self.assertLess(time.monotonic(), deadline, "le calcul de groupes ne se termine jamais")
        self.root.update()

    def test_near_group_shows_similarity_percentage_and_label_in_treeview(self):
        app = self._make_app()
        base = int("10" * 32, 2)  # hash equilibre (pas a faible entropie)
        close = base ^ 0b11  # distance de Hamming 2
        p1 = self._make_image_file("a.jpg")
        p2 = self._make_image_file("b.jpg")
        app.db.upsert_photo(str(p1), 1000, 1.0, 800, 600, "sha-a", base, None)
        app.db.upsert_photo(str(p2), 1000, 1.0, 800, 600, "sha-b", close, None)

        app._refresh_groups()
        self._wait_grouping_idle(app)

        self.assertEqual(len(app._groups), 1)
        group = app._groups[0]
        self.assertEqual(group.kind, "near")
        self.assertEqual(group.max_distance, 2)

        iid = next(iter(app._groups_by_iid))
        values = app.groups_tree.item(iid, "values")
        confidence_text = values[3]
        expected_pct = grouping.similarity_percent(2)
        self.assertIn(f"{expected_pct}%", confidence_text)
        self.assertIn("Tres proches", confidence_text)

    def test_exact_group_shows_100_percent_identical(self):
        app = self._make_app()
        p1 = self._make_image_file("dup1.jpg")
        p2 = self._make_image_file("dup2.jpg")
        app.db.upsert_photo(str(p1), 1000, 1.0, 800, 600, "sha-same", 0, None)
        app.db.upsert_photo(str(p2), 1000, 1.0, 800, 600, "sha-same", 0, None)

        app._refresh_groups()
        self._wait_grouping_idle(app)

        self.assertEqual(app._groups[0].kind, "exact")
        iid = next(iter(app._groups_by_iid))
        values = app.groups_tree.item(iid, "values")
        self.assertEqual(values[3], "100% - Identique")

    def test_card_shows_distance_relative_to_keeper(self):
        app = self._make_app()
        base = int("10" * 32, 2)
        close = base ^ 0b11  # distance 2
        p_keeper = self._make_image_file("keeper.jpg")
        p_other = self._make_image_file("other.jpg")
        # La photo de plus grande resolution est suggeree comme keeper
        # (voir grouping.suggest_keeper).
        app.db.upsert_photo(str(p_keeper), 1000, 1.0, 4000, 3000, "sha-a", base, None)
        app.db.upsert_photo(str(p_other), 1000, 1.0, 800, 600, "sha-b", close, None)

        app._refresh_groups()
        self._wait_grouping_idle(app)

        iid = next(iter(app._groups_by_iid))
        app.groups_tree.selection_set(iid)
        app._on_group_select()
        self.root.update()

        card_texts = []
        for card in app.cards_inner.winfo_children():
            texts = [child["text"] for child in card.winfo_children() if "text" in child.keys()]
            card_texts.append(" | ".join(texts))
        joined = "\n".join(card_texts)
        self.assertIn("Distance : 2/6", joined)
        self.assertIn("★ Suggeree a garder", joined)

    def test_exact_group_card_shows_copie_exacte_instead_of_distance(self):
        app = self._make_app()
        p1 = self._make_image_file("dup1.jpg")
        p2 = self._make_image_file("dup2.jpg")
        app.db.upsert_photo(str(p1), 1000, 1.0, 800, 600, "sha-same", 0, None)
        app.db.upsert_photo(str(p2), 1000, 1.0, 800, 600, "sha-same", 0, None)

        app._refresh_groups()
        self._wait_grouping_idle(app)

        iid = next(iter(app._groups_by_iid))
        app.groups_tree.selection_set(iid)
        app._on_group_select()
        self.root.update()

        card_texts = []
        for card in app.cards_inner.winfo_children():
            texts = [child["text"] for child in card.winfo_children() if "text" in child.keys()]
            card_texts.append(" | ".join(texts))
        joined = "\n".join(card_texts)
        self.assertIn("Copie exacte", joined)
        self.assertNotIn("Distance :", joined)


class TestOrphanCopyNotLeftUnindexedOnMoveFailure(unittest.TestCase):
    """Verrouille E4 (audit du 2026-07-22) : quand shutil.move() bascule sur
    son chemin copie+suppression (volumes differents) et que SEULE la
    suppression de la source echoue (fichier source en lecture seule /
    permissions restreintes), une copie complete existe deja dans le
    dossier de revision au moment ou l'exception est levee.
    _move_checked_photos() ne doit alors ni pretendre a l'utilisateur
    qu'"aucune action n'a eu lieu" (l'ancien message "Echec pour : ..."),
    ni laisser cette copie orpheline hors de l'index (jamais reperee par un
    scan futur, le dossier de revision etant exclu du parcours). A
    l'inverse, un veritable echec de copie (source intacte, destination
    absente/incomplete) doit rester rapporte comme un echec et ne doit
    laisser aucun fichier partiel trainer dans le dossier de revision."""

    def setUp(self):
        try:
            self.root = Tk()
        except TclError as exc:
            self.skipTest(f"Pas d'affichage disponible pour un test Tk reel : {exc}")
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.photos_dir = self.tmp_dir / "photos"
        self.photos_dir.mkdir()
        self.review_dir = self.tmp_dir / "revision"
        self._apps = []

        # showerror/showinfo neutralises (vraies boites modales) ;
        # showwarning capture pour inspecter le message reellement montre a
        # l'utilisateur, sans jamais toucher aux widgets/logique metier.
        self._patchers = [
            mock.patch("tkinter.messagebox.showerror"),
            mock.patch("tkinter.messagebox.showinfo"),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)
        self.mock_showwarning = mock.patch("tkinter.messagebox.showwarning").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        for app in self._apps:
            try:
                app.db.close()
            except Exception:
                pass
        try:
            self.root.destroy()
        except TclError:
            pass

    def _make_app(self) -> "gui.PhotoTriApp":
        with mock.patch.object(gui, "_data_dir", return_value=self.tmp_dir / "appdata"), \
             mock.patch.object(gui.update_checker, "start_update_check"):
            app = gui.PhotoTriApp(self.root)
        self._apps.append(app)
        return app

    def _index_photo(self, app, name: str):
        path = self.photos_dir / name
        Image.new("RGB", (16, 16), (10, 20, 30)).save(path)
        stat = path.stat()
        photo_id = app.db.upsert_photo(
            str(path), stat.st_size, stat.st_mtime, 16, 16, "sha-test", 0, None,
        )
        return photo_id, path

    def _select_group_for_move(self, app, keep_id, move_id):
        group = grouping.PhotoGroup(kind="exact", photo_ids=sorted([keep_id, move_id]), max_distance=0)
        app._selected_group = group
        app._checkbox_vars = {
            keep_id: gui.BooleanVar(value=False),
            move_id: gui.BooleanVar(value=True),
        }

    def test_delete_failure_after_successful_copy_is_indexed_not_reported_as_pure_failure(self):
        app = self._make_app()
        keep_id, _ = self._index_photo(app, "garder.jpg")
        move_id, move_path = self._index_photo(app, "a_deplacer.jpg")
        self._select_group_for_move(app, keep_id, move_id)
        app.review_folder_var.set(str(self.review_dir))

        def fake_move(src, dst):
            # Reproduit le chemin copy+unlink emprunte par shutil.move()
            # entre deux volumes differents, avec seule la suppression
            # finale de la source qui echoue (permissions/lecture seule) -
            # une copie complete existe deja quand l'exception remonte.
            shutil.copy2(src, dst)
            raise OSError(5, "Acces refuse")

        with mock.patch.object(gui.shutil, "move", side_effect=fake_move):
            app._move_checked_photos()

        dest_path = self.review_dir / "a_deplacer.jpg"
        self.assertTrue(dest_path.exists(), "la copie complete doit rester dans le dossier de revision")
        self.assertTrue(move_path.exists(), "la source n'a pas pu etre supprimee (simule) - toujours presente")

        row = app.db.get_photo(move_id)
        self.assertEqual(row["status"], "moved", "le fichier est bel et bien range : l'index doit le refleter")
        self.assertEqual(row["moved_to"], str(dest_path))

        self.mock_showwarning.assert_called_once()
        shown_message = self.mock_showwarning.call_args.args[1]
        self.assertNotIn("Echec pour", shown_message, "ne doit pas etre presente comme un pur echec")
        self.assertIn("original n'a PAS pu etre", shown_message)
        self.assertIn("a_deplacer.jpg", shown_message)

    def test_genuine_copy_failure_leaves_no_orphan_file_and_is_reported_as_failure(self):
        app = self._make_app()
        keep_id, _ = self._index_photo(app, "garder2.jpg")
        move_id, move_path = self._index_photo(app, "echec.jpg")
        self._select_group_for_move(app, keep_id, move_id)
        app.review_folder_var.set(str(self.review_dir))

        def fake_move(src, dst):
            # Veritable echec de copie (ex. disque plein en cours
            # d'ecriture) : la source reste intacte, aucune copie complete
            # n'existe.
            raise OSError(28, "Espace disque insuffisant")

        with mock.patch.object(gui.shutil, "move", side_effect=fake_move):
            app._move_checked_photos()

        dest_path = self.review_dir / "echec.jpg"
        self.assertFalse(dest_path.exists(), "aucune copie partielle ne doit rester orpheline")
        self.assertTrue(move_path.exists())

        row = app.db.get_photo(move_id)
        self.assertEqual(row["status"], "active", "un veritable echec ne doit pas marquer la photo comme deplacee")

        self.mock_showwarning.assert_called_once()
        shown_message = self.mock_showwarning.call_args.args[1]
        self.assertIn("Echec pour", shown_message)
        self.assertIn("echec.jpg", shown_message)


if __name__ == "__main__":
    unittest.main()
