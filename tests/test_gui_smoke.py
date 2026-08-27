"""Test de fumee de bout en bout pour gui.py : pilote la VRAIE interface
Tkinter (vrai `Tk()`, vrai thread de scan, vraie base SQLite temporaire,
vraies images Pillow sur disque) sur le parcours principal de PhotoTri -
choisir un dossier, analyser, regrouper, deplacer non destructivement,
rescanner.

Correctif I1 (audit du 2026-07-22) : ce depot ne contenait jusqu'ici AUCUN
test de fumee GUI versionne - seuls des scripts equivalents residaient dans
un repertoire de travail externe (jamais executes automatiquement, jamais
decouverts par un contributeur clonant le depot). C'est precisement cet
angle mort qui avait laisse passer F1 (boutons Analyser/Arreter pousses
hors champ par un chemin de dossier long) lors d'audits precedents : les
tests unitaires existants (test_hashing/test_db/test_scanner/test_grouping)
ne pilotent jamais la vraie geometrie Tkinter. Ce fichier rapatrie ce
scenario dans le depot, sur le modele du smoke test deja utilise pendant
l'audit (vrai Tk(), mocks uniquement sur tkinter.messagebox/filedialog -
jamais sur les widgets ou la logique metier), en y ajoutant une capture
d'ecran reelle (PIL.ImageGrab) de l'etat final comme verification visuelle
de non-regression, en complement des assertions programmatiques."""

import random
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from tkinter import TclError, Tk
from unittest import mock

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui
import hashing


def _checker_image(seed, extra_block=None):
    """Image texturee en damier (des blocs de couleurs variees, PAS un
    gradient lisse) : hash equilibre (popcount ~24-36 sur 64), evite de
    declencher la verification d'appoint A1 (hashing.is_low_entropy_hash)
    pour ce scenario qui porte sur le parcours GUI, pas sur A1 lui-meme
    (deja verrouille dans tests/test_grouping.py::TestLowEntropyFalsePositives).
    Un gradient lisse, teste initialement ici, produit au contraire un
    dHash a popcount EXTREME (tous les pixels adjacents varient dans le
    meme sens) - exactement le cas que A1 vise a proteger, mauvais choix
    pour un scenario "parcours GUI nominal". `seed` DOIT differer entre deux
    images qui ne sont pas censees etre relation - un damier genere avec le
    meme seed est litteralement le meme fichier une fois encode en JPEG
    (bug trouve en ecrivant ce test : un seed fixe partage faisait tomber
    "rafale_1" dans le groupe EXACT de "photo_a" au lieu du groupe "near"
    attendu)."""
    img = Image.new("RGB", (200, 200), "white")
    d = ImageDraw.Draw(img)
    rng = random.Random(seed)
    for by in range(0, 200, 20):
        for bx in range(0, 200, 20):
            c = rng.randint(0, 255)
            d.rectangle([bx, by, bx + 20, by + 20], fill=(c, (c * 3) % 256, 255 - c))
    if extra_block is not None:
        d.rectangle(extra_block, fill=(255, 0, 0))
    return img


def _independent_image():
    img = Image.new("RGB", (200, 200), "black")
    d = ImageDraw.Draw(img)
    for y in range(0, 200, 10):
        for x in range(0, 200, 10):
            if (x + y) % 20 == 0:
                d.rectangle([x, y, x + 8, y + 8], fill="white")
    return img


