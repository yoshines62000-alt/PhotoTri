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
from pathlib import Path

from PIL import Image, UnidentifiedImageError
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


class UnreadableImageError(Exception):
    """Le fichier ne peut pas etre ouvert/decode comme image (corrompu,
    format non supporte par Pillow, ou pas une image du tout malgre son
    extension) - distinct d'une erreur disque (FileNotFoundError,
    PermissionError), que l'appelant doit traiter separement."""


def file_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    """Empreinte du contenu OCTET PAR OCTET du fichier. Deux fichiers avec
    ce meme hash sont des doublons exacts au sens strict (copie bit a bit,
    quel que soit le nom/l'emplacement) - contrairement au dHash ci-dessous,
    qui peut rapprocher deux fichiers visuellement identiques mais
    physiquement differents (recompression, metadonnees EXIF differentes)."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_dhash(image: Image.Image, hash_size: int = HASH_SIZE) -> int:
    """Hachage perceptuel dHash : convertit en niveaux de gris, redimensionne
    a (hash_size+1) x hash_size, puis encode 1 bit par pixel selon qu'il est
    plus clair ou plus sombre que son voisin de droite. Renvoie un entier de
    hash_size*hash_size bits, comparable via hamming_distance."""
    small = image.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
    pixels = list(small.getdata())
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
    ignorer et signaler" d'une vraie erreur de programmation."""
    try:
        with Image.open(path) as img:
            return compute_dhash(img, hash_size=hash_size)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise UnreadableImageError(f"Image illisible : {path}") from exc


def hamming_distance(a: int, b: int) -> int:
    """Nombre de bits differents entre deux hachages. 0 = images (quasi)
    identiques au sens du dHash ; plus la valeur augmente, plus les images
    different visuellement. XOR isole les bits differents, bit_count()
    (Python 3.10+) les compte sans boucle manuelle."""
    return (a ^ b).bit_count()


def is_image_file(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS
