"""Verrouille D2 (audit du 2026-07-22) : requirements.txt fixait
Pillow>=10.0 sans aucune borne superieure, ce qui incluait entierement la
plage 10.3.0-12.1.1 vulnerable a CVE-2026-25990 (ecriture hors limites via
un PSD forge, CVSS 8.9, corrigee en 12.1.1) et CVE-2026-40192 (bombe de
decompression via donnees FITS, corrigee en 12.2.0). Ces tests s'assurent
que le plancher declare dans requirements.txt exclut bien cette plage
connue vulnerable, et que la version de Pillow effectivement installee la
respecte - un plancher releve dans le fichier mais jamais reellement
installe ne protegerait personne."""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REQUIREMENTS_PATH = Path(__file__).resolve().parent.parent / "requirements.txt"

# La plus basse version Pillow qui exclut A LA FOIS CVE-2026-25990 (corrigee
# en 12.1.1) et CVE-2026-40192 (corrigee en 12.2.0) - donc 12.2.0.
MINIMUM_SAFE_PILLOW_VERSION = (12, 2, 0)


def _parse_version(text: str) -> tuple:
    return tuple(int(part) for part in re.findall(r"\d+", text))


class TestRequirementsPillowFloor(unittest.TestCase):
    def setUp(self):
        content = REQUIREMENTS_PATH.read_text(encoding="utf-8")
        self.pillow_lines = [
            line.strip() for line in content.splitlines()
            if line.strip() and not line.strip().startswith("#")
            and re.match(r"(?i)^pillow(?!-heif)\b", line.strip())
        ]

    def test_pillow_requirement_line_is_present_exactly_once(self):
        self.assertEqual(len(self.pillow_lines), 1, self.pillow_lines)

    def test_pillow_floor_excludes_known_vulnerable_range(self):
        line = self.pillow_lines[0]
        match = re.match(r"(?i)^pillow\s*>=\s*([0-9][0-9.]*)", line)
        self.assertIsNotNone(match, f"pas de plancher '>=' trouve dans requirements.txt : {line!r}")
        floor = _parse_version(match.group(1))
        self.assertGreaterEqual(
            floor, MINIMUM_SAFE_PILLOW_VERSION,
            f"plancher Pillow {floor} n'exclut pas la plage vulnerable "
            f"10.3.0-12.1.1/12.2.0 (CVE-2026-25990 CVSS 8.9 / CVE-2026-40192) "
            f"- {MINIMUM_SAFE_PILLOW_VERSION} minimum requis",
        )

    def test_pillow_requirement_has_no_upper_bound_below_the_safe_floor(self):
        # S'assure qu'aucun plafond ("<X") ne vient neutraliser le plancher
        # de securite en empechant justement l'installation d'une version sure.
        line = self.pillow_lines[0]
        for upper_bound in re.findall(r"<\s*([0-9][0-9.]*)", line):
            self.assertGreater(_parse_version(upper_bound), MINIMUM_SAFE_PILLOW_VERSION)

    def test_installed_pillow_meets_the_safe_floor(self):
        # Le plancher du fichier ne protege reellement que si la version
        # installee dans l'environnement le respecte deja (verifie a
        # l'audit via `pip show Pillow` avant de choisir la borne).
        import PIL
        installed = _parse_version(PIL.__version__)
        self.assertGreaterEqual(
            installed, MINIMUM_SAFE_PILLOW_VERSION,
            f"Pillow {PIL.__version__} installee est elle-meme dans (ou sous) la plage vulnerable",
        )


if __name__ == "__main__":
    unittest.main()