class GuiSmokeTestCase(unittest.TestCase):
    def setUp(self):
        try:
            self.root = Tk()
        except TclError as exc:
            self.skipTest(f"Pas d'affichage disponible pour piloter la vraie GUI Tkinter : {exc}")
            return

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.photos_dir = self.tmp / "photos"
        self.photos_dir.mkdir()
        self.data_dir = self.tmp / "appdata"

        # messagebox.showerror/showwarning/showinfo/askyesno ouvrent de
        # vraies boites MODALES Windows qui bloqueraient ce test
        # indefiniment (personne pour cliquer "OK") si un chemin d'erreur
        # inattendu se declenchait - neutralisees ici, jamais les widgets
        # ni la logique metier eux-memes (voir docstring de module).
        self._patchers = [
            mock.patch("tkinter.messagebox.showerror"),
            mock.patch("tkinter.messagebox.showwarning"),
            mock.patch("tkinter.messagebox.showinfo"),
            mock.patch("opl_theme.dialogue", return_value=True),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)

        with mock.patch.object(gui, "_data_dir", return_value=self.data_dir), \
                mock.patch.object(gui.update_checker, "start_update_check"):
            self.app = gui.PhotoTriApp(self.root)
        self.addCleanup(self._teardown_app)

    def _teardown_app(self):
        try:
            self.app.db.close()
        except Exception:
            pass
        try:
            self.root.destroy()
        except TclError:
            pass

    def _wait_idle(self, timeout=15):
        """Le scan, le regroupement ET le deplacement (voir
        _move_checked_photos, threade depuis le correctif de l'audit du
        2026-07-28 - meme pattern que _scan_worker) tournent chacun sur leur
        propre thread - attendre les trois drapeaux plutot qu'un seul evite
        une course ou le test lirait un etat intermediaire (ex. fichier pas
        encore effectivement deplace sur le disque au moment des
        assertions)."""
        deadline = time.monotonic() + timeout
        while self.app._scanning or self.app._grouping_in_progress or self.app._moving:
            self.root.update()
            time.sleep(0.02)
            self.assertLess(time.monotonic(), deadline, "l'app ne redevient jamais idle (timeout)")
        self.root.update()

    def test_scan_group_move_and_rescan_end_to_end(self):
        # -- jeu de photos : 1 doublon exact, 1 groupe de quasi-doublons ----
        # (rafale), 1 photo independante qui ne doit rejoindre aucun groupe.
        base = _checker_image(seed=1)
        base.save(self.photos_dir / "photo_a.jpg", quality=95)
        shutil.copy(self.photos_dir / "photo_a.jpg", self.photos_dir / "photo_a_copie.jpg")
        self.assertEqual(
            hashing.file_sha256(self.photos_dir / "photo_a.jpg"),
            hashing.file_sha256(self.photos_dir / "photo_a_copie.jpg"),
        )

        rafale_1 = _checker_image(seed=2)
        rafale_1.save(self.photos_dir / "rafale_1.jpg", quality=95)
        rafale_2 = _checker_image(seed=2, extra_block=[80, 80, 120, 120])
        rafale_2.save(self.photos_dir / "rafale_2.jpg", quality=95)
        d12 = hashing.hamming_distance(
            hashing.compute_dhash_from_path(self.photos_dir / "rafale_1.jpg"),
            hashing.compute_dhash_from_path(self.photos_dir / "rafale_2.jpg"),
        )
        self.assertTrue(0 < d12 <= 6, f"rafale_1/rafale_2 doivent etre proches mais pas identiques (distance={d12})")

        independent = _independent_image()
        independent.save(self.photos_dir / "photo_independante.jpg", quality=95)

        # -- selection du dossier + lancement d'un vrai scan threade --------
        with mock.patch.object(gui.filedialog, "askdirectory", return_value=str(self.photos_dir)):
            self.app._choose_folder()
        self.root.update()
        self.assertEqual(str(self.app.scan_button["state"]), "normal")

        self.app._start_scan()
        # Pendant le scan, les controles sensibles doivent rester
        # desactives (non-regression : bug d'acces SQLite concurrent
        # corrige lors d'un audit precedent, voir le docstring de gui.py).
        self.assertEqual(str(self.app.move_button["state"]), "disabled")
        self.assertEqual(str(self.app.choose_folder_button["state"]), "disabled")
        self._wait_idle()
        self.assertEqual(str(self.app.move_button["state"]), "normal")

        self.assertEqual(self.app.db.count_photos(), 5)

        # -- groupes attendus : 1 exact (2 photos), 1 near (2 photos), -----
        # la photo independante hors de tout groupe.
        kinds = [g.kind for g in self.app._groups]
        self.assertEqual(kinds.count("exact"), 1)
        self.assertEqual(kinds.count("near"), 1)
        exact_group = next(g for g in self.app._groups if g.kind == "exact")
        near_group = next(g for g in self.app._groups if g.kind == "near")
        self.assertEqual(len(exact_group.photo_ids), 2)
        self.assertEqual(len(near_group.photo_ids), 2)
        independent_row = self.app.db.get_photo_by_path(str(self.photos_dir / "photo_independante.jpg"))
        all_grouped = set(exact_group.photo_ids) | set(near_group.photo_ids)
        self.assertNotIn(independent_row["id"], all_grouped)

        # -- indicateur de confiance (A2) present sur les deux groupes ------
        # (format "XX% - Libelle", ex. "97% - Tres proches").
        for iid, group in self.app._groups_by_iid.items():
            values = self.app.groups_tree.item(iid, "values")
            confidence_text = values[3]
            self.assertIn("%", confidence_text)
            self.assertIn(" - ", confidence_text)
        exact_iid = next(iid for iid, g in self.app._groups_by_iid.items() if g.kind == "exact")
        self.assertEqual(self.app.groups_tree.item(exact_iid, "values")[3], "100% - Identique")

        # -- selection du groupe near dans le vrai Treeview -> vraies -------
        # vignettes generees.
        near_iid = next(iid for iid, g in self.app._groups_by_iid.items() if g.kind == "near")
        self.app.groups_tree.selection_set(near_iid)
        self.app._on_group_select()
        self.root.update()
        self.assertEqual(len(self.app.cards_inner.winfo_children()), 2)
        self.assertEqual(len(self.app._thumbnail_refs), 2)
        checked = [pid for pid, var in self.app._checkbox_vars.items() if var.get()]
        self.assertEqual(len(checked), 1, "1 seule photo (pas la suggestion de conservation) doit etre cochee")

        # -- deplacement reel (non destructif) -------------------------------
        self.app.review_folder_var.set(str(self.tmp / "revision"))
        moved_before = self.app.db.conn.execute("SELECT COUNT(*) AS n FROM photos WHERE status='moved'").fetchone()["n"]
        self.app._move_checked_photos()
        self._wait_idle()
        moved_after = self.app.db.conn.execute("SELECT COUNT(*) AS n FROM photos WHERE status='moved'").fetchone()["n"]
        self.assertEqual(moved_after - moved_before, 1)
        review_dir = Path(self.app.review_folder_var.get())
        self.assertEqual(len(list(review_dir.iterdir())), 1)

        # -- rescan : la photo deplacee ne doit pas revenir dans l'index -----
        self.app._start_scan()
        self._wait_idle()
        self.assertEqual(self.app.db.count_photos(), 4)

        # -- capture d'ecran reelle de l'etat final (verification visuelle, --
        # PIL.ImageGrab) - jamais ecrite dans le depot, seulement dans le
        # dossier temporaire du test. Best-effort : un environnement sans
        # affichage capturable ne doit pas faire echouer le test, seulement
        # sauter cette verification supplementaire.
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.focus_force()
        self.root.geometry("+50+50")
        self.root.update()
        time.sleep(0.3)
        self.root.update()
        try:
            from PIL import ImageGrab
            x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
            w, h = self.root.winfo_width(), self.root.winfo_height()
            shot_path = self.tmp / "smoke_final_state.png"
            ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(shot_path)
            self.assertTrue(shot_path.exists())
            self.assertGreater(shot_path.stat().st_size, 0)
        except Exception as exc:
            print(f"(capture d'ecran non disponible dans cet environnement : {exc})")

    def test_uncaught_callback_exception_is_logged_and_shown_not_silently_dropped(self):
        # Non-regression (voir docstring de gui.py, _handle_callback_exception) :
        # sans root.report_callback_exception, Tkinter avale silencieusement
        # toute exception levee dans un callback - invisible dans l'exe
        # empaquete sans console.
        from tkinter import ttk
        btn = ttk.Button(self.root, text="boom", command=lambda: (_ for _ in ()).throw(RuntimeError("erreur de test simulee")))
        try:
            btn.invoke()
            self.root.update()
            log_path = self.data_dir / "erreurs.log"
            self.assertTrue(log_path.exists())
            self.assertIn("erreur de test simulee", log_path.read_text(encoding="utf-8"))
            self.assertTrue(self.root.winfo_exists())
        finally:
            btn.destroy()


if __name__ == "__main__":
    unittest.main()
