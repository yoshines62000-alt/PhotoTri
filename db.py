"""Couche donnees de PhotoTri (SQLite, sans dependance externe).

La base sert d'INDEX reconstructible, pas de source de verite : les vraies
photos restent sur le disque, a leur emplacement d'origine. Si le fichier
`photos.sqlite` est perdu, un simple rescan le reconstruit entierement -
c'est pourquoi, contrairement a Coffre/TempoFacture, ce module n'offre pas
de fonction de sauvegarde dediee : il n'y a rien d'irremplacable ici.
"""

from __future__ import annotations

import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def phash_to_sqlite(value: int) -> int:
    """Convertit un dHash 64 bits NON SIGNE (0 .. 2**64-1, voir hashing.py)
    vers un entier signe 64 bits que sqlite3 accepte d'inserer tel quel.

    Le driver sqlite3 de Python leve OverflowError des qu'on tente de lier
    un entier hors de l'intervalle signe 64 bits (-2**63 .. 2**63-1) : la
    moitie des hachages possibles (bit de poids fort a 1) declenchait donc
    un plantage systematique a l'insertion avant ce correctif. struct
    pack/unpack Q->q reinterprete le meme motif binaire de 64 bits sans
    perte, contrairement a un simple modulo/soustraction qui serait plus
    fragile a relire correctement."""
    return struct.unpack("<q", struct.pack("<Q", value))[0]


def phash_from_sqlite(value: int) -> int:
    """Operation inverse de phash_to_sqlite : reconstruit l'entier non
    signe original a partir de la valeur signee stockee en base."""
    return struct.unpack("<Q", struct.pack("<q", value))[0]


class Database:
    """Enveloppe fine autour de sqlite3 : une connexion, un schema, des
    methodes CRUD explicites. Pas d'ORM."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self._create_schema()

    def close(self) -> None:
        self.conn.close()

    def _create_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            width INTEGER,
            height INTEGER,
            sha256 TEXT NOT NULL,
            phash INTEGER NOT NULL,
            taken_at TEXT,
            rating INTEGER NOT NULL DEFAULT 0 CHECK (rating BETWEEN 0 AND 5),
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'moved')),
            moved_to TEXT,
            scanned_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_photos_sha256 ON photos(sha256);
        CREATE INDEX IF NOT EXISTS idx_photos_phash ON photos(phash);
        CREATE INDEX IF NOT EXISTS idx_photos_status ON photos(status);
        """)
        self.conn.commit()

    # -- ecriture ---------------------------------------------------------------

    def upsert_photo(
        self, path: str, size: int, mtime: float, width: Optional[int], height: Optional[int],
        sha256: str, phash: int, taken_at: Optional[str],
    ) -> int:
        """Cree OU met a jour l'entree d'une photo (cle unique : `path`).
        Une mise a jour ne touche JAMAIS `rating`/`status`/`moved_to` : ce
        sont des annotations de l'utilisateur, pas des donnees derivees du
        fichier, elles doivent survivre a un rescan qui detecte juste que
        le fichier a change de taille/date."""
        self.conn.execute(
            """INSERT INTO photos (path, size, mtime, width, height, sha256, phash, taken_at, scanned_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                   size = excluded.size, mtime = excluded.mtime,
                   width = excluded.width, height = excluded.height,
                   sha256 = excluded.sha256, phash = excluded.phash,
                   taken_at = excluded.taken_at, scanned_at = excluded.scanned_at""",
            (path, size, mtime, width, height, sha256, phash_to_sqlite(phash), taken_at, _now_iso()),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT id FROM photos WHERE path = ?", (path,)).fetchone()
        return row["id"]

    def set_rating(self, photo_id: int, rating: int) -> None:
        if not 0 <= rating <= 5:
            raise ValueError("La note doit etre comprise entre 0 et 5.")
        self.conn.execute("UPDATE photos SET rating = ? WHERE id = ?", (rating, photo_id))
        self.conn.commit()

    def mark_moved(self, photo_id: int, moved_to: str) -> None:
        self.conn.execute(
            "UPDATE photos SET status = 'moved', moved_to = ? WHERE id = ?", (moved_to, photo_id),
        )
        self.conn.commit()

    def delete_by_paths(self, paths: list) -> None:
        """Supprime les entrees dont le chemin ne correspond plus a aucun
        fichier reel (utilise par scanner.py pour purger les photos
        deplacees/supprimees manuellement hors de PhotoTri depuis le
        dernier scan)."""
        self.conn.executemany("DELETE FROM photos WHERE path = ?", [(p,) for p in paths])
        self.conn.commit()

    # -- lecture ------------------------------------------------------------------

    def get_photo(self, photo_id: int):
        return self.conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()

    def get_photo_by_path(self, path: str):
        return self.conn.execute("SELECT * FROM photos WHERE path = ?", (path,)).fetchone()

    def list_active_photos(self) -> list:
        """Photos encore a leur emplacement d'origine (jamais deplacees par
        PhotoTri) - c'est sur cet ensemble que le regroupement de doublons
        doit toujours travailler, pour ne jamais reproposer une photo deja
        traitee lors d'un scan precedent."""
        return self.conn.execute("SELECT * FROM photos WHERE status = 'active' ORDER BY path").fetchall()

    def list_paths_under(self, root: str) -> set:
        """Tous les chemins actuellement en base sous `root` (prefixe de
        dossier) - utilise par scanner.py pour detecter, par difference
        avec la liste reelle sur disque, les fichiers supprimes/deplaces
        manuellement depuis le dernier scan et purger leur entree perimee."""
        like_pattern = root.rstrip("\\/") + "%"
        rows = self.conn.execute("SELECT path FROM photos WHERE path LIKE ?", (like_pattern,)).fetchall()
        return {row["path"] for row in rows}

    def count_photos(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM photos").fetchone()["n"]
