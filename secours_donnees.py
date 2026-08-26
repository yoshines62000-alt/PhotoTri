#!/usr/bin/env python3
"""Secours : exporter ses donnees SANS l'application, avec Python seul.

    python secours_donnees.py chemin\vers\fichier.sqlite            (liste les tables)
    python secours_donnees.py chemin\vers\fichier.sqlite --dossier export

Ecrit, dans le dossier choisi, un CSV PAR TABLE et un JSON de l'ensemble —
lisibles par un tableur ou n'importe quel outil. Le fichier d'origine est
ouvert EN LECTURE SEULE : il n'est jamais modifie, et rien n'est cree a cote.

POURQUOI CE FICHIER EXISTE
--------------------------
Ces applications rangent tout dans un seul fichier SQLite, un format public
que Python sait lire sans rien installer. Si un jour l'executable ne se lance
plus — Windows qui bloque, un antivirus, une machine changee —, vos donnees ne
sont pas perdues : ce script les sort telles quelles. Il ne connait pas le
sens des colonnes (c'est l'application qui le connait) ; il les exporte
toutes, avec leurs noms, et c'est a vous de retrouver les votres.

Ce fichier est IDENTIQUE dans les applications Open Projects Lab qui
s'appuient sur SQLite (Enveloppe, TempoFacture, PhotoTri) : bibliotheque
standard seule, aucune dependance.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath


class DonneesIllisibles(Exception):
    """Pas un fichier SQLite, ou un fichier altere."""


#: Caracteres par lesquels Excel/LibreOffice reconnaissent une FORMULE. Meme
#: liste que le _csv_safe des exports de l'application (csv_export.py,
#: csv_transactions.py) : cet export-ci ecrit les memes donnees, pour le meme
#: tableur, il doit offrir la meme protection (CWE-1236, audit du 2026-08-26).
_PREFIXES_FORMULE = ("=", "+", "-", "@", "\t", "\r")


def _csv_sur(valeur):
    """Neutralise l'injection de formule CSV : une cellule texte commencant par
    =, +, - ou @ est prefixee d'une apostrophe, ce qui force le tableur a la
    lire comme du texte. Les valeurs non-str (nombres, None) sont intactes."""
    if isinstance(valeur, str) and valeur.startswith(_PREFIXES_FORMULE):
        return "'" + valeur
    return valeur


def _nom_de_fichier_sur(nom: str, rang: int) -> str:
    """Un nom de table est une chaine ARBITRAIRE : rien n'empeche une base
    d'en porter une nommee "../../evade" ou "co:n*". Le nom venant du fichier
    lu — donc potentiellement d'un fichier recu de l'exterieur, ce qui est
    precisement le cas d'usage d'un outil de secours —, on n'ecrit jamais
    directement sous ce nom : on n'en garde que la derniere composante, on
    remplace ce que Windows refuse, et on retombe sur « table-N » s'il ne
    reste rien. Mesure a l'audit du 2026-08-26 : sans cela, une table nommee
    "../../evade" faisait ecrire deux dossiers au-dessus de celui demande."""
    base = PurePosixPath(PureWindowsPath(nom).name).name
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(" .")
    return base if base else f"table-{rang}"


def lire_tables(chemin: Path) -> dict:
    """Rend {table: [ {colonne: valeur, ...}, ... ]} pour TOUTES les tables
    utilisateur du fichier, ouvert en lecture seule. Les valeurs binaires
    (BLOB) sont rendues en hexadecimal : un CSV ne transporte pas d'octets."""
    chemin = Path(chemin)
    if not chemin.is_file():
        raise DonneesIllisibles(f"fichier introuvable : {chemin}")
    # mode=ro : le FICHIER n'est jamais modifie. SQLite peut creer a cote ses
    # compagnons -wal/-shm si la base est en journal WAL — c'est lui qui les
    # gere, la base elle-meme reste intacte au bit pres (le test le prouve par
    # empreinte). On ne passe PAS immutable=1 : une base fermee salement laisse
    # ses dernieres ecritures dans -wal, et c'est precisement le cas d'un
    # secours ; immutable les ignorerait et lirait des donnees perimees.
    conn = sqlite3.connect(f"file:{chemin.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        try:
            noms = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        except sqlite3.DatabaseError as exc:
            raise DonneesIllisibles(f"ce fichier n'est pas une base SQLite lisible ({exc})") from exc
        tables = {}
        for nom in noms:
            lignes = []
            # Le nom vient de sqlite_master, pas d'une saisie ; les guillemets
            # doubles sont l'echappement SQL d'un identifiant.
            for ligne in conn.execute(f'SELECT * FROM "{nom.replace(chr(34), chr(34) * 2)}"'):
                lignes.append({k: (v.hex() if isinstance(v, (bytes, memoryview)) else v) for k, v in dict(ligne).items()})
            tables[nom] = lignes
        return tables
    finally:
        conn.close()


def exporter(tables: dict, dossier: Path) -> list:
    """Un CSV par table + donnees.json. Rend la liste des fichiers ecrits."""
    dossier = Path(dossier)
    dossier.mkdir(parents=True, exist_ok=True)
    ecrits = []
    vus = set()
    for rang, (nom, lignes) in enumerate(tables.items(), start=1):
        fichier = _nom_de_fichier_sur(nom, rang)
        while fichier.lower() in vus:      # deux tables peuvent se reduire au meme nom
            fichier = f"{fichier}-{rang}"
        vus.add(fichier.lower())
        cible = dossier / f"{fichier}.csv"
        with open(cible, "w", encoding="utf-8", newline="") as f:
            colonnes = list(lignes[0].keys()) if lignes else []
            w = csv.DictWriter(f, fieldnames=colonnes)
            w.writeheader()
            for ligne in lignes:
                w.writerow({k: _csv_sur(v) for k, v in ligne.items()})
        ecrits.append(cible)
    cible = dossier / "donnees.json"
    cible.write_text(json.dumps(tables, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    ecrits.append(cible)
    return ecrits


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Exporte un fichier SQLite d'Open Projects Lab en CSV et JSON, sans l'application.")
    p.add_argument("fichier", help="le fichier .sqlite de l'application")
    p.add_argument("--dossier", metavar="DOSSIER", help="ou ecrire l'export (un CSV par table + donnees.json)")
    a = p.parse_args(argv)
    try:
        tables = lire_tables(Path(a.fichier))
    except DonneesIllisibles as exc:
        print(f"secours_donnees : {exc}", file=sys.stderr)
        return 2
    for nom, lignes in tables.items():
        print(f"  {nom:28s} {len(lignes)} ligne(s)")
    if a.dossier:
        for f in exporter(tables, Path(a.dossier)):
            print(f"  ecrit : {f}")
    else:
        print("Pour tout exporter : --dossier DOSSIER")
    return 0


if __name__ == "__main__":
    sys.exit(main())
