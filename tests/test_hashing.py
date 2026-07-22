import struct
import sys
import tempfile
import unittest
import unittest.mock
import warnings
import zlib
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hashing


def _make_decompression_bomb_png(path: Path, width: int = 60000, height: int = 60000) -> None:
    """Fabrique un PNG dont l'en-tete IHDR revendique des dimensions
    demesurees (60000x60000 par defaut) alors que le fichier reste minuscule
    - reproduit fidelement un en-tete corrompu ou piege (audit du
    2026-07-22, C1) : on part d'un vrai petit PNG valide puis on ecrase
    uniquement les 8 octets width/height de son chunk IHDR (en recalculant
    le CRC32 du chunk pour que Pillow accepte toujours le fichier comme un
    PNG bien forme et tente de le decoder, plutot que de le rejeter des la
    verification de signature)."""
    real = path.with_suffix(".tmp.png")
    Image.new("RGB", (4, 4), "red").save(real, format="PNG")
    data = bytearray(real.read_bytes())
    real.unlink()

    assert data[12:16] == b"IHDR"
    struct.pack_into(">II", data, 16, width, height)
    chunk_type_and_data = bytes(data[12:12 + 4 + 13])
    new_crc = zlib.crc32(chunk_type_and_data) & 0xFFFFFFFF
    struct.pack_into(">I", data, 12 + 4 + 13, new_crc)
    path.write_bytes(bytes(data))


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

    def test_decompression_bomb_header_is_reported_as_unreadable_not_fatal(self):
        # Verrouille C1 (audit du 2026-07-22) : PIL.Image.DecompressionBombError
        # herite directement d'Exception (ni OSError ni ValueError) - un
        # en-tete PNG corrompu ou piege revendiquant des dimensions
        # demesurees (60000x60000 ici, comme dans l'audit) ne doit jamais
        # fuiter cette exception brute hors de compute_dhash_from_path : elle
        # doit etre convertie en UnreadableImageError, exactement comme
        # n'importe quel autre fichier illisible, pour que l'appelant
        # (scanner.scan_folder) puisse continuer le scan au lieu d'avorter.
        bomb = self.tmp / "bomb.png"
        _make_decompression_bomb_png(bomb)
        with self.assertRaises(hashing.UnreadableImageError):
            hashing.compute_dhash_from_path(bomb)

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


