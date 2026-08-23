"""La fenetre de contact : on montre EXACTEMENT ce qui part, avant que ca parte.

Le test qui compte n'est pas « le formulaire s'affiche » : c'est que le corps
MONTRE dans l'apercu est, a l'octet pres, le corps ENVOYE — et que l'apercu dit
aussi ce qui ne part pas (nom de machine, nom d'utilisateur), en le mesurant
sur les vraies valeurs de cette machine plutot qu'en le supposant.
"""
import getpass
import io
import json
import platform
import sys
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import opl_contact


def _payload(**kw):
    base = dict(app="Appli", version="1.2.3", type_="Signaler un bug", email=" moi@exemple.fr ", message="  ça plante  ")
    base.update(kw)
    return opl_contact._payload(**base)


class TestCorpsEnvoye(unittest.TestCase):
    def test_cinq_champs_pas_un_de_plus(self):
        corps = opl_contact.corps_envoye(_payload())
        self.assertEqual(set(corps), {"message", "email", "produit", "version", "os"})
        # L'horodatage et le type internes NE PARTENT PAS sous leur nom.
        self.assertNotIn("horodatage", json.dumps(corps))

    def test_le_type_est_prefixe_au_message_et_les_champs_sont_nettoyes(self):
        corps = opl_contact.corps_envoye(_payload())
        self.assertEqual(corps["message"], "[Signaler un bug] ça plante")
        self.assertEqual(corps["email"], "moi@exemple.fr")
        self.assertEqual(corps["produit"], "Appli")
        self.assertEqual(corps["version"], "1.2.3")
        self.assertEqual(corps["os"], f"{platform.system()} {platform.release()}")

    def test_sans_type_le_message_part_nu(self):
        corps = opl_contact.corps_envoye(_payload(type_=""))
        self.assertEqual(corps["message"], "ça plante")


class TestVerifier(unittest.TestCase):
    """Les memes refus que le service contact-intake, AVANT d'envoyer."""

    def test_message_vide(self):
        self.assertIn("écrire un message", opl_contact.verifier(opl_contact.corps_envoye(_payload(message="   "))))

    def test_message_trop_long(self):
        corps = opl_contact.corps_envoye(_payload(message="x" * 6000))
        self.assertIn("trop long", opl_contact.verifier(corps))

    def test_email_invalide_mais_email_vide_accepte(self):
        self.assertIn("e-mail", opl_contact.verifier(opl_contact.corps_envoye(_payload(email="pas-un-mail"))))
        self.assertIsNone(opl_contact.verifier(opl_contact.corps_envoye(_payload(email=""))))

    def test_les_limites_sont_celles_du_service(self):
        # contact-intake/config.js : maxLen — recopie a la main, ce test est le rappel.
        self.assertEqual(opl_contact.LIMITES, {"message": 5000, "email": 200, "produit": 80, "version": 40, "os": 120})

    def test_corps_valide(self):
        self.assertIsNone(opl_contact.verifier(opl_contact.corps_envoye(_payload())))


class TestApercu(unittest.TestCase):
    def setUp(self):
        self.corps = opl_contact.corps_envoye(_payload())
        self.texte = opl_contact.apercu(self.corps, "https://contact.exemple/api", Path("C:/x/outbox.jsonl"))

    def test_montre_la_destination_et_chaque_valeur(self):
        self.assertIn("https://contact.exemple/api", self.texte)
        for valeur in self.corps.values():
            self.assertIn(valeur, self.texte, valeur)

    def test_dit_ce_qui_ne_part_pas_et_le_mesure(self):
        """Le nom de CETTE machine et de CET utilisateur ne sont nulle part dans
        l'apercu — on le mesure, on ne se contente pas de l'affirmer."""
        for interdit in ("identifiant de machine", "nom", "fichier", "adresse IP", "empreinte tronquée"):
            self.assertIn(interdit, self.texte)
        hote = platform.node()
        if hote:
            self.assertNotIn(hote, json.dumps(self.corps), "le nom de machine a fuite dans le corps")
        try:
            utilisateur = getpass.getuser()
        except Exception:  # pragma: no cover
            utilisateur = ""
        if utilisateur and utilisateur.lower() not in self.corps["message"].lower():
            self.assertNotIn(utilisateur, json.dumps(self.corps), "le nom d'utilisateur a fuite dans le corps")

    def test_nomme_la_boite_d_envoi_locale_et_l_absence_de_renvoi(self):
        self.assertIn("outbox.jsonl", self.texte)
        self.assertIn("jamais renvoyé", self.texte)

    def test_sans_url_l_apercu_dit_que_l_envoi_est_desactive(self):
        self.assertIn("désactivé", opl_contact.apercu(self.corps, None))


