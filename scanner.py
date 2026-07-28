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

# Nombre d'upserts groupes dans une meme transaction SQLite pendant un scan,
# au lieu d'un commit (fsync disque) a chaque photo. 200 est un compromis :
# assez grand pour eliminer l'essentiel du cout de fsync sur une grosse
# bibliotheque, assez petit pour qu'une interruption (Arreter, crash) ne
# perde jamais plus qu'un lot de travail deja effectue.
_COMMIT_BATCH_SIZE = 200


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


def _iter_image_files(root: Path, exclude_dir: Optional[Path] = None):
    """Parcourt `root` recursivement. Si `exclude_dir` est fourni et se
    trouve sous `root` (ou est confondu avec `root`), tout le sous-arbre
    correspondant est ignore - c'est ce qui empeche le dossier de revision
    (destination des photos "rangees" non destructivement) de se faire
    reindexer comme des photos actives neuves quand il est place a
    l'interieur meme du dossier scanne (bug trouve a l'audit : un doublon
    deplace vers ce dossier revenait polluer l'index au rescan suivant,
    annulant l'interet du rangement)."""
    exclude_resolved = None
    if exclude_dir is not None:
        try:
            exclude_resolved = Path(exclude_dir).resolve()
        except OSError:
            exclude_resolved = Path(exclude_dir)

    for dirpath, dirnames, filenames in os.walk(root):
        if exclude_resolved is not None:
            current = Path(dirpath).resolve()
            if current == exclude_resolved or exclude_resolved in current.parents:
                dirnames[:] = []
                continue
            # Empeche aussi de descendre DANS le dossier exclu depuis un
            # parent : on filtre les sous-dossiers enfants avant que
            # os.walk n'y entre, plutot que de le detecter apres coup.
            dirnames[:] = [
                d for d in dirnames
                if (Path(dirpath) / d).resolve() != exclude_resolved
            ]
        for name in filenames:
            path = Path(dirpath) / name
            if hashing.is_image_file(path):
                yield path