class TestFormatRestrictionIsAppliedOnOpen(unittest.TestCase):
    """Verrouille D1 (audit du 2026-07-22) : le filtrage par extension
    (is_image_file/IMAGE_EXTENSIONS) n'est PAS une barriere de securite -
    Pillow determine le format reel d'un fichier par son contenu, pas par
    son nom. Ces tests verrouillent la mitigation ceinture-et-bretelles :
    Image.open() est desormais toujours appele avec
    formats=ALLOWED_PILLOW_FORMATS, ce qui fait echouer proprement
    (UnreadableImageError, deja geree comme "fichier illisible, ignore")
    toute ouverture d'un fichier dont le contenu reel est un format hors de
    cette liste, quelle que soit son extension."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_extension_alone_does_not_guarantee_the_real_format(self):
        # Preuve du constat de l'audit : is_image_file() se fie uniquement
        # au suffixe du nom de fichier, un .jpg dont le contenu reel est un
        # format totalement different passe donc ce filtre sans encombre.
        disguised = self.tmp / "deguise.jpg"
        Image.new("RGB", (16, 16), "green").save(disguised, format="PPM")
        self.assertTrue(hashing.is_image_file(disguised))
        with Image.open(disguised) as img:
            self.assertNotEqual(img.format, "JPEG")

    def test_disguised_file_with_allowed_content_still_opens(self):
        # Un fichier .jpg dont le contenu reel est un PNG (deguisement
        # frequent et inoffensif : simple renommage manuel) doit continuer a
        # s'ouvrir normalement - PNG fait partie des formats autorises.
        disguised = self.tmp / "vraiment_un_png.jpg"
        Image.new("RGB", (16, 16), "green").save(disguised, format="PNG")
        h = hashing.compute_dhash_from_path(disguised)
        self.assertIsInstance(h, int)

    def test_disguised_file_with_disallowed_content_is_rejected(self):
        # Un fichier .jpg dont le contenu reel est un format que Pillow sait
        # decoder mais que PhotoTri n'a jamais l'intention de traiter (ici
        # PPM, hors de ALLOWED_PILLOW_FORMATS) doit etre rejete comme
        # illisible plutot que decode par son "vrai" decodeur - exactement
        # le vecteur decrit dans l'audit (PSD/FITS/ICO/etc. deguises en
        # .jpg/.png, combine a une eventuelle CVE Pillow non corrigee - D2).
        disguised = self.tmp / "deguise.jpg"
        Image.new("RGB", (16, 16), "green").save(disguised, format="PPM")
        with self.assertRaises(hashing.UnreadableImageError):
            hashing.compute_dhash_from_path(disguised)


class TestIsLowEntropyHash(unittest.TestCase):
    """Verrouille la detection introduite pour A1 (audit du 2026-07-22) :
    un dHash dont le nombre de bits a 1 est proche de 0 ou du maximum
    signale une image a variance locale quasi nulle (ciel uni, mur, neige,
    photo tres sur/sous-exposee) - c'est ce genre de hash qui produisait des
    faux positifs massifs, le dHash etant aveugle a la couleur/teinte reelle
    de telles images."""

    def test_zero_hash_is_low_entropy(self):
        self.assertTrue(hashing.is_low_entropy_hash(0))

    def test_all_ones_hash_is_low_entropy(self):
        self.assertTrue(hashing.is_low_entropy_hash((1 << 64) - 1))

    def test_near_zero_popcount_is_low_entropy(self):
        self.assertTrue(hashing.is_low_entropy_hash(0b1111))  # popcount=4, marge par defaut

    def test_balanced_hash_is_not_low_entropy(self):
        # ~32 bits a 1 sur 64 - typique d'une vraie photo texturee.
        balanced = int("10" * 32, 2)
        self.assertFalse(hashing.is_low_entropy_hash(balanced))

    def test_just_above_margin_is_not_low_entropy(self):
        five_bits = 0b11111  # popcount=5 > marge par defaut (4)
        self.assertFalse(hashing.is_low_entropy_hash(five_bits))


class TestColorSignature(unittest.TestCase):
    """Verrouille le second signal introduit pour A1 : deux images uniformes
    de teintes totalement differentes doivent avoir des dHash identiques
    (le dHash seul ne code que des comparaisons de luminosite entre pixels
    adjacents) MAIS des signatures couleur nettement eloignees - c'est cette
    difference que grouping.py exploite pour departager les faux positifs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _solid(self, color, name):
        p = self.tmp / name
        Image.new("RGB", (200, 200), color).save(p, quality=95)
        return p

    def test_solid_color_images_share_the_same_dhash(self):
        # Reproduit exactement le constat de l'audit (A1) : rouge, bleu et
        # vert uniformes ont tous un dHash de 0 - distance de Hamming 0
        # entre eux, quel que soit le seuil configure.
        red = self._solid((255, 0, 0), "red.jpg")
        blue = self._solid((0, 0, 255), "blue.jpg")
        green = self._solid((0, 255, 0), "green.jpg")
        h_red = hashing.compute_dhash_from_path(red)
        h_blue = hashing.compute_dhash_from_path(blue)
        h_green = hashing.compute_dhash_from_path(green)
        self.assertEqual(h_red, 0)
        self.assertEqual(hashing.hamming_distance(h_red, h_blue), 0)
        self.assertEqual(hashing.hamming_distance(h_red, h_green), 0)
        self.assertTrue(hashing.is_low_entropy_hash(h_red))

    def test_color_signature_distance_is_large_for_different_hues(self):
        red = self._solid((255, 0, 0), "red.jpg")
        blue = self._solid((0, 0, 255), "blue.jpg")
        sig_red = hashing.compute_color_signature_from_path(red)
        sig_blue = hashing.compute_color_signature_from_path(blue)
        # Mesure a l'audit (voir grouping._LOW_ENTROPY_COLOR_MAX_DISTANCE,
        # fixe a 150) : des teintes clairement distinctes sont toujours a
        # plus de 750 de distance sur cette echelle (max possible 2295).
        self.assertGreater(hashing.color_signature_distance(sig_red, sig_blue), 750)

    def test_color_signature_distance_is_small_for_recompressed_same_color(self):
        # La meme couleur, reencodee a une qualite JPEG differente, doit
        # rester tres proche - le correctif A1 ne doit pas introduire de
        # faux NEGATIF sur un vrai quasi-doublon de ce type.
        a = self.tmp / "gray_a.jpg"
        b = self.tmp / "gray_b.jpg"
        Image.new("RGB", (200, 200), (128, 128, 128)).save(a, quality=95)
        Image.new("RGB", (200, 200), (130, 130, 130)).save(b, quality=60)
        sig_a = hashing.compute_color_signature_from_path(a)
        sig_b = hashing.compute_color_signature_from_path(b)
        self.assertLess(hashing.color_signature_distance(sig_a, sig_b), 150)

    def test_identical_image_has_zero_color_distance(self):
        p = self._solid((10, 200, 90), "solid.jpg")
        sig1 = hashing.compute_color_signature_from_path(p)
        sig2 = hashing.compute_color_signature_from_path(p)
        self.assertEqual(hashing.color_signature_distance(sig1, sig2), 0)

    def test_unreadable_image_raises(self):
        bogus = self.tmp / "pas_une_image.jpg"
        bogus.write_bytes(b"ceci n'est pas du tout une image")
        with self.assertRaises(hashing.UnreadableImageError):
            hashing.compute_color_signature_from_path(bogus)


