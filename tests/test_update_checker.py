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
        # Pour la suite (verification du SHA-256 par l'updater) : l'entree
        # complete est accessible, pas seulement le tag.
        urlopen, _ = _selon_url(flux=_flux("v1.2.3"))
        with patch("urllib.request.urlopen", side_effect=urlopen):
            entree = update_checker.fetch_from_flux("owner/repo")
        self.assertEqual(entree["binaire"]["sha256"], "a" * 64)
        self.assertEqual(entree["binaire"]["taille_octets"], 12345)

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


if __name__ == "__main__":
    unittest.main()
