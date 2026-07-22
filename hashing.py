"""Primitives de hachage pures, sans dependance DB/GUI : identite exacte
d'un fichier (sha256) et hachage perceptuel d'une image (dHash) pour
detecter les quasi-doublons (rafales, recompressions, leger recadrage).

dHash (difference hash) plutot que aHash/pHash : robuste aux petits
changements de luminosite/contraste (contrairement a aHash, sensible a la
moyenne globale), et bien plus simple/rapide qu'un pHash (DCT) pour un
resultat suffisant sur des photos "presque identiques" - ce n'est PAS un
hachage invariant a la rotation ou au recadrage important, ce qui est un
compromis assume : ces cas plus complexes sortent du champ de l'outil.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from PIL import Image
import pillow_heif

# Enregistre l'ouvreur HEIF/HEIC de Pillow des l'import de ce module (qui est
# le module le plus bas dans l'arbre d'import - gui.py importe scanner.py qui
# importe hashing.py). Sans cet enregistrement, Pillow seul ne sait pas
# ouvrir les .heic/.heif (aucun plugin HEIF integre) : Image.open() leve
# UnidentifiedImageError sur toute photo iPhone, alors meme que ces
# extensions figurent dans IMAGE_EXTENSIONS ci-dessous.
pillow_heif.register_heif_opener()

HASH_SIZE = 8  # -> hash de HASH_SIZE * HASH_SIZE bits (64 bits par defaut)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff", ".tif", ".webp", ".heic", ".heif"}

# Limite assumee (A4 de l'audit) : pour un GIF ANIME, `Image.open()` seul
# n'expose que la premiere frame (Pillow ne saute jamais automatiquement a
# une frame suivante sans appel explicite a `img.seek()`) - ni le hachage
# exact (sha256, sur l'octet du fichier entier, non affecte) ni le hachage
# perceptuel (dHash, ci-dessous) ne tiennent donc compte des frames
# suivantes d'un GIF anime. Impact reel juge marginal (les GIF animes ne
# representent qu'une fraction infime d'une phototheque personnelle) :
# documente dans le README plutot que corrige par un hachage multi-frame,
# qui ajouterait de la complexite pour un benefice tres limite au vu de la
# frequence d'usage reelle.

# Le filtrage par extension (is_image_file, ci-dessous) n'est PAS une
# barriere de securite : Pillow determine le format reel d'un fichier par
# son contenu ("magic bytes"), pas par son nom - un fichier nomme ".jpg" dont
# le contenu reel est un PSD/FITS/ICO/etc. serait decode par Pillow avec le
# decodeur correspondant a son VRAI format, quelle que soit son extension
# (verifie a l'audit). Passer `formats=ALLOWED_PILLOW_FORMATS` a
# Image.open() restreint explicitement les decodeurs effectivement utilises
# a ceux correspondant aux extensions annoncees par IMAGE_EXTENSIONS : toute
# tentative d'ouverture d'un fichier deguise dont le contenu reel est un
# format hors de cette liste echoue proprement avec UnidentifiedImageError
# (deja geree comme "image illisible, ignoree") au lieu d'etre decodee par
# un decodeur Pillow potentiellement plus expose. Ceinture-et-bretelles en
# complement du plancher de version Pillow (requirements.txt), pas un
# remplacement.
ALLOWED_PILLOW_FORMATS = ("JPEG", "PNG", "BMP", "GIF", "TIFF", "WEBP", "HEIF")


class UnreadableImageError(Exception):
    """Le fichier ne peut pas etre ouvert/decode comme image (corrompu,
    format non supporte par Pillow, ou pas une image du tout malgre son
    extension) - distinct d'une erreur disque (FileNotFoundError,
    PermissionError), que l'appelant doit traiter separement."""


# Longueur totale de chemin (en caracteres) au-dela de laquelle Windows peut
# refuser d'ouvrir un fichier faute de support "long path" active - la vraie
# limite historique MAX_PATH est 260, mais quelques caracteres de marge
# (plutot que pile 260) evitent de rater les cas limites lies aux variations
# de comptage entre composants du chemin.
_WINDOWS_MAX_PATH = 260


