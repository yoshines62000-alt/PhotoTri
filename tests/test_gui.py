"""Tests d'interface utilisant un vrai `Tk()` (pas de mock du widget lui
meme) : necessaires pour verrouiller des bugs de mise en page qui ne se
manifestent que dans la geometrie reellement calculee par Tkinter (voir F1
dans l'audit du 2026-07-22), invisibles a la seule lecture du code. Ignores
proprement (`skipTest`) si aucun affichage n'est disponible pour ouvrir une
fenetre Tk (ex. environnement CI sans serveur graphique)."""

import gc
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import types
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
        # Force une collecte cyclique AVANT de creer un nouveau Tk() -
        # observe en ecrivant ces tests : ce fichier cree/detruit des
        # dizaines de racines Tk reelles a la suite, et Widget.destroy() ne
        # libere pas les commandes Tcl enregistrees via .bind()/`command=`
        # (seule la destruction de l'interpreteur/racine le fait) - les
        # fermetures Python qui en decoulent (souvent auto-referentes via
        # `self`) restent alors comme garbage CYCLIQUE, que seul gc.collect()
        # peut liberer (le comptage de references seul ne suffit pas). Sans
        # ce nettoyage explicite entre les tests, ce garbage s'accumule au
        # fil de l'execution complete de la suite au point de retarder une
        # collecte automatique ulterieure pile pendant l'attente bornee
        # d'un thread d'arriere-plan (calcul de groupes) ci-dessous,
        # jusqu'a lui faire depasser son delai - un artefact de la suite de
        # tests elle-meme (aucune consequence pour l'application reelle, qui
        # ne cree jamais des dizaines de fenetres Tk a la suite dans le
        # meme processus).
        gc.collect()
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


class TestMoveButtonLabelFitsItsOwnWidth(unittest.TestCase):
    """Verrouille F2 (audit du 2026-07-22) : ttk.Button ne redimensionne
    jamais son texte - un widget trop etroit pour son contenu se contente
    de le laisser deborder de son propre cadre (derniere(s) lettre(s)
    coupees) plutot que de l'ajuster. Le bouton "Deplacer..." se
    retrouvait ainsi compresse sous sa largeur naturelle des que l'espace
    manquait dans la barre d'action - y compris avant meme la taille
    minimale officiellement supportee (root.minsize(850, 550), capture
    09_zoom_bottombar.png de l'audit). Le libelle a ete raccourci a
    "Deplacer la selection" (le contexte "vers le dossier de revision" est
    de toute facon deja visible juste a gauche via le champ "Dossier de
    revision :")."""

    def setUp(self):
        # Force une collecte cyclique AVANT de creer un nouveau Tk() -
        # observe en ecrivant ces tests : ce fichier cree/detruit des
        # dizaines de racines Tk reelles a la suite, et Widget.destroy() ne
        # libere pas les commandes Tcl enregistrees via .bind()/`command=`
        # (seule la destruction de l'interpreteur/racine le fait) - les
        # fermetures Python qui en decoulent (souvent auto-referentes via
        # `self`) restent alors comme garbage CYCLIQUE, que seul gc.collect()
        # peut liberer (le comptage de references seul ne suffit pas). Sans
        # ce nettoyage explicite entre les tests, ce garbage s'accumule au
        # fil de l'execution complete de la suite au point de retarder une
        # collecte automatique ulterieure pile pendant l'attente bornee
        # d'un thread d'arriere-plan (calcul de groupes) ci-dessous,
        # jusqu'a lui faire depasser son delai - un artefact de la suite de
        # tests elle-meme (aucune consequence pour l'application reelle, qui
        # ne cree jamais des dizaines de fenetres Tk a la suite dans le
        # meme processus).
        gc.collect()
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

    def _select_a_group(self, app):
        # Le bouton Deplacer (comme tout le panneau de detail) n'est
        # empaquete que lorsqu'un groupe est selectionne (_on_group_select) -
        # sans cette etape, self.detail_frame (et donc self.move_button)
        # n'est jamais affiche, quelle que soit la taille de la fenetre.
        p1 = self.tmp_dir / "dup1.jpg"
        p2 = self.tmp_dir / "dup2.jpg"
        Image.new("RGB", (32, 32), (120, 120, 120)).save(p1)
        Image.new("RGB", (32, 32), (120, 120, 120)).save(p2)
        app.db.upsert_photo(str(p1), 1000, 1.0, 800, 600, "sha-same", 0, None)
        app.db.upsert_photo(str(p2), 1000, 1.0, 800, 600, "sha-same", 0, None)
        app._refresh_groups()
        deadline = time.monotonic() + 10
        while app._grouping_in_progress:
            self.root.update()
            time.sleep(0.01)
            self.assertLess(time.monotonic(), deadline, "le calcul de groupes ne se termine jamais")
        iid = next(iter(app._groups_by_iid))
        app.groups_tree.selection_set(iid)
        app._on_group_select()
        self.root.update_idletasks()
        self.root.update()

    def test_move_button_label_is_short(self):
        app = self._make_app()
        self.assertEqual(app.move_button.cget("text"), "Deplacer la selection")

    def test_move_button_is_not_squeezed_below_its_natural_width(self):
        # A la taille de fenetre par defaut de l'application (1150x720,
        # root.geometry en __init__, jamais retrecie ici) : le bouton doit
        # etre affiche a sa largeur naturelle complete (winfo_width ==
        # winfo_reqwidth), jamais compresse en dessous (ce qui produirait
        # le texte tronque decrit dans l'audit).
        app = self._make_app()
        self._select_a_group(app)
        btn = app.move_button
        self.assertEqual(btn.winfo_ismapped(), 1, "bouton Deplacer non mappe a la taille de fenetre par defaut")
        self.assertEqual(
            btn.winfo_width(), btn.winfo_reqwidth(),
            "le bouton Deplacer est compresse sous sa largeur naturelle : son texte deborderait de son cadre",
        )


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
        # Force une collecte cyclique AVANT de creer un nouveau Tk() -
        # observe en ecrivant ces tests : ce fichier cree/detruit des
        # dizaines de racines Tk reelles a la suite, et Widget.destroy() ne
        # libere pas les commandes Tcl enregistrees via .bind()/`command=`
        # (seule la destruction de l'interpreteur/racine le fait) - les
        # fermetures Python qui en decoulent (souvent auto-referentes via
        # `self`) restent alors comme garbage CYCLIQUE, que seul gc.collect()
        # peut liberer (le comptage de references seul ne suffit pas). Sans
        # ce nettoyage explicite entre les tests, ce garbage s'accumule au
        # fil de l'execution complete de la suite au point de retarder une
        # collecte automatique ulterieure pile pendant l'attente bornee
        # d'un thread d'arriere-plan (calcul de groupes) ci-dessous,
        # jusqu'a lui faire depasser son delai - un artefact de la suite de
        # tests elle-meme (aucune consequence pour l'application reelle, qui
        # ne cree jamais des dizaines de fenetres Tk a la suite dans le
        # meme processus).
        gc.collect()
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


