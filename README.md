# PhotoTri

[![Dernière version](https://img.shields.io/github/v/release/yoshines62000-alt/PhotoTri?label=derni%C3%A8re%20version)](https://github.com/yoshines62000-alt/PhotoTri/releases/latest)
[![Téléchargements](https://img.shields.io/github/downloads/yoshines62000-alt/PhotoTri/total?label=t%C3%A9l%C3%A9chargements)](https://github.com/yoshines62000-alt/PhotoTri/releases/latest)

**[⬇️ Télécharger l'exécutable (.exe) — aucune installation requise](https://github.com/yoshines62000-alt/PhotoTri/releases/latest)**

Détecteur de photos en double et quasi-en-double (rafales, recompressions)
— gratuit, open source, 100 % local, sans compte, sans cloud. Alternative
libre aux outils de tri photo payants ou dépendants du cloud : vos photos
ne quittent jamais votre machine, et rien n'est jamais supprimé sans votre
accord.

## Fonctionnalités

- **Doublons exacts** : détection par empreinte de fichier (sha256) —
  deux copies bit à bit identiques, quel que soit leur nom ou leur
  emplacement.
- **Quasi-doublons** : détection par hachage perceptuel (dHash) — repère
  les photos visuellement quasi identiques (rafales, recompressions,
  légères retouches) même si le fichier a changé.
- **Sensibilité réglable** : ajustez le seuil de similarité pour les
  quasi-doublons selon vos besoins.
- **Suggestion automatique** : dans chaque groupe, la photo à conserver
  est pré-suggérée (meilleure résolution, puis meilleure qualité).
- **Rangement non destructif** : les doublons sélectionnés sont
  **déplacés** vers un dossier de révision de votre choix — jamais
  supprimés automatiquement.
- **Scan incrémental** : un nouveau scan ne retraite que les fichiers
  nouveaux ou modifiés, rapide même sur une grosse bibliothèque.
- **100 % local, zéro cloud** : aucune connexion réseau, aucun compte,
  aucune télémétrie.
- **Gratuit et open source, pour toujours**.

## Démarrage rapide

1. [**Téléchargez `PhotoTri.exe`**](https://github.com/yoshines62000-alt/PhotoTri/releases/latest)
   depuis la dernière release.
2. Double-cliquez dessus : la fenêtre de l'application s'ouvre directement,
   sans installation, sans Python.
3. Cliquez sur **Choisir un dossier à analyser...**, puis **Analyser
   (scanner)**.

L'exécutable n'étant pas signé numériquement, Windows SmartScreen peut
afficher un avertissement au premier lancement : cliquez sur **Informations
complémentaires** puis **Exécuter quand même**.

## Lancer depuis le code source

Alternative à l'exécutable, pour les développeurs ou par souci de
transparence : double-cliquez sur **[`Lancer.vbs`](Lancer.vbs)** — la
fenêtre s'ouvre directement, sans console.

Une dépendance tierce est nécessaire (la bibliothèque `Pillow`) :

```bash
python -m pip install -r requirements.txt
```

## Utilisation

1. **Choisir un dossier à analyser...** : sélectionnez le dossier
   contenant vos photos (les sous-dossiers sont inclus automatiquement).
2. **Analyser (scanner)** : parcourt le dossier, calcule les empreintes de
   chaque photo. Le scan tourne en arrière-plan et peut être interrompu à
   tout moment avec **Arrêter**.
3. Les groupes de doublons/quasi-doublons apparaissent dans la liste de
   gauche. Sélectionnez-en un pour voir les photos concernées, côte à
   côte, avec leurs vignettes.
4. Dans chaque groupe, la photo suggérée à conserver n'est **pas** cochée
   par défaut ; les autres le sont. Ajustez les cases selon votre choix.
5. **Déplacer les photos cochées vers le dossier de révision** : les
   photos cochées sont déplacées (jamais supprimées) vers le dossier de
   révision affiché, modifiable via **Changer...**.
6. **Recalculer les groupes** : recalcule les groupes avec un nouveau
   réglage de sensibilité, sans refaire un scan complet.

## Confidentialité

- Aucune donnée ne quitte votre machine : pas de compte, pas de serveur,
  pas de télémétrie, aucune synchronisation.
- L'index (chemins, empreintes, notes) est stocké dans
  `%APPDATA%\PhotoTri\phototri.sqlite`. Il est entièrement reconstructible
  par un nouveau scan si perdu — vos vraies photos restent toujours à leur
  emplacement d'origine (ou dans le dossier de révision que vous avez
  choisi) tant que vous ne les déplacez pas vous-même.

## Créer un exécutable autonome (.exe)

Pour distribuer l'outil sans que le destinataire ait besoin d'installer
Python, un exécutable Windows autonome peut être généré avec
[PyInstaller](https://pyinstaller.org/) :

```bash
python -m pip install pyinstaller
python -m PyInstaller PhotoTri.spec
```

L'exécutable est produit dans `dist/PhotoTri.exe` (fichier unique, sans
console). Le fichier `.spec` du dépôt fixe la configuration de build pour un
résultat reproductible. Les dossiers `build/` et `dist/` ne sont pas suivis
par Git.

## Tests

Une suite de tests automatisés couvre le hachage perceptuel (dHash,
distance de Hamming), la couche base de données (index incrémental,
préservation des annotations utilisateur), le scanner (parcours récursif,
lecture EXIF, purge des fichiers disparus) et le regroupement des
doublons (doublons exacts, quasi-doublons, garantie de détection du
chemin rapide par bandes) sur de vraies images générées avec Pillow.

```bash
python -m unittest discover tests -v
```

## Structure du projet

```
hashing.py             # primitives pures : empreinte de fichier (sha256), hachage perceptuel (dHash)
db.py                   # couche donnees SQLite : index reconstructible des photos
scanner.py              # parcours recursif d'un dossier, lecture EXIF, hachage incremental
grouping.py             # regroupement des doublons exacts et quasi-doublons
gui.py                  # interface graphique Tkinter (scan en arriere-plan)
tests/                  # tests automatises
requirements.txt        # Pillow
Lancer.vbs              # raccourci de lancement double-clic (sans console)
Lancer.bat              # raccourci de lancement double-clic (avec console, pour debug)
PhotoTri.spec            # configuration de build PyInstaller (.exe autonome)
icon.ico                # icone de l'application et de l'executable
.gitignore
LICENSE                 # licence MIT
README.md
```

## Licence

Ce projet est publié sous licence [MIT](LICENSE) : gratuit, open source, et
libre de réutilisation, modification et redistribution.

## Soutenir le projet

<div align="center">

**Cet outil est gratuit, open source, et le restera toujours.**
Pas de version payante, pas de fonctionnalité cachée derrière un paywall.

Si PhotoTri vous aide à faire le tri dans vos photos sans abonnement, un
petit café est toujours très apprécié. 🙌

[![Offrez-moi un café sur Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/yoshines62000)

</div>
