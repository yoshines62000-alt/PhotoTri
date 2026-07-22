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


# Migrations de schema, dans l'ORDRE : chaque fonction recoit la connexion
# sqlite3 (deja ouverte, hors de toute transaction explicite) et doit amener
# le schema de la version correspondant a son INDEX dans cette liste a la
# version suivante - ex. _MIGRATIONS[0] fait passer de la version 0 (base
# jamais migree) a la version 1. Vide pour l'instant : le schema n'a jamais
# change depuis la creation du projet (verifie a l'audit via
# `git log --oneline -- db.py`, constat E3), cette liste n'existe qu'en
# PREPARATION de la premiere evolution future du schema. Quand ce jour
# viendra : ajouter la nouvelle colonne/table a `_create_schema` ci-dessous
# (pour que les bases NEUVES la recoivent directement) ET ajouter ici une
# fonction de migration correspondante, typiquement un `ALTER TABLE ... ADD
# COLUMN ...` (pour que les bases EXISTANTES d'utilisateurs deja installes
# la recoivent aussi a la prochaine ouverture) - les deux chemins doivent
# aboutir au meme schema final.
_MIGRATIONS: list = []

# Version courante du schema = nombre de migrations enregistrees ci-dessus
# (toujours en phase avec _MIGRATIONS, jamais a incrementer manuellement).
# Comparee au PRAGMA user_version reellement stocke dans le fichier sqlite a
# chaque ouverture (_apply_migrations) : risque prospectif identifie a
# l'audit (E3) - jusqu'ici, `_create_schema` ci-dessous n'utilisait que
# `CREATE TABLE IF NOT EXISTS` (no-op sur une table deja existante), sans
# aucun filet si une future version ajoutait une colonne : les bases
# utilisateur deja creees par une version anterieure ne l'auraient jamais
# recue, et le premier INSERT/UPDATE la referencant aurait echoue avec
# `sqlite3.OperationalError: no such column`.
SCHEMA_VERSION = len(_MIGRATIONS)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Amene `conn` du schema qu'elle a reellement (PRAGMA user_version,
    0 par defaut pour toute base jamais migree - y compris une base creee
    avant l'introduction de ce mecanisme) au schema courant (SCHEMA_VERSION),
    en executant dans l'ordre les seules migrations manquantes. No-op total
    (aucune lecture supplementaire, aucun commit) sur une base deja a jour -
    c'est le cas de TOUTES les bases aujourd'hui, `_MIGRATIONS` etant encore
    vide (E3 est un risque prospectif, pas un bug actuel)."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for migration in _MIGRATIONS[current:SCHEMA_VERSION]:
        migration(conn)
    if current != SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")
        conn.commit()


