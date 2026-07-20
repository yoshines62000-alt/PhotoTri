"""Regroupement des doublons et quasi-doublons parmi les photos actives.

Deux niveaux de regroupement :
- doublons EXACTS : meme sha256 (contenu de fichier bit a bit identique).
- quasi-doublons : dHash a une distance de Hamming <= seuil (rafales,
  recompressions, leger recadrage).

Comparer toutes les paires de photos (O(n^2)) pour trouver les
quasi-doublons devient lent sur une grosse bibliotheque (des dizaines de
millions de paires au-dela de quelques milliers de photos). Le chemin
rapide indexe les photos par bandes de bits du phash ("multi-index
hashing") : si la distance de Hamming entre deux hachages est strictement
inferieure au nombre de bandes, au moins une bande leur est FORCEMENT
identique (principe des tiroirs) - filtrer sur "au moins une bande
identique" ne peut donc jamais manquer une vraie paire, seulement generer
des faux positifs elimines ensuite par la verification exacte de la
distance. Ce chemin rapide n'est valide QUE si le seuil demande reste
strictement inferieur au nombre de bandes ; au-dela, on retombe sur la
comparaison exhaustive (plus lente mais toujours correcte) plutot que de
risquer des paires manquees silencieusement."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

import hashing
from db import phash_from_sqlite

HASH_BITS = hashing.HASH_SIZE * hashing.HASH_SIZE  # 64
DEFAULT_BANDS = 8  # 64 / 8 = 8 bits par bande -> chemin rapide garanti pour seuil < 8
DEFAULT_NEAR_DUPLICATE_THRESHOLD = 6


@dataclass
class PhotoGroup:
    kind: str  # "exact" | "near"
    photo_ids: list


class _UnionFind:
    def __init__(self, ids):
        self._parent = {i: i for i in ids}

    def find(self, x):
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb


def _lsh_candidates(hashes: dict, bands: int) -> set:
    """Paires (id_a, id_b) candidates via l'indexage par bandes decrit plus
    haut - toujours un SUR-ensemble des vraies paires sous le seuil."""
    band_bits = HASH_BITS // bands
    buckets: dict = {}
    for photo_id, phash in hashes.items():
        for band_index in range(bands):
            key = (band_index, (phash >> (band_index * band_bits)) & ((1 << band_bits) - 1))
            buckets.setdefault(key, []).append(photo_id)

    candidates = set()
    for ids in buckets.values():
        if len(ids) < 2:
            continue
        ids_sorted = sorted(ids)
        for i, id_a in enumerate(ids_sorted):
            for id_b in ids_sorted[i + 1:]:
                candidates.add((id_a, id_b))
    return candidates


def _all_pairs_candidates(hashes: dict) -> "combinations":
    return combinations(sorted(hashes.keys()), 2)


def group_photos(photos: list, near_duplicate_threshold: int = DEFAULT_NEAR_DUPLICATE_THRESHOLD, bands: int = DEFAULT_BANDS) -> list:
    """Regroupe `photos` (lignes de db.list_active_photos(), ou tout objet
    supportant le meme acces par cle) en groupes de doublons exacts et de
    quasi-doublons. Une photo sans aucun doublon/quasi-doublon n'apparait
    dans aucun groupe. Une photo deja dans un groupe exact n'est jamais
    reproposee dans un groupe "near" - le groupe exact est strictement
    plus precis (memes octets, pas juste visuellement proche)."""
    by_id = {p["id"]: p for p in photos}

    # 1) Doublons exacts (sha256).
    exact_buckets: dict = {}
    for photo in photos:
        exact_buckets.setdefault(photo["sha256"], []).append(photo["id"])
    exact_groups = [PhotoGroup(kind="exact", photo_ids=sorted(ids)) for ids in exact_buckets.values() if len(ids) > 1]
    grouped_ids = {pid for g in exact_groups for pid in g.photo_ids}

    # 2) Quasi-doublons parmi les photos pas deja dans un groupe exact.
    remaining_ids = [p["id"] for p in photos if p["id"] not in grouped_ids]
    hashes = {pid: phash_from_sqlite(by_id[pid]["phash"]) for pid in remaining_ids}

    if near_duplicate_threshold < bands:
        candidates = _lsh_candidates(hashes, bands)
    else:
        candidates = _all_pairs_candidates(hashes)

    uf = _UnionFind(remaining_ids)
    for id_a, id_b in candidates:
        if hashing.hamming_distance(hashes[id_a], hashes[id_b]) <= near_duplicate_threshold:
            uf.union(id_a, id_b)

    clusters: dict = {}
    for pid in remaining_ids:
        root = uf.find(pid)
        clusters.setdefault(root, []).append(pid)
    near_groups = [PhotoGroup(kind="near", photo_ids=sorted(ids)) for ids in clusters.values() if len(ids) > 1]

    return exact_groups + near_groups


def suggest_keeper(photos_in_group: list) -> int:
    """Suggere l'id de la photo a conserver dans un groupe : la plus grande
    resolution (largeur*hauteur) d'abord (probablement la source, pas une
    version redimensionnee/partagee), puis la taille de fichier la plus
    grande en cas d'egalite (probablement moins compressee), puis l'id le
    plus petit en dernier recours (choix deterministe, reproductible)."""
    def score(p):
        return ((p["width"] or 0) * (p["height"] or 0), p["size"], -p["id"])
    return max(photos_in_group, key=score)["id"]