def scan_folder(
    root: Path, db: Database,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    exclude_dir: Optional[Path] = None,
) -> ScanResult:
    """Scanne `root` recursivement et indexe chaque photo dans `db`.

    `progress_callback(done, total, current_path)` est appele apres chaque
    fichier traite (y compris ceux ignores/en erreur) pour piloter une
    barre de progression GUI. `should_stop()` est verifie avant chaque
    fichier pour permettre une annulation cooperative depuis le thread
    appelant - un scan peut prendre plusieurs minutes sur une grosse
    bibliotheque, l'utilisateur doit pouvoir l'interrompre.

    `exclude_dir`, s'il est fourni, delimite un sous-arbre entierement
    ignore du parcours (typiquement le dossier de revision courant) - meme
    s'il se trouve a l'interieur de `root`, ce qui peut arriver si
    l'utilisateur l'a place la manuellement.

    En fin de scan, toute entree deja en base sous `root` mais dont le
    fichier n'a pas ete retrouve sur le disque est purgee (suppression
    manuelle depuis le dernier scan) - sans consequence sur les vraies
    photos, uniquement sur l'index reconstructible."""
    root = Path(root)
    files = list(_iter_image_files(root, exclude_dir=exclude_dir))
    result = ScanResult(total_found=len(files))
    seen_paths = set()
    stopped_early = False

    # La boucle de scan elle-meme est enveloppee dans un try/finally dont le
    # seul role est de garantir le commit du lot en cours avant qu'une
    # exception ne se propage hors de scan_folder - bug trouve a l'audit
    # (C4) : les erreurs de decodage d'image sont deja rattrapees ci-dessous
    # (cf. commentaire plus bas), mais une authentique erreur disque/SQLite
    # (ex. disque plein pendant un upsert ou un commit intermediaire, cf.
    # C5) n'etait, elle, interceptee nulle part dans cette fonction : elle
    # remontait directement jusqu'au commit final inconditionnel (ligne
    # `db.commit()` juste apres la boucle), qui n'etait donc jamais atteint.
    # Jusqu'a _COMMIT_BATCH_SIZE photos deja hachees et upsertees dans la
    # transaction SQLite en cours restaient alors non validees et etaient
    # perdues a la fermeture de la connexion, en plus de l'erreur elle-meme.
    # Le `finally` ci-dessous s'execute quelle que soit l'issue de la boucle
    # (fin normale, `break` sur arret demande, ou exception qui continue de
    # se propager ensuite) et valide systematiquement ce qui a deja ete
    # ecrit, sans jamais avaler l'exception d'origine.
    try:
        for index, path in enumerate(files, start=1):
            if should_stop is not None and should_stop():
                stopped_early = True
                break
            path_str = str(path)
            seen_paths.add(path_str)
            try:
                # os.stat(hashing.long_path(path)) plutot que path.stat() -
                # correctif M1 de l'audit : un chemin dont la longueur totale
                # depasse l'ancienne limite Windows MAX_PATH (260 caracteres)
                # est desormais reellement lu quand le systeme le permet, au
                # lieu de se contenter de l'echec gracieux deja en place
                # (capture OSError ci-dessous, inchangee pour tout le reste).
                stat = os.stat(hashing.long_path(path))
            except OSError as exc:
                result.errors.append((path_str, str(exc)))
                if progress_callback:
                    progress_callback(index, result.total_found, path_str)
                continue

            # Rehachage uniquement si taille OU date de modification a
            # change depuis le dernier scan connu - limite theorique mineure
            # assumee (B3 de l'audit) : un outil qui modifierait le contenu
            # d'un fichier en preservant intentionnellement sa taille ET son
            # mtime exacts (rare - certains outils de synchronisation/
            # edition EXIF en place) ne serait pas detecte comme change.
            # Compromis standard et raisonnable pour la performance d'un
            # rescan incremental sur une grosse bibliotheque - la plupart des
            # outils de deduplication incrementale font ce meme choix.
            existing = db.get_photo_by_path(path_str)
            if existing is not None and existing["size"] == stat.st_size and existing["mtime"] == stat.st_mtime:
                result.skipped_unchanged += 1
                if progress_callback:
                    progress_callback(index, result.total_found, path_str)
                continue

            try:
                sha = hashing.file_sha256(path)
                with hashing.open_image(path) as img:
                    width, height = img.size
                    taken_at = _extract_taken_at(img)
                    phash = hashing.compute_dhash(img)
            except Exception as exc:
                # Capture volontairement large (pas seulement
                # UnreadableImageError/OSError/ValueError) : Pillow peut lever,
                # selon le format et le type de corruption, des exceptions qui
                # n'heritent d'aucune de ces classes - notamment
                # PIL.Image.DecompressionBombError (en-tete corrompu ou piege
                # revendiquant des dimensions demesurees), qui herite directement
                # d'Exception. Avant ce correctif, un seul fichier dans cet etat
                # faisait avorter tout le scan sans rien committer du lot en
                # cours (bug trouve a l'audit) au lieu d'etre traite comme les
                # autres fichiers illisibles ci-dessus (errors, compteur inclus).
                result.errors.append((path_str, f"{type(exc).__name__}: {exc}"))
                if progress_callback:
                    progress_callback(index, result.total_found, path_str)
                continue

            # commit=False : le fsync disque est differe et groupe (voir le
            # commit periodique juste en dessous) plutot que paye a chaque
            # photo - mesure a l'audit, ce commit par ligne dominait le temps
            # d'un gros scan. Un commit final inconditionnel apres la boucle
            # (et un commit ici tous les _COMMIT_BATCH_SIZE upserts) garantit
            # qu'un arret/crash en cours de scan ne perd jamais plus qu'un lot
            # deja traite, jamais tout le travail depuis le debut du scan.
            db.upsert_photo(path_str, stat.st_size, stat.st_mtime, width, height, sha, phash, taken_at, commit=False)
            result.scanned += 1
            if result.scanned % _COMMIT_BATCH_SIZE == 0:
                db.commit()
            if progress_callback:
                progress_callback(index, result.total_found, path_str)
    finally:
        # Valide tout travail restant depuis le dernier commit groupe (moins
        # de _COMMIT_BATCH_SIZE photos depuis lors) - sans ce commit, ces
        # dernieres photos scannees resteraient dans une transaction jamais
        # validee et seraient perdues, que la boucle se soit terminee
        # normalement, ait ete interrompue via `should_stop`, ou qu'une
        # exception soit en train de se propager.
        db.commit()

    # Un scan interrompu (bouton "Arreter", fermeture de la fenetre) n'a vu
    # qu'une partie des fichiers reellement presents sur le disque - purger
    # "les chemins en base mais pas vus" traiterait a tort tout fichier pas
    # encore atteint comme disparu (bug trouve a l'audit : perte silencieuse
    # de notes/statuts deja attribues, et faux groupes de doublons au
    # prochain calcul). La purge n'a de sens que pour un scan complet.
    if not stopped_early:
        stale = db.list_paths_under(str(root)) - seen_paths
        if stale:
            db.delete_by_paths(list(stale))
            result.pruned = len(stale)

    return result
