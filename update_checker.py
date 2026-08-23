"""Verification de mise a jour : le flux d'Open Projects Lab d'abord, GitHub en repli.

Toujours execute en arriere-plan (voir start_update_check) : une verification
ratee (pas de connexion, serveur inaccessible...) ne doit JAMAIS empecher
l'application de demarrer ou de fonctionner normalement.

POURQUOI LE FLUX, ET POURQUOI UN REPLI
--------------------------------------
Jusqu'au 2026-08-23, cette verification interrogeait l'API GitHub depuis la
machine de l'utilisateur, sans authentification. GitHub plafonne alors a
60 requetes par heure et PAR ADRESSE IP : derriere un NAT partage (fournisseur
d'acces, entreprise, campus), ce quota est celui de tout le monde a la fois, et
la verification echouait EN SILENCE - l'utilisateur restait sur une vieille
version en croyant etre a jour.

`https://openprojectslab.com/api/v1/versions.json` est produit a chaque
publication depuis les releases GitHub, servi en fichier statique, sans quota,
et son contrat est fige (on y ajoute des champs, on n'en retire jamais - voir
l'ADR 009 d'Open Projects Lab). Il porte aussi la taille et l'empreinte SHA-256
du binaire, pour la suite.

Le repli sur GitHub reste : si le flux est injoignable, illisible, d'un schema
inconnu, ou ne connait pas cette application, on fait exactement ce qu'on
faisait avant. Rien n'est perdu par rapport a l'ancien comportement, et les
deux chemins aboutissent au meme resultat : le tag de la derniere release.

CE QUI EST ENVOYE : une requete GET, sans aucune donnee, vers le site d'Open
Projects Lab (et, en repli seulement, vers l'API GitHub). Aucune information
sur la machine n'est transmise au-dela de ce que toute requete HTTP contient.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import urllib.error
import urllib.request

REQUEST_TIMEOUT_SECONDS = 5

#: Le flux d'Open Projects Lab. Chemin FIGE (ADR 009) : une rupture s'appellerait
#: /api/v2/ et /api/v1/ continuerait d'etre servi.
FLUX_URL = "https://openprojectslab.com/api/v1/versions.json"
#: La version du SCHEMA que ce module sait lire. Un flux qui en annonce une
#: autre est refuse - on ne devine pas ce qu'on ne sait pas lire, on se replie.
FLUX_SCHEMA = 1

_ERREURS_RESEAU = (urllib.error.URLError, TimeoutError, ValueError, OSError)


def _parse_version(tag: str) -> tuple:
    """Convertit "v1.2.10" en (1, 2, 10) pour une comparaison numerique -
    une comparaison de chaines mettrait a tort "v1.10.0" avant "v1.9.0"."""
    numbers = re.findall(r"\d+", tag)
    return tuple(int(n) for n in numbers) if numbers else (0,)


def is_newer(remote_tag: str, current_version: str) -> bool:
    return _parse_version(remote_tag) > _parse_version(current_version)


def slug_de(repo: str) -> str:
    """"yoshines62000-alt/Coffre" -> "coffre" : la cle de l'application dans le
    flux est le nom du depot en minuscules. C'est la convention du catalogue
    d'Open Projects Lab (apps.json), verifiee pour les sept applications."""
    return repo.rsplit("/", 1)[-1].lower()


def _lire_json(url: str, repo: str, timeout: float, accept: str):
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"{repo} update-checker"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_from_flux(repo: str, timeout: float = REQUEST_TIMEOUT_SECONDS):
    """Renvoie l'entree de cette application dans le flux d'Open Projects Lab
    (un dict avec au moins "etiquette", "version", "binaire"), ou None si le
    flux est injoignable, illisible, d'un autre schema, ou ne la connait pas.

    TOUT echec rend None, jamais une exception : l'appelant se replie sur
    GitHub. Un flux partiel n'est pas "un peu" utilisable - soit l'entree est
    complete et du schema attendu, soit on ne s'y fie pas."""
    try:
        data = _lire_json(FLUX_URL, repo, timeout, "application/json")
    except _ERREURS_RESEAU:
        return None
    if not isinstance(data, dict) or data.get("schema") != FLUX_SCHEMA:
        return None
    entree = (data.get("applications") or {}).get(slug_de(repo))
    if not isinstance(entree, dict):
        return None
    etiquette = entree.get("etiquette")
    if not isinstance(etiquette, str) or not etiquette:
        return None
    return entree


def fetch_from_github(repo: str, timeout: float = REQUEST_TIMEOUT_SECONDS):
    """L'ancien chemin, inchange : le tag de la derniere release via l'API
    GitHub, ou None. Soumis au quota de 60 requetes/heure/IP - d'ou son role
    de REPLI, plus de premier choix."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        data = _lire_json(url, repo, timeout, "application/vnd.github+json")
    except _ERREURS_RESEAU:
        return None
    tag = data.get("tag_name") if isinstance(data, dict) else None
    return tag if isinstance(tag, str) and tag else None


def fetch_latest_release_tag(repo: str, timeout: float = REQUEST_TIMEOUT_SECONDS):
    """Renvoie le tag de la derniere release publiee (ex: "v1.0.8"), ou None
    si les DEUX chemins echouent. Le flux d'abord ; GitHub si le flux ne
    repond pas pour cette application.

    La signature et le resultat sont ceux d'avant la bascule : gui.py et les
    tests qui patchent cette fonction n'ont rien a savoir du flux."""
    entree = fetch_from_flux(repo, timeout)
    if entree is not None:
        return entree["etiquette"]
    return fetch_from_github(repo, timeout)


def start_update_check(current_version: str, repo: str, result_queue: "queue.Queue") -> None:
    """Lance la verification sur un thread separe (jamais sur le thread Tk)
    et depose UN SEUL message dans `result_queue` a la fin :
    ("up_to_date", tag), ("update_available", tag) ou ("check_failed", None).
    Meme mecanisme thread + queue.Queue + root.after(...) que le reste de
    cette suite d'outils (jamais de mutation directe d'un widget Tkinter
    depuis un thread autre que le thread principal)."""
    def worker():
        tag = fetch_latest_release_tag(repo)
        if tag is None:
            result_queue.put(("check_failed", None))
        elif is_newer(tag, current_version):
            result_queue.put(("update_available", tag))
        else:
            result_queue.put(("up_to_date", tag))

    threading.Thread(target=worker, daemon=True).start()
