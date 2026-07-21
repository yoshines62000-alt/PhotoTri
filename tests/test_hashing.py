import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hashing


def _make_image(path: Path, kind: str = "gradient", size=(64, 64)) -> None:
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    if kind == "gradient":
        for x in range(size[0]):
            shade = int(255 * x / size[0])
            d.line([(x, 0), (x, size[1])], fill=(shade, shade, shade))
    elif kind == "solid_black":
        img = Image.new("RGB", size, "black")
    elif kind == "solid_white":
        img = Image.new("RGB", size, "white")
    elif kind == "checker":
        for y in range(size[1]):
            for x in range(size[0]):
                if (x // 8 + y // 8) % 2 == 0:
                    img.putpixel((x, y), (0, 0, 0))
    img.save(path)


class TestFileSha256(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_identical_content_same_hash(self):
        a = self.tmp / "a.bin"
        b = self.tmp / "b.bin"
        a.write_bytes(b"contenu identique")
        b.write_bytes(b"contenu identique")
        self.assertEqual(hashing.file_sha256(a), hashing.file_sha256(b))

    def test_different_content_different_hash(self):
        a = self.tmp / "a.bin"
        b = self.tmp / "b.bin"
        a.write_bytes(b"contenu A")
        b.write_bytes(b"contenu B")
        self.assertNotEqual(hashing.file_sha256(a), hashing.file_sha256(b))


class TestDHash(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_identical_image_distance_zero(self):
        p = self.tmp / "gradient.png"
        _make_image(p, "gradient")
        h1 = hashing.compute_dhash_from_path(p)
        h2 = hashing.compute_dhash_from_path(p)
        self.assertEqual(hashing.hamming_distance(h1, h2), 0)

    def test_very_different_images_large_distance(self):
        black = self.tmp / "black.png"
        checker = self.tmp / "checker.png"
        _make_image(black, "solid_black")
        _make_image(checker, "checker")
        h1 = hashing.compute_dhash_from_path(black)
        h2 = hashing.compute_dhash_from_path(checker)
        self.assertGreater(hashing.hamming_distance(h1, h2), 20)

    def test_resave_same_image_small_distance(self):
        # Reencoder la meme image (qualite JPEG differente) doit rester tres
        # proche au sens perceptuel, meme si le fichier et son sha256
        # different completement.
        p1 = self.tmp / "gradient1.jpg"
        p2 = self.tmp / "gradient2.jpg"
        img = Image.new("RGB", (64, 64), "white")
        d = ImageDraw.Draw(img)
        for x in range(64):
            shade = int(255 * x / 64)
            d.line([(x, 0), (x, 64)], fill=(shade, shade, shade))
        img.save(p1, quality=95)
        img.save(p2, quality=60)
        self.assertNotEqual(hashing.file_sha256(p1), hashing.file_sha256(p2))
        h1 = hashing.compute_dhash_from_path(p1)
        h2 = hashing.compute_dhash_from_path(p2)
        self.assertLessEqual(hashing.hamming_distance(h1, h2), 6)

    def test_unreadable_image_raises(self):
        bogus = self.tmp / "pas_une_image.jpg"
        bogus.write_bytes(b"ceci n'est pas du tout une image")
        with self.assertRaises(hashing.UnreadableImageError):
            hashing.compute_dhash_from_path(bogus)

    def test_hamming_distance_symmetric(self):
        self.assertEqual(hashing.hamming_distance(0b1010, 0b0110), hashing.hamming_distance(0b0110, 0b1010))
        self.assertEqual(hashing.hamming_distance(0b1111, 0b1111), 0)
        self.assertEqual(hashing.hamming_distance(0b0000, 0b1111), 4)


class TestHeicSupport(unittest.TestCase):
    """Verifie que le HEIC/HEIF n'est pas seulement liste dans
    IMAGE_EXTENSIONS mais reellement decodable de bout en bout : Pillow seul
    n'a aucun plugin HEIF, c'est pillow_heif.register_heif_opener() (appele
    a l'import de hashing.py) qui comble ce trou."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_heif_opener_registered_on_import(self):
        # hashing est deja importe plus haut dans ce fichier : l'enregistrement
        # a donc deja eu lieu. Pillow doit desormais connaitre .heic/.heif.
        exts = Image.registered_extensions()
        self.assertIn(".heic", exts)
        self.assertIn(".heif", exts)

    def test_heic_roundtrip_hash_computed(self):
        # Genere un vrai fichier .heic (pas un .jpg renomme) et verifie que
        # compute_dhash_from_path le decode et produit un hash exploitable,
        # exactement comme pour un .png.
        p = self.tmp / "gradient.heic"
        _make_image(p, "gradient")
        h = hashing.compute_dhash_from_path(p)
        self.assertIsInstance(h, int)

        p_png = self.tmp / "gradient.png"
        _make_image(p_png, "gradient")
        h_png = hashing.compute_dhash_from_path(p_png)
        # Meme contenu visuel encode en HEIC vs PNG : hash perceptuel tres proche.
        self.assertLessEqual(hashing.hamming_distance(h, h_png), 6)

    def test_heic_thumbnail_openable(self):
        # Simule l'usage fait par gui.py pour generer une vignette : ouvrir
        # l'image et pouvoir la manipuler (thumbnail) sans exception.
        p = self.tmp / "checker.heic"
        _make_image(p, "checker")
        with Image.open(p) as img:
            img.load()
            img.thumbnail((32, 32))
            self.assertLessEqual(img.size[0], 32)
            self.assertLessEqual(img.size[1], 32)


class TestIsImageFile(unittest.TestCase):
    def test_recognizes_common_extensions(self):
        for name in ("photo.jpg", "photo.JPEG", "photo.png", "photo.webp", "photo.heic"):
            self.assertTrue(hashing.is_image_file(Path(name)), name)

    def test_rejects_non_image_extensions(self):
        for name in ("document.pdf", "notes.txt", "archive.zip"):
            self.assertFalse(hashing.is_image_file(Path(name)), name)


if __name__ == "__main__":
    unittest.main()
