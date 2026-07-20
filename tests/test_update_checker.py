import queue
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import update_checker


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


class TestFetchLatestReleaseTag(unittest.TestCase):
    def test_returns_tag_on_success(self):
        response = MagicMock()
        response.read.return_value = b'{"tag_name": "v1.2.3"}'
        response.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=response):
            tag = update_checker.fetch_latest_release_tag("owner/repo")
        self.assertEqual(tag, "v1.2.3")

    def test_returns_none_on_network_error(self):
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("pas de reseau")):
            tag = update_checker.fetch_latest_release_tag("owner/repo")
        self.assertIsNone(tag)

    def test_returns_none_on_malformed_json(self):
        response = MagicMock()
        response.read.return_value = b"pas du json"
        response.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=response):
            tag = update_checker.fetch_latest_release_tag("owner/repo")
        self.assertIsNone(tag)

    def test_returns_none_when_tag_name_missing(self):
        response = MagicMock()
        response.read.return_value = b'{"name": "Version 1.2.3"}'
        response.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=response):
            tag = update_checker.fetch_latest_release_tag("owner/repo")
        self.assertIsNone(tag)


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
