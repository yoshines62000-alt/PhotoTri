"""Le menu Aide de l'en-tete partage : ses destinations, et ce qu'il fait au clic.

Un menu qui promet « Glossaire » doit ouvrir LE glossaire — l'URL est mesuree,
pas supposee — et « Signaler un probleme » doit appeler le contact fourni par
l'application, pas ouvrir un navigateur.
"""
import sys
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import opl_theme


class TestUrlAide(unittest.TestCase):
    def test_les_trois_destinations_sont_sur_le_site(self):
        self.assertEqual(opl_theme.url_aide("fiche", "coffre"), "https://openprojectslab.com/logiciels/coffre/")
        self.assertEqual(opl_theme.url_aide("glossaire"), "https://openprojectslab.com/glossaire/")
        self.assertEqual(opl_theme.url_aide("communaute"), "https://openprojectslab.com/communaute/")

    def test_les_chemins_sont_ceux_de_la_table_de_routes_du_site(self):
        # Recopies de site/src/i18n/index.ts — ce test est le rappel que les
        # deux doivent bouger ensemble.
        self.assertEqual(opl_theme.AIDE, {"fiche": "/logiciels/{slug}/", "glossaire": "/glossaire/", "communaute": "/communaute/"})


class TestMenuAide(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except tk.TclError as exc:  # pragma: no cover
            raise unittest.SkipTest(f"pas d'affichage Tk : {exc}")
        opl_theme.apply(cls.root, "Appli")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def _labels(self, menu):
        return [menu.entrycget(i, "label") for i in range(menu.index("end") + 1) if menu.type(i) == "command"]

    def test_sans_slug_il_n_y_a_pas_de_menu(self):
        cadre = opl_theme.entete(self.root, "Appli", "Accroche")
        self.assertIsNone(cadre.menu_aide)

    def test_avec_slug_le_menu_mene_a_la_fiche_au_glossaire_et_a_la_communaute(self):
        appels = []
        cadre = opl_theme.entete(self.root, "Appli", "Accroche", slug="appli", version="1.2.3",
                                 on_contact=lambda: appels.append("contact"))
        menu = cadre.menu_aide
        self.assertIsNotNone(menu)
        labels = self._labels(menu)
        self.assertEqual(len(labels), 5, labels)
        with patch("webbrowser.open") as ouvrir:
            menu.invoke(0)
            menu.invoke(1)
            menu.invoke(2)
        self.assertEqual([c.args[0] for c in ouvrir.call_args_list], [
            "https://openprojectslab.com/logiciels/appli/",
            "https://openprojectslab.com/glossaire/",
            "https://openprojectslab.com/communaute/",
        ])
        self.assertEqual(appels, [], "ouvrir une page ne doit pas ouvrir le contact")

    def test_signaler_un_probleme_appelle_le_contact_sans_navigateur(self):
        appels = []
        cadre = opl_theme.entete(self.root, "Appli", slug="appli", version="1.2.3",
                                 on_contact=lambda: appels.append("contact"))
        menu = cadre.menu_aide
        index = next(i for i in range(menu.index("end") + 1)
                     if menu.type(i) == "command" and "Signaler" in menu.entrycget(i, "label"))
        with patch("webbrowser.open") as ouvrir:
            menu.invoke(index)
        self.assertEqual(appels, ["contact"])
        ouvrir.assert_not_called()

    def test_sans_contact_l_entree_signaler_n_existe_pas(self):
        cadre = opl_theme.entete(self.root, "Appli", slug="appli", version="1.2.3")
        self.assertFalse(any("Signaler" in l for l in self._labels(cadre.menu_aide)))

    def test_a_propos_dit_la_version_et_la_licence(self):
        cadre = opl_theme.entete(self.root, "Appli", slug="appli", version="1.2.3")
        menu = cadre.menu_aide
        dernier = menu.index("end")
        self.assertEqual(menu.entrycget(dernier, "state"), "disabled", "une ligne d'information, pas une action")
        self.assertIn("v1.2.3", menu.entrycget(dernier, "label"))
        self.assertIn("MIT", menu.entrycget(dernier, "label"))


if __name__ == "__main__":
    unittest.main()