class Database:
    """Enveloppe fine autour de sqlite3 : une connexion, un schema, des
    methodes CRUD explicites. Pas d'ORM."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        # busy_timeout POSE EN PREMIER, avant journal_mode=WAL : si une autre
        # connexion tient deja un verrou sur le fichier au moment de
        # l'ouverture (ex. deux instances de PhotoTri.exe lancees a
        # quelques millisecondes d'ecart par un double-clic), c'est la mise
        # en place du mode WAL elle-meme qui a besoin d'attendre ce verrou -
        # pose apres, busy_timeout arrive trop tard pour la proteger et
        # PRAGMA journal_mode=WAL peut lever "database is locked" (course
        # reproduite a l'audit). Une fois busy_timeout actif, la connexion
        # attend jusqu'a 10s au lieu d'echouer immediatement, y compris pour
        # ce tout premier PRAGMA. WAL : les lecteurs (ex: l'UI qui affiche
        # des groupes) ne bloquent plus l'ecrivain (le thread de scan) et
        # reciproquement. busy_timeout genereux en complement (au lieu de se
        # fier au seul defaut de sqlite3.connect) : defense en profondeur
        # si jamais un futur chemin de code venait a ouvrir deux connexions
        # actives en meme temps.
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_schema()
        # Amene une base PRE-EXISTANTE (creee par une version anterieure) au
        # schema courant si necessaire - voir _apply_migrations/E3. Applique
        # APRES _create_schema : sur une base neuve, la table est deja creee
        # dans son etat le plus recent (rien a migrer, simple marquage du
        # numero de version) ; sur une base existante, _create_schema est un
        # no-op (CREATE TABLE IF NOT EXISTS) et c'est cette etape qui fait le
        # vrai travail de mise a niveau.
        _apply_migrations(self.conn)

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
        CREATE INDEX IF NOT EXISTS idx_photos_status ON photos(status);
        """)
        self.conn.commit()

    # -- ecriture ---------------------------------------------------------------

    def upsert_photo(
        self, path: str, size: int, mtime: float, width: Optional[int], height: Optional[int],
        sha256: str, phash: int, taken_at: Optional[str],
        commit: bool = True,
    ) -> int:
        """Cree OU met a jour l'entree d'une photo (cle unique : `path`).
        Une mise a jour ne touche JAMAIS `rating`/`status`/`moved_to` : ce
        sont des annotations de l'utilisateur, pas des donnees derivees du
        fichier, elles doivent survivre a un rescan qui detecte juste que
        le fichier a change de taille/date.

        `commit=False` laisse l'ecriture dans la transaction SQLite en
        cours sans la valider (fsync) immediatement - utilise par
        scanner.py pour grouper les commits d'un scan en masse (voir
        `Database.commit`) plutot que de payer un fsync disque a chaque
        photo. Par defaut (True) le comportement est inchange : chaque
        appel isole reste immediatement durable, ce qui garde tous les
        autres appelants (tests, code futur) surs sans y penser."""
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
        if commit:
            self.conn.commit()
        row = self.conn.execute("SELECT id FROM photos WHERE path = ?", (path,)).fetchone()
        return row["id"]

    def commit(self) -> None:
        """Valide (fsync) la transaction SQLite en cours. A appeler apres
        une serie d'`upsert_photo(..., commit=False)` - typiquement par lot
        pendant un scan - pour grouper les ecritures disque sans perdre le
        travail deja fait si le processus s'arrete entre deux lots."""
        self.conn.commit()

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
        """Tous les chemins actuellement en base sous `root` (dossier) -
        utilise par scanner.py pour detecter, par difference avec la liste
        reelle sur disque, les fichiers supprimes/deplaces manuellement
        depuis le dernier scan et purger leur entree perimee.

        Le motif LIKE echappe les caracteres speciaux `%`/`_` presents dans
        `root` lui-meme ET exige une frontiere de separateur de chemin apres
        le prefixe - bug trouve a l'audit : sans ces deux garde-fous,
        `D:\\Photos` matchait aussi `D:\\PhotosBackup` (prefixe partiel, pas
        un sous-dossier) et `D:\\Vacances_2024` matchait aussi
        `D:\\Vacances-2024` (le `_` de LIKE est un joker un-caractere-
        quelconque) - un rescan purgeait alors de l'index des photos
        totalement etrangeres au dossier scanne."""
        root = root.rstrip("\\/")
        escaped = root.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        # Le separateur ajoute ici doit LUI AUSSI etre une sequence d'echappement
        # valide ("\\\\" = un backslash litteral echappe), pas juste un backslash
        # brut : sous ESCAPE '\', un backslash brut juste avant "%" transformerait
        # ce "%" en caractere litteral au lieu du joker attendu, empechant le
        # motif de matcher quoi que ce soit sous le dossier.
        pattern = escaped + "\\\\" + "%"
        rows = self.conn.execute(
            "SELECT path FROM photos WHERE path LIKE ? ESCAPE '\\'", (pattern,),
        ).fetchall()
        return {row["path"] for row in rows}

    def count_photos(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM photos").fetchone()["n"]