class TestNoDeprecatedGetdataUsage(unittest.TestCase):
    """Verrouille I2 (audit du 2026-07-22) : Image.Image.getdata() est
    deprecie depuis Pillow 12.2 et sera supprime en Pillow 14 (prevue
    2027-10-15, message Pillow explicite : "Use get_flattened_data
    instead"). compute_dhash/compute_color_signature ne doivent plus jamais
    l'appeler - toute reapparition de getdata() dans ces fonctions ferait
    remonter un DeprecationWarning ici, transforme en erreur par
    simplefilter("error", ...) pour que le test echoue au lieu de se
    contenter d'un warning silencieusement ignore."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_compute_dhash_raises_no_deprecation_warning(self):
        p = self.tmp / "gradient.png"
        _make_image(p, "gradient")
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            h = hashing.compute_dhash_from_path(p)
        self.assertIsInstance(h, int)

    def test_compute_color_signature_raises_no_deprecation_warning(self):
        p = self.tmp / "gradient.png"
        _make_image(p, "gradient")
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            sig = hashing.compute_color_signature_from_path(p)
        self.assertEqual(len(sig), 9)


class TestIsImageFile(unittest.TestCase):
    def test_recognizes_common_extensions(self):
        for name in ("photo.jpg", "photo.JPEG", "photo.png", "photo.webp", "photo.heic"):
            self.assertTrue(hashing.is_image_file(Path(name)), name)

    def test_rejects_non_image_extensions(self):
        for name in ("document.pdf", "notes.txt", "archive.zip"):
            self.assertFalse(hashing.is_image_file(Path(name)), name)


class TestLongPathPrefix(unittest.TestCase):
    """Verrouille M1 (audit du 2026-07-22) : un chemin Windows absolu qui
    depasse l'ancienne limite MAX_PATH (260 caracteres) doit recevoir le
    prefixe etendu ``\\\\?\\`` (ou ``\\\\?\\UNC\\`` pour un chemin reseau)
    avant tout appel systeme (open/os.stat), pour permettre de le lire
    reellement plutot que de se contenter de l'echec gracieux deja en place
    (fichier compte comme "illisible", scan/hachage jamais interrompus)."""

    def test_short_path_is_returned_unchanged(self):
        short = "C:\\Photos\\a.jpg"
        self.assertEqual(hashing.long_path(short), short)

    def test_long_local_path_gets_the_extended_prefix(self):
        raw = "C:\\Photos\\" + ("a" * 300) + ".jpg"
        self.assertGreaterEqual(len(raw), 260)
        result = hashing.long_path(raw)
        self.assertTrue(result.startswith("\\\\?\\"))
        self.assertTrue(result.endswith(raw))

    def test_long_unc_path_gets_the_unc_extended_prefix(self):
        raw = "\\\\serveur\\partage\\" + ("a" * 300) + ".jpg"
        self.assertGreaterEqual(len(raw), 260)
        result = hashing.long_path(raw)
        self.assertTrue(result.startswith("\\\\?\\UNC\\"), result[:20])
        self.assertNotIn("\\\\?\\UNC\\\\", result, "le double backslash du chemin UNC d'origine ne doit pas etre duplique")

    def test_already_prefixed_path_is_left_untouched(self):
        raw = "\\\\?\\C:\\Photos\\" + ("a" * 300) + ".jpg"
        self.assertEqual(hashing.long_path(raw), raw)

    def test_accepts_path_objects_not_only_strings(self):
        long_name = "a" * 300 + ".jpg"
        raw_path = Path("C:\\Photos\\" + long_name)
        result = hashing.long_path(raw_path)
        self.assertTrue(result.startswith("\\\\?\\"))

    def test_non_windows_platform_never_adds_a_prefix(self):
        raw = "C:\\Photos\\" + ("a" * 300) + ".jpg"
        with unittest.mock.patch.object(hashing.os, "name", "posix"):
            self.assertEqual(hashing.long_path(raw), raw)

    def test_file_sha256_uses_long_path_for_the_actual_open_call(self):
        # Verrouille le cablage reel (pas seulement l'existence de la
        # fonction pure) : file_sha256 doit passer par long_path() pour
        # ouvrir le fichier, pas par le chemin brut.
        tmp = Path(tempfile.mkdtemp())
        p = tmp / "a.bin"
        p.write_bytes(b"contenu")
        calls = []
        real_long_path = hashing.long_path

        def spy(path):
            calls.append(str(path))
            return real_long_path(path)

        with unittest.mock.patch.object(hashing, "long_path", side_effect=spy):
            digest = hashing.file_sha256(p)
        self.assertEqual(calls, [str(p)])
        self.assertEqual(digest, hashing.file_sha256(p))

    def test_compute_dhash_from_path_uses_long_path_for_the_actual_open_call(self):
        tmp = Path(tempfile.mkdtemp())
        p = tmp / "gradient.png"
        _make_image(p, "gradient")
        calls = []
        real_long_path = hashing.long_path

        def spy(path):
            calls.append(str(path))
            return real_long_path(path)

        with unittest.mock.patch.object(hashing, "long_path", side_effect=spy):
            h = hashing.compute_dhash_from_path(p)
        self.assertEqual(calls, [str(p)])
        self.assertIsInstance(h, int)


if __name__ == "__main__":
    unittest.main()
