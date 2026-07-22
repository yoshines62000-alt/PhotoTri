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
risquer des paires manquees silencieusement.

Deux correctifs trouves a l'audit du 22/07/2026 (A1 et B1) s'appliquent
tous les deux a ce module :

- A1 : le dHash seul est aveugle a la couleur/teinte reelle d'une image a
  faible entropie (ciel uni, mur, neige...) - toutes ces images produisent
  (quasiment) le meme hash quelle que soit leur couleur, ce qui cree des
  faux positifs massifs (deux rafales sans aucun rapport visuel regroupees
  ensemble). _passes_color_check() rouvre les fichiers concernes pour un
  second signal (compute_color_signature) UNIQUEMENT quand au moins un des
  deux hachages impliques est a faible entropie (hashing.is_low_entropy_hash)
  - le cout de cette verification supplementaire reste donc negligeable sur
  une bibliotheque normale, ou les hachages a faible entropie sont rares.
  Cette verification est ce qui introduit la seule dependance de ce module a
  PIL/au disque - le reste du regroupement reste purement algorithmique.

- B1 : le chemin rapide ci-dessus degenere lui-meme en O(k^2) des qu'un
  bucket LSH contient un grand nombre d'identifiants (mesure a l'audit :
  45s sur 6000 photos a faible entropie, qui collisionnent presque toutes
  exactement sur chaque bande). _bucket_pairs() borne le nombre de paires
  generees par bucket au-dela d'une taille donnee - voir son docstring pour
  le compromis exact."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import comb
from typing import Callable, Optional

import hashing
from db import phash_from_sqlite

HASH_BITS = hashing.HASH_SIZE * hashing.HASH_SIZE  # 64
DEFAULT_BANDS = 8  # 64 / 8 = 8 bits par bande -> chemin rapide garanti pour seuil < 8
DEFAULT_NEAR_DUPLICATE_THRESHOLD = 6

# Au-dela de cette taille de bucket LSH, une comparaison exhaustive de
# toutes les paires internes redeviendrait O(k^2) sur k (voir B1 de
# l'audit : cas pathologique ou des milliers de photos a faible entropie
# (A1) partagent (quasiment) le meme hash et retombent donc TOUTES dans les
# memes buckets sur les 8 bandes). Au-dela de ce seuil, chaque element n'est
# compare qu'a une FENETRE de voisins tries par valeur de hash
# (_LARGE_BUCKET_WINDOW) plutot qu'a la totalite du bucket.
_LARGE_BUCKET_THRESHOLD = 400
_LARGE_BUCKET_WINDOW = 50

# Distance maximale (voir hashing.color_signature_distance, max possible
# 2295) en-deca de laquelle deux photos a hash dHash proche mais a faible
# entropie sont malgre tout considerees comme la meme couleur/scene -
# mesure empiriquement (voir tests) : deux images de teintes clairement
# distinctes (rouge/bleu/vert/noir/blanc/gris) sont TOUJOURS a plus de 750
# de distance ; deux recompressions/legeres variations de la meme couleur
# restent sous quelques dizaines. 150 laisse une marge confortable des deux
# cotes.
_LOW_ENTROPY_COLOR_MAX_DISTANCE = 150


@dataclass
class PhotoGroup:
    kind: str  # "exact" | "near"
    photo_ids: list
    # Distance de Hamming maximale entre deux membres du groupe (le "maillon
    # le plus faible") - 0 pour un groupe "exact" (memes octets, donc memes
    # hachages). Utilise par l'UI pour afficher un indicateur de confiance
    # par groupe (voir A2 de l'audit : aucun signal de similarite n'etait
    # jamais affiche a l'utilisateur).
    max_distance: int = 0


def similarity_percent(distance: int, hash_bits: int = HASH_BITS) -> int:
    """Pourcentage de similarite affiche dans l'UI (A2) : 100% pour une
    distance de Hamming nulle, decroissant lineairement jusqu'a 0% a
    `hash_bits` (distance maximale possible)."""
    hash_bits = max(hash_bits, 1)
    return round((1 - max(0, min(distance, hash_bits)) / hash_bits) * 100)


def confidence_label(max_distance: int, threshold: int) -> str:
    """Libelle de confiance qualitatif (A2) : derive de la distance de
    Hamming maximale entre deux membres d'un groupe "near", relativement au
    seuil de sensibilite utilise pour le regroupement - un groupe dont le
    maillon le plus faible est tres en-dessous du seuil merite plus de
    confiance qu'un groupe qui ne tient qu'a la limite du seuil choisi."""
    if threshold <= 0:
        return "Tres proches"
    ratio = max_distance / threshold
    if ratio <= 1 / 3:
        return "Tres proches"
    if ratio <= 2 / 3:
        return "Proches"
    return "Limite"


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