class TestApercuEgalEnvoi(unittest.TestCase):
    """LE COEUR : le dict montre est le dict envoye, a l'octet pres."""

    def test_le_corps_poste_est_celui_de_l_apercu(self):
        corps = opl_contact.corps_envoye(_payload())
        capture = {}

        def urlopen(request, timeout=None):
            capture["url"] = request.full_url
            capture["corps"] = json.loads(request.data.decode("utf-8"))
            capture["content_type"] = request.get_header("Content-type")
            r = MagicMock()
            r.read.return_value = b'{"ok": true}'
            r.__enter__.return_value = r
            return r

        with patch("urllib.request.urlopen", side_effect=urlopen), \
             patch.object(opl_contact, "FILONAUT_CONTACT_URL", "https://contact.exemple/api"):
            opl_contact._poster_corps(corps)
        self.assertEqual(capture["corps"], corps)
        self.assertEqual(capture["url"], "https://contact.exemple/api")
        self.assertEqual(capture["content_type"], "application/json")
        # Et l'apercu est construit sur ce meme dict : ce qui est montre est
        # ce qui est parti.
        self.assertIn(json.dumps(corps, ensure_ascii=False, indent=2), opl_contact.apercu(corps, "x"))

    def test_un_refus_du_serveur_leve(self):
        r = MagicMock()
        r.read.return_value = b'{"ok": false, "error": "message trop long"}'
        r.__enter__.return_value = r
        with patch("urllib.request.urlopen", return_value=r), \
             patch.object(opl_contact, "FILONAUT_CONTACT_URL", "https://contact.exemple/api"):
            with self.assertRaises(RuntimeError) as cm:
                opl_contact._poster_corps({"message": "x"})
        self.assertIn("trop long", str(cm.exception))


class TestFenetre(unittest.TestCase):
    """La fenetre reelle : « Envoyer » MONTRE, seul « Envoyer tel quel » ENVOIE."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except tk.TclError as exc:  # pragma: no cover
            raise unittest.SkipTest(f"pas d'affichage Tk : {exc}")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def _ouvrir(self):
        win = opl_contact.ouvrir(self.root, app="Appli", version="1.2.3")
        self.addCleanup(lambda: win.winfo_exists() and win.destroy())
        return win

    def test_envoyer_montre_l_apercu_sans_rien_envoyer(self):
        win = self._ouvrir()
        win.message.insert("1.0", "le bouton bleu ne répond plus")
        with patch("urllib.request.urlopen") as urlopen:
            win.bouton_envoyer.invoke()
            self.root.update()
        urlopen.assert_not_called()
        texte = win.apercu.get("1.0", "end")
        self.assertIn("le bouton bleu ne répond plus", texte)
        self.assertIn(opl_contact.FILONAUT_CONTACT_URL, texte)
        self.assertIn("Rien d'autre n'est envoyé", texte)

    def test_un_message_vide_reste_sur_le_formulaire(self):
        win = self._ouvrir()
        with patch("urllib.request.urlopen") as urlopen:
            win.bouton_envoyer.invoke()
            self.root.update()
        urlopen.assert_not_called()
        self.assertEqual(win.apercu.get("1.0", "end").strip(), "", "aucun apercu ne doit etre rempli")

    def test_envoyer_tel_quel_envoie_exactement_l_apercu(self):
        win = self._ouvrir()
        win.message.insert("1.0", "le bouton bleu ne répond plus")
        capture = {}

        def urlopen(request, timeout=None):
            capture["corps"] = json.loads(request.data.decode("utf-8"))
            r = MagicMock()
            r.read.return_value = b'{"ok": true}'
            r.__enter__.return_value = r
            return r

        with patch("urllib.request.urlopen", side_effect=urlopen):
            win.bouton_envoyer.invoke()
            self.root.update()
            montre = win.apercu.get("1.0", "end")
            win.bouton_confirmer.invoke()
            self.root.update()
        self.assertEqual(capture["corps"]["message"], "[Signaler un bug] le bouton bleu ne répond plus")
        self.assertIn(json.dumps(capture["corps"], ensure_ascii=False, indent=2), montre,
                      "ce qui est parti n'est pas ce qui a ete montre")

    def test_modifier_revient_au_formulaire_sans_envoyer(self):
        win = self._ouvrir()
        win.message.insert("1.0", "une idée")
        with patch("urllib.request.urlopen") as urlopen:
            win.bouton_envoyer.invoke()
            self.root.update()
            win.bouton_modifier.invoke()
            self.root.update()
        urlopen.assert_not_called()
        self.assertTrue(win.message.winfo_ismapped() or win.message.winfo_manager(), "le formulaire doit etre revenu")


if __name__ == "__main__":
    unittest.main()
