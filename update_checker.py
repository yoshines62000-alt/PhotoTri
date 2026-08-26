"""Verification de mise a jour : le flux d'Open Projects Lab d'abord, GitHub en repli.
Et, quand le flux a repondu, TELECHARGEMENT VERIFIE du binaire par l'empreinte.

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
du binaire.

Le repli sur GitHub reste : si le flux est injoignable, illisible, d'un schema
inconnu, ou ne connait pas cette application, on fait exactement ce qu'on
faisait avant. Rien n'est perdu par rapport a l'ancien comportement.

POURQUOI TELECHARGER ICI PLUTOT QUE D'OUVRIR LA PAGE GITHUB
-----------------------------------------------------------
Avant, « Telecharger » ouvrait la page des releases dans le navigateur ; la
verification de l'empreinte SHA-256 etait laissee a l'utilisateur — qui ne la
faisait pas. Quand le flux a repondu, on connait l'adresse du binaire, sa
taille et son empreinte : on peut donc le telecharger NOUS-MEMES, calculer
l'empreinte au fil de l'eau, et REFUSER le fichier si elle differe. Un
telechargement verifie par le programme vaut mieux qu'une consigne que
personne ne suit.

Trois limites, tenues par le code :
  - on ne telecharge que depuis https://github.com/... : un flux compromis ne
    fera pas chercher un binaire ailleurs ;
  - un fichier dont l'empreinte ou la taille differe est SUPPRIME, et on le dit ;
    on n'ouvre rien, on ne propose pas de l'executer quand meme ;
  - le fichier telecharge n'est JAMAIS execute par ce module : il est depose
    dans le dossier Telechargements, le dossier est ouvert, l'utilisateur
    decide. Sans flux (repli GitHub), on ouvre la page comme avant.

CE QUI EST ENVOYE : des requetes GET, sans aucune donnee, vers le site d'Open
Projects Lab et vers GitHub. Aucune information sur la machine n'est transmise
au-dela de ce que toute requete HTTP contient.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

REQUEST_TIMEOUT_SECONDS = 5
#: Delai entre deux lectures pendant un telechargement (pas un delai total : un
#: binaire de 16 Mo sur une connexion lente a le droit de prendre son temps,
#: une connexion qui ne repond plus pendant 30 s n'a pas le droit de bloquer).
DOWNLOAD_TIMEOUT_SECONDS = 30
#: Taille des morceaux lus : assez gros pour ne pas ralentir, assez petit pour
#: que l'empreinte se calcule au fil de l'eau sans tout garder en memoire.
TAILLE_MORCEAU = 64 * 1024

#: Plafond de taille annoncee. La borne BASSE (taille <= 0) ne suffisait pas :
#: le controle de telechargement ne se declenche qu'en DEPASSANT la taille
#: annoncee, donc l'annoncer enorme le desarme et un flux compromis pouvait
#: faire ecrire jusqu'a saturation du disque (mesure a l'audit du 2026-08-26 :
#: 10**15 octets acceptes sans broncher). 512 Mio laisse une marge confortable
#: - le plus gros binaire publie de la suite tient largement en dessous.
TAILLE_MAX_OCTETS = 512 * 1024 * 1024
#: Le flux d'Open Projects Lab. Chemin FIGE (ADR 009) : une rupture s'appellerait
#: /api/v2/ et /api/v1/ continuerait d'etre servi.
FLUX_URL = "https://openprojectslab.com/api/v1/versions.json"
#: La version du SCHEMA que ce module sait lire. Un flux qui en annonce une
#: autre est refuse - on ne devine pas ce qu'on ne sait pas lire, on se replie.
FLUX_SCHEMA = 1
#: Les seuls hotes depuis lesquels un binaire peut etre telecharge. Les sept
#: applications sont publiees sur GitHub ; un flux qui designerait une autre
#: adresse serait un flux compromis, pas une nouveaute.
HOTES_AUTORISES = ("github.com", "objects.githubusercontent.com")

_ERREURS_RESEAU = (urllib.error.URLError, TimeoutError, ValueError, OSError)

#: L'entree du flux obtenue lors de la derniere verification, par depot. C'est
#: ce qui permet au clic « Telecharger » de savoir QUOI telecharger et QUELLE
#: empreinte attendre. None (ou absent) = la verification est passee par GitHub,
#: on n'a qu'un tag : le clic ouvre la page comme avant.
_DERNIERE_ENTREE: dict = {}
_TELECHARGEMENTS_EN_COURS: set = set()


class TelechargementInvalide(Exception):
    """Le fichier recu n'est pas celui annonce (empreinte, taille, adresse)."""


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

    Effet de bord voulu : l'entree du flux (ou None) est memorisee pour ce
    depot, afin que le clic « Telecharger » sache quoi verifier.

    La signature et le resultat sont ceux d'avant la bascule : gui.py et les
    tests qui patchent cette fonction n'ont rien a savoir du flux."""
    entree = fetch_from_flux(repo, timeout)
    _DERNIERE_ENTREE[repo] = entree
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


