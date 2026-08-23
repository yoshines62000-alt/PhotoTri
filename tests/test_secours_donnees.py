"""Le script de secours exporte une base creee par l'APPLICATION, sans elle.

Ce test ne connait pas le sens des tables : il cree la base par `db.Database`
(le vrai schema, les vraies migrations), remplit UNE ligne par table en
lisant `PRAGMA table_info` — les colonnes NOT NULL sans defaut recoivent une
valeur de leur type —, puis verifie que tout ressort, a l'identique, dans un
CSV par table et un JSON. Il est IDENTIQUE dans Enveloppe, TempoFacture et
PhotoTri, parce que le script l'est.
"""
import ast
import csv
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as dbmod
import secours_donnees as secours


def _valeur_pour(type_sql: str, i: int):
    t = (type_sql or "").upper()
    if "INT" in t:
        return i
    if "REAL" in t or "FLOA" in t or "DOUB" in t:
        return 1.5 * i
    if "BLOB" in t:
        return bytes([i % 256, 0xAB])
    return f"valeur-{i}"


def _remplir_une_ligne_par_table(chemin: Path) -> dict:
    """Insere une ligne dans CHAQUE table du schema reel, en respectant les
    NOT NULL sans defaut. Rend {table: {colonne: valeur inseree}}."""
    conn = sqlite3.connect(chemin)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")   # on teste l'export, pas le modele
        noms = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        inseres = {}
        for n, table in enumerate(noms, start=1):
            colonnes = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            valeurs = {}
            for cid, nom, typ, notnull, defaut, pk in colonnes:
                if pk and "INT" in (typ or "").upper():
                    continue                      # autoincrement : laisse SQLite faire
                if notnull and defaut is None:
                    valeurs[nom] = _valeur_pour(typ, n)
                elif nom.lower() == "name":
                    valeurs[nom] = f"ligne-{table}"
            if valeurs:
                cols = ", ".join(f'"{c}"' for c in valeurs)
                marques = ", ".join("?" for _ in valeurs)
                conn.execute(f'INSERT INTO "{table}" ({cols}) VALUES ({marques})', list(valeurs.values()))
            else:
                conn.execute(f'INSERT INTO "{table}" DEFAULT VALUES')
            inseres[table] = valeurs
        conn.commit()
        return inseres
    finally:
        conn.close()


class TestSecoursDonnees(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dossier = Path(self._tmp.name)
        self.chemin = self.dossier / "donnees.sqlite"
        base = dbmod.Database(self.chemin)       # le VRAI schema, par l'application
        try:
            base.close()
        except Exception:
            pass
        self.inseres = _remplir_une_ligne_par_table(self.chemin)

    def tearDown(self):
        self._tmp.cleanup()

    def test_toutes_les_tables_du_schema_reel_ressortent(self):
        tables = secours.lire_tables(self.chemin)
        self.assertEqual(set(tables), set(self.inseres), "une table du schema manque a l'export")
        for table, valeurs in self.inseres.items():
            self.assertGreaterEqual(len(tables[table]), 1, table)
            # La ligne insérée FIGURE dans l'export — sans exiger qu'elle soit
            # seule : certaines tables (settings de TempoFacture) sont
            # pré-remplies par l'application, et ma ligne y est la deuxième.
            attendu = {c: (v.hex() if isinstance(v, bytes) else v) for c, v in valeurs.items()}
            trouve = any(all(ligne.get(c) == a for c, a in attendu.items()) for ligne in tables[table])
            self.assertTrue(trouve or not attendu, f"la ligne insérée est absente de l'export de {table}")

    def test_un_csv_par_table_et_un_json(self):
        tables = secours.lire_tables(self.chemin)
        ecrits = secours.exporter(tables, self.dossier / "export")
        noms = sorted(p.name for p in ecrits)
        self.assertEqual(noms, sorted([f"{t}.csv" for t in tables] + ["donnees.json"]))
        table = next(iter(self.inseres))
        with open(self.dossier / "export" / f"{table}.csv", encoding="utf-8", newline="") as f:
            lignes = list(csv.DictReader(f))
        self.assertEqual(len(lignes), 1)
        relu = json.loads((self.dossier / "export" / "donnees.json").read_text(encoding="utf-8"))
        self.assertEqual(set(relu), set(tables))

    def test_la_base_elle_meme_n_est_pas_modifiee(self):
        # LA garantie qui compte : le fichier .sqlite est intact au bit pres.
        # SQLite peut creer ses compagnons -wal/-shm en ouvrant une base WAL en
        # lecture (il les gere lui-meme) ; ce qu'on interdit, c'est toute
        # modification de la base et l'apparition d'un fichier ETRANGER.
        avant = hashlib.sha256(self.chemin.read_bytes()).hexdigest()
        secours.lire_tables(self.chemin)
        self.assertEqual(hashlib.sha256(self.chemin.read_bytes()).hexdigest(), avant,
                         "le fichier de base a ete modifie")
        etrangers = [p.name for p in self.dossier.iterdir()
                     if p.name != self.chemin.name and not p.name.startswith(self.chemin.name)]
        self.assertEqual(etrangers, [], f"un fichier etranger est apparu : {etrangers}")

    def test_un_blob_est_rendu_en_hexadecimal(self):
        # Aucun schema d'application n'a de colonne BLOB aujourd'hui ; on en
        # fabrique une pour prouver que le rendu hex fonctionne — un CSV ne
        # transporte pas d'octets bruts, et un JSON non plus.
        base = self.dossier / "avec_blob.sqlite"
        conn = sqlite3.connect(base)
        conn.execute("CREATE TABLE fichiers (id INTEGER PRIMARY KEY, contenu BLOB)")
        conn.execute("INSERT INTO fichiers (contenu) VALUES (?)", (bytes([0xDE, 0xAD, 0xBE, 0xEF]),))
        conn.commit()
        conn.close()
        tables = secours.lire_tables(base)
        self.assertEqual(tables["fichiers"][0]["contenu"], "deadbeef")
        # Et l'export JSON reste du JSON valide (pas d'octets bruts dedans).
        secours.exporter(tables, self.dossier / "blob-export")
        relu = json.loads((self.dossier / "blob-export" / "donnees.json").read_text(encoding="utf-8"))
        self.assertEqual(relu["fichiers"][0]["contenu"], "deadbeef")

    def test_un_fichier_qui_n_est_pas_une_base_est_refuse(self):
        faux = self.dossier / "faux.sqlite"
        faux.write_bytes(b"ceci n'est pas une base SQLite, juste du texte " * 20)
        with self.assertRaises(secours.DonneesIllisibles):
            secours.lire_tables(faux)
        self.assertEqual(secours.main([str(faux)]), 2)

    def test_un_fichier_absent_est_refuse(self):
        with self.assertRaises(secours.DonneesIllisibles):
            secours.lire_tables(self.dossier / "inexistant.sqlite")

    def test_la_ligne_de_commande_exporte(self):
        code = secours.main([str(self.chemin), "--dossier", str(self.dossier / "cli")])
        self.assertEqual(code, 0)
        self.assertTrue((self.dossier / "cli" / "donnees.json").exists())


class TestAutonomie(unittest.TestCase):
    def test_le_script_n_importe_que_la_bibliotheque_standard(self):
        source = (Path(__file__).resolve().parent.parent / "secours_donnees.py").read_text(encoding="utf-8")
        modules = set()
        for noeud in ast.walk(ast.parse(source)):
            if isinstance(noeud, ast.Import):
                modules |= {a.name.split(".")[0] for a in noeud.names}
            elif isinstance(noeud, ast.ImportFrom) and noeud.module:
                modules.add(noeud.module.split(".")[0])
        hors = sorted(m for m in modules if m not in sys.stdlib_module_names)
        self.assertEqual(hors, [], f"dependances hors bibliotheque standard : {hors}")


if __name__ == "__main__":
    unittest.main()