def long_path(path) -> str:
    """Renvoie une representation de `path` utilisable pour les appels
    systeme (open, os.stat...) sans etre soumise a l'ancienne limite
    MAX_PATH (260 caracteres) de Windows : prefixe les chemins absolus
    suffisamment longs par ``\\\\?\\`` (chemin "etendu", ou ``\\\\?\\UNC\\``
    pour un partage reseau) - un prefixe reconnu nativement par les API
    Windows sous-jacentes, qui contourne MAX_PATH sans necessiter le moindre
    reglage systeme (contrairement au support "long path" opt-in de
    Windows 10+, jamais garanti actif sur la machine de l'utilisateur).

    Correctif de l'audit (M1) : avant ce correctif, un chemin trop long
    echouait proprement (deja rattrape comme "fichier illisible, ignore" par
    les try/except deja en place dans scanner.py/gui.py - degradation
    gracieuse) mais SANS JAMAIS ETRE REELLEMENT LU, alors meme que Windows
    aurait pu le lire via ce prefixe. No-op (renvoie `str(path)` sans
    modification) sur toute plateforme non-Windows, tout chemin deja assez
    court, ou deja prefixe. Le prefixe change la semantique de resolution du
    chemin par l'OS (pris litteralement, aucun ``.``/``..`` resolu par la
    couche filesystem) : il ne doit donc etre applique qu'a un chemin deja
    absolu et normalise, ce que garantissent tous les appelants actuels
    (chemins construits par `os.walk`/`Path.resolve`, jamais saisis
    librement par l'utilisateur a cet endroit)."""
    text = str(path)
    if os.name != "nt" or len(text) < _WINDOWS_MAX_PATH or text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        # Chemin UNC (\\serveur\partage\...) : le prefixe etendu
        # correspondant est \\?\UNC\serveur\partage\..., pas simplement
        # \\?\ colle devant - sinon Windows interpreterait l'ensemble comme
        # un chemin local commencant par un backslash litteral.
        return "\\\\?\\UNC\\" + text.lstrip("\\")
    return "\\\\?\\" + text