# --- telechargement verifie -------------------------------------------------

def dossier_telechargements() -> Path:
    """Le dossier Telechargements de l'utilisateur - son nom physique est
    « Downloads » quelle que soit la langue de Windows. A defaut, le dossier
    personnel : on ne cree pas de dossier chez l'utilisateur sans le lui dire."""
    candidat = Path.home() / "Downloads"
    return candidat if candidat.is_dir() else Path.home()


def _verifier_binaire_annonce(entree: dict, repo: str = "") -> tuple:
    """Extrait (url, nom, taille, sha256) de l'entree du flux, ou leve
    TelechargementInvalide si l'un des quatre ne convient pas. On REFUSE avant
    la moindre requete : une adresse hors GitHub, une empreinte qui n'en est
    pas une, une taille absurde - rien de tout cela ne merite un octet.

    `repo` (ex. "yoshines62000-alt/Coffre"), quand il est fourni, restreint
    l'adresse aux releases de CE depot et plus seulement a l'hote github.com.
    Sans lui, un flux compromis pouvait designer
    github.com/<attaquant>/depot/releases/download/... : une adresse GitHub
    parfaitement legitime, servant un binaire arbitraire - et l'empreinte
    attendue venant du MEME flux, elle ne discriminait rien (constat de
    l'audit du 2026-08-26). Le controle passe de « chez GitHub » a « chez
    moi ». Il ne s'applique qu'a github.com : objects.githubusercontent.com,
    ou GitHub redirige ses telechargements, a une arborescence opaque qu'on
    ne peut pas rattacher a un depot - ce cas reste couvert par le seul
    controle d'hote."""
    binaire = entree.get("binaire") if isinstance(entree, dict) else None
    if not isinstance(binaire, dict):
        raise TelechargementInvalide("le flux ne decrit aucun binaire")
    url, nom = binaire.get("url"), binaire.get("nom")
    taille, sha = binaire.get("taille_octets"), binaire.get("sha256")
    if not isinstance(url, str) or not isinstance(nom, str) or not nom:
        raise TelechargementInvalide("adresse ou nom de fichier manquant")
    parties = urllib.parse.urlsplit(url)
    hote = (parties.hostname or "").lower()
    if parties.scheme != "https" or hote not in HOTES_AUTORISES:
        raise TelechargementInvalide(f"adresse refusee : {url}")
    if repo and hote == "github.com" and not parties.path.startswith(f"/{repo}/releases/download/"):
        raise TelechargementInvalide(f"adresse hors des releases de {repo} : {url}")
    if not isinstance(taille, int) or isinstance(taille, bool) or taille <= 0:
        raise TelechargementInvalide("taille annoncee absente ou absurde")
    if taille > TAILLE_MAX_OCTETS:
        raise TelechargementInvalide(
            f"taille annoncee absurde : {taille} octets, maximum {TAILLE_MAX_OCTETS}")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
        raise TelechargementInvalide("empreinte SHA-256 annoncee invalide")
    # Le nom vient du flux : on n'en garde que la derniere composante, jamais
    # un chemin - `../` dans un nom de fichier n'a rien a faire ici.
    return url, Path(nom).name, taille, sha.lower()


def _chemin_libre(dossier: Path, nom: str, sha: str) -> Path:
    """`nom`, ou `nom (2)`, `nom (3)`... si un AUTRE fichier porte deja ce nom.
    Un fichier identique (meme empreinte) est reutilise tel quel."""
    cible = dossier / nom
    n = 1
    while cible.exists():
        if _sha256_de(cible) == sha:
            return cible
        n += 1
        cible = dossier / f"{Path(nom).stem} ({n}){Path(nom).suffix}"
    return cible


def _sha256_de(chemin: Path) -> str:
    h = hashlib.sha256()
    with open(chemin, "rb") as f:
        for morceau in iter(lambda: f.read(TAILLE_MORCEAU), b""):
            h.update(morceau)
    return h.hexdigest()