class TestRefreshGroupsPassesBandsMatchingSensitivityThreshold(unittest.TestCase):
    """Verrouille le correctif de l'audit du 2026-07-28 (gui.py:301) : le
    Spinbox de sensibilite (plage 0-20) permettait a l'utilisateur de
    depasser grouping.DEFAULT_BANDS (8) sans que la valeur choisie n'ait
    jamais d'effet reel sur le parametre `bands` transmis a
    grouping.group_photos() - toujours fige a DEFAULT_BANDS. Or
    group_photos() n'emprunte son chemin rapide (indexage LSH) que si
    `near_duplicate_threshold < bands` (voir grouping.py) : tout seuil >= 8
    retombait donc silencieusement sur la comparaison exhaustive O(n^2)
    (lente sur une grosse bibliotheque), sans le moindre avertissement ni le
    moindre gain reel a choisir une valeur superieure a 7 dans le Spinbox.
    _refresh_groups() doit desormais faire varier `bands` avec le seuil
    choisi pour que le chemin rapide reste utilisable sur toute la plage
    0-20 du Spinbox, tout en laissant DEFAULT_BANDS inchange pour la plage
    0-7 (comportement deja eprouve)."""

    def setUp(self):
        gc.collect()
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

    def _wait_grouping_idle(self, app, timeout=10):
        deadline = time.monotonic() + timeout
        while app._grouping_in_progress:
            self.root.update()
            time.sleep(0.01)
            self.assertLess(time.monotonic(), deadline, "le calcul de groupes ne se termine jamais")
        self.root.update()

    def test_bands_grows_with_threshold_above_default(self):
        app = self._make_app()
        app.threshold_var.set(15)  # > DEFAULT_BANDS (8), pourtant autorise par le Spinbox (plage 0-20)

        with mock.patch.object(gui.grouping, "group_photos", return_value=[]) as mock_group_photos:
            app._refresh_groups()
            self._wait_grouping_idle(app)

        mock_group_photos.assert_called_once()
        kwargs = mock_group_photos.call_args.kwargs
        self.assertEqual(kwargs["near_duplicate_threshold"], 15)
        self.assertIn("bands", kwargs, "bands doit desormais etre transmis explicitement a group_photos()")
        self.assertGreater(
            kwargs["bands"], kwargs["near_duplicate_threshold"],
            "bands doit rester STRICTEMENT superieur au seuil - condition exacte du chemin rapide de group_photos()",
        )

    def test_bands_stays_at_default_for_threshold_below_default_bands(self):
        # Comportement inchange pour la plage 0-7 : DEFAULT_BANDS (8) suffit
        # deja a garantir le chemin rapide, pas de raison de le faire varier.
        app = self._make_app()
        app.threshold_var.set(4)

        with mock.patch.object(gui.grouping, "group_photos", return_value=[]) as mock_group_photos:
            app._refresh_groups()
            self._wait_grouping_idle(app)

        kwargs = mock_group_photos.call_args.kwargs
        self.assertEqual(kwargs["bands"], grouping.DEFAULT_BANDS)

    def test_bands_at_spinbox_maximum_still_exceeds_threshold(self):
        # Borne haute du Spinbox (to=20, voir threshold_spinbox) : meme au
        # maximum autorise, bands doit rester superieur au seuil plutot que
        # de retomber sur la comparaison exhaustive sans avertissement.
        app = self._make_app()
        app.threshold_var.set(20)

        with mock.patch.object(gui.grouping, "group_photos", return_value=[]) as mock_group_photos:
            app._refresh_groups()
            self._wait_grouping_idle(app)

        kwargs = mock_group_photos.call_args.kwargs
        self.assertEqual(kwargs["near_duplicate_threshold"], 20)
        self.assertGreater(kwargs["bands"], 20)


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
        # Force une collecte cyclique AVANT de creer un nouveau Tk() -
        # observe en ecrivant ces tests : ce fichier cree/detruit des
        # dizaines de racines Tk reelles a la suite, et Widget.destroy() ne
        # libere pas les commandes Tcl enregistrees via .bind()/`command=`
        # (seule la destruction de l'interpreteur/racine le fait) - les
        # fermetures Python qui en decoulent (souvent auto-referentes via
        # `self`) restent alors comme garbage CYCLIQUE, que seul gc.collect()
        # peut liberer (le comptage de references seul ne suffit pas). Sans
        # ce nettoyage explicite entre les tests, ce garbage s'accumule au
        # fil de l'execution complete de la suite au point de retarder une
        # collecte automatique ulterieure pile pendant l'attente bornee
        # d'un thread d'arriere-plan (calcul de groupes) ci-dessous,
        # jusqu'a lui faire depasser son delai - un artefact de la suite de
        # tests elle-meme (aucune consequence pour l'application reelle, qui
        # ne cree jamais des dizaines de fenetres Tk a la suite dans le
        # meme processus).
        gc.collect()
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

    def _wait_move_idle(self, app, timeout=10):
        """_move_checked_photos() deplace desormais les photos sur un thread
        dedie (voir gui.py, meme pattern que _scan_worker) plutot qu'en
        synchrone sur le thread principal - il faut donc pomper la boucle
        Tkinter (root.update()) jusqu'a ce que le thread ait pousse son
        resultat dans la file et que _poll_move_queue l'ait traite, avant de
        pouvoir observer l'etat final (fichiers deplaces, index a jour,
        messagebox affichee)."""
        deadline = time.monotonic() + timeout
        while app._moving:
            self.root.update()
            time.sleep(0.01)
            self.assertLess(time.monotonic(), deadline, "le deplacement ne se termine jamais")
        self.root.update()

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
            self._wait_move_idle(app)

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
            self._wait_move_idle(app)

        dest_path = self.review_dir / "echec.jpg"
        self.assertFalse(dest_path.exists(), "aucune copie partielle ne doit rester orpheline")
        self.assertTrue(move_path.exists())

        row = app.db.get_photo(move_id)
        self.assertEqual(row["status"], "active", "un veritable echec ne doit pas marquer la photo comme deplacee")

        self.mock_showwarning.assert_called_once()
        shown_message = self.mock_showwarning.call_args.args[1]
        self.assertIn("Echec pour", shown_message)
        self.assertIn("echec.jpg", shown_message)


