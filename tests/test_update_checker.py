import hashlib
import json
import queue
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import update_checker


def _reponse(corps: bytes):
    """Un faux `urlopen` : l'objet renvoye se lit et s'utilise en `with`."""
    response = MagicMock()
    response.read.return_value = corps
    response.__enter__.return_value = response
    return response


def _reponse_en_flux(corps: bytes):
    """Comme _reponse, mais `read(n)` sert le corps MORCEAU PAR MORCEAU — c'est
    ainsi que le telechargement le lit, et un faux qui rendrait tout d'un coup
    ne prouverait pas que l'empreinte se calcule au fil de l'eau."""
    response = MagicMock()
    position = {"i": 0}

    def read(n=-1):
        debut = position["i"]
        fin = len(corps) if n is None or n < 0 else min(len(corps), debut + n)
        position["i"] = fin
        return corps[debut:fin]

    response.read.side_effect = read
    response.__enter__.return_value = response
    return response


def _flux(etiquette="v1.2.3", schema=1, slug="repo", **supplement):
    """Un flux Open Projects Lab minimal mais CONFORME au contrat (ADR 009)."""
    entree = {
        "nom": "Repo", "depot": f"owner/{slug}", "version": etiquette.lstrip("v"),
        "etiquette": etiquette, "publie_le": "2026-08-10T13:27:43Z",
        "page": f"https://github.com/owner/{slug}/releases/tag/{etiquette}", "notes": "",
        "binaire": {"nom": "Repo.exe", "taille_octets": 12345, "sha256": "a" * 64,
                    "url": f"https://github.com/owner/{slug}/releases/download/{etiquette}/Repo.exe"},
    }
    entree.update(supplement)
    return json.dumps({"schema": schema, "genere_le": "2026-08-23T06:00:00Z",
                       "applications": {slug: entree}}).encode("utf-8")


def _selon_url(flux=None, github=None):
    """Un `urlopen` qui repond selon l'URL demandee, et qui NOTE ce qu'on lui a
    demande : c'est ainsi qu'on prouve que GitHub n'est PAS appele quand le
    flux a repondu. Une valeur `Exception` est LEVEE, un `bytes` est servi."""
    appels = []

    def urlopen(request, timeout=None):
        url = request.full_url
        appels.append(url)
        cible = flux if url == update_checker.FLUX_URL else github
        if isinstance(cible, BaseException):
            raise cible
        if cible is None:
            raise urllib.error.URLError("aucune reponse prevue pour " + url)
        return _reponse(cible)

    return urlopen, appels


class TestParseVersionAndIsNewer(unittest.TestCase):
    def test_parses_dotted_version(self):
        self.assertEqual(update_checker._parse_version("v1.2.10"), (1, 2, 10))
        self.assertEqual(update_checker._parse_version("2.0.0"), (2, 0, 0))

    def test_numeric_comparison_not_lexicographic(self):
        # Une comparaison de chaines mettrait "v1.10.0" AVANT "v1.9.0".
        self.assertTrue(update_checker.is_newer("v1.10.0", "v1.9.0"))
        self.assertFalse(update_checker.is_newer("v1.9.0", "v1.10.0"))

    def test_equal_version_is_not_newer(self):
        self.assertFalse(update_checker.is_newer("v1.0.0", "v1.0.0"))

    def test_older_version_is_not_newer(self):
        self.assertFalse(update_checker.is_newer("v1.0.0", "v1.2.0"))


class TestSlug(unittest.TestCase):
    def test_le_slug_est_le_nom_du_depot_en_minuscules(self):
        # La convention du catalogue d'Open Projects Lab, pour les sept apps.
        self.assertEqual(update_checker.slug_de("yoshines62000-alt/Coffre"), "coffre")
        self.assertEqual(update_checker.slug_de("yoshines62000-alt/DownloadOrganizer"), "downloadorganizer")
        self.assertEqual(update_checker.slug_de("PhotoTri"), "phototri")