def file_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    """Empreinte du contenu OCTET PAR OCTET du fichier. Deux fichiers avec
    ce meme hash sont des doublons exacts au sens strict (copie bit a bit,
    quel que soit le nom/l'emplacement) - contrairement au dHash ci-dessous,
    qui peut rapprocher deux fichiers visuellement identiques mais
    physiquement differents (recompression, metadonnees EXIF differentes)."""
    digest = hashlib.sha256()
    with open(long_path(path), "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_dhash(image: Image.Image, hash_size: int = HASH_SIZE) -> int:
    """Hachage perceptuel dHash : convertit en niveaux de gris, redimensionne
    a (hash_size+1) x hash_size, puis encode 1 bit par pixel selon qu'il est
    plus clair ou plus sombre que son voisin de droite. Renvoie un entier de
    hash_size*hash_size bits, comparable via hamming_distance."""
    target_size = (hash_size + 1, hash_size)
    # draft() demande au decodeur JPEG de ne decoder qu'a une resolution
    # proche de la cible (le plus proche facteur 1/1, 1/2, 1/4, 1/8...) au
    # lieu de decoder en pleine resolution avant de reduire - le hash final
    # est de toute facon calcule sur une grille hash_size x hash_size, donc
    # les details perdus par ce sous-echantillonnage anticipe n'affectent
    # pas le resultat au-dela d'une tolerance de quelques bits (mesure a
    # l'audit). Sans effet sur les formats non-JPEG (no-op silencieux -
    # PNG/BMP/etc. continuent d'etre decodes en pleine resolution, sans
    # regression de vitesse ni de resultat par rapport a avant ce correctif).
    image.draft("RGB", target_size)
    small = image.convert("L").resize(target_size, Image.LANCZOS)
    # get_flattened_data() plutot que getdata() (bug trouve a l'audit, I2) :
    # Image.Image.getdata() est deprecie depuis Pillow 12.2 et sera
    # supprime en Pillow 14 (prevue 2027-10-15) - meme sequence de valeurs
    # en sortie (verifie a l'audit), disponible dans le plancher de version
    # deja retenu par requirements.txt (Pillow>=12.3.0).
    pixels = list(small.get_flattened_data())
    bits = 0
    for row in range(hash_size):
        row_start = row * (hash_size + 1)
        for col in range(hash_size):
            bits <<= 1
            if pixels[row_start + col] > pixels[row_start + col + 1]:
                bits |= 1
    return bits


def compute_dhash_from_path(path: Path, hash_size: int = HASH_SIZE) -> int:
    """Ouvre et hache un fichier image depuis le disque. Leve
    UnreadableImageError (plutot que de laisser fuiter l'exception Pillow
    brute) pour que scanner.py puisse distinguer "image illisible, a
    ignorer et signaler" d'une vraie erreur de programmation.

    La capture est deliberement large (`Exception`, pas seulement
    `UnidentifiedImageError`/`OSError`/`ValueError`) : Pillow leve, selon le
    format et le type de corruption, des exceptions qui n'heritent d'aucune
    de ces trois classes - notamment `PIL.Image.DecompressionBombError`
    (herite directement d'`Exception`), declenchee par un en-tete corrompu
    ou piege revendiquant des dimensions demesurees. Avant ce correctif, une
    telle exception remontait brute et faisait avorter tout le scan pour un
    seul fichier (bug trouve a l'audit) au lieu d'etre traitee comme les
    autres fichiers illisibles.

    `long_path(path)` (M1) plutot que `path` directement : permet de lire un
    fichier dont le chemin complet depasse MAX_PATH quand Windows le
    permet, au lieu de se contenter de l'echec gracieux deja en place."""
    try:
        with Image.open(long_path(path), formats=ALLOWED_PILLOW_FORMATS) as img:
            return compute_dhash(img, hash_size=hash_size)
    except Exception as exc:
        raise UnreadableImageError(f"Image illisible : {path} ({type(exc).__name__}: {exc})") from exc


def hamming_distance(a: int, b: int) -> int:
    """Nombre de bits differents entre deux hachages. 0 = images (quasi)
    identiques au sens du dHash ; plus la valeur augmente, plus les images
    different visuellement. XOR isole les bits differents, bit_count()
    (Python 3.10+) les compte sans boucle manuelle."""
    return (a ^ b).bit_count()


# Marge (en nombre de bits) en-deca de laquelle un hash est considere a
# "faible entropie" - voir is_low_entropy_hash ci-dessous. 4 bits sur 64
# reste tres strict (une vraie photo texturee a normalement un nombre de
# bits a 1 proche de 32, pas de 0/64), tout en couvrant confortablement le
# cas mesure a l'audit (images uniformes -> hash exactement 0, popcount=0).
LOW_ENTROPY_MARGIN_BITS = 4


def is_low_entropy_hash(value: int, hash_size: int = HASH_SIZE, margin: int = LOW_ENTROPY_MARGIN_BITS) -> bool:
    """Signale un dHash a tres faible entropie : nombre de bits a 1 proche
    de 0 ou du maximum (hash_size*hash_size). Un tel hash provient presque
    toujours d'une image a variance locale quasi nulle (ciel uni, mur,
    neige, photo tres sur/sous-exposee) - le dHash n'encode QUE des
    comparaisons de luminosite entre pixels adjacents, donc de telles images
    produisent (quasiment) toujours le meme hash quelle que soit leur
    couleur/teinte reelle (bug trouve a l'audit, A1 : rouge, bleu, vert,
    noir, blanc uniformes produisent tous exactement le meme hash - distance
    de Hamming 0 entre eux, quel que soit le seuil de sensibilite configure).
    Utilise par grouping.py pour savoir quand une verification
    supplementaire (compute_color_signature) est necessaire avant de
    rapprocher deux photos : appliquer cette verification a CHAQUE paire
    serait inutilement couteux (elle rouvre les fichiers), alors qu'elle
    n'est utile que pour ce cas pathologique precis."""
    total_bits = hash_size * hash_size
    popcount = value.bit_count()
    return popcount <= margin or popcount >= (total_bits - margin)


def compute_color_signature(image: Image.Image, size: int = 12) -> tuple:
    """Signal complementaire au dHash, PAS invariant a la luminosite relative
    (contrairement au dHash) : capture la couleur moyenne (R, G, B) sur
    chacun des trois tiers horizontaux de l'image, redimensionnee a
    `size` x `size` pour rester tres bon marche. Sert uniquement a departager
    les faux positifs de bas niveau du dHash decrits dans A1 (audit du
    2026-07-22) : deux images uniformes de teintes totalement differentes
    (rouge/bleu/vert/noir/blanc...) ont le meme dHash (0) mais des couleurs
    moyennes tres eloignees - voir color_signature_distance. N'est PAS un
    hachage perceptuel general (aucune invariance a la luminosite/au
    contraste), delibererement : ce n'est qu'un signal d'appoint local a
    grouping.py, jamais utilise seul."""
    small = image.convert("RGB").resize((size, size), Image.BILINEAR)
    # get_flattened_data() plutot que getdata(), deprecie - voir le
    # commentaire equivalent dans compute_dhash ci-dessus (I2).
    pixels = list(small.get_flattened_data())
    third = max(size // 3, 1)
    bands = []
    for band_index in range(3):
        row_start = band_index * third
        row_end = size if band_index == 2 else row_start + third
        band_pixels = pixels[row_start * size: row_end * size] or pixels
        r = sum(p[0] for p in band_pixels) // len(band_pixels)
        g = sum(p[1] for p in band_pixels) // len(band_pixels)
        b = sum(p[2] for p in band_pixels) // len(band_pixels)
        bands.extend((r, g, b))
    return tuple(bands)


def compute_color_signature_from_path(path: Path, size: int = 12) -> tuple:
    """Equivalent de compute_dhash_from_path pour compute_color_signature :
    memes garanties (formats restreints via ALLOWED_PILLOW_FORMATS, capture
    large des erreurs de decodage converties en UnreadableImageError,
    prefixe long_path (M1) applique de la meme facon)."""
    try:
        with Image.open(long_path(path), formats=ALLOWED_PILLOW_FORMATS) as img:
            return compute_color_signature(img, size=size)
    except Exception as exc:
        raise UnreadableImageError(f"Image illisible : {path} ({type(exc).__name__}: {exc})") from exc


def color_signature_distance(a: tuple, b: tuple) -> int:
    """Distance simple (somme des ecarts absolus canal par canal) entre deux
    signatures de compute_color_signature. 0 = couleurs moyennes identiques
    sur les trois tiers ; maximum possible 9*255=2295 (3 tiers x 3 canaux)."""
    return sum(abs(x - y) for x, y in zip(a, b))


def is_image_file(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS
