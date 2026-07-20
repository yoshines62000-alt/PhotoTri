"""Parcours recursif d'un dossier et indexation des photos en base :
lecture EXIF, hachage exact (sha256) et perceptuel (dHash), avec reprise
incrementale - un fichier dont la taille ET la date de modification n'ont
pas change depuis le dernier scan n'est jamais rehache, ce qui est
essentiel pour qu'un rescan d'une bibliotheque de dizaines de milliers de
photos reste rapide plutot que de tout retraiter a chaque fois."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PIL import ExifTags, Image

import hashing
from db import Database

_TAG_DATETIME_ORIGINAL = next((k for k, v in ExifTags.TAGS.items() if v == "DateTimeOriginal"), None)
_TAG_DATETIME = next((k for k, v in ExifTags.TAGS.items() if v == "DateTime"), None)


@dataclass
class ScanResult:
    total_found: int = 0
    scanned: int = 0
    skipped_unchanged: int = 0
    pruned: int = 0
    errors: list = field(default_factory=list)  # liste de (path, message)


def _extract_taken_at(img: Image.Image) -> Optional[str]:
    """Date de prise de vue (EXIF DateTimeOriginal, ou a defaut DateTime)
    au format ISO 8601, ou None si absente/illisible. Ne leve JAMAIS :
    l'absence de metadonnee EXIF est le cas normal pour une capture
    d'ecran ou une image telechargee depuis le web, pas une erreur."""
    try:
        exif = img.getexif()
        raw = None
        if _TAG_DATETIME_ORIGINAL is not None:
            raw = exif.get(_TAG_DATETIME_ORIGINAL)
        if not raw and _TAG_DATETIME is not None:
            raw = exif.get(_TAG_DATETIME)
        if not raw:
            return None
        dt = datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S")
        return dt.isoformat()
    except Exception:
        return None


def _iter_image_files(root: Path):
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            if hashing.is_image_file(path):
                yield path


def scan_folder(
    root: Path, db: Database,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> ScanResult:
    """Scanne `root` recursivement et indexe chaque photo dans `db`.

    `progress_callback(done, total, current_path)` est appele apres chaque
    fichier traite (y compris ceux ignores/en erreur) pour piloter une
    barre de progression GUI. `should_stop()` est verifie avant chaque
    fichier pour permettre une annulation cooperative depuis le thread
    appelant - un scan peut prendre plusieurs minutes sur une grosse
    bibliotheque, l'utilisateur doit pouvoir l'interrompre.

    En fin de scan, toute entree deja en base sous `root` mais dont le
    fichier n'a pas ete retrouve sur le disque est purgee (suppression
    manuelle depuis le dernier scan) - sans consequence sur les vraies
    photos, uniquement sur l'index reconstructible."""
    root = Path(root)
    files = list(_iter_image_files(root))
    result = ScanResult(total_found=len(files))
    seen_paths = set()

    for index, path in enumerate(files, start=1):
        if should_stop is not None and should_stop():
            break
        path_str = str(path)
        seen_paths.add(path_str)
        try:
            stat = path.stat()
        except OSError as exc:
            result.errors.append((path_str, str(exc)))
            if progress_callback:
                progress_callback(index, result.total_found, path_str)
            continue

        existing = db.get_photo_by_path(path_str)
        if existing is not None and existing["size"] == stat.st_size and existing["mtime"] == stat.st_mtime:
            result.skipped_unchanged += 1
            if progress_callback:
                progress_callback(index, result.total_found, path_str)
            continue

        try:
            sha = hashing.file_sha256(path)
            with Image.open(path) as img:
                width, height = img.size
                taken_at = _extract_taken_at(img)
                phash = hashing.compute_dhash(img)
        except (hashing.UnreadableImageError, OSError, ValueError) as exc:
            result.errors.append((path_str, str(exc)))
            if progress_callback:
                progress_callback(index, result.total_found, path_str)
            continue

        db.upsert_photo(path_str, stat.st_size, stat.st_mtime, width, height, sha, phash, taken_at)
        result.scanned += 1
        if progress_callback:
            progress_callback(index, result.total_found, path_str)

    stale = db.list_paths_under(str(root)) - seen_paths
    if stale:
        db.delete_by_paths(list(stale))
        result.pruned = len(stale)

    return result