class TestFriendlyErrorText(unittest.TestCase):
    """Verrouille C3 (audit du 2026-07-22) : le texte brut d'une exception
    Python/Pillow/OS (presque toujours en anglais, parfois anxiogene hors
    contexte - ex. "decompression bomb DOS attack", codes WinError bruts)
    ne doit plus jamais atteindre l'utilisateur tel quel dans une boite de
    dialogue. `_friendly_error_text()` est la seule fonction chargee de
    cette traduction ; comme c'est une fonction pure (aucun etat, aucun
    widget), elle est testee ici directement, sans avoir besoin d'un vrai
    `Tk()`."""

    def test_decompression_bomb_maps_to_corrupted_file_message_without_anxious_wording(self):
        exc = Image.DecompressionBombError(
            "Image size (3600000000 pixels) exceeds limit of 178956970 "
            "pixels, could be decompression bomb DOS attack."
        )
        message = gui._friendly_error_text(exc)
        self.assertNotIn("attack", message.lower())
        self.assertNotIn("DOS", message)
        self.assertIn("corrompus", message.lower())

    def test_unidentified_image_maps_to_corrupted_file_message(self):
        from PIL import UnidentifiedImageError
        message = gui._friendly_error_text(UnidentifiedImageError("cannot identify image file 'x.jpg'"))
        self.assertIn("corrompus", message.lower())

    def test_permission_error_maps_to_access_denied_message(self):
        message = gui._friendly_error_text(PermissionError(13, "Permission denied"))
        self.assertIn("Acces refuse", message)

    def test_file_not_found_maps_to_missing_file_message(self):
        message = gui._friendly_error_text(FileNotFoundError(2, "No such file or directory"))
        self.assertIn("introuvable", message.lower())

    def test_sqlite_locked_maps_to_database_busy_message(self):
        message = gui._friendly_error_text(sqlite3.OperationalError("database is locked"))
        self.assertIn("base de donnees", message.lower())

    def test_disk_full_maps_to_disk_space_message(self):
        message = gui._friendly_error_text(OSError(28, "No space left on device"))
        self.assertIn("Espace disque insuffisant", message)

    def test_generic_os_error_maps_to_disk_access_message(self):
        message = gui._friendly_error_text(OSError(6, "The handle is invalid"))
        self.assertIn("acces au disque", message.lower())

    def test_unknown_exception_falls_back_to_generic_message(self):
        message = gui._friendly_error_text(ValueError("un detail technique quelconque"))
        self.assertEqual(message, "Une erreur inattendue est survenue.")

    def test_no_raw_technical_numbers_leak_into_the_translated_message(self):
        # Non-regression directe du symptome mesure a l'audit : le message
        # traduit ne doit jamais contenir le detail technique brut de
        # l'exception d'origine.
        exc = Image.DecompressionBombError(
            "Image size (3600000000 pixels) exceeds limit of 178956970 pixels"
        )
        message = gui._friendly_error_text(exc)
        self.assertNotIn("3600000000", message)
        self.assertNotIn("178956970", message)


class TestShowFriendlyErrorDisplaysTranslatedMessageAndLogsDetail(unittest.TestCase):
    """Complement d'integration a TestFriendlyErrorText (C3) : verrouille
    que `_show_friendly_error()` - le point d'entree reellement appele par
    l'ouverture de l'index, l'echec de scan et l'echec de creation du
    dossier de revision - affiche bien le message francais generique dans
    la boite de dialogue (jamais le texte brut de l'exception), tout en
    conservant le detail technique complet dans erreurs.log (rien n'est
    perdu, simplement plus impose en premiere lecture)."""

    def setUp(self):
        # Force une collecte cyclique AVANT de creer un nouveau Tk() -
        # observe en ecrivant ces tests : ce fichier cree/detruit des
        # dizaines de racines Tk reelles a la suite, et Widget.destroy() ne
        # libere pas les commandes Tcl enregistrees via .bind()/`command=`
        # (seule la destruction de l'interpreteur/racine le fait) - les
        # fermetures Python qui en decoulent (souvent auto-referentes via
        # `self`) restent alors comme garbage CYCLIQUE, que seul gc.collect()
        # peut liberer (le comptage de references seul ne suffit pas). Sans
        # ce nettoyage explicite entre les tests, ce garbage s'accumule au
        # fil de l'execution complete de la suite au point de retarder une
        # collecte automatique ulterieure pile pendant l'attente bornee
        # d'un thread d'arriere-plan (calcul de groupes) ci-dessous,
        # jusqu'a lui faire depasser son delai - un artefact de la suite de
        # tests elle-meme (aucune consequence pour l'application reelle, qui
        # ne cree jamais des dizaines de fenetres Tk a la suite dans le
        # meme processus).
        gc.collect()
        try:
            self.root = Tk()
        except TclError as exc:
            self.skipTest(f"Pas d'affichage disponible pour un test Tk reel : {exc}")
        self.tmp_dir = Path(tempfile.mkdtemp())
        self._apps = []
        self.mock_showerror = mock.patch("tkinter.messagebox.showerror").start()
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
        with mock.patch.object(gui, "_data_dir", return_value=self.tmp_dir), \
             mock.patch.object(gui.update_checker, "start_update_check"):
            app = gui.PhotoTriApp(self.root)
        self._apps.append(app)
        return app

    def test_dialog_shows_french_generic_text_not_raw_exception_text(self):
        app = self._make_app()
        exc = Image.DecompressionBombError(
            "Image size (3600000000 pixels) exceeds limit of 178956970 pixels, "
            "could be decompression bomb DOS attack."
        )
        app._show_friendly_error("L'analyse a echoue", exc)

        self.mock_showerror.assert_called_once()
        shown_message = self.mock_showerror.call_args.args[1]
        self.assertNotIn("attack", shown_message.lower())
        self.assertNotIn("178956970", shown_message)
        self.assertIn("corrompus", shown_message.lower())

    def test_technical_detail_is_preserved_in_errors_log(self):
        app = self._make_app()
        exc = ValueError("detail technique precis attendu dans le journal")
        app._show_friendly_error("Une operation a echoue", exc)

        log_path = app.db_path.parent / "erreurs.log"
        self.assertTrue(log_path.exists(), "le detail technique complet doit rester consultable")
        content = log_path.read_text(encoding="utf-8")
        self.assertIn("detail technique precis attendu dans le journal", content)

        shown_message = self.mock_showerror.call_args.args[1]
        self.assertIn(str(log_path), shown_message, "la boite de dialogue doit indiquer ou trouver le detail")


class TestDiskSpaceCheckBeforeBatchMove(unittest.TestCase):
    """Verrouille E5 (audit du 2026-07-22) : avant ce correctif,
    `_move_checked_photos()` ne verifiait jamais l'espace disque disponible
    sur le volume de destination avant de lancer une serie de
    `shutil.move()` - un lot qui epuise l'espace disque a mi-chemin suit le
    meme chemin de code (et le meme risque de copie partielle non indexee)
    que E4. Le controle estime la taille des photos cochees qui necessitent
    reellement une copie (volume de destination different de la source -
    seul cas ou `shutil.move()` copie plutot que de renommer atomiquement)
    et la compare a l'espace libre sur le volume de destination, avec
    confirmation bloquante de l'utilisateur si l'estimation le depasse.

    Le dossier de revision et les photos source residant reellement sur le
    meme volume dans cet environnement de test, `Path.stat()` est
    monkeypatche pour que le dossier de revision paraisse sur un volume
    different (st_dev distinct) - condition necessaire pour que la taille
    des photos soit effectivement comptabilisee plutot qu'ignoree par
    l'optimisation "meme volume, os.rename() ne copie rien"."""

    def setUp(self):
        # Force une collecte cyclique AVANT de creer un nouveau Tk() -
        # observe en ecrivant ces tests : ce fichier cree/detruit des
        # dizaines de racines Tk reelles a la suite, et Widget.destroy() ne
        # libere pas les commandes Tcl enregistrees via .bind()/`command=`
        # (seule la destruction de l'interpreteur/racine le fait) - les
        # fermetures Python qui en decoulent (souvent auto-referentes via
        # `self`) restent alors comme garbage CYCLIQUE, que seul gc.collect()
        # peut liberer (le comptage de references seul ne suffit pas). Sans
        # ce nettoyage explicite entre les tests, ce garbage s'accumule au
        # fil de l'execution complete de la suite au point de retarder une
        # collecte automatique ulterieure pile pendant l'attente bornee
        # d'un thread d'arriere-plan (calcul de groupes) ci-dessous,
        # jusqu'a lui faire depasser son delai - un artefact de la suite de
        # tests elle-meme (aucune consequence pour l'application reelle, qui
        # ne cree jamais des dizaines de fenetres Tk a la suite dans le
        # meme processus).
        gc.collect()
        try:
            self.root = Tk()
        except TclError as exc:
            self.skipTest(f"Pas d'affichage disponible pour un test Tk reel : {exc}")
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.photos_dir = self.tmp_dir / "photos"
        self.photos_dir.mkdir()
        self.review_dir = self.tmp_dir / "revision"
        self.review_dir.mkdir()
        self._apps = []

        self._patchers = [
            mock.patch("tkinter.messagebox.showerror"),
            mock.patch("tkinter.messagebox.showinfo"),
            mock.patch("tkinter.messagebox.showwarning"),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)
        self.mock_askyesno = mock.patch("tkinter.messagebox.askyesno").start()
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

    def _select_one_of_two_for_move(self, app, keep_id, move_id):
        # Un seul coche sur deux (pas la totalite du groupe) : evite de
        # declencher le tout autre askyesno de confirmation "toutes les
        # photos du groupe sont cochees", pour isoler celui de l'espace
        # disque.
        group = grouping.PhotoGroup(kind="exact", photo_ids=sorted([keep_id, move_id]), max_distance=0)
        app._selected_group = group
        app._checkbox_vars = {
            keep_id: gui.BooleanVar(value=False),
            move_id: gui.BooleanVar(value=True),
        }

    def _patch_review_dir_on_a_different_volume(self):
        """Fait paraitre `self.review_dir` sur un volume different de celui
        des photos source (st_dev distinct), pour que le correctif E5
        comptabilise reellement leur taille au lieu de l'ignorer via
        l'optimisation "meme volume" (voir le commentaire de classe)."""
        real_stat = Path.stat
        review_dir = self.review_dir

        def fake_stat(path_obj, *args, **kwargs):
            result = real_stat(path_obj, *args, **kwargs)
            if path_obj == review_dir:
                return types.SimpleNamespace(st_dev=result.st_dev + 1, st_size=result.st_size)
            return types.SimpleNamespace(st_dev=result.st_dev, st_size=result.st_size)

        patcher = mock.patch.object(Path, "stat", new=fake_stat)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _wait_move_idle(self, app, timeout=10):
        """_move_checked_photos() deplace desormais les photos sur un thread
        dedie (voir gui.py, meme pattern que _scan_worker) plutot qu'en
        synchrone sur le thread principal - il faut donc pomper la boucle
        Tkinter (root.update()) jusqu'a ce que le thread ait pousse son
        resultat dans la file et que _poll_move_queue l'ait traite, avant de
        pouvoir observer l'etat final (fichiers deplaces, index a jour,
        messagebox affichee). Sans effet (retourne immediatement) si le
        deplacement ne demarre jamais - ex. refus a la confirmation d'espace
        disque, qui `return` avant meme de lancer le thread."""
        deadline = time.monotonic() + timeout
        while app._moving:
            self.root.update()
            time.sleep(0.01)
            self.assertLess(time.monotonic(), deadline, "le deplacement ne se termine jamais")
        self.root.update()

    def test_insufficient_space_asks_confirmation_and_aborts_when_declined(self):
        app = self._make_app()
        keep_id, _ = self._index_photo(app, "garder.jpg")
        move_id, move_path = self._index_photo(app, "a_deplacer.jpg")
        self._select_one_of_two_for_move(app, keep_id, move_id)
        app.review_folder_var.set(str(self.review_dir))
        self._patch_review_dir_on_a_different_volume()

        self.mock_askyesno.return_value = False
        with mock.patch.object(gui.shutil, "disk_usage") as mock_disk_usage, \
             mock.patch.object(gui.shutil, "move") as mock_move:
            mock_disk_usage.return_value = types.SimpleNamespace(total=10**9, used=10**9 - 1, free=1)
            app._move_checked_photos()
            mock_move.assert_not_called()

        self.mock_askyesno.assert_called_once()
        prompt = self.mock_askyesno.call_args.args[1]
        self.assertIn("Espace disque insuffisant", prompt)
        self.assertTrue(move_path.exists(), "rien ne doit avoir ete deplace apres un refus")
        row = app.db.get_photo(move_id)
        self.assertEqual(row["status"], "active")

    def test_insufficient_space_proceeds_when_confirmed(self):
        app = self._make_app()
        keep_id, _ = self._index_photo(app, "garder2.jpg")
        move_id, move_path = self._index_photo(app, "a_deplacer2.jpg")
        self._select_one_of_two_for_move(app, keep_id, move_id)
        app.review_folder_var.set(str(self.review_dir))
        self._patch_review_dir_on_a_different_volume()

        self.mock_askyesno.return_value = True
        with mock.patch.object(gui.shutil, "disk_usage") as mock_disk_usage:
            mock_disk_usage.return_value = types.SimpleNamespace(total=10**9, used=10**9 - 1, free=1)
            app._move_checked_photos()
            self._wait_move_idle(app)

        self.mock_askyesno.assert_called_once()
        self.assertFalse(move_path.exists(), "la photo confirmee doit avoir ete deplacee")
        dest_path = self.review_dir / "a_deplacer2.jpg"
        self.assertTrue(dest_path.exists())
        row = app.db.get_photo(move_id)
        self.assertEqual(row["status"], "moved")

    def test_sufficient_space_does_not_prompt(self):
        app = self._make_app()
        keep_id, _ = self._index_photo(app, "garder3.jpg")
        move_id, move_path = self._index_photo(app, "a_deplacer3.jpg")
        self._select_one_of_two_for_move(app, keep_id, move_id)
        app.review_folder_var.set(str(self.review_dir))
        self._patch_review_dir_on_a_different_volume()

        with mock.patch.object(gui.shutil, "disk_usage") as mock_disk_usage:
            mock_disk_usage.return_value = types.SimpleNamespace(
                total=10**12, used=0, free=10**12,
            )
            app._move_checked_photos()
            self._wait_move_idle(app)

        self.mock_askyesno.assert_not_called()
        self.assertFalse(move_path.exists(), "le deplacement doit avoir eu lieu normalement")
        dest_path = self.review_dir / "a_deplacer3.jpg"
        self.assertTrue(dest_path.exists())

    def test_same_volume_move_is_never_blocked_by_the_disk_space_check(self):
        # Cas le plus courant en usage reel (dossier de revision place par
        # defaut a cote du dossier scanne, meme volume que lui) :
        # shutil.move() emprunte alors os.rename(), qui ne copie rien - le
        # correctif E5 ne doit donc jamais bloquer ce cas, meme si
        # shutil.disk_usage() rapporte tres peu d'espace libre.
        app = self._make_app()
        keep_id, _ = self._index_photo(app, "garder4.jpg")
        move_id, move_path = self._index_photo(app, "a_deplacer4.jpg")
        self._select_one_of_two_for_move(app, keep_id, move_id)
        app.review_folder_var.set(str(self.review_dir))
        # Volontairement PAS de _patch_review_dir_on_a_different_volume ici :
        # dans cet environnement de test, review_dir et photos_dir sont
        # bien sur le meme volume reel.

        with mock.patch.object(gui.shutil, "disk_usage") as mock_disk_usage:
            mock_disk_usage.return_value = types.SimpleNamespace(total=10**9, used=10**9 - 1, free=1)
            app._move_checked_photos()
            self._wait_move_idle(app)

        self.mock_askyesno.assert_not_called()
        self.assertFalse(move_path.exists())
        dest_path = self.review_dir / "a_deplacer4.jpg"
        self.assertTrue(dest_path.exists())


class TestMoveDoesNotBlockMainThread(unittest.TestCase):
    """Verrouille le correctif de l'audit du 2026-07-28 : _move_checked_photos()
    tournait auparavant entierement en synchrone sur le thread principal
    Tkinter (aucun thread, aucune barre de progression), contrairement au
    scan (deja threade, voir _scan_worker) - un gros lot de photos gelait
    l'interface pendant toute la duree du deplacement. `shutil.move` est ici
    bloque via un threading.Event le temps de verifier que l'appel a
    _move_checked_photos() revient IMMEDIATEMENT (avant que le fichier ne
    soit reellement deplace) et que le deplacement s'execute bien sur un
    thread separe du thread principal - une synchronisation explicite par
    Event plutot qu'une simple mesure de temps ecoule, pour un test
    deterministe (pas de faux-negatif possible sous charge CI)."""

    def setUp(self):
        gc.collect()
        try:
            self.root = Tk()
        except TclError as exc:
            self.skipTest(f"Pas d'affichage disponible pour un test Tk reel : {exc}")
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.photos_dir = self.tmp_dir / "photos"
        self.photos_dir.mkdir()
        self.review_dir = self.tmp_dir / "revision"
        self._apps = []

        self._patchers = [
            mock.patch("tkinter.messagebox.showerror"),
            mock.patch("tkinter.messagebox.showinfo"),
            mock.patch("tkinter.messagebox.showwarning"),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)
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

    def test_move_call_returns_before_worker_finishes_and_runs_off_the_main_thread(self):
        app = self._make_app()
        keep_id, _ = self._index_photo(app, "garder.jpg")
        move_id, move_path = self._index_photo(app, "a_deplacer.jpg")
        group = grouping.PhotoGroup(kind="exact", photo_ids=sorted([keep_id, move_id]), max_distance=0)
        app._selected_group = group
        app._checkbox_vars = {
            keep_id: gui.BooleanVar(value=False),
            move_id: gui.BooleanVar(value=True),
        }
        app.review_folder_var.set(str(self.review_dir))

        main_thread = threading.current_thread()
        worker_threads = []
        entered = threading.Event()
        release = threading.Event()
        real_move = shutil.move

        def blocking_move(src, dst):
            # Enregistre le thread d'execution et bloque jusqu'a ce que le
            # test le libere explicitement (`release`) - le seul moyen de
            # prouver de facon deterministe que _move_checked_photos() rend
            # la main avant que ce deplacement ne soit termine, sans
            # dependre d'un delai arbitraire.
            worker_threads.append(threading.current_thread())
            entered.set()
            release.wait(timeout=5)
            return real_move(src, dst)

        with mock.patch.object(gui.shutil, "move", side_effect=blocking_move):
            app._move_checked_photos()
            # L'appel revient ICI, avant meme que blocking_move() ait pu
            # rendre la main (bloque sur `release`) - bug trouve a l'audit :
            # avant ce correctif, cette ligne n'etait atteinte qu'apres la
            # fin complete du deplacement (execution 100% synchrone).
            self.assertTrue(entered.wait(timeout=5), "le thread de deplacement n'a jamais demarre")
            self.assertTrue(app._moving, "_moving doit rester True pendant que le thread travaille encore")
            self.assertTrue(move_path.exists(), "le fichier ne doit pas encore avoir ete deplace (bloque via l'Event)")
            self.assertEqual(len(worker_threads), 1)
            self.assertNotEqual(
                worker_threads[0], main_thread,
                "shutil.move doit s'executer sur un thread separe du thread principal Tkinter, pas le geler",
            )

            release.set()
            deadline = time.monotonic() + 10
            while app._moving:
                self.root.update()
                time.sleep(0.01)
                self.assertLess(time.monotonic(), deadline, "le deplacement ne se termine jamais")
            self.root.update()

        self.assertFalse(move_path.exists(), "le fichier doit finalement avoir ete deplace")
        row = app.db.get_photo(move_id)
        self.assertEqual(row["status"], "moved", "l'index doit refleter le deplacement une fois le thread termine")


class TestPlaceholderTextReflectsAppState(unittest.TestCase):
    """Verrouille F3 (audit du 2026-07-22) : le texte du panneau de detail
    (affiche tant qu'aucun groupe n'est selectionne a gauche) doit refleter
    l'etat reel de l'application - message d'accueil initial, "Analyse en
    cours..." pendant un scan deja lance, puis une invite a selectionner un
    groupe ou "Aucun doublon..." une fois le regroupement termine - au lieu
    de rester fige sur le tout premier message quel que soit l'etat reel
    (bug trouve a l'audit : le message d'accueil restait affiche meme
    pendant un scan deja en cours sur un dossier deja choisi)."""

    def setUp(self):
        # Force une collecte cyclique AVANT de creer un nouveau Tk() -
        # observe en ecrivant ces tests : ce fichier cree/detruit des
        # dizaines de racines Tk reelles a la suite, et Widget.destroy() ne
        # libere pas les commandes Tcl enregistrees via .bind()/`command=`
        # (seule la destruction de l'interpreteur/racine le fait) - les
        # fermetures Python qui en decoulent (souvent auto-referentes via
        # `self`) restent alors comme garbage CYCLIQUE, que seul gc.collect()
        # peut liberer (le comptage de references seul ne suffit pas). Sans
        # ce nettoyage explicite entre les tests, ce garbage s'accumule au
        # fil de l'execution complete de la suite au point de retarder une
        # collecte automatique ulterieure pile pendant l'attente bornee
        # d'un thread d'arriere-plan (calcul de groupes) ci-dessous,
        # jusqu'a lui faire depasser son delai - un artefact de la suite de
        # tests elle-meme (aucune consequence pour l'application reelle, qui
        # ne cree jamais des dizaines de fenetres Tk a la suite dans le
        # meme processus).
        gc.collect()
        try:
            self.root = Tk()
        except TclError as exc:
            self.skipTest(f"Pas d'affichage disponible pour un test Tk reel : {exc}")
        self.tmp_dir = Path(tempfile.mkdtemp())
        self._apps = []
        self._patchers = [
            mock.patch("tkinter.messagebox.showerror"),
            mock.patch("tkinter.messagebox.showwarning"),
            mock.patch("tkinter.messagebox.showinfo"),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)

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

    def _wait_grouping_idle(self, app, timeout=15):
        # Meme pattern que TestConfidenceIndicatorInUI (deja eprouve dans ce
        # fichier) : n'attend QUE le regroupement, pas un scan reel - un
        # vrai thread de scan (nouvelle connexion SQLite, vrai decodage
        # d'image) est plus lourd et s'est avere sujet a une instabilite
        # Tcl/Tk propre a cet environnement de test quand il est combine a
        # de nombreux autres cycles Tk() dans le meme processus (observe en
        # ecrivant ces tests) - inutile ici, ces scenarios ne testent que le
        # texte affiche APRES un regroupement, jamais le scan lui-meme.
        deadline = time.monotonic() + timeout
        while app._grouping_in_progress:
            self.root.update()
            time.sleep(0.01)
            self.assertLess(time.monotonic(), deadline, "le calcul de groupes ne se termine jamais")
        self.root.update()

    def test_initial_text_before_any_interaction(self):
        app = self._make_app()
        self.assertEqual(app.detail_placeholder.cget("text"), gui.PLACEHOLDER_TEXT_INITIAL)

    def test_text_switches_to_scanning_as_soon_as_a_scan_starts(self):
        app = self._make_app()
        app.selected_folder = self.tmp_dir
        # Le thread de scan reel (nouvelle connexion SQLite, decodage
        # d'image) n'a aucun rapport avec ce qui est verrouille ici (la mise
        # a jour SYNCHRONE du texte du placeholder, avant meme que le thread
        # ne demarre) : `threading.Thread` est neutralisee pour qu'aucun
        # thread reel ne soit cree, evitant tout travail/IO superflu pour
        # cette seule assertion.
        with mock.patch.object(gui.threading, "Thread") as mock_thread_cls:
            app._start_scan()
            mock_thread_cls.assert_called_once()
            mock_thread_cls.return_value.start.assert_called_once()
        # Verifie AVANT tout root.update()/traitement de la file : le texte
        # doit deja refleter "scan en cours" des l'appel a _start_scan, pas
        # seulement une fois le scan effectivement termine.
        self.assertEqual(app.detail_placeholder.cget("text"), gui.PLACEHOLDER_TEXT_SCANNING)

    def test_text_invites_to_select_a_group_once_duplicates_are_found(self):
        app = self._make_app()
        p1 = self.tmp_dir / "a.jpg"
        p2 = self.tmp_dir / "b.jpg"
        Image.new("RGB", (16, 16), (10, 20, 30)).save(p1)
        Image.new("RGB", (16, 16), (10, 20, 30)).save(p2)
        app.db.upsert_photo(str(p1), 1000, 1.0, 16, 16, "sha-same", 0, None)
        app.db.upsert_photo(str(p2), 1000, 1.0, 16, 16, "sha-same", 0, None)

        app._refresh_groups()
        self._wait_grouping_idle(app)

        self.assertGreater(len(app._groups), 0)
        self.assertEqual(app.detail_placeholder.cget("text"), gui.PLACEHOLDER_TEXT_SELECT_GROUP)

    def test_text_shows_empty_message_when_no_duplicates_found(self):
        app = self._make_app()
        p = self.tmp_dir / "unique.jpg"
        Image.new("RGB", (16, 16), (10, 20, 30)).save(p)
        app.db.upsert_photo(str(p), 1000, 1.0, 16, 16, "sha-unique", 0, None)

        app._refresh_groups()
        self._wait_grouping_idle(app)

        self.assertEqual(app._groups, [])
        self.assertEqual(app.detail_placeholder.cget("text"), gui.PLACEHOLDER_TEXT_EMPTY)