class TestFluxDAbordGitHubEnRepli(unittest.TestCase):
    """Le coeur de la bascule : le flux repond -> GitHub n'est PAS appele ;
    le flux defaille, de n'importe quelle facon -> GitHub comme avant."""

    def setUp(self):
        update_checker._DERNIERE_ENTREE.clear()

    def test_le_flux_suffit_et_github_n_est_pas_appele(self):
        urlopen, appels = _selon_url(flux=_flux("v1.2.3"), github=b'{"tag_name": "v9.9.9"}')
        with patch("urllib.request.urlopen", side_effect=urlopen):
            tag = update_checker.fetch_latest_release_tag("owner/repo")
        self.assertEqual(tag, "v1.2.3")
        self.assertEqual(appels, [update_checker.FLUX_URL], "GitHub a ete appele alors que le flux avait repondu")

    def test_flux_injoignable_alors_github(self):
        urlopen, appels = _selon_url(flux=urllib.error.URLError("serveur injoignable"), github=b'{"tag_name": "v1.2.3"}')
        with patch("urllib.request.urlopen", side_effect=urlopen):
            tag = update_checker.fetch_latest_release_tag("owner/repo")
        self.assertEqual(tag, "v1.2.3")
        self.assertEqual(len(appels), 2)

    def test_flux_en_delai_depasse_alors_github(self):
        urlopen, _ = _selon_url(flux=TimeoutError("trop long"), github=b'{"tag_name": "v1.2.3"}')
        with patch("urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(update_checker.fetch_latest_release_tag("owner/repo"), "v1.2.3")

    def test_flux_illisible_alors_github(self):
        urlopen, _ = _selon_url(flux=b"<html>503 Service Unavailable</html>", github=b'{"tag_name": "v1.2.3"}')
        with patch("urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(update_checker.fetch_latest_release_tag("owner/repo"), "v1.2.3")

    def test_flux_d_un_autre_schema_alors_github(self):
        # Un schema qu'on ne sait pas lire n'est pas "a peu pres" lisible.
        urlopen, _ = _selon_url(flux=_flux("v5.0.0", schema=2), github=b'{"tag_name": "v1.2.3"}')
        with patch("urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(update_checker.fetch_latest_release_tag("owner/repo"), "v1.2.3")

    def test_flux_qui_ne_connait_pas_cette_application_alors_github(self):
        urlopen, _ = _selon_url(flux=_flux("v5.0.0", slug="autre"), github=b'{"tag_name": "v1.2.3"}')
        with patch("urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(update_checker.fetch_latest_release_tag("owner/repo"), "v1.2.3")

    def test_flux_sans_etiquette_alors_github(self):
        urlopen, _ = _selon_url(flux=_flux(etiquette=""), github=b'{"tag_name": "v1.2.3"}')
        with patch("urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(update_checker.fetch_latest_release_tag("owner/repo"), "v1.2.3")

    def test_les_deux_chemins_en_echec_rendent_none(self):
        urlopen, appels = _selon_url(flux=urllib.error.URLError("x"), github=urllib.error.URLError("y"))
        with patch("urllib.request.urlopen", side_effect=urlopen):
            self.assertIsNone(update_checker.fetch_latest_release_tag("owner/repo"))
        self.assertEqual(len(appels), 2, "les deux chemins doivent avoir ete tentes")

    def test_l_entree_du_flux_porte_l_empreinte_du_binaire(self):
        urlopen, _ = _selon_url(flux=_flux("v1.2.3"))
        with patch("urllib.request.urlopen", side_effect=urlopen):
            entree = update_checker.fetch_from_flux("owner/repo")
        self.assertEqual(entree["binaire"]["sha256"], "a" * 64)
        self.assertEqual(entree["binaire"]["taille_octets"], 12345)

    def test_l_entree_est_memorisee_pour_le_clic(self):
        # Via le flux : l'entree est gardee. Via GitHub : None est garde — le
        # clic saura qu'il n'a aucune empreinte a verifier.
        urlopen, _ = _selon_url(flux=_flux("v1.2.3"))
        with patch("urllib.request.urlopen", side_effect=urlopen):
            update_checker.fetch_latest_release_tag("owner/repo")
        self.assertEqual(update_checker._DERNIERE_ENTREE["owner/repo"]["etiquette"], "v1.2.3")
        urlopen, _ = _selon_url(flux=urllib.error.URLError("x"), github=b'{"tag_name": "v1.2.3"}')
        with patch("urllib.request.urlopen", side_effect=urlopen):
            update_checker.fetch_latest_release_tag("owner/repo")
        self.assertIsNone(update_checker._DERNIERE_ENTREE["owner/repo"])

    def test_le_user_agent_nomme_l_application(self):
        vu = {}

        def urlopen(request, timeout=None):
            vu["ua"] = request.get_header("User-agent")
            return _reponse(_flux("v1.0.0"))

        with patch("urllib.request.urlopen", side_effect=urlopen):
            update_checker.fetch_latest_release_tag("owner/repo")
        self.assertEqual(vu["ua"], "owner/repo update-checker")


class TestFetchFromGithub(unittest.TestCase):
    """Le chemin de repli, inchange par rapport a l'ancien comportement."""

    def test_returns_tag_on_success(self):
        with patch("urllib.request.urlopen", return_value=_reponse(b'{"tag_name": "v1.2.3"}')):
            self.assertEqual(update_checker.fetch_from_github("owner/repo"), "v1.2.3")

    def test_returns_none_on_network_error(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("pas de reseau")):
            self.assertIsNone(update_checker.fetch_from_github("owner/repo"))

    def test_returns_none_on_malformed_json(self):
        with patch("urllib.request.urlopen", return_value=_reponse(b"pas du json")):
            self.assertIsNone(update_checker.fetch_from_github("owner/repo"))

    def test_returns_none_when_tag_name_missing(self):
        with patch("urllib.request.urlopen", return_value=_reponse(b'{"name": "Version 1.2.3"}')):
            self.assertIsNone(update_checker.fetch_from_github("owner/repo"))


class TestStartUpdateCheck(unittest.TestCase):
    def _run_and_get_result(self, current_version, fetch_return):
        result_queue = queue.Queue()
        with patch.object(update_checker, "fetch_latest_release_tag", return_value=fetch_return):
            update_checker.start_update_check(current_version, "owner/repo", result_queue)
            status, tag = result_queue.get(timeout=2)
        return status, tag

    def test_reports_update_available(self):
        status, tag = self._run_and_get_result("v1.0.0", "v1.2.0")
        self.assertEqual(status, "update_available")
        self.assertEqual(tag, "v1.2.0")

    def test_reports_up_to_date(self):
        status, tag = self._run_and_get_result("v1.2.0", "v1.2.0")
        self.assertEqual(status, "up_to_date")

    def test_reports_up_to_date_when_local_is_newer(self):
        status, tag = self._run_and_get_result("v1.5.0", "v1.2.0")
        self.assertEqual(status, "up_to_date")

    def test_reports_check_failed_when_fetch_fails(self):
        status, tag = self._run_and_get_result("v1.0.0", None)
        self.assertEqual(status, "check_failed")
        self.assertIsNone(tag)

    def test_runs_on_background_thread_not_blocking_caller(self):
        result_queue = queue.Queue()
        with patch.object(update_checker, "fetch_latest_release_tag", side_effect=lambda *a, **k: (time.sleep(0.2), "v9.9.9")[1]):
            started_at = time.monotonic()
            update_checker.start_update_check("v1.0.0", "owner/repo", result_queue)
            elapsed = time.monotonic() - started_at
        self.assertLess(elapsed, 0.1, "start_update_check doit revenir immediatement, sans attendre le worker")
        status, tag = result_queue.get(timeout=2)
        self.assertEqual(status, "update_available")


# --- telechargement verifie -------------------------------------------------

CORPS = bytes(range(256)) * 600          # 153 600 octets, plus que TAILLE_MORCEAU
SHA_CORPS = hashlib.sha256(CORPS).hexdigest()


def _entree(corps=CORPS, sha=None, taille=None, url="https://github.com/owner/repo/releases/download/v2/Repo.exe", nom="Repo.exe"):
    return {"etiquette": "v2.0.0", "binaire": {
        "nom": nom, "url": url,
        "taille_octets": len(corps) if taille is None else taille,
        "sha256": hashlib.sha256(corps).hexdigest() if sha is None else sha,
    }}


class TestTelechargerEtVerifier(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.dossier = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_un_binaire_conforme_est_garde_et_renvoye(self):
        with patch("urllib.request.urlopen", return_value=_reponse_en_flux(CORPS)):
            chemin = update_checker.telecharger_et_verifier(_entree(), self.dossier)
        self.assertEqual(chemin, self.dossier / "Repo.exe")
        self.assertEqual(chemin.read_bytes(), CORPS)
        self.assertFalse((self.dossier / "Repo.exe.partiel").exists(), "le fichier partiel doit avoir ete renomme")

    def test_une_empreinte_qui_differe_supprime_le_fichier_et_leve(self):
        """LE COEUR : un fichier qui n'est pas celui annonce ne reste pas sur le
        disque, et l'appelant le sait."""
        with patch("urllib.request.urlopen", return_value=_reponse_en_flux(CORPS)):
            with self.assertRaises(update_checker.TelechargementInvalide) as cm:
                update_checker.telecharger_et_verifier(_entree(sha="b" * 64), self.dossier)
        self.assertIn("empreinte", str(cm.exception))
        self.assertEqual(list(self.dossier.iterdir()), [], "rien ne doit rester dans le dossier")

    def test_une_taille_qui_differe_supprime_le_fichier_et_leve(self):
        with patch("urllib.request.urlopen", return_value=_reponse_en_flux(CORPS)):
            with self.assertRaises(update_checker.TelechargementInvalide):
                update_checker.telecharger_et_verifier(_entree(taille=len(CORPS) - 1), self.dossier)
        self.assertEqual(list(self.dossier.iterdir()), [])

    def test_un_fichier_plus_court_qu_annonce_est_refuse_a_la_fin(self):
        """Le cas que le controle EN COURS de flux ne peut pas voir : la
        connexion se ferme proprement avant la taille annoncee. Seule la
        comparaison finale l'attrape — et l'empreinte, calculee sur ce qu'on a
        recu, peut tres bien etre la bonne si le flux ment sur la taille."""
        entree = _entree(taille=len(CORPS) + 1)
        with patch("urllib.request.urlopen", return_value=_reponse_en_flux(CORPS)):
            with self.assertRaises(update_checker.TelechargementInvalide) as cm:
                update_checker.telecharger_et_verifier(entree, self.dossier)
        self.assertIn("taille", str(cm.exception))
        self.assertEqual(list(self.dossier.iterdir()), [])

    def test_un_fichier_plus_gros_qu_annonce_est_coupe_sans_attendre_la_fin(self):
        lu = {"n": 0}
        response = _reponse_en_flux(CORPS)
        original = response.read.side_effect

        def read(n=-1):
            lu["n"] += 1
            return original(n)

        response.read.side_effect = read
        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(update_checker.TelechargementInvalide):
                update_checker.telecharger_et_verifier(_entree(taille=update_checker.TAILLE_MORCEAU), self.dossier)
        self.assertLess(lu["n"], 3, "on a continue a lire apres avoir depasse la taille annoncee")
        self.assertEqual(list(self.dossier.iterdir()), [])

    def test_une_adresse_hors_github_est_refusee_avant_toute_requete(self):
        with patch("urllib.request.urlopen") as urlopen:
            for url in ("https://exemple.org/Repo.exe", "http://github.com/owner/repo/Repo.exe",
                        "https://github.com.evil.example/Repo.exe", "ftp://github.com/x"):
                with self.assertRaises(update_checker.TelechargementInvalide, msg=url):
                    update_checker.telecharger_et_verifier(_entree(url=url), self.dossier)
        urlopen.assert_not_called()

    def test_une_taille_annoncee_demesuree_est_refusee_avant_toute_requete(self):
        """La borne BASSE (taille <= 0) ne suffit pas : le controle en cours de
        telechargement ne se declenche qu'en DEPASSANT la taille annoncee, donc
        l'annoncer enorme le desarme et laisse ecrire jusqu'a saturation du
        disque. Mesure a l'audit du 2026-08-26 : 10**15 octets acceptes."""
        with patch("urllib.request.urlopen") as urlopen:
            for taille in (update_checker.TAILLE_MAX_OCTETS + 1, 10 ** 15):
                with self.assertRaises(update_checker.TelechargementInvalide, msg=str(taille)):
                    update_checker.telecharger_et_verifier(_entree(taille=taille), self.dossier)
        urlopen.assert_not_called()

    def test_un_binaire_hors_des_releases_du_depot_est_refuse_quand_le_depot_est_connu(self):
        """L'hote seul ne suffit pas : github.com/<attaquant>/... est une
        adresse GitHub parfaitement legitime. Quand le depot est connu, on
        exige que l'adresse soit une release DE CE DEPOT - l'empreinte
        attendue venant du meme flux, elle ne discrimine rien (audit)."""
        bonne = "https://github.com/owner/repo/releases/download/v2/Repo.exe"
        with patch("urllib.request.urlopen") as urlopen:
            for url in ("https://github.com/attaquant/piege/releases/download/v2/Repo.exe",
                        "https://github.com/owner/repo/raw/master/Repo.exe",
                        "https://github.com/owner/repo-bis/releases/download/v2/Repo.exe"):
                with self.assertRaises(update_checker.TelechargementInvalide, msg=url):
                    update_checker.telecharger_et_verifier(_entree(url=url), self.dossier, repo="owner/repo")
        urlopen.assert_not_called()
        # ... et l'adresse legitime du meme depot passe toujours.
        with patch("urllib.request.urlopen", return_value=_reponse_en_flux(CORPS)):
            chemin = update_checker.telecharger_et_verifier(_entree(url=bonne), self.dossier, repo="owner/repo")
        self.assertTrue(chemin.exists())

    def test_une_empreinte_ou_une_taille_annoncee_invalide_est_refusee_avant_toute_requete(self):
        with patch("urllib.request.urlopen") as urlopen:
            for mauvais in ({"sha": "pas-une-empreinte"}, {"sha": "a" * 63}, {"taille": 0}, {"taille": -5}, {"taille": True}):
                with self.assertRaises(update_checker.TelechargementInvalide, msg=str(mauvais)):
                    update_checker.telecharger_et_verifier(_entree(**mauvais), self.dossier)
        urlopen.assert_not_called()

    def test_le_nom_de_fichier_ne_peut_pas_sortir_du_dossier(self):
        with patch("urllib.request.urlopen", return_value=_reponse_en_flux(CORPS)):
            chemin = update_checker.telecharger_et_verifier(_entree(nom="../../evil.exe"), self.dossier)
        self.assertEqual(chemin.parent, self.dossier)
        self.assertEqual(chemin.name, "evil.exe")

    def test_un_fichier_identique_deja_present_est_reutilise_sans_requete(self):
        (self.dossier / "Repo.exe").write_bytes(CORPS)
        with patch("urllib.request.urlopen") as urlopen:
            chemin = update_checker.telecharger_et_verifier(_entree(), self.dossier)
        urlopen.assert_not_called()
        self.assertEqual(chemin, self.dossier / "Repo.exe")

    def test_un_autre_fichier_du_meme_nom_n_est_pas_ecrase(self):
        (self.dossier / "Repo.exe").write_bytes(b"un autre programme, precieux")
        with patch("urllib.request.urlopen", return_value=_reponse_en_flux(CORPS)):
            chemin = update_checker.telecharger_et_verifier(_entree(), self.dossier)
        self.assertEqual(chemin.name, "Repo (2).exe")
        self.assertEqual((self.dossier / "Repo.exe").read_bytes(), b"un autre programme, precieux")

    def test_une_panne_reseau_en_cours_de_route_ne_laisse_pas_de_partiel(self):
        response = _reponse_en_flux(CORPS)
        original = response.read.side_effect
        compteur = {"n": 0}

        def read(n=-1):
            compteur["n"] += 1
            if compteur["n"] == 2:
                raise TimeoutError("connexion perdue")
            return original(n)

        response.read.side_effect = read
        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(TimeoutError):
                update_checker.telecharger_et_verifier(_entree(), self.dossier)
        self.assertEqual(list(self.dossier.iterdir()), [])


class TestOuvrirMiseAJour(unittest.TestCase):
    """Le clic : page GitHub sans flux, telechargement verifie avec."""

    def setUp(self):
        import tempfile
        update_checker._DERNIERE_ENTREE.clear()
        update_checker._TELECHARGEMENTS_EN_COURS.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.dossier = Path(self._tmp.name)
        self.textes = []
        self.var = MagicMock()
        self.var.set.side_effect = self.textes.append
        # Un planificateur SYNCHRONE : ce que `root.after(0, fn)` ferait au
        # prochain tour de boucle, on le fait tout de suite.
        self.planifier = lambda delai, fn: fn()

    def tearDown(self):
        self._tmp.cleanup()

    def test_sans_flux_le_clic_ouvre_la_page_github_comme_avant(self):
        update_checker._DERNIERE_ENTREE["owner/repo"] = None
        with patch("webbrowser.open") as ouvrir:
            fil = update_checker.ouvrir_mise_a_jour("owner/repo", "https://github.com/owner/repo/releases/latest", self.var, self.planifier)
        self.assertIsNone(fil)
        ouvrir.assert_called_once_with("https://github.com/owner/repo/releases/latest")
        self.assertEqual(self.textes, [])

    def test_avec_flux_le_clic_telecharge_verifie_et_ouvre_le_dossier(self):
        update_checker._DERNIERE_ENTREE["owner/repo"] = _entree()
        with patch("urllib.request.urlopen", return_value=_reponse_en_flux(CORPS)), \
             patch("webbrowser.open") as ouvrir_page, patch.object(update_checker, "_ouvrir_dossier") as ouvrir_dossier:
            fil = update_checker.ouvrir_mise_a_jour("owner/repo", "https://github.com/x", self.var, self.planifier, self.dossier)
            fil.join(timeout=5)
        ouvrir_page.assert_not_called()
        ouvrir_dossier.assert_called_once_with(self.dossier)
        self.assertTrue((self.dossier / "Repo.exe").exists())
        self.assertIn("Telechargement de Repo.exe", self.textes[0])
        self.assertIn("Telecharge et verifie", self.textes[-1])

    def test_une_empreinte_qui_differe_n_ouvre_rien_et_le_dit(self):
        update_checker._DERNIERE_ENTREE["owner/repo"] = _entree(sha="c" * 64)
        with patch("urllib.request.urlopen", return_value=_reponse_en_flux(CORPS)), \
             patch("webbrowser.open") as ouvrir_page, patch.object(update_checker, "_ouvrir_dossier") as ouvrir_dossier:
            fil = update_checker.ouvrir_mise_a_jour("owner/repo", "https://github.com/x", self.var, self.planifier, self.dossier)
            fil.join(timeout=5)
        ouvrir_page.assert_not_called()
        ouvrir_dossier.assert_not_called()
        self.assertIn("refuse", self.textes[-1])
        self.assertEqual(list(self.dossier.iterdir()), [], "le fichier refuse doit avoir disparu")
        # Le clic suivant reprend l'ancien chemin : la page GitHub.
        self.assertIsNone(update_checker._DERNIERE_ENTREE["owner/repo"])

    def test_un_second_clic_pendant_le_telechargement_ne_relance_rien(self):
        update_checker._DERNIERE_ENTREE["owner/repo"] = _entree()
        update_checker._TELECHARGEMENTS_EN_COURS.add("owner/repo")
        with patch("urllib.request.urlopen") as urlopen, patch("webbrowser.open") as ouvrir:
            fil = update_checker.ouvrir_mise_a_jour("owner/repo", "https://github.com/x", self.var, self.planifier, self.dossier)
        self.assertIsNone(fil)
        urlopen.assert_not_called()
        ouvrir.assert_not_called()


if __name__ == "__main__":
    unittest.main()
