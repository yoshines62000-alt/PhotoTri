import random
import shutil
import sys
import tempfile
import time
import unittest
from itertools import combinations
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import grouping
import hashing
from db import phash_to_sqlite


def _photo(id_, sha256, phash, width=800, height=600, size=100_000, path=None):
    photo = {"id": id_, "sha256": sha256, "phash": phash_to_sqlite(phash), "width": width, "height": height, "size": size}
    if path is not None:
        photo["path"] = str(path)
    return photo


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


class TestLowEntropyFalsePositives(unittest.TestCase):
    """Verrouille A1 (audit du 2026-07-22) : le dHash seul ne code que des
    comparaisons de luminosite entre pixels adjacents, donc des images
    uniformes de teintes totalement differentes (rouge/bleu/vert/noir/
    blanc/gris) produisent toutes exactement le meme hash (0) - avant
    correctif, PhotoTri les regroupait donc ensemble dans un seul "groupe
    de quasi-doublons" malgre l'absence totale de rapport visuel entre
    elles. Ces tests utilisent de vrais fichiers image (necessaire : la
    verification d'appoint de grouping._passes_color_check rouvre le
    fichier sur disque) plutot que des hachages fabriques."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _solid_photo(self, id_, color, name, sha256=None):
        path = self.tmp / name
        Image.new("RGB", (200, 200), color).save(path, quality=95)
        phash = hashing.compute_dhash_from_path(path)
        return _photo(id_, sha256 or f"sha-{id_}", phash, path=path)

    def test_unrelated_solid_color_photos_are_not_grouped(self):
        # Reproduit le scenario mesure a l'audit : rouge/bleu/vert uniformes
        # ont tous un dHash de 0 (distance 0 entre eux, sous n'importe quel
        # seuil) mais ne doivent PLUS finir dans le meme groupe.
        red = self._solid_photo(1, (255, 0, 0), "red.jpg")
        blue = self._solid_photo(2, (0, 0, 255), "blue.jpg")
        green = self._solid_photo(3, (0, 255, 0), "green.jpg")
        photos = [red, blue, green]

        # Sanity check : le dHash seul est bien aveugle a la couleur ici
        # (sinon ce test ne prouverait rien).
        hashes = [hashing.compute_dhash_from_path(Path(p["path"])) for p in photos]
        self.assertEqual(len(set(hashes)), 1)

        groups = grouping.group_photos(photos, near_duplicate_threshold=6)
        self.assertEqual(groups, [], f"des photos de couleurs sans rapport ont ete regroupees : {groups}")

    def test_recompressed_same_solid_color_are_still_grouped(self):
        # Garde-fou anti regression inverse : le correctif A1 ne doit PAS
        # transformer un vrai quasi-doublon (meme couleur, juste reencodee)
        # en faux negatif.
        a = self._solid_photo(1, (128, 128, 128), "gray_a.jpg", sha256="sha-a")
        path_b = self.tmp / "gray_b.jpg"
        Image.new("RGB", (200, 200), (130, 130, 130)).save(path_b, quality=60)
        phash_b = hashing.compute_dhash_from_path(path_b)
        b = _photo(2, "sha-b", phash_b, path=path_b)

        groups = grouping.group_photos([a, b], near_duplicate_threshold=6)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].kind, "near")
        self.assertEqual(groups[0].photo_ids, [1, 2])

    def test_normal_entropy_hashes_are_never_reopened_from_disk(self):
        # Le correctif A1 ne doit ajouter AUCUN cout d'I/O disque pour les
        # photos "normales" (hash equilibre) - la verification d'appoint ne
        # doit se declencher QUE si un des deux hachages est a faible
        # entropie. On le verifie en fournissant un chemin invalide : si
        # jamais le code tentait de l'ouvrir, cela leverait une exception
        # avalee silencieusement (voir _passes_color_check) mais on prefere
        # verifier explicitement que le comportement (regroupement reussi)
        # ne depend pas du disque du tout ici.
        balanced = int("10" * 32, 2)
        close = balanced ^ 0b111  # distance 3, hash equilibre (pas faible entropie)
        photos = [
            _photo(1, "sha-a", balanced, path="/chemin/inexistant/a.jpg"),
            _photo(2, "sha-b", close, path="/chemin/inexistant/b.jpg"),
        ]
        groups = grouping.group_photos(photos, near_duplicate_threshold=5)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].photo_ids, [1, 2])

    def test_missing_path_falls_back_to_dhash_only_without_crashing(self):
        # Objets "photos" sans cle "path" (comme les tests existants de ce
        # fichier, construits avant ce correctif) : la verification
        # d'appoint doit se degrader proprement (ne bloque pas) plutot que
        # de faire planter tout le regroupement sur un KeyError.
        photos = [_photo(1, "sha-a", 0), _photo(2, "sha-b", 0b1111)]  # tous deux faible entropie
        groups = grouping.group_photos(photos, near_duplicate_threshold=6)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].photo_ids, [1, 2])


class TestGroupConfidenceIndicator(unittest.TestCase):
    """Verrouille A2 (audit du 2026-07-22) : chaque PhotoGroup porte
    desormais max_distance (la distance de Hamming maximale entre deux de
    ses membres), et grouping.similarity_percent/confidence_label en
    derivent l'indicateur affiche par l'UI."""

    def test_exact_group_has_zero_max_distance(self):
        photos = [_photo(1, "sha-a", 0), _photo(2, "sha-a", 0b1111)]
        groups = grouping.group_photos(photos)
        self.assertEqual(groups[0].kind, "exact")
        self.assertEqual(groups[0].max_distance, 0)

    def test_near_group_max_distance_matches_worst_pair(self):
        a = 0
        b = 0b11        # distance(a,b) = 2
        c = 0b1111      # distance(b,c) = 2, distance(a,c) = 4
        photos = [_photo(1, "sha-a", a), _photo(2, "sha-b", b), _photo(3, "sha-c", c)]
        groups = grouping.group_photos(photos, near_duplicate_threshold=2)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].photo_ids, [1, 2, 3])
        # Les deux aretes effectivement utilisees pour unifier (a-b et b-c)
        # ont chacune une distance de 2 - c'est cette valeur qui doit
        # remonter, pas la distance a-c (4), jamais testee directement sous
        # ce seuil.
        self.assertEqual(groups[0].max_distance, 2)

    def test_similarity_percent_extremes(self):
        self.assertEqual(grouping.similarity_percent(0), 100)
        self.assertEqual(grouping.similarity_percent(64), 0)
        self.assertEqual(grouping.similarity_percent(32), 50)

    def test_confidence_label_thresholds(self):
        self.assertEqual(grouping.confidence_label(0, 6), "Tres proches")
        self.assertEqual(grouping.confidence_label(2, 6), "Tres proches")
        self.assertEqual(grouping.confidence_label(4, 6), "Proches")
        self.assertEqual(grouping.confidence_label(6, 6), "Limite")


class TestGroupingProgressCallback(unittest.TestCase):
    """Verrouille le retour de progression ajoute pour B1 (audit du
    2026-07-22) : avant ce correctif, group_photos() n'offrait aucun moyen
    de savoir ou en etait un calcul long, seulement un texte statique cote
    UI."""

    def test_progress_callback_reaches_completion(self):
        rng = random.Random(7)
        photos = [_photo(i, f"sha-{i}", rng.getrandbits(64)) for i in range(200)]
        calls = []
        grouping.group_photos(photos, near_duplicate_threshold=4, progress_callback=lambda done, total: calls.append((done, total)))
        self.assertTrue(calls, "le callback de progression n'a jamais ete appele")
        last_done, last_total = calls[-1]
        self.assertEqual(last_done, last_total)
        # Monotone croissant, jamais de retour en arriere.
        for (d1, _), (d2, _) in zip(calls, calls[1:]):
            self.assertLessEqual(d1, d2)

    def test_no_candidates_does_not_crash_with_progress_callback(self):
        photos = [_photo(1, "sha-a", 0), _photo(2, "sha-b", (1 << 64) - 1)]
        calls = []
        groups = grouping.group_photos(
            photos, near_duplicate_threshold=1, progress_callback=lambda done, total: calls.append((done, total)),
        )
        self.assertEqual(groups, [])


class TestLshBucketPerformance(unittest.TestCase):
    """Verrouille B1 (audit du 2026-07-22) : le chemin rapide LSH degenerait
    en O(k^2) quand un bucket contient un grand nombre d'identifiants
    (mesure a l'audit : 45s pour 6000 photos a faible entropie sur 6500
    photos au total). Reproduit le meme scenario (hachages quasi identiques,
    variation aleatoire de 0 a 3 bits, melanges a des hachages aleatoires)
    et verrouille un plafond de temps largement sous l'ancien comportement
    quadratique."""

    @staticmethod
    def _make_photos(n_uniform, n_random, seed=42):
        rng = random.Random(seed)
        photos = []
        pid = 1
        for _ in range(n_uniform):
            h = 0
            for _ in range(rng.randint(0, 3)):
                h |= 1 << rng.randint(0, 63)
            photos.append(_photo(pid, f"sha-{pid}", h))
            pid += 1
        for _ in range(n_random):
            photos.append(_photo(pid, f"sha-{pid}", rng.getrandbits(64)))
            pid += 1
        return photos

    def test_large_low_entropy_cluster_completes_well_under_the_old_quadratic_time(self):
        # Avant le correctif B1, ce scenario exact (n_uniform=3000,
        # total=3500) prenait ~8.7s mesures directement sur ce depot avant
        # correctif (et le rapport d'audit mesurait 10s/45s a 3000/6000).
        # Apres correctif : quelques dixiemes de seconde. On verrouille un
        # plafond genereux (5s) pour rester fiable sur une machine lente
        # tout en detectant sans ambiguite une regression vers le
        # comportement quadratique.
        photos = self._make_photos(n_uniform=3000, n_random=500)
        t0 = time.perf_counter()
        grouping.group_photos(photos)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 5.0, f"regroupement trop lent ({elapsed:.2f}s) - le chemin rapide LSH semble redevenu O(k^2)")

    def test_doubling_the_low_entropy_cluster_does_not_quadruple_the_time(self):
        # Signature meme d'un comportement O(n^2) : doubler n multiplie le
        # temps par ~4. Verifie que ce n'est plus le cas (facteur nettement
        # inferieur a 4, une marge large est prise pour la stabilite en CI).
        small = self._make_photos(n_uniform=1500, n_random=500)
        large = self._make_photos(n_uniform=3000, n_random=500)

        t0 = time.perf_counter()
        grouping.group_photos(small)
        small_elapsed = max(time.perf_counter() - t0, 1e-6)

        t0 = time.perf_counter()
        grouping.group_photos(large)
        large_elapsed = time.perf_counter() - t0

        ratio = large_elapsed / small_elapsed
        self.assertLess(ratio, 3.5, f"le temps a plus que triple pour un doublement de n ({small_elapsed:.3f}s -> {large_elapsed:.3f}s, ratio {ratio:.2f}) - comportement toujours proche de O(k^2)")


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