def _bucket_pairs(ids_sorted: list) -> "list":
    """Genere les paires candidates a l'interieur d'UN bucket LSH (tous ses
    membres partagent deja exactement les memes bits sur cette bande).

    En-dessous de _LARGE_BUCKET_THRESHOLD : comparaison exhaustive
    classique (toutes les paires), comme avant ce correctif - c'est le cas
    de la tres grande majorite des buckets sur une bibliotheque normale.

    Au-dela : bug de performance trouve a l'audit (B1) - un bucket peut
    contenir des milliers d'identifiants quand beaucoup de photos ont un
    hash quasi identique (typiquement des images a faible entropie, voir
    A1 : ciel/neige/mur uniformes qui hachent presque toutes vers la meme
    valeur), et la comparaison exhaustive redevient alors O(k^2) - mesure a
    45s pour 6000 photos de ce type. Chaque element n'est alors compare
    qu'a une fenetre de _LARGE_BUCKET_WINDOW voisins tries par VALEUR DE
    HASH (pas par id) : des hachages quasi identiques se retrouvent
    necessairement proches dans cet ordre, donc la tres grande majorite des
    paires reellement sous le seuil restent detectees, pour un cout borne a
    O(k * fenetre) au lieu de O(k^2). Ce n'est plus une garantie absolue de
    ne rater aucune paire (contrairement au chemin rapide standard - voir
    le docstring de module) mais un compromis borne et documente, explicite
    demande par l'audit plutot qu'une explosion combinatoire complete."""
    if len(ids_sorted) <= _LARGE_BUCKET_THRESHOLD:
        return [(id_a, id_b) for i, id_a in enumerate(ids_sorted) for id_b in ids_sorted[i + 1:]]
    return [
        (id_a, id_b)
        for i, id_a in enumerate(ids_sorted)
        for id_b in ids_sorted[i + 1: i + 1 + _LARGE_BUCKET_WINDOW]
    ]


def _lsh_candidates(hashes: dict, bands: int) -> set:
    """Paires (id_a, id_b) candidates via l'indexage par bandes decrit plus
    haut - toujours un SUR-ensemble des vraies paires sous le seuil (sauf
    dans le cas borne des buckets surdimensionnes, voir _bucket_pairs)."""
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
        # Trie par VALEUR DE HASH (pas par id) : c'est ce qui rend la
        # fenetre de _bucket_pairs pertinente sur les gros buckets - des
        # hachages proches se retrouvent adjacents dans cet ordre, ce qui
        # ne serait pas le cas d'un tri par id (arbitraire du point de vue
        # de la distance de Hamming).
        ids_sorted = sorted(ids, key=lambda pid: hashes[pid])
        candidates.update(_bucket_pairs(ids_sorted))
    return candidates


def _all_pairs_candidates(hashes: dict) -> "combinations":
    return combinations(sorted(hashes.keys()), 2)


def _cached_color_signature(pid, by_id: dict, cache: dict):
    """Calcule (ou relit depuis `cache`) la signature couleur d'une photo.

    Le cache est INDISPENSABLE, pas une simple micro-optimisation : dans le
    scenario pathologique de B1 (des milliers de photos a faible entropie),
    une meme photo apparait dans de tres nombreuses paires candidates -
    sans cache, chacune de ces paires rouvrirait et redecoderait le meme
    fichier depuis le disque, ce qui reintroduirait un cout O(candidats)
    d'I/O disque a l'endroit meme que le correctif B1 cherche a borner.
    Avec le cache, chaque photo n'est jamais decodee plus d'une fois par
    appel a group_photos(), quel que soit le nombre de paires ou elle
    apparait. `None` (memorise egalement) signale un fichier illisible -
    voir _passes_color_check."""
    if pid in cache:
        return cache[pid]
    try:
        sig = hashing.compute_color_signature_from_path(by_id[pid]["path"])
    except Exception:
        sig = None
    cache[pid] = sig
    return sig


def _passes_color_check(id_a, id_b, by_id: dict, cache: dict) -> bool:
    """Verification d'appoint appliquee UNIQUEMENT quand au moins un des
    deux hachages dHash impliques est a faible entropie (voir
    hashing.is_low_entropy_hash) - bug trouve a l'audit (A1) : le dHash seul
    ne code que des comparaisons de luminosite entre pixels ADJACENTS, donc
    deux images uniformes de teintes totalement differentes (rouge, bleu,
    vert, noir, blanc...) produisent exactement le meme hash, quel que soit
    le seuil de sensibilite configure. Rouvre les deux fichiers (via le
    cache ci-dessus) pour comparer leur couleur moyenne par tiers
    (hashing.compute_color_signature) - seul endroit ou ce module touche au
    disque, delibererement limite a ce cas rare (voir docstring de module).

    En cas d'echec de lecture (fichier deplace/supprime entre le scan et ce
    calcul, ou objet `photos` de test sans cle "path") : ne bloque PAS le
    rapprochement plutot que de faire echouer tout le regroupement pour une
    seule photo illisible - le comportement retombe alors sur le dHash seul,
    identique a avant ce correctif."""
    sig_a = _cached_color_signature(id_a, by_id, cache)
    sig_b = _cached_color_signature(id_b, by_id, cache)
    if sig_a is None or sig_b is None:
        return True
    return hashing.color_signature_distance(sig_a, sig_b) <= _LOW_ENTROPY_COLOR_MAX_DISTANCE