class TestSecondaryTextColorMeetsWcagAaContrast(unittest.TestCase):
    """Verrouille F4 (audit du 2026-07-22) : le texte "secondaire" (taille/
    dimensions/date sur les cartes, barre de statut, numero de version...)
    doit atteindre un ratio de contraste WCAG AA (>= 4.5:1 pour du texte de
    taille normale) sur le fond du theme ttk "alt" utilise par l'application
    (#d9d9d9) - l'ancien gris #666 n'offrait qu'environ 4.07:1."""

    @staticmethod
    def _srgb_channel_to_linear(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    def _relative_luminance(self, hex_color: str) -> float:
        hex_color = hex_color.lstrip("#")
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
        rl, gl, bl = (self._srgb_channel_to_linear(v) for v in (r, g, b))
        return 0.2126 * rl + 0.7152 * gl + 0.0722 * bl

    def _contrast_ratio(self, hex_a: str, hex_b: str) -> float:
        la, lb = self._relative_luminance(hex_a), self._relative_luminance(hex_b)
        lighter, darker = max(la, lb), min(la, lb)
        return (lighter + 0.05) / (darker + 0.05)

    def test_secondary_text_color_meets_aa_contrast_on_the_alt_theme_background(self):
        ratio = self._contrast_ratio(gui.SECONDARY_TEXT_COLOR, "#d9d9d9")
        self.assertGreaterEqual(ratio, 4.5, f"ratio de contraste {ratio:.2f}:1 insuffisant (seuil WCAG AA 4.5:1)")

    def test_old_insufficiently_contrasted_gray_is_no_longer_used(self):
        source = Path(gui.__file__).read_text(encoding="utf-8")
        self.assertNotIn(
            'foreground="#666"', source,
            "l'ancien gris #666 (contraste WCAG insuffisant) ne doit plus etre utilise directement",
        )


class TestRatingStarsOnPhotoCard(unittest.TestCase):
    """Verrouille J1 (audit du 2026-07-22) : la colonne `rating` et
    `Database.set_rating()` etaient entierement implementees et testees cote
    donnees (tests/test_db.py) mais jamais exposees dans l'interface. Chaque
    carte photo doit desormais afficher RATING_MAX (5) etoiles cliquables,
    refletant fidelement la note deja en base et permettant de la modifier -
    avec reinitialisation a 0 en cliquant sur l'etoile deja active (seul
    moyen d'annuler une notation)."""

    def setUp(self):
        # Force une collecte cyclique AVANT de creer un nouveau Tk() -
        # observe en ecrivant ces tests : ce fichier cree/detruit des
        # dizaines de racines Tk reelles a la suite, et Widget.destroy() ne
        # libere pas les commandes Tcl enregistrees via .bind()/`command=`
        # (seule la destruction de l'interpreteur/racine le fait) - les
        # fermetures Python qui en decoulent (souvent auto-referentes via
        # `self`) restent alors comme garbage CYCLIQUE, que seul gc.collect()
        # peut liberer (le comptage de references seul ne suffit pas). Sans
        # ce nettoyage explicite entre les tests, ce garbage s'accumule au
        # fil de l'execution complete de la suite au point de retarder une
        # collecte automatique ulterieure pile pendant l'attente bornee
        # d'un thread d'arriere-plan (calcul de groupes) ci-dessous,
        # jusqu'a lui faire depasser son delai - un artefact de la suite de
        # tests elle-meme (aucune consequence pour l'application reelle, qui
        # ne cree jamais des dizaines de fenetres Tk a la suite dans le
        # meme processus).
        gc.collect()
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

    def _make_image_file(self, name: str) -> Path:
        path = self.tmp_dir / name
        Image.new("RGB", (16, 16), (10, 20, 30)).save(path)
        return path

    def _select_a_group(self, app, id1, id2):
        group = grouping.PhotoGroup(kind="exact", photo_ids=sorted([id1, id2]), max_distance=0)
        app._show_group_detail(group)
        self.root.update()

    def _make_two_photo_group(self, app):
        p1 = self._make_image_file("a.jpg")
        p2 = self._make_image_file("b.jpg")
        id1 = app.db.upsert_photo(str(p1), 1000, 1.0, 800, 600, "sha-same", 0, None)
        id2 = app.db.upsert_photo(str(p2), 1000, 1.0, 800, 600, "sha-same", 0, None)
        self._select_a_group(app, id1, id2)
        return id1, id2

    def test_card_shows_five_star_labels_matching_zero_rating_by_default(self):
        app = self._make_app()
        id1, _ = self._make_two_photo_group(app)

        labels = app._rating_star_labels[id1]
        self.assertEqual(len(labels), gui.RATING_MAX)
        self.assertTrue(all(lbl.cget("text") == "☆" for lbl in labels))

    def test_card_reflects_rating_already_stored_in_db(self):
        app = self._make_app()
        p1 = self._make_image_file("a.jpg")
        p2 = self._make_image_file("b.jpg")
        id1 = app.db.upsert_photo(str(p1), 1000, 1.0, 800, 600, "sha-same", 0, None)
        id2 = app.db.upsert_photo(str(p2), 1000, 1.0, 800, 600, "sha-same", 0, None)
        app.db.set_rating(id1, 3)

        self._select_a_group(app, id1, id2)

        labels = app._rating_star_labels[id1]
        filled = [lbl.cget("text") == "★" for lbl in labels]
        self.assertEqual(filled, [True, True, True, False, False])

    def test_clicking_a_star_sets_the_rating_and_persists_it(self):
        app = self._make_app()
        id1, _ = self._make_two_photo_group(app)

        app._set_photo_rating(id1, 4)

        self.assertEqual(app.db.get_photo(id1)["rating"], 4)
        labels = app._rating_star_labels[id1]
        filled = [lbl.cget("text") == "★" for lbl in labels]
        self.assertEqual(filled, [True, True, True, True, False])

    def test_clicking_the_already_active_star_resets_the_rating_to_zero(self):
        app = self._make_app()
        id1, _ = self._make_two_photo_group(app)
        app._set_photo_rating(id1, 3)
        self.assertEqual(app.db.get_photo(id1)["rating"], 3)

        app._set_photo_rating(id1, 3)  # meme etoile -> toggle a 0

        self.assertEqual(app.db.get_photo(id1)["rating"], 0)
        labels = app._rating_star_labels[id1]
        self.assertTrue(all(lbl.cget("text") == "☆" for lbl in labels))

    def test_rating_of_one_photo_does_not_affect_the_other_photo_in_the_group(self):
        app = self._make_app()
        id1, id2 = self._make_two_photo_group(app)

        app._set_photo_rating(id1, 5)

        self.assertEqual(app.db.get_photo(id1)["rating"], 5)
        self.assertEqual(app.db.get_photo(id2)["rating"], 0)
        self.assertTrue(all(lbl.cget("text") == "☆" for lbl in app._rating_star_labels[id2]))

    def test_each_star_label_has_a_click_handler_registered(self):
        app = self._make_app()
        id1, _ = self._make_two_photo_group(app)

        for star_label in app._rating_star_labels[id1]:
            self.assertTrue(star_label.bind("<Button-1>"), "chaque etoile doit avoir un gestionnaire de clic lie")


class TestRatingStarsKeyboardAccessible(unittest.TestCase):
    """Verrouille M2 (audit du 2026-07-28) : avant ce correctif, la
    notation par etoiles n'etait utilisable qu'a la souris (ttk.Label sans
    `takefocus` ni binding clavier) - inatteignable par Tab, donc
    inutilisable au clavier seul. Meme structure de test que
    TestRatingStarsOnPhotoCard (racine Tk reelle necessaire).

    Verifie la PRESENCE des bindings clavier via `.bind(sequence)` (comme
    le fait deja test_each_star_label_has_a_click_handler_registered pour
    <Button-1>) et le COMPORTEMENT en appelant directement les methodes
    nommees sous-jacentes (_handle_rating_key_press, _focus_star) plutot
    que de simuler des evenements clavier synthetiques via event_generate()
    - delibere : `event_generate` s'est revele peu fiable ici pour livrer
    un vrai focus/evenement clavier une fois plusieurs dizaines de racines
    Tk reelles creees puis detruites a la suite dans le meme processus (le
    lot de tests de ce fichier), un artefact de l'environnement de test deja
    documente ailleurs dans ce fichier (voir le commentaire de setUp de
    TestRatingStarsOnPhotoCard) plutot qu'un probleme du correctif lui-meme
    - appeler directement les methodes nommees reste une verification tout
    aussi fidele du COMPORTEMENT sans dependre de cette livraison."""

    def setUp(self):
        gc.collect()
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

    def _make_image_file(self, name: str) -> Path:
        path = self.tmp_dir / name
        Image.new("RGB", (16, 16), (10, 20, 30)).save(path)
        return path

    def _make_two_photo_group(self, app):
        p1 = self._make_image_file("a.jpg")
        p2 = self._make_image_file("b.jpg")
        id1 = app.db.upsert_photo(str(p1), 1000, 1.0, 800, 600, "sha-same", 0, None)
        id2 = app.db.upsert_photo(str(p2), 1000, 1.0, 800, 600, "sha-same", 0, None)
        group = grouping.PhotoGroup(kind="exact", photo_ids=sorted([id1, id2]), max_distance=0)
        app._show_group_detail(group)
        self.root.update()
        return id1, id2

    def test_star_labels_are_focusable(self):
        app = self._make_app()
        id1, _ = self._make_two_photo_group(app)

        for star_label in app._rating_star_labels[id1]:
            self.assertEqual(
                str(star_label.cget("takefocus")), "1",
                "chaque etoile doit avoir takefocus active pour etre atteignable au Tab",
            )

    def test_each_star_label_has_keyboard_activation_handlers_registered(self):
        # Meme verification que test_each_star_label_has_a_click_handler_registered
        # (deja dans TestRatingStarsOnPhotoCard) mais pour les 3 sequences
        # clavier ajoutees par M2 : Entree/Espace (meme action qu'un clic),
        # <Key> generique (chiffres 1-5, voir _handle_rating_key_press).
        app = self._make_app()
        id1, _ = self._make_two_photo_group(app)

        for star_label in app._rating_star_labels[id1]:
            self.assertTrue(star_label.bind("<Return>"), "Entree doit noter comme un clic")
            self.assertTrue(star_label.bind("<space>"), "Espace doit noter comme un clic")
            self.assertTrue(star_label.bind("<Key>"), "un chiffre doit pouvoir noter directement")

    def test_each_star_label_has_arrow_key_navigation_registered(self):
        app = self._make_app()
        id1, _ = self._make_two_photo_group(app)

        for star_label in app._rating_star_labels[id1]:
            self.assertTrue(star_label.bind("<Left>"), "Gauche doit deplacer le focus dans la rangee")
            self.assertTrue(star_label.bind("<Right>"), "Droite doit deplacer le focus dans la rangee")

    def test_handle_rating_key_press_sets_the_rating_for_a_digit(self):
        # Comportement reel du binding <Key> (voir _handle_rating_key_press) :
        # un chiffre 1-5 definit directement la note, sans avoir besoin de
        # naviguer jusqu'a l'etoile correspondante au prealable.
        app = self._make_app()
        id1, _ = self._make_two_photo_group(app)

        app._handle_rating_key_press(id1, "4")

        self.assertEqual(app.db.get_photo(id1)["rating"], 4)
        labels = app._rating_star_labels[id1]
        filled = [lbl.cget("text") == "★" for lbl in labels]
        self.assertEqual(filled, [True, True, True, True, False])

    def test_handle_rating_key_press_toggles_to_zero_on_the_same_digit(self):
        # Meme regle de toggle qu'un clic sur l'etoile deja active (voir
        # _set_photo_rating, reutilisee par _handle_rating_key_press).
        app = self._make_app()
        id1, _ = self._make_two_photo_group(app)
        app._handle_rating_key_press(id1, "3")
        self.assertEqual(app.db.get_photo(id1)["rating"], 3)

        app._handle_rating_key_press(id1, "3")

        self.assertEqual(app.db.get_photo(id1)["rating"], 0)

    def test_handle_rating_key_press_ignores_non_digit_keys(self):
        app = self._make_app()
        id1, _ = self._make_two_photo_group(app)
        app._handle_rating_key_press(id1, "3")

        for char in ("a", "0", "6", "\r", " "):
            app._handle_rating_key_press(id1, char)

        # Toujours 3 : aucune de ces touches n'est un chiffre 1-5, la note
        # posee au debut du test ne doit pas avoir bouge.
        self.assertEqual(app.db.get_photo(id1)["rating"], 3)

    def test_focus_star_moves_focus_to_the_requested_index(self):
        app = self._make_app()
        id1, _ = self._make_two_photo_group(app)
        labels = app._rating_star_labels[id1]
        for lbl in labels:
            lbl.focus_set = mock.MagicMock()

        app._focus_star(labels, 3)

        labels[3].focus_set.assert_called_once()
        for other_index, lbl in enumerate(labels):
            if other_index != 3:
                lbl.focus_set.assert_not_called()

    def test_focus_star_clamps_below_the_first_star(self):
        # Pas de sortie de la rangee par la gauche (index -1 inexistant) -
        # le focus doit rester sur la premiere etoile.
        app = self._make_app()
        id1, _ = self._make_two_photo_group(app)
        labels = app._rating_star_labels[id1]
        for lbl in labels:
            lbl.focus_set = mock.MagicMock()

        app._focus_star(labels, -1)

        labels[0].focus_set.assert_called_once()

    def test_focus_star_clamps_beyond_the_last_star(self):
        app = self._make_app()
        id1, _ = self._make_two_photo_group(app)
        labels = app._rating_star_labels[id1]
        for lbl in labels:
            lbl.focus_set = mock.MagicMock()

        app._focus_star(labels, 999)

        labels[-1].focus_set.assert_called_once()


class TestDetailThumbnailsArePaginated(unittest.TestCase):
    """Verrouille M1 (audit du 2026-07-28) : avant ce correctif,
    _show_group_detail construisait TOUTES les cartes d'un groupe (decodage
    Pillow + redimensionnement de chaque vignette compris) en une seule
    passe synchrone sur le thread principal, sans aucune limite - un gros
    groupe de quasi-doublons pouvait donc geler l'interface. Verifie que
    l'affichage d'un groupe est desormais borne a
    gui.DETAIL_THUMBNAILS_PAGE_SIZE cartes, avec un bouton "Afficher plus"
    pour charger la suite par lots a la demande."""

    def setUp(self):
        gc.collect()
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

    def _make_large_group(self, app, count: int):
        # Chemins volontairement inexistants sur disque : `path` est une
        # colonne UNIQUE de la base (voir db.py), donc chaque photo a besoin
        # d'un chemin distinct, mais _build_photo_card retombe deja
        # gracieusement sur "[image indisponible]" pour toute photo dont le
        # fichier ne peut pas etre ouvert (son try/except autour de
        # hashing.open_image couvre deja FileNotFoundError) - inutile donc
        # d'ecrire `count` vrais fichiers image juste pour compter des
        # cartes construites.
        photo_ids = [
            app.db.upsert_photo(
                str(self.tmp_dir / f"photo_{i}.jpg"), 1000, 1.0, 800, 600, "sha-same", 0, None,
            )
            for i in range(count)
        ]
        group = grouping.PhotoGroup(kind="exact", photo_ids=sorted(photo_ids), max_distance=0)
        app._show_group_detail(group)
        self.root.update()
        return group

    def _card_count(self, app) -> int:
        return len(app.cards_inner.winfo_children())

    def test_large_group_display_is_capped_to_the_page_size_plus_one_button(self):
        app = self._make_app()
        total = gui.DETAIL_THUMBNAILS_PAGE_SIZE + 15
        self._make_large_group(app, total)

        # +1 : le bouton "Afficher plus" ajoute apres les cartes visibles,
        # puisque le groupe compte plus de photos que la page courante.
        self.assertEqual(self._card_count(app), gui.DETAIL_THUMBNAILS_PAGE_SIZE + 1)

    def test_small_group_shows_every_photo_without_a_more_button(self):
        app = self._make_app()
        total = min(3, gui.DETAIL_THUMBNAILS_PAGE_SIZE)
        self._make_large_group(app, total)

        self.assertEqual(self._card_count(app), total)

    def test_clicking_show_more_reveals_additional_thumbnails(self):
        app = self._make_app()
        total = gui.DETAIL_THUMBNAILS_PAGE_SIZE + 15
        self._make_large_group(app, total)

        app._show_more_detail_thumbnails()
        self.root.update()

        expected_visible = min(2 * gui.DETAIL_THUMBNAILS_PAGE_SIZE, total)
        # Le groupe compte 35 photos avec PAGE_SIZE=20 : le second lot
        # affiche donc les 15 restantes sans bouton "Afficher plus" de plus
        # (35 <= 2 * 20).
        self.assertEqual(self._card_count(app), expected_visible)

    def test_selecting_a_different_group_resets_pagination(self):
        app = self._make_app()
        total = gui.DETAIL_THUMBNAILS_PAGE_SIZE + 15
        group_a = self._make_large_group(app, total)
        app._show_more_detail_thumbnails()  # agrandit la pagination du groupe A
        self.root.update()
        self.assertGreater(app._detail_visible_count, gui.DETAIL_THUMBNAILS_PAGE_SIZE)

        # Un DEUXIEME groupe, distinct de group_a, redemarre a la premiere
        # page plutot que d'heriter de la pagination agrandie precedente.
        p1 = self.tmp_dir / "other1.jpg"
        p2 = self.tmp_dir / "other2.jpg"
        Image.new("RGB", (16, 16), (1, 2, 3)).save(p1)
        Image.new("RGB", (16, 16), (1, 2, 3)).save(p2)
        id1 = app.db.upsert_photo(str(p1), 1000, 1.0, 800, 600, "sha-other", 0, None)
        id2 = app.db.upsert_photo(str(p2), 1000, 1.0, 800, 600, "sha-other", 0, None)
        group_b = grouping.PhotoGroup(kind="exact", photo_ids=sorted([id1, id2]), max_distance=0)

        app._show_group_detail(group_b)
        self.root.update()

        self.assertEqual(app._detail_visible_count, gui.DETAIL_THUMBNAILS_PAGE_SIZE)


if __name__ == "__main__":
    unittest.main()
