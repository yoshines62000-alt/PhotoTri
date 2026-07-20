import random
import sys
import unittest
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import grouping
import hashing
from db import phash_to_sqlite


def _photo(id_, sha256, phash, width=800, height=600, size=100_000):
    return {"id": id_, "sha256": sha256, "phash": phash_to_sqlite(phash), "width": width, "height": height, "size": size}


class TestExactGroups(unittest.TestCase):
    def test_two_identical_files_grouped_as_exact(self):
        photos = [_photo(1, "sha-a", 0b0), _photo(2, "sha-a", 0b1111)]
        groups = grouping.group_photos(photos)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].kind, "exact")
        self.assertEqual(groups[0].photo_ids, [1, 2])

    def test_singleton_not_grouped(self):
        photos = [_photo(1, "sha-a", 0), _photo(2, "sha-b", 0xFFFFFFFFFFFFFFFF)]
        groups = grouping.group_photos(photos, near_duplicate_threshold=1)
        self.assertEqual(groups, [])

    def test_three_way_exact_group(self):
        photos = [_photo(i, "sha-x", 0) for i in (1, 2, 3)]
        groups = grouping.group_photos(photos)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].photo_ids, [1, 2, 3])


class TestNearDuplicateGroups(unittest.TestCase):
    def test_close_hashes_grouped_as_near(self):
        base = 0b1010101010101010101010101010101010101010101010101010101010101010 & ((1 << 64) - 1)
        close = base ^ 0b111  # distance 3
        photos = [_photo(1, "sha-a", base), _photo(2, "sha-b", close)]
        groups = grouping.group_photos(photos, near_duplicate_threshold=5)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].kind, "near")
        self.assertEqual(groups[0].photo_ids, [1, 2])

    def test_far_hashes_not_grouped(self):
        photos = [_photo(1, "sha-a", 0), _photo(2, "sha-b", (1 << 64) - 1)]
        groups = grouping.group_photos(photos, near_duplicate_threshold=5)
        self.assertEqual(groups, [])

    def test_exact_group_not_reproposed_as_near(self):
        # Meme sha256 -> deja dans un groupe exact ; ne doit pas en plus
        # apparaitre dans un groupe "near" avec une 3e photo proche.
        photos = [
            _photo(1, "sha-a", 0),
            _photo(2, "sha-a", 0),
            _photo(3, "sha-b", 0b1),  # proche de 1/2 mais sha256 different
        ]
        groups = grouping.group_photos(photos, near_duplicate_threshold=5)
        exact = [g for g in groups if g.kind == "exact"]
        near = [g for g in groups if g.kind == "near"]
        self.assertEqual(len(exact), 1)
        self.assertEqual(exact[0].photo_ids, [1, 2])
        self.assertEqual(near, [])  # id 3 seul restant -> pas de groupe

    def test_transitive_chain_forms_one_cluster(self):
        # a-b distance 2, b-c distance 2, a-c distance 4 (> seuil de 3 pris isolement)
        # mais l'union-find doit quand meme les regrouper via b (transitivite).
        a = 0
        b = 0b11
        c = 0b1111
        photos = [_photo(1, "sha-a", a), _photo(2, "sha-b", b), _photo(3, "sha-c", c)]
        groups = grouping.group_photos(photos, near_duplicate_threshold=2)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].photo_ids, [1, 2, 3])


class TestFastPathMatchesBruteForce(unittest.TestCase):
    """Le chemin rapide (LSH par bandes) doit produire EXACTEMENT le meme
    resultat que la comparaison exhaustive de toutes les paires, quel que
    soit le seuil (tant qu'il reste < bands) : c'est la garantie meme qui
    justifie l'optimisation, verifiee ici sur des hachages aleatoires
    plutot que suppose correct sur la seule base du raisonnement."""

    def test_random_hashes_threshold_below_bands(self):
        rng = random.Random(42)
        for _trial in range(5):
            photos = [_photo(i, f"sha-{i}", rng.getrandbits(64)) for i in range(60)]
            threshold = 4
            fast = grouping.group_photos(photos, near_duplicate_threshold=threshold, bands=8)
            # Reference : force brute sur les memes photos.
            by_id = {p["id"]: p for p in photos}
            from db import phash_from_sqlite
            uf_ref = grouping._UnionFind([p["id"] for p in photos])
            for id_a, id_b in combinations([p["id"] for p in photos], 2):
                ha = phash_from_sqlite(by_id[id_a]["phash"])
                hb = phash_from_sqlite(by_id[id_b]["phash"])
                if hashing.hamming_distance(ha, hb) <= threshold:
                    uf_ref.union(id_a, id_b)
            ref_clusters = {}
            for p in photos:
                root = uf_ref.find(p["id"])
                ref_clusters.setdefault(root, []).append(p["id"])
            ref_groups = sorted(sorted(ids) for ids in ref_clusters.values() if len(ids) > 1)
            fast_groups = sorted(sorted(g.photo_ids) for g in fast)
            self.assertEqual(fast_groups, ref_groups)

    def test_threshold_at_or_above_bands_falls_back_to_exhaustive(self):
        # Seuil >= bands : le chemin rapide n'a plus de garantie, le code
        # doit alors passer par la comparaison exhaustive (toujours
        # correcte). On verifie juste qu'aucune paire evidente n'est
        # manquee dans ce mode.
        photos = [_photo(1, "sha-a", 0), _photo(2, "sha-b", 0b11111111)]  # distance 8
        groups = grouping.group_photos(photos, near_duplicate_threshold=8, bands=8)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].photo_ids, [1, 2])


class TestSuggestKeeper(unittest.TestCase):
    def test_prefers_higher_resolution(self):
        photos = [
            _photo(1, "s1", 0, width=800, height=600),
            _photo(2, "s2", 0, width=4000, height=3000),
        ]
        self.assertEqual(grouping.suggest_keeper(photos), 2)

    def test_tie_breaks_on_file_size(self):
        photos = [
            _photo(1, "s1", 0, width=800, height=600, size=50_000),
            _photo(2, "s2", 0, width=800, height=600, size=200_000),
        ]
        self.assertEqual(grouping.suggest_keeper(photos), 2)

    def test_tie_breaks_on_lowest_id(self):
        photos = [
            _photo(5, "s1", 0, width=800, height=600, size=100),
            _photo(2, "s2", 0, width=800, height=600, size=100),
        ]
        self.assertEqual(grouping.suggest_keeper(photos), 2)


if __name__ == "__main__":
    unittest.main()
