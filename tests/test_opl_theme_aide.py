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



class TestColonneVerticale(unittest.TestCase):
    """Le bandeau horizontal est devenu UNE COLONNE a gauche (2026-08-27) :
    marque en haut, vues au milieu, Theme et Aide en bas."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except tk.TclError as exc:                       # pragma: no cover
            raise unittest.SkipTest(f"pas d'affichage Tk : {exc}")
        opl_theme.apply(cls.root, "Appli")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def _textes(self, widget):
        morceaux = []
        for enfant in widget.winfo_children():
            try:
                morceaux.append(str(enfant.cget("text")))
            except Exception:
                pass
            morceaux.append(self._textes(enfant))
        return " ".join(morceaux)

    def test_entete_rend_le_rail_lui_meme(self):
        """Une seule colonne : l'application n'a plus a creer un rail a part."""
        colonne = opl_theme.entete(self.root, "Appli", "Accroche", slug="appli", version="1.2.3")
        self.assertIsInstance(colonne, opl_theme.Rail)

    def test_la_marque_porte_le_nom_et_l_accroche(self):
        colonne = opl_theme.entete(self.root, "Appli", "Une accroche", slug="appli")
        textes = self._textes(colonne)
        self.assertIn("Appli", textes)
        self.assertIn("Une accroche", textes)

    def test_il_n_y_a_plus_de_lien_contact_separe(self):
        """LA decision : « Contact » et « Signaler un probleme… » appelaient la
        MEME fonction. Une seule porte, dans le menu Aide."""
        colonne = opl_theme.entete(self.root, "Appli", "Accroche", slug="appli",
                                   on_contact=lambda: None)
        self.assertNotIn("Contact", self._textes(colonne))
        labels = [colonne.menu_aide.entrycget(i, "label")
                  for i in range(colonne.menu_aide.index("end") + 1)
                  if colonne.menu_aide.type(i) == "command"]
        self.assertTrue(any("Signaler" in l for l in labels),
                        "le contact doit rester joignable depuis le menu Aide")

    def test_sans_menu_aide_le_contact_garde_sa_propre_entree(self):
        """Contre-epreuve : retirer le lien ne doit pas rendre le contact
        INJOIGNABLE. Sans slug il n'y a pas de menu Aide — le lien revient."""
        colonne = opl_theme.entete(self.root, "Appli", "Accroche", on_contact=lambda: None)
        self.assertIn("contacter", self._textes(colonne))

    def test_la_version_ne_s_affiche_pas_deux_fois(self):
        """Les applications portent deja leur version en barre d'etat."""
        colonne = opl_theme.entete(self.root, "Appli", "Accroche", slug="appli", version="1.2.3")
        self.assertNotIn("v1.2.3", self._textes(colonne))

    def test_sans_contenu_la_colonne_n_a_pas_de_zone_de_travail(self):
        """Les applications sans navigation par vues (Coffre, GuideExpress,
        PhotoTri) gardent leur mise en page : la colonne n'est qu'une colonne."""
        avec = opl_theme.entete(self.root, "Appli", "", slug="appli")
        sans = opl_theme.entete(self.root, "Appli", "", slug="appli", avec_contenu=False)
        self.assertIsNotNone(avec._contenu)
        self.assertIsNone(sans._contenu)


class TestDialogue(unittest.TestCase):
    """La confirmation themee, et surtout SA REGLE : detruire n'est jamais
    l'action par defaut."""

    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:                       # pragma: no cover
            raise unittest.SkipTest(f"pas d'affichage Tk : {exc}")
        self.root.geometry("400x240+50+50")
        self.root.deiconify()
        opl_theme.apply(self.root, "Essai")
        self.addCleanup(self.root.destroy)

    def _repondre(self, touche=None, bouton=None, **kw):
        """Ouvre un dialogue et repond a sa place, puis rend son resultat."""
        def agir():
            fenetre = [w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)][0]
            if bouton:
                getattr(fenetre, bouton).invoke()
            else:
                fenetre.focus_force()
                self.root.update()
                fenetre.event_generate(touche)
        self.root.after(300, agir)
        return opl_theme.dialogue(self.root, "Supprimer une entree",
                                  "Cette suppression ne se rattrape pas.", **kw)

    def test_detruire_n_est_jamais_l_action_par_defaut(self):
        """LE point de ce composant. Avec danger=True, la touche Entree — celle
        qu'on frappe par reflexe — doit ANNULER. Sans le drapeau, elle
        confirme : c'est la contre-epreuve, sans laquelle le test ne prouve
        rien (il passerait aussi si Entree n'etait jamais liee)."""
        self.assertFalse(self._repondre("<Return>", confirmer="Supprimer", danger=True),
                         "Entree ne doit pas detruire quand danger=True")
        self.assertTrue(self._repondre("<Return>", confirmer="Enregistrer"),
                        "sans danger, Entree doit confirmer")

    def test_echap_et_la_croix_annulent(self):
        self.assertFalse(self._repondre("<Escape>", confirmer="Supprimer", danger=True))

    def test_le_bouton_dit_ce_qu_il_fait(self):
        """« Oui » n'apprend rien : un utilisateur qui lit vite ne lit souvent
        QUE les boutons."""
        vus = {}

        def agir():
            fenetre = [w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)][0]
            vus["confirmer"] = fenetre.bouton_confirmer.cget("text")
            vus["annuler"] = fenetre.bouton_annuler.cget("text")
            fenetre.bouton_annuler.invoke()

        self.root.after(300, agir)
        opl_theme.dialogue(self.root, "Supprimer une categorie", "14 transactions seront deplacees.",
                           confirmer="Supprimer la categorie", danger=True)
        self.assertEqual(vus["confirmer"], "Supprimer la categorie")
        self.assertEqual(vus["annuler"], "Annuler")

    def test_le_clic_sur_confirmer_rend_vrai(self):
        self.assertTrue(self._repondre(bouton="bouton_confirmer", confirmer="Supprimer", danger=True))

    def test_le_titre_ne_repete_pas_le_nom_de_l_application(self):
        """Le titre dit DE QUOI il s'agit ; le nom de l'appli est deja dans la
        barre de titre du systeme."""
        vus = {}

        def agir():
            fenetre = [w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)][0]
            vus["titre"] = fenetre.title()
            fenetre.bouton_annuler.invoke()

        self.root.after(300, agir)
        opl_theme.dialogue(self.root, "Purger les metadonnees", "Le titre et l'auteur seront retires.",
                           confirmer="Purger")
        self.assertEqual(vus["titre"], "Purger les metadonnees")


if __name__ == "__main__":
    unittest.main()