def telecharger_et_verifier(entree: dict, dossier: Path | None = None,
                            timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
                            repo: str = "") -> Path:
    """Telecharge le binaire decrit par `entree` dans `dossier`, en calculant
    l'empreinte SHA-256 au fil de l'eau, et ne le garde QUE si taille et
    empreinte sont exactement celles annoncees. Renvoie le chemin du fichier.

    Leve TelechargementInvalide (fichier partiel supprime) si quoi que ce soit
    differe, et laisse remonter les erreurs reseau. Ne lance jamais le fichier."""
    url, nom, taille, sha = _verifier_binaire_annonce(entree, repo)
    dossier = Path(dossier) if dossier is not None else dossier_telechargements()
    final = _chemin_libre(dossier, nom, sha)
    if final.exists():        # deja la, deja verifie par _chemin_libre
        return final
    partiel = dossier / (nom + ".partiel")
    h = hashlib.sha256()
    recu = 0
    # slug_de() attend un DEPOT ("auteur/Coffre" -> "coffre") ; l'appliquer a
    # une URL donnait un User-Agent "coffre.exe update-checker" (mesure a
    # l'audit). Le nom du depot n'est pas toujours connu ici : un libelle
    # constant dit la meme chose et ne peut pas deriver.
    request = urllib.request.Request(url, headers={"User-Agent": "Open Projects Lab update-checker"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reponse, open(partiel, "wb") as sortie:
            for morceau in iter(lambda: reponse.read(TAILLE_MORCEAU), b""):
                recu += len(morceau)
                if recu > taille:
                    # Plus gros qu'annonce : inutile d'attendre la fin pour savoir
                    # que ce n'est pas le bon fichier.
                    raise TelechargementInvalide(f"plus gros qu'annonce ({taille} octets)")
                h.update(morceau)
                sortie.write(morceau)
        if recu != taille:
            raise TelechargementInvalide(f"taille recue {recu}, annoncee {taille}")
        if h.hexdigest() != sha:
            raise TelechargementInvalide("l'empreinte SHA-256 du fichier recu differe de celle annoncee")
    except BaseException:
        try:
            partiel.unlink()
        except OSError:
            pass
        raise
    os.replace(partiel, final)
    return final


def ouvrir_mise_a_jour(repo: str, releases_url: str, var, planifier, dossier: Path | None = None):
    """Ce que fait le clic sur « Telecharger » :
      - si la derniere verification est passee par le FLUX : telecharge le
        binaire en arriere-plan, verifie son empreinte, ouvre le dossier ;
      - sinon (repli GitHub, aucune empreinte connue) : ouvre la page des
        releases, comme avant.

    `var` est la StringVar du libelle d'etat, `planifier` la fonction
    `root.after` de l'application : tout ce qui touche a Tk passe par elle,
    depuis le thread principal. Renvoie le thread lance (ou None), pour les
    tests. Un second clic pendant un telechargement ne relance rien."""
    entree = _DERNIERE_ENTREE.get(repo)
    if not entree:
        webbrowser.open(releases_url)
        return None
    if repo in _TELECHARGEMENTS_EN_COURS:
        return None
    _TELECHARGEMENTS_EN_COURS.add(repo)
    nom = Path(str((entree.get("binaire") or {}).get("nom") or "fichier")).name

    def dire(texte: str) -> None:
        planifier(0, lambda: var.set(texte))

    def worker():
        try:
            dire(f"Telechargement de {nom}... (empreinte verifiee a l'arrivee)")
            chemin = telecharger_et_verifier(entree, dossier, repo=repo)
        except TelechargementInvalide as exc:
            # Le fichier n'est PAS celui annonce : il est supprime, on le dit,
            # on n'ouvre rien. Le clic suivant reprend l'ancien chemin (la page
            # GitHub) plutot que de retenter un fichier qu'on vient de refuser.
            _DERNIERE_ENTREE[repo] = None
            dire(f"Telechargement refuse : {exc}. Fichier supprime. Cliquer pour ouvrir la page GitHub.")
        except _ERREURS_RESEAU as exc:
            dire(f"Telechargement impossible ({exc.__class__.__name__}). Cliquer pour reessayer.")
        else:
            dire(f"Telecharge et verifie : {chemin.name} (dossier {chemin.parent})")
            # Le dossier, pas le fichier : on montre, on n'execute pas.
            planifier(0, lambda: _ouvrir_dossier(chemin.parent))
        finally:
            _TELECHARGEMENTS_EN_COURS.discard(repo)

    fil = threading.Thread(target=worker, daemon=True)
    fil.start()
    return fil


def _ouvrir_dossier(dossier: Path) -> None:
    try:
        os.startfile(str(dossier))          # Windows : l'explorateur sur le dossier
    except (AttributeError, OSError):
        webbrowser.open(dossier.as_uri())    # ailleurs, ou si l'explorateur refuse
