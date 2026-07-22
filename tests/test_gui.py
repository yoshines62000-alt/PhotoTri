"""Tests d'interface utilisant un vrai `Tk()` (pas de mock du widget lui
meme) : necessaires pour verrouiller des bugs de mise en page qui ne se
manifestent que dans la geometrie reellement calculee par Tkinter (voir F1
dans l'audit du 2026-07-22), invisibles a la seule lecture du code. Ignores
proprement (`skipTest`) si aucun affichage n'est disponible pour ouvrir une
fenetre Tk (ex. environnement CI sans serveur graphique)."""

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


if __name__ == "__main__":
    unittest.main()