def group_photos(
    photos: list,
    near_duplicate_threshold: int = DEFAULT_NEAR_DUPLICATE_THRESHOLD,
    bands: int = DEFAULT_BANDS,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> list:
    """Regroupe `photos` (lignes de db.list_active_photos(), ou tout objet
    supportant le meme acces par cle) en groupes de doublons exacts et de
    quasi-doublons. Une photo sans aucun doublon/quasi-doublon n'apparait
    dans aucun groupe. Une photo deja dans un groupe exact n'est jamais
    reproposee dans un groupe "near" - le groupe exact est strictement
    plus precis (memes octets, pas juste visuellement proche).

    `progress_callback(done, total)`, si fourni, est appele periodiquement
    pendant la verification des paires candidates (l'etape qui peut prendre
    du temps sur une grosse bibliotheque) - correctif B1 de l'audit : avant
    ce correctif, le calcul des groupes n'offrait aucun retour de
    progression, seulement un texte statique "Calcul des groupes...",
    laissant l'utilisateur sans aucune indication pendant potentiellement
    plusieurs minutes."""
    by_id = {p["id"]: p for p in photos}

    # 1) Doublons exacts (sha256).
    exact_buckets: dict = {}
    for photo in photos:
        exact_buckets.setdefault(photo["sha256"], []).append(photo["id"])
    exact_groups = [PhotoGroup(kind="exact", photo_ids=sorted(ids), max_distance=0) for ids in exact_buckets.values() if len(ids) > 1]
    grouped_ids = {pid for g in exact_groups for pid in g.photo_ids}

    # 2) Quasi-doublons parmi les photos pas deja dans un groupe exact.
    remaining_ids = [p["id"] for p in photos if p["id"] not in grouped_ids]
    hashes = {pid: phash_from_sqlite(by_id[pid]["phash"]) for pid in remaining_ids}

    if near_duplicate_threshold < bands:
        candidates = _lsh_candidates(hashes, bands)
        total_candidates = len(candidates)
    else:
        candidates = _all_pairs_candidates(hashes)
        total_candidates = comb(len(remaining_ids), 2)

    uf = _UnionFind(remaining_ids)
    # Rapporte la progression au plus toutes les ~200 tranches (assez
    # souvent pour une barre de progression fluide, assez rare pour ne pas
    # dominer le temps de calcul avec des appels de callback).
    report_every = max(total_candidates // 200, 1)
    # Distance de chaque paire effectivement unifiee (pas rejetee par le
    # seuil ni par _passes_color_check) - sert ensuite a calculer
    # max_distance par groupe SANS recomparer toutes les paires d'un
    # cluster (voir juste en dessous) : recalculer max(...) sur
    # combinations(cluster, 2) reintroduirait exactement le O(k^2) que le
    # correctif B1 cherche a eviter, cette fois sur un gros cluster
    # legitimement fusionne plutot que sur un bucket LSH.
    union_edges = []
    color_sig_cache: dict = {}
    for index, (id_a, id_b) in enumerate(candidates, start=1):
        ha, hb = hashes[id_a], hashes[id_b]
        distance = hashing.hamming_distance(ha, hb)
        if distance <= near_duplicate_threshold:
            if (hashing.is_low_entropy_hash(ha) or hashing.is_low_entropy_hash(hb)) \
                    and not _passes_color_check(id_a, id_b, by_id, color_sig_cache):
                pass  # faux positif de bas niveau (A1) : hash proche, couleur trop differente
            else:
                uf.union(id_a, id_b)
                union_edges.append((id_a, id_b, distance))
        if progress_callback is not None and (index % report_every == 0 or index == total_candidates):
            progress_callback(index, total_candidates)

    clusters: dict = {}
    for pid in remaining_ids:
        root = uf.find(pid)
        clusters.setdefault(root, []).append(pid)

    max_distance_by_root: dict = {}
    for id_a, _id_b, distance in union_edges:
        root = uf.find(id_a)
        if distance > max_distance_by_root.get(root, 0):
            max_distance_by_root[root] = distance

    near_groups = [
        PhotoGroup(kind="near", photo_ids=sorted(ids), max_distance=max_distance_by_root.get(root, 0))
        for root, ids in clusters.items() if len(ids) > 1
    ]

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
