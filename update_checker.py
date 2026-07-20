"""Verification de mise a jour via l'API publique GitHub Releases - pas de
serveur dedie, aucune donnee envoyee au-dela d'une simple requete HTTP GET
vers l'API GitHub (la ou les releases de cet outil sont deja publiees).
Toujours execute en arriere-plan (voir start_update_check) : une
verification ratee (pas de connexion, GitHub inaccessible...) ne doit
JAMAIS empecher l'application de demarrer ou de fonctionner normalement."""

from __future__ import annotations

import json
import queue
import re
import threading
import urllib.error
import urllib.request

REQUEST_TIMEOUT_SECONDS = 5


def _parse_version(tag: str) -> tuple:
    """Convertit "v1.2.10" en (1, 2, 10) pour une comparaison numerique -
    une comparaison de chaines mettrait a tort "v1.10.0" avant "v1.9.0"."""
    numbers = re.findall(r"\d+", tag)
    return tuple(int(n) for n in numbers) if numbers else (0,)


def is_newer(remote_tag: str, current_version: str) -> bool:
    return _parse_version(remote_tag) > _parse_version(current_version)


def fetch_latest_release_tag(repo: str, timeout: float = REQUEST_TIMEOUT_SECONDS):
    """Renvoie le tag de la derniere release publiee (ex: "v1.0.8"), ou
    None si la verification echoue pour n'importe quelle raison."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": repo})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        tag = data.get("tag_name")
        return tag if isinstance(tag, str) and tag else None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


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
