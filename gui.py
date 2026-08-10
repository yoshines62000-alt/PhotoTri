"""Interface Tkinter de PhotoTri : recherche de photos en double (doublons
exacts et quasi-doublons - rafales, recompressions) dans un dossier, avec
rangement non destructif (deplacement, jamais de suppression automatique).

Le scan (parcours disque + hachage) tourne sur un thread separe pour ne
jamais geler l'interface sur une grosse bibliotheque : sqlite3 interdisant
de reutiliser une connexion depuis un autre thread que celui qui l'a
creee, le thread de scan ouvre sa PROPRE connexion vers le meme fichier
(voir _scan_worker) plutot que de partager self.db. Pendant toute la duree
du scan, `_set_scan_controls_state("disabled")` desactive Deplacer,
Changer..., Choisir un dossier et la selection de groupes - ce n'est PAS
une simple precaution UX : sans ca, un clic sur Deplacer pendant un scan
ferait ecrire self.db ET worker_db en meme temps sur le meme fichier
(bug trouve a l'audit ; corrige egalement en defense en profondeur par le
mode WAL + busy_timeout de db.py)."""

from __future__ import annotations

import queue
import shutil
import sys
import threading
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional
from tkinter import (
    BOTH, END, HORIZONTAL, LEFT, RIGHT, TOP, X, Y, VERTICAL,
    BooleanVar, Canvas, IntVar, StringVar, Tk, Toplevel, ttk, filedialog, messagebox,
)
from tkinter import font as tkfont

from PIL import ImageTk

import grouping
import hashing
import scanner
import opl_theme
import opl_contact
import update_checker
from db import Database, phash_from_sqlite

APP_TITLE = "PhotoTri"
DONATE_URL = "https://ko-fi.com/yoshines62000"
APP_VERSION = "1.0.10"
UPDATE_REPO = "yoshines62000-alt/PhotoTri"
RELEASES_URL = f"https://github.com/{UPDATE_REPO}/releases/latest"
THUMBNAIL_SIZE = (150, 150)
BODY_FONT = ("Segoe UI", 10)

# Nombre de cartes (vignettes) construites d'un coup pour un groupe (M1 de
# l'audit du 2026-07-28) : avant ce correctif, _show_group_detail
# construisait TOUTES les cartes d'un groupe en une seule passe synchrone
# sur le thread principal (decodage Pillow + redimensionnement de chaque
# vignette compris), sans aucune limite - un groupe de plusieurs centaines
# de quasi-doublons (rafale photo/video tres longue) gelait donc l'interface
# le temps de tout decoder. Plutot que de deplacer le decodage sur un thread
# dedie (ImageTk.PhotoImage doit de toute facon etre cree sur le thread Tk,
# ce qui aurait quand meme impose de reconstruire les widgets par lots via
# une file, pour un gain limite sur les groupes usuels), la pagination borne
# directement le travail synchrone d'un seul affichage a un nombre constant
# de vignettes, quelle que soit la taille reelle du groupe - plus simple a
# integrer proprement ici, avec un bouton "Afficher plus" (voir
# _show_group_detail/_show_more_detail_thumbnails) pour charger la suite par
# lots du meme calibre a la demande.
DETAIL_THUMBNAILS_PAGE_SIZE = 20

# Gris utilise pour tout le texte "secondaire" (taille/dimensions/date sur
# les cartes, barre de statut, numero de version...) - #595959 plutot que le
# #666 initial (bug trouve a l'audit, F4) : sur le fond du theme ttk "alt"
# utilise par l'application (#d9d9d9), #666 offrait un ratio de contraste
# WCAG d'environ 4.07:1, sous le seuil de 4.5:1 requis par le niveau AA pour
# du texte de taille normale - #595959 offre environ 4.9:1, au-dessus du
# seuil, pour un rendu visuellement quasi identique.
SECONDARY_TEXT_COLOR = "#595959"

# Couleur des etoiles pleines du controle de notation (J1, voir
# _build_rating_stars) - dore, pour se distinguer nettement du texte
# secondaire (SECONDARY_TEXT_COLOR) utilise pour les etoiles vides.
RATING_STAR_COLOR = "#c9a227"
RATING_MAX = 5

# Textes du panneau de detail affiches tant qu'aucun groupe n'est
# selectionne a gauche - varient selon l'etat reel de l'application
# (correctif de l'audit, F3 : avant ce correctif, le tout premier message
# restait affiche tel quel meme pendant un scan deja en cours sur un
# dossier deja choisi, ce qui n'avait plus de sens des que l'utilisateur
# avait clairement depasse cette etape).
PLACEHOLDER_TEXT_INITIAL = "Choisissez un dossier, lancez une analyse, puis selectionnez un groupe a gauche."
PLACEHOLDER_TEXT_SCANNING = "Analyse en cours..."
PLACEHOLDER_TEXT_SELECT_GROUP = "Selectionnez un groupe a gauche pour voir le detail."
PLACEHOLDER_TEXT_EMPTY = "Aucun doublon ni quasi-doublon trouve dans les photos indexees."


def _resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def _data_dir() -> Path:
    return Path.home() / "AppData" / "Roaming" / "PhotoTri"


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("o", "Ko", "Mo", "Go"):
        if size < 1024 or unit == "Go":
            return f"{size:.0f} {unit}" if unit == "o" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} Go"


def _friendly_error_text(exc: BaseException) -> str:
    """Traduit une exception technique (message brut Python/Pillow/OS,
    presque toujours en anglais et parfois anxiogene hors contexte - ex.
    "decompression bomb DOS attack", codes WinError bruts) en une phrase
    generique en francais adaptee a un public non technique (bug trouve a
    l'audit, C3). Le detail technique complet n'est jamais perdu : il reste
    enregistre dans erreurs.log (voir _log_technical_error), simplement
    plus impose en premiere lecture dans une boite de dialogue. Le
    classement ci-dessous est deliberement approximatif (recherche de
    sous-chaines dans le type ET le message de l'exception) plutot qu'une
    correspondance exhaustive type-par-type : le but n'est pas de
    diagnostiquer precisement l'erreur, juste d'eviter d'exposer du texte
    technique brut quand une categorie usuelle est reconnaissable, avec un
    message generique en dernier recours."""
    detail = f"{type(exc).__name__} {exc}".lower()
    if (
        "decompressionbomb" in detail
        or "unidentifiedimage" in detail
        or "unreadableimage" in detail
        or "cannot identify image file" in detail
    ):
        return "Un ou plusieurs fichiers semblent corrompus ou dans un format inattendu."
    if (
        isinstance(exc, PermissionError)
        or "permission" in detail
        or "acces refuse" in detail
        or "access is denied" in detail
        or "winerror 5" in detail
    ):
        return "Acces refuse a un fichier ou un dossier (verifiez les droits d'acces)."
    if (
        "no space left" in detail
        or "disk full" in detail
        or "winerror 112" in detail
        or "espace disque" in detail
    ):
        return "Espace disque insuffisant."
    if (
        isinstance(exc, FileNotFoundError)
        or "no such file" in detail
        or "winerror 2" in detail
        or "winerror 3" in detail
    ):
        return "Un fichier ou un dossier attendu est introuvable (peut-etre deplace ou supprime entre-temps)."
    if "database is locked" in detail or "sqlite" in detail:
        return "La base de donnees de PhotoTri est temporairement inaccessible (reessayez dans un instant)."
    if isinstance(exc, OSError):
        return "Un probleme d'acces au disque ou aux fichiers est survenu."
    return "Une erreur inattendue est survenue."


class _Tooltip:
    """Info-bulle minimale (Tkinter n'en fournit pas nativement) : affiche
    `text_getter()` dans une petite fenetre sans decoration au survol de
    `widget`, la referme au depart de la souris. Utilisee pour garder le
    chemin de dossier complet consultable meme quand son affichage est
    tronque (voir _update_folder_label)."""

    def __init__(self, widget, text_getter):
        self._widget = widget
        self._text_getter = text_getter
        self._tipwindow: Optional[Toplevel] = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, event=None):
        text = self._text_getter()
        if not text or self._tipwindow is not None:
            return
        x = self._widget.winfo_rootx() + 4
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        self._tipwindow = tw = Toplevel(self._widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        try:
            tw.attributes("-topmost", True)
        except Exception:
            pass
        ttk.Label(
            tw, text=text, foreground=opl_theme.couleur("texte"), font=BODY_FONT,
            background=opl_theme.couleur("surbrillance"), relief="solid", borderwidth=1, padding=(4, 2),
        ).pack()

    def _hide(self, event=None):
        if self._tipwindow is not None:
            self._tipwindow.destroy()
            self._tipwindow = None


def _unique_destination(dest_dir: Path, filename: str) -> Path:
    """Evite d'ecraser un fichier existant dans le dossier de revision (deux
    photos de dossiers source differents peuvent tres bien s'appeler
    toutes les deux "IMG_0001.jpg") en ajoutant un suffixe numerote."""
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem, suffix = Path(filename).stem, Path(filename).suffix
    n = 2
    while True:
        candidate = dest_dir / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


class PhotoTriApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1150x720")
        self.root.minsize(850, 550)
        opl_theme.apply(self.root, "PhotoTri")

        icon_path = _resource_path("icon.ico")
        if icon_path.exists():
            try:
                self.root.iconbitmap(str(icon_path))
            except Exception:
                pass

        self.db_path = _data_dir() / "phototri.sqlite"
        try:
            self.db = Database(self.db_path)
        except Exception as exc:
            self._show_friendly_error("Impossible d'ouvrir l'index de PhotoTri", exc)
            self.root.destroy()
            raise SystemExit(1)

        self.selected_folder = None
        self.review_folder_var = StringVar()
        self.threshold_var = IntVar(value=grouping.DEFAULT_NEAR_DUPLICATE_THRESHOLD)
        self._scanning = False
        self._stop_event = threading.Event()
        self._queue: "queue.Queue" = queue.Queue()
        self._scan_thread = None
        self._groups: list = []
        self._groups_by_iid: dict = {}
        self._selected_group = None
        # Nombre de cartes actuellement affichees pour le groupe selectionne
        # (voir DETAIL_THUMBNAILS_PAGE_SIZE/_show_group_detail) - reinitialise
        # a DETAIL_THUMBNAILS_PAGE_SIZE des qu'un groupe DIFFERENT est
        # selectionne, mais conserve/augmente quand "Afficher plus" est
        # cliquee sur le meme groupe.
        self._detail_visible_count = DETAIL_THUMBNAILS_PAGE_SIZE
        self._thumbnail_refs: list = []
        self._checkbox_vars: dict = {}
        # Id de la photo suggeree a garder (keeper) du groupe actuellement
        # affiche - memorise ici par _show_group_detail pour que l'action
        # "Cocher les doublons (sauf la suggeree)" sache quelle case ne
        # JAMAIS cocher, sans recalculer suggest_keeper.
        self._current_keeper_id = None
        # Selection persistante inter-groupes (feature "tout sauf la
        # suggeree", portee "tous les groupes") : ids des photos marquees a
        # deplacer dans des groupes NON affiches (donc sans BooleanVar en
        # memoire). _move_checked_photos les reunit avec les cases cochees du
        # groupe visible. Videe a chaque nouveau calcul de groupes
        # (_apply_groups) car les ids d'un calcul precedent n'ont plus de
        # sens apres un regroupement. Ne contient jamais un keeper.
        self._marked_ids: set = set()
        # Widgets etoiles de notation (J1) indexes par id de photo, pour que
        # les clics ("<Button-1>") et un rafraichissement ulterieur puissent
        # retrouver les 5 Label d'une carte sans reparcourir tout le canvas.
        self._rating_star_labels: dict = {}
        # Identifiants Tcl des gestionnaires (clic ET clavier - voir M2 de
        # l'audit du 2026-07-28 dans _build_rating_stars) lies aux etoiles
        # actuellement affichees (voir _build_rating_stars/_show_group_detail) -
        # necessaires pour les liberer explicitement (Widget.destroy() ne le
        # fait PAS automatiquement : une commande enregistree via .bind()
        # reste vivante dans l'interpreteur Tcl tant qu'elle n'est pas
        # explicitement supprimee ou que la racine Tk elle-meme n'est pas
        # detruite) avant de reconstruire les cartes - sans ce nettoyage,
        # chaque changement de groupe affiche laisse une fermeture Python
        # orpheline referencant `self`, un cycle de references que seul le
        # ramasse-miettes cyclique peut resoudre ; observe en ecrivant ce
        # correctif : leur accumulation au fil de nombreuses reconstructions
        # de cartes pouvait retarder une collecte cyclique ulterieure au
        # point de faire paraitre l'application figee pendant plusieurs
        # secondes.
        self._rating_star_funcids: list = []
        self._grouping_in_progress = False
        self._grouping_thread = None
        self._grouping_queue: "queue.Queue" = queue.Queue()
        self._pending_status_prefix = None
        # Deplacement des photos cochees (voir _move_checked_photos) : meme
        # pattern thread + Queue + root.after que le scan (_scan_worker) et
        # le regroupement (_refresh_groups) - bug trouve a l'audit : cette
        # operation tournait auparavant entierement en synchrone sur le
        # thread principal Tkinter, gelant l'interface pendant tout le
        # deplacement d'un gros lot de photos.
        self._moving = False
        self._move_thread = None
        self._move_queue: "queue.Queue" = queue.Queue()
        # Seuil utilise pour le DERNIER calcul de groupes termine - voir
        # _refresh_groups. Initialise a la valeur par defaut pour que
        # l'indicateur de confiance (A2) ait toujours une valeur coherente
        # meme avant le tout premier calcul.
        self._last_threshold = grouping.DEFAULT_NEAR_DUPLICATE_THRESHOLD

        self._build_layout()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # Filet de securite global : sans ceci, Tkinter avale silencieusement
        # toute exception levee dans un callback (clic de bouton...),
        # invisible dans l'exe package sans console - bug trouve a l'audit.
        self.root.report_callback_exception = self._handle_callback_exception

    # -- construction de l'interface ---------------------------------------------

    def _build_layout(self):
        opl_theme.entete(self.root, "PhotoTri", "Detecteur de photos en double", on_contact=lambda: opl_contact.ouvrir(self.root, app="PhotoTri", version=APP_VERSION)).pack(fill=X)

        top = ttk.Frame(self.root)
        top.pack(fill=X, padx=10, pady=10)
        self.top_frame = top

        self.choose_folder_button = ttk.Button(top, text="Choisir un dossier a analyser...", command=self._choose_folder)
        self.choose_folder_button.pack(side=LEFT)
        self._folder_full_path = "Aucun dossier choisi"
        self.folder_label_var = StringVar(value=self._folder_full_path)
        # Pas de largeur/wraplength fixe ici : la largeur affichee est
        # recalculee dynamiquement (_update_folder_label) en fonction de
        # l'espace reellement disponible, pour ne jamais repousser les
        # controles de droite (Analyser/Arreter) hors champ - voir
        # _folder_label_available_width.
        self.folder_label = ttk.Label(top, textvariable=self.folder_label_var, foreground=opl_theme.couleur("texte"), font=BODY_FONT)
        self.folder_label.pack(side=LEFT, padx=10)
        self._folder_font = tkfont.Font(font=BODY_FONT)
        self._folder_tooltip = _Tooltip(self.folder_label, lambda: self._folder_full_path)

        right_controls = ttk.Frame(top)
        right_controls.pack(side=RIGHT)
        self.right_controls_frame = right_controls
        ttk.Label(right_controls, text="Sensibilite quasi-doublons :", foreground=opl_theme.couleur("texte"), font=BODY_FONT).pack(side=LEFT)
        # validate="key" + validatecommand bloque toute saisie non numerique a
        # la racine - bug trouve a l'audit : IntVar.get() levait TclError sur
        # une saisie libre non numerique, plantage silencieux (aucune console
        # dans l'exe package) du prochain recalcul de groupes.
        validate_digits_cmd = self.root.register(self._validate_digits_input)
        threshold_spinbox = ttk.Spinbox(
            right_controls, from_=0, to=20, textvariable=self.threshold_var, width=4,
            validate="key", validatecommand=(validate_digits_cmd, "%P"),
        )
        threshold_spinbox.pack(side=LEFT, padx=(4, 10))
        self.recompute_button = ttk.Button(right_controls, text="Recalculer les groupes", command=self._refresh_groups)
        self.recompute_button.pack(side=LEFT, padx=(0, 10))
        self.scan_button = ttk.Button(right_controls, text="Analyser (scanner)", command=self._start_scan, state="disabled", style="Accent.TButton")
        self.scan_button.pack(side=LEFT)
        self.stop_button = ttk.Button(right_controls, text="Arreter", command=self._request_stop, state="disabled")
        self.stop_button.pack(side=LEFT, padx=(6, 0))

        # <Configure> de `top` capture les redimensionnements reels de la
        # fenetre (fill=X propage la largeur de root a top) ; recalculer la
        # troncature du chemin affiche a chaque fois garantit que les
        # boutons Analyser/Arreter restent toujours visibles, meme sur un
        # chemin de dossier long (bug trouve a l'audit : sans cette borne,
        # un chemin OneDrive realiste de 144 caracteres poussait ces deux
        # boutons entierement hors champ - ismapped=0, non cliquables - a
        # la taille de fenetre par defaut). Lie ici, une fois tous les
        # widgets de `top` construits, pour que le premier <Configure>
        # (declenche au tout premier affichage de la fenetre) trouve deja
        # right_controls_frame pret.
        top.bind("<Configure>", lambda event: self._update_folder_label())
        self._update_folder_label()

        progress_frame = ttk.Frame(self.root)
        progress_frame.pack(fill=X, padx=10)
        self.progress_bar = ttk.Progressbar(progress_frame, orient=HORIZONTAL, mode="determinate")
        self.progress_bar.pack(fill=X, side=TOP)
        self.status_var = StringVar(value="")
        ttk.Label(progress_frame, textvariable=self.status_var, foreground=SECONDARY_TEXT_COLOR).pack(anchor="w", pady=(2, 8))

        body = ttk.PanedWindow(self.root, orient=HORIZONTAL)
        body.pack(fill=BOTH, expand=True, padx=10, pady=(0, 5))

        left = ttk.Frame(body)
        body.add(left, weight=1)
        # Colonne "Confiance" ajoutee (A2 de l'audit) : avant ce correctif,
        # rien dans l'UI ne permettait de distinguer un vrai quasi-doublon
        # d'un faux positif du dHash (voir A1) sans comparer visuellement
        # chaque carte - un gros groupe heterogene (ex. 25 photos sans
        # rapport, cas mesure a l'audit) ne portait aucune indication.
        columns = ("kind", "count", "size", "confidence")
        self.groups_tree = ttk.Treeview(left, columns=columns, show="headings", height=20)
        for col, label, width in [
            ("kind", "Type", 90), ("count", "Photos", 60), ("size", "Poids total", 100),
            ("confidence", "Confiance", 130),
        ]:
            self.groups_tree.heading(col, text=label)
            self.groups_tree.column(col, width=width, anchor="w")
        self.groups_tree.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar = ttk.Scrollbar(left, orient=VERTICAL, command=self.groups_tree.yview)
        scrollbar.pack(side=LEFT, fill=Y)
        self.groups_tree.configure(yscrollcommand=scrollbar.set)
        self.groups_tree.bind("<<TreeviewSelect>>", self._on_group_select)

        right = ttk.Frame(body)
        body.add(right, weight=3)
        self._build_detail_panel(right)

        bottom_bar = ttk.Frame(self.root)
        bottom_bar.pack(fill=X, side="bottom")
        ttk.Label(bottom_bar, text=f"v{APP_VERSION}", foreground=SECONDARY_TEXT_COLOR).pack(side=LEFT, padx=(8, 0), pady=4)
        self.update_status_var = StringVar(value="")
        self.update_status_label = ttk.Label(bottom_bar, textvariable=self.update_status_var, foreground=SECONDARY_TEXT_COLOR)
        self.update_status_label.pack(side=LEFT, padx=(6, 0), pady=4)
        donate_label = ttk.Label(bottom_bar, text="☕ Soutenir le projet", foreground=opl_theme.couleur("lien"), cursor="hand2")
        donate_label.pack(side=RIGHT, padx=8, pady=4)
        donate_label.bind("<Button-1>", lambda event: webbrowser.open(DONATE_URL))

        self._update_check_queue = queue.Queue()
        update_checker.start_update_check(APP_VERSION, UPDATE_REPO, self._update_check_queue)
        self.root.after(500, self._poll_update_check)

    def _poll_update_check(self):
        try:
            status, tag = self._update_check_queue.get_nowait()
        except queue.Empty:
            self.root.after(500, self._poll_update_check)
            return
        if status == "update_available":
            self.update_status_var.set(f"Mise a jour disponible : {tag} - Telecharger")
            self.update_status_label.configure(foreground=opl_theme.couleur("lien"), cursor="hand2")
            self.update_status_label.bind("<Button-1>", lambda event: webbrowser.open(RELEASES_URL))
        elif status == "up_to_date":
            self.update_status_var.set("A jour")
            self.update_status_label.configure(foreground=opl_theme.couleur("succes"), cursor="")
        # "check_failed" (hors ligne, GitHub inaccessible...) : on ne
        # revendique rien plutot que d'afficher a tort "a jour".

    def _build_detail_panel(self, parent):
        self.detail_placeholder = ttk.Label(
            parent, text=PLACEHOLDER_TEXT_INITIAL,
            foreground=opl_theme.couleur("texte"), font=BODY_FONT, wraplength=500, justify="left",
        )
        self.detail_placeholder.pack(padx=15, pady=15, anchor="w")

        self.detail_frame = ttk.Frame(parent)

        # Barre de selection groupee (feature "tout sauf la photo suggeree") :
        # coche en un clic tous les doublons a deplacer SAUF la photo suggeree
        # a garder (le keeper, marque d'une ★). Placee AU-DESSUS des cartes et
        # separee de la barre d'action "Deplacer" du bas, pour ne pas
        # comprimer le bouton Deplacer sous sa largeur naturelle (verrouille
        # par TestMoveButtonLabelFitsItsOwnWidth). Se branche sur les
        # BooleanVar existantes des cartes (self._checkbox_vars) plutot que de
        # reinventer un mecanisme de selection.
        selection_bar = ttk.Frame(self.detail_frame)
        selection_bar.pack(fill=X, pady=(0, 6))
        ttk.Label(selection_bar, text="Selection :", style="Section.TLabel").pack(side=LEFT, padx=(0, 6))
        self.select_dupes_button = ttk.Button(
            selection_bar, text="Cocher les doublons (sauf ★)",
            command=lambda: self._check_duplicates_except_keeper(all_groups=False),
        )
        self.select_dupes_button.pack(side=LEFT)
        self.select_dupes_all_button = ttk.Button(
            selection_bar, text="... dans tous les groupes",
            command=lambda: self._check_duplicates_except_keeper(all_groups=True),
        )
        self.select_dupes_all_button.pack(side=LEFT, padx=(6, 0))

        canvas_frame = ttk.Frame(self.detail_frame)
        canvas_frame.pack(fill=BOTH, expand=True)
        self.cards_canvas = Canvas(canvas_frame, highlightthickness=0)
        h_scroll = ttk.Scrollbar(canvas_frame, orient=HORIZONTAL, command=self.cards_canvas.xview)
        v_scroll = ttk.Scrollbar(canvas_frame, orient=VERTICAL, command=self.cards_canvas.yview)
        self.cards_canvas.configure(xscrollcommand=h_scroll.set, yscrollcommand=v_scroll.set)
        self.cards_canvas.pack(side=TOP, fill=BOTH, expand=True)
        h_scroll.pack(side=TOP, fill=X)
        v_scroll.pack(side=RIGHT, fill=Y)
        self.cards_inner = ttk.Frame(self.cards_canvas)
        self._cards_window = self.cards_canvas.create_window((0, 0), window=self.cards_inner, anchor="nw")
        self.cards_inner.bind("<Configure>", lambda e: self.cards_canvas.configure(scrollregion=self.cards_canvas.bbox("all")))

        action_bar = ttk.Frame(self.detail_frame)
        action_bar.pack(fill=X, pady=(8, 0))
        ttk.Label(action_bar, text="Dossier de revision :", foreground=opl_theme.couleur("texte"), font=BODY_FONT).pack(side=LEFT)
        ttk.Entry(action_bar, textvariable=self.review_folder_var, width=45, state="readonly").pack(side=LEFT, padx=5)
        self.change_review_button = ttk.Button(action_bar, text="Changer...", command=self._choose_review_folder)
        self.change_review_button.pack(side=LEFT)
        # Libelle raccourci (bug trouve a l'audit, F2) : le texte complet
        # "Deplacer les photos cochees vers le dossier de revision" se
        # retrouvait compresse en dessous de sa largeur naturelle des que
        # l'espace manquait dans la barre d'action - ttk.Button ne
        # redimensionne pas son texte, il le laisse simplement deborder de
        # son propre cadre (derniere(s) lettre(s) coupees), en particulier
        # a la taille minimale officiellement supportee par l'application
        # (root.minsize(850, 550)). Le contexte "vers le dossier de
        # revision" reste de toute facon deja visible juste a gauche via le
        # champ "Dossier de revision :", ce qui rend la version longue
        # redondante.
        self.move_button = ttk.Button(
            action_bar, text="Deplacer la selection",
            command=self._move_checked_photos,
        )
        self.move_button.pack(side=RIGHT)

    # -- choix des dossiers --------------------------------------------------------

    def _choose_folder(self):
        chosen = filedialog.askdirectory(title="Choisir le dossier de photos a analyser")
        if not chosen:
            return
        self.selected_folder = Path(chosen)
        self._folder_full_path = str(self.selected_folder)
        self._update_folder_label()
        self.scan_button.configure(state="normal")
        if not self.review_folder_var.get():
            # A cote du dossier scanne, PAS dedans - bug trouve a l'audit :
            # un defaut a l'interieur du dossier scanne faisait reindexer
            # comme photos actives neuves les doublons "ranges" (deplaces
            # non destructivement) des le rescan suivant, annulant
            # l'interet du rangement. scan_folder exclut en plus
            # activement ce dossier du parcours (defense en profondeur),
            # y compris si l'utilisateur le replace manuellement dedans
            # via "Changer...".
            self.review_folder_var.set(str(self.selected_folder.parent / f"{self.selected_folder.name}_PhotoTri_a_revoir"))

    def _choose_review_folder(self):
        chosen = filedialog.askdirectory(title="Choisir le dossier de revision (destination des doublons deplaces)")
        if chosen:
            self.review_folder_var.set(chosen)

    # -- affichage du chemin de dossier (largeur bornee) ----------------------------

    def _folder_label_available_width(self) -> int:
        """Largeur en pixels que le libellé du dossier peut occuper sans
        repousser `right_controls_frame` (Sensibilite/Recalculer/Analyser/
        Arreter) hors de la fenetre. Base sur la largeur REELLEMENT allouee
        a `top` (bornee par la fenetre, meme si les enfants demanderaient
        plus - c'est precisement l'absence de cette borne qui causait le
        bug : un Label sans largeur maximale peut demander une largeur
        illimitee, forçant right_controls_frame a etre packe hors champ).
        Marge genereuse (60px) pour absorber les paddings/bordures internes
        non capturees par winfo_reqwidth()."""
        self.top_frame.update_idletasks()
        total_width = self.top_frame.winfo_width()
        if total_width <= 1:
            # Fenetre pas encore mappee (tout premier appel avant affichage) :
            # se rabattre sur la largeur de la fenetre elle-meme.
            total_width = self.root.winfo_width() or 1150
        reserved = self.choose_folder_button.winfo_reqwidth() + self.right_controls_frame.winfo_reqwidth()
        margin = 60
        return max(total_width - reserved - margin, 40)

    def _truncate_text_to_pixel_width(self, text: str, max_width: int) -> str:
        """Tronque `text` (en gardant la FIN de la chaine - c'est la partie
        la plus informative d'un chemin, ex. le nom du sous-dossier final)
        pour que sa largeur rendue avec self._folder_font ne depasse pas
        `max_width` pixels, en prefixant par "..." si tronque. Mesure reelle
        via Font.measure plutot qu'un simple compte de caracteres : robuste
        aux polices a chasse variable et aux parametres d'echelle Windows."""
        if self._folder_font.measure(text) <= max_width:
            return text
        ellipsis = "..."
        if self._folder_font.measure(ellipsis) >= max_width:
            return ellipsis
        lo, hi, best = 0, len(text), ""
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = text[-mid:] if mid > 0 else ""
            if self._folder_font.measure(ellipsis + candidate) <= max_width:
                best = candidate
                lo = mid + 1
            else:
                hi = mid - 1
        return ellipsis + best

    def _update_folder_label(self) -> None:
        """Recalcule le texte affiche pour le chemin de dossier choisi en
        fonction de l'espace reellement disponible - voir
        _folder_label_available_width. Le chemin complet reste toujours
        consultable via l'info-bulle (_folder_tooltip) au survol."""
        available = self._folder_label_available_width()
        truncated = self._truncate_text_to_pixel_width(self._folder_full_path, available)
        if truncated != self.folder_label_var.get():
            self.folder_label_var.set(truncated)

    # -- scan (thread separe) ------------------------------------------------------

    def _set_scan_controls_state(self, state: str) -> None:
        """Active/desactive les controles qui ne doivent jamais etre utilises
        pendant qu'un scan ecrit sur sa propre connexion SQLite (`worker_db`)
        - bug trouve a l'audit : le bouton Deplacer, le bouton Changer... et
        la selection de groupes restaient actifs pendant un scan, rendant
        possible un acces concurrent reel a `phototri.sqlite` alors meme que
        le commentaire en tete de fichier affirmait le contraire."""
        self.recompute_button.configure(state=state)
        self.choose_folder_button.configure(state=state)
        self.move_button.configure(state=state)
        self.change_review_button.configure(state=state)
        if state == "disabled":
            self.groups_tree.state(("disabled",))
        else:
            self.groups_tree.state(("!disabled",))

    def _start_scan(self):
        if self._scanning or self.selected_folder is None:
            return
        self._scanning = True
        self._stop_event.clear()
        self.scan_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._set_scan_controls_state("disabled")
        # Mode indetermine pendant le listing initial des fichiers (peut
        # prendre du temps sur un tres gros dossier, avant le premier
        # progress_callback) ; _poll_scan_queue bascule en mode determine
        # des la premiere vraie progression recue.
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start(10)
        self.status_var.set("Analyse en cours...")
        # Texte du panneau de detail mis a jour pour refleter l'etat reel de
        # l'application (correctif de l'audit, F3) : avant ce correctif, le
        # message d'accueil initial ("Choisissez un dossier...") restait
        # affiche tel quel meme pendant un scan deja en cours sur un dossier
        # deja choisi - sans effet visible si un groupe est actuellement
        # affiche (le placeholder est alors masque), mais correct des que
        # l'utilisateur revient au panneau de detail (aucun groupe
        # selectionne) pendant le scan.
        self.detail_placeholder.configure(text=PLACEHOLDER_TEXT_SCANNING)

        # Capture depuis le thread principal (Tkinter StringVar) avant de
        # lancer le thread de scan : le dossier de revision courant, quel
        # que soit son emplacement choisi par l'utilisateur, est exclu du
        # parcours pour qu'un doublon deplace la ne revienne jamais polluer
        # l'index au rescan suivant (voir _iter_image_files).
        review_folder = self.review_folder_var.get()
        exclude_dir = Path(review_folder) if review_folder else None
        thread = threading.Thread(
            target=self._scan_worker, args=(self.selected_folder, exclude_dir), daemon=True,
        )
        self._scan_thread = thread
        thread.start()
        self.root.after(100, self._poll_scan_queue)

    def _request_stop(self):
        self._stop_event.set()
        self.status_var.set("Arret demande...")

    def _scan_worker(self, folder: Path, exclude_dir: Optional[Path] = None):
        worker_db = None
        try:
            # Construction de la connexion DEDANS le try - bug trouve a
            # l'audit : si l'ouverture echoue (fichier verrouille, disque
            # plein, chemin invalide...), l'exception n'etait interceptee
            # nulle part, le thread mourait silencieusement (pas de console
            # dans l'exe package) et rien n'etait jamais pousse dans la
            # file, laissant l'app bloquee indefiniment sur "Analyse en
            # cours..." avec les boutons desactives a vie.
            worker_db = Database(self.db_path)

            def on_progress(done, total, path):
                self._queue.put(("progress", done, total, path))

            result = scanner.scan_folder(
                folder, worker_db, progress_callback=on_progress, should_stop=self._stop_event.is_set,
                exclude_dir=exclude_dir,
            )
            self._queue.put(("done", result))
        except Exception as exc:
            # L'exception elle-meme est poussee dans la file (pas juste son
            # texte) : _poll_scan_queue, sur le thread principal, en tire un
            # message francais generique via _show_friendly_error plutot que
            # d'afficher tel quel le texte brut de l'exception (bug trouve a
            # l'audit, C3) - le detail technique complet part dans
            # erreurs.log, jamais perdu.
            self._queue.put(("error", exc))
        finally:
            if worker_db is not None:
                worker_db.close()

    def _poll_scan_queue(self):
        try:
            while True:
                message = self._queue.get_nowait()
                kind = message[0]
                if kind == "progress":
                    _, done, total, path = message
                    if str(self.progress_bar["mode"]) == "indeterminate":
                        self.progress_bar.stop()
                        self.progress_bar.configure(mode="determinate")
                    self.progress_bar.configure(value=done, maximum=max(total, 1))
                    self.status_var.set(f"{done} / {total} - {path}")
                elif kind == "done":
                    self._on_scan_finished(message[1])
                    return
                elif kind == "error":
                    self._scanning = False
                    self.scan_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self._set_scan_controls_state("normal")
                    self.progress_bar.stop()
                    self.progress_bar.configure(mode="determinate")
                    self._show_friendly_error("L'analyse a echoue", message[1])
                    return
        except queue.Empty:
            pass
        if self._scanning:
            self.root.after(100, self._poll_scan_queue)

    def _on_scan_finished(self, result: scanner.ScanResult):
        self._scanning = False
        self.scan_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self._set_scan_controls_state("normal")
        if str(self.progress_bar["mode"]) == "indeterminate":
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate")
        summary = (
            f"Analyse terminee : {result.scanned} photo(s) indexee(s), "
            f"{result.skipped_unchanged} inchangee(s), {result.pruned} disparue(s) retiree(s) de l'index."
        )
        if result.errors:
            summary += f" {len(result.errors)} fichier(s) illisible(s) ignore(s)."
        self._pending_status_prefix = summary
        self._refresh_groups()

    # -- regroupement et affichage --------------------------------------------------

    @staticmethod
    def _validate_digits_input(proposed: str) -> bool:
        """Bloque toute saisie non numerique dans le Spinbox de sensibilite
        AVANT qu'elle n'atteigne l'IntVar - bug trouve a l'audit :
        `IntVar.get()` leve `TclError` sur une saisie libre non numerique
        (le Spinbox n'a par defaut aucune limite sur la saisie clavier,
        seulement sur les fleches), plantage silencieux du prochain calcul
        de groupes (aucune console dans l'exe package pour le voir)."""
        return proposed == "" or proposed.isdigit()

    def _get_threshold(self) -> int:
        """Lecture defensive de threshold_var : une chaine vide (champ en
        cours d'edition) ou hors bornes (ex: saisie "9999", que
        `_validate_digits_input` n'empeche pas puisqu'elle ne borne que les
        CARACTERES, pas la valeur finale) ne doit jamais planter le calcul
        de groupes ni forcer silencieusement le chemin exhaustif O(n^2)
        (voir grouping.py) sans borne raisonnable."""
        try:
            value = self.threshold_var.get()
        except Exception:
            value = grouping.DEFAULT_NEAR_DUPLICATE_THRESHOLD
            self.threshold_var.set(value)
        return max(0, min(20, value))

    def _refresh_groups(self):
        if self._grouping_in_progress or self._scanning:
            return
        photos = list(self.db.list_active_photos())
        threshold = self._get_threshold()
        # Memorise le seuil reellement utilise pour CE calcul - relu par
        # _apply_groups/_build_photo_card pour l'indicateur de confiance
        # (A2) : l'utilisateur peut modifier le Spinbox entre le lancement
        # du calcul et l'affichage du resultat, le libelle affiche doit
        # rester coherent avec le regroupement effectivement obtenu plutot
        # que de relire threshold_var a un moment quelconque plus tard.
        self._last_threshold = threshold
        # Nombre de bandes de l'indexage LSH (voir grouping.py) transmis a
        # group_photos, ajuste pour rester STRICTEMENT superieur au seuil
        # choisi - c'est exactement la condition (near_duplicate_threshold <
        # bands) que group_photos verifie pour emprunter son chemin rapide.
        # Bug trouve a l'audit : le Spinbox de sensibilite autorise 0-20
        # (voir threshold_spinbox) mais `bands` restait fige a
        # grouping.DEFAULT_BANDS (8), jamais transmis depuis l'UI - tout
        # seuil >= 8 retombait donc silencieusement sur la comparaison
        # exhaustive O(n^2) (lente sur une grosse bibliotheque), sans le
        # moindre avertissement ni le moindre effet reel de la valeur
        # choisie au-dela de 7. DEFAULT_BANDS reste le plancher ici pour ne
        # rien changer au comportement deja eprouve des seuils 0-7.
        bands = max(grouping.DEFAULT_BANDS, threshold + 1)
        self._grouping_in_progress = True
        self.scan_button.configure(state="disabled")
        self._set_scan_controls_state("disabled")
        # Mode indetermine pendant la preparation des candidats (indexage
        # LSH, pas de granularite avant la boucle de verification des
        # paires) ; _poll_grouping_queue bascule en mode determine des la
        # premiere progression recue - meme logique que _start_scan, voir
        # B1 de l'audit : avant ce correctif, aucun retour de progression
        # n'etait jamais offert ici, seulement ce texte statique.
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start(10)
        self.status_var.set("Calcul des groupes...")

        def on_progress(done, total):
            self._grouping_queue.put(("progress", done, total))

        def worker():
            groups = grouping.group_photos(
                photos, near_duplicate_threshold=threshold, bands=bands, progress_callback=on_progress,
            )
            self._grouping_queue.put(("done", groups))

        thread = threading.Thread(target=worker, daemon=True)
        self._grouping_thread = thread
        thread.start()
        self.root.after(50, self._poll_grouping_queue)

    def _poll_grouping_queue(self):
        try:
            while True:
                kind, *rest = self._grouping_queue.get_nowait()
                if kind == "progress":
                    done, total = rest
                    if str(self.progress_bar["mode"]) == "indeterminate":
                        self.progress_bar.stop()
                        self.progress_bar.configure(mode="determinate")
                    self.progress_bar.configure(value=done, maximum=max(total, 1))
                    self.status_var.set(f"Calcul des groupes... {done} / {total}")
                elif kind == "done":
                    groups = rest[0]
                    self._grouping_in_progress = False
                    self.scan_button.configure(state="normal")
                    self._set_scan_controls_state("normal")
                    if str(self.progress_bar["mode"]) == "indeterminate":
                        self.progress_bar.stop()
                        self.progress_bar.configure(mode="determinate")
                    self._apply_groups(groups)
                    return
        except queue.Empty:
            pass
        if self._grouping_in_progress:
            self.root.after(50, self._poll_grouping_queue)

    def _confidence_text(self, group) -> str:
        """Libelle de la colonne "Confiance" (A2 de l'audit) : un groupe
        "exact" est par construction une copie bit a bit (sha256 identique),
        donc toujours 100% ; un groupe "near" affiche le pourcentage de
        similarite (grouping.similarity_percent) et le libelle qualitatif
        (grouping.confidence_label) derives de la distance de Hamming
        maximale entre deux de ses membres (group.max_distance) - le
        "maillon le plus faible" du groupe, celui qui merite le plus de
        vigilance de la part de l'utilisateur avant un deplacement en
        masse."""
        if group.kind == "exact":
            return "100% - Identique"
        pct = grouping.similarity_percent(group.max_distance)
        return f"{pct}% - {grouping.confidence_label(group.max_distance, self._last_threshold)}"

    def _apply_groups(self, groups: list) -> None:
        # Toute selection inter-groupes precedente (self._marked_ids) porte
        # sur des ids issus du calcul precedent : elle n'a plus de sens apres
        # un nouveau regroupement, on repart d'une selection vide.
        self._marked_ids.clear()
        self._groups = groups
        self._groups.sort(key=lambda g: (g.kind != "exact", -len(g.photo_ids)))
        # Un seul appel a list_active_photos() puis un dict en memoire,
        # plutot qu'un self.db.get_photo(pid) par photo par groupe -
        # optimisation trouvee a l'audit (N+1 requetes SQLite), qui a aussi
        # pour effet de securiser l'acces : une photo disparue entre le
        # calcul des groupes et cet affichage est simplement absente du
        # dict plutot que de faire planter `row["size"]` sur None.
        photos_by_id = {p["id"]: p for p in self.db.list_active_photos()}

        self.groups_tree.delete(*self.groups_tree.get_children())
        self._groups_by_iid = {}
        for index, group in enumerate(self._groups):
            iid = str(index)
            self._groups_by_iid[iid] = group
            total_size = sum(
                (photos_by_id[pid]["size"] or 0) for pid in group.photo_ids if pid in photos_by_id
            )
            label = "Exact" if group.kind == "exact" else "Quasi-doublon"
            self.groups_tree.insert(
                "", END, iid=iid,
                values=(label, len(group.photo_ids), _format_size(total_size), self._confidence_text(group)),
            )

        self._show_group_detail(None)
        # Texte du placeholder mis a jour selon le resultat reel du calcul
        # (F3 de l'audit) : "Aucun doublon..." si le regroupement est vide
        # (deja gere avant ce correctif), sinon une invite a selectionner un
        # groupe plutot que de laisser trainer le message d'accueil initial
        # ("Choisissez un dossier...") ou "Analyse en cours..." (F3), qui
        # n'ont plus de sens une fois qu'un calcul de groupes a abouti.
        self.detail_placeholder.configure(
            text=PLACEHOLDER_TEXT_EMPTY if not self._groups else PLACEHOLDER_TEXT_SELECT_GROUP,
        )

        group_message = f"{len(self._groups)} groupe(s) de doublons/quasi-doublons trouve(s)."
        prefix = self._pending_status_prefix
        self._pending_status_prefix = None
        self.status_var.set(f"{prefix} {group_message}" if prefix else group_message)

    def _on_group_select(self, event=None):
        selection = self.groups_tree.selection()
        if not selection:
            return
        group = self._groups_by_iid.get(selection[0])
        self._show_group_detail(group)

    def _show_group_detail(self, group):
        # Reinitialise la pagination (M1 de l'audit du 2026-07-28, voir
        # DETAIL_THUMBNAILS_PAGE_SIZE) UNIQUEMENT quand le groupe affiche
        # change reellement - _show_more_detail_thumbnails() rappelle cette
        # meme methode avec le MEME objet groupe (comparaison par identite)
        # apres avoir augmente _detail_visible_count, ce qui ne doit surtout
        # pas se faire ecraser ici a chaque clic sur "Afficher plus".
        if group is not self._selected_group:
            self._detail_visible_count = DETAIL_THUMBNAILS_PAGE_SIZE
        self._selected_group = group
        self._thumbnail_refs = []
        self._checkbox_vars = {}
        self._rating_star_labels = {}
        # Libere explicitement les commandes Tcl des etoiles de l'affichage
        # precedent AVANT de detruire les cartes (voir le commentaire de
        # _rating_star_funcids dans __init__) - Widget.destroy() ne le fait
        # pas tout seul.
        for star_label, funcid in self._rating_star_funcids:
            try:
                star_label.deletecommand(funcid)
            except Exception:
                pass
        self._rating_star_funcids = []
        for widget in self.cards_inner.winfo_children():
            widget.destroy()

        if group is None:
            self._current_keeper_id = None
            self.detail_frame.pack_forget()
            self.detail_placeholder.pack(padx=15, pady=15, anchor="w")
            return
        self.detail_placeholder.pack_forget()
        self.detail_frame.pack(fill=BOTH, expand=True)

        # Filtre les photos disparues depuis le calcul du groupe (deplacees
        # hors de PhotoTri entre-temps, etc.) - bug trouve a l'audit :
        # self.db.get_photo(pid) peut renvoyer None, et l'indexation directe
        # photo["size"] plus bas plantait alors le callback (silencieusement,
        # cf. _handle_callback_exception plus haut dans ce fichier).
        photos = [p for p in (self.db.get_photo(pid) for pid in group.photo_ids) if p is not None]
        if len(photos) < 2:
            ttk.Label(
                self.cards_inner,
                text="Ce groupe n'est plus valide (photo(s) disparue(s) depuis le calcul - relancez une analyse).",
                foreground=opl_theme.couleur("texte"), font=BODY_FONT, wraplength=500,
            ).grid(row=0, column=0, padx=15, pady=15)
            return
        keeper_id = grouping.suggest_keeper(photos)
        self._current_keeper_id = keeper_id
        keeper_phash = phash_from_sqlite(next(p["phash"] for p in photos if p["id"] == keeper_id))

        # Pagination (M1 de l'audit du 2026-07-28) : seules les
        # `_detail_visible_count` premieres photos du groupe sont
        # effectivement construites en cartes ici, ce qui borne le travail
        # synchrone (decodage Pillow + redimensionnement compris, voir
        # _build_photo_card) a un nombre constant quelle que soit la taille
        # reelle du groupe - voir DETAIL_THUMBNAILS_PAGE_SIZE pour le detail
        # du choix (pagination plutot que thread dedie).
        visible_count = min(self._detail_visible_count, len(photos))
        for column, photo in enumerate(photos[:visible_count]):
            self._build_photo_card(
                self.cards_inner, column, photo, is_keeper=(photo["id"] == keeper_id),
                group_kind=group.kind, keeper_phash=keeper_phash,
            )

        remaining = len(photos) - visible_count
        if remaining > 0:
            more_button = ttk.Button(
                self.cards_inner,
                text=f"Afficher {min(DETAIL_THUMBNAILS_PAGE_SIZE, remaining)} de plus... ({remaining} restante(s))",
                command=self._show_more_detail_thumbnails,
            )
            more_button.grid(row=0, column=visible_count, padx=6, pady=6, sticky="n")

    def _show_more_detail_thumbnails(self) -> None:
        """Gestionnaire du bouton "Afficher plus" (M1 de l'audit du
        2026-07-28, voir DETAIL_THUMBNAILS_PAGE_SIZE) : agrandit la
        pagination d'un lot supplementaire puis reconstruit l'affichage du
        groupe courant - simple reappel de _show_group_detail avec le MEME
        objet groupe (voir sa garde sur `is not` en tete de methode), qui ne
        reinitialise donc pas _detail_visible_count qu'on vient juste
        d'augmenter."""
        if self._selected_group is None:
            return
        self._detail_visible_count += DETAIL_THUMBNAILS_PAGE_SIZE
        self._show_group_detail(self._selected_group)

    def _build_photo_card(self, parent, column, photo, is_keeper: bool, group_kind: str, keeper_phash: int):
        card = ttk.Frame(parent, relief="solid", borderwidth=1, padding=6)
        card.grid(row=0, column=column, padx=6, pady=6, sticky="n")

        thumb_label = ttk.Label(card)
        thumb_label.pack()
        try:
            # hashing.open_image() (M1, et F4 de l'audit du 2026-07-28 pour
            # la factorisation elle-meme) plutot que Image.open(photo["path"])
            # directement : permet d'afficher la vignette d'une photo dont le
            # chemin complet depasse MAX_PATH quand Windows le permet, au
            # lieu de se contenter de l'echec gracieux ("[image indisponible]")
            # deja en place ci-dessous.
            with hashing.open_image(photo["path"]) as img:
                # draft() accelere nettement le decodage JPEG quand on ne
                # veut qu'une vignette (no-op silencieux sur les formats non
                # JPEG) - optimisation trouvee a l'audit, le decodage pleine
                # resolution etait fait pour rien avant le .thumbnail().
                img.draft("RGB", THUMBNAIL_SIZE)
                img = img.copy()
            img.thumbnail(THUMBNAIL_SIZE)
            photo_image = ImageTk.PhotoImage(img)
            self._thumbnail_refs.append(photo_image)
            thumb_label.configure(image=photo_image)
        except Exception:
            thumb_label.configure(text="[image indisponible]", foreground=opl_theme.couleur("texte"), font=BODY_FONT, width=20)

        filename = Path(photo["path"]).name
        ttk.Label(card, text=filename, foreground=opl_theme.couleur("texte"), font=BODY_FONT, wraplength=THUMBNAIL_SIZE[0]).pack()
        dims = f"{photo['width'] or '?'} x {photo['height'] or '?'}" if photo["width"] else "Dimensions inconnues"
        ttk.Label(card, text=f"{dims} - {_format_size(photo['size'])}", foreground=SECONDARY_TEXT_COLOR).pack()
        ttk.Label(card, text=photo["taken_at"][:10] if photo["taken_at"] else "Date inconnue", foreground=SECONDARY_TEXT_COLOR).pack()

        # Distance par rapport a la photo suggeree gardee (A2 de l'audit) :
        # avant ce correctif, rien sur une carte ne permettait de savoir a
        # quel point elle ressemblait vraiment aux autres membres du groupe.
        # Un groupe "exact" est une copie bit a bit (sha256 identique) - la
        # notion de distance de Hamming n'y a pas la meme portee qu'un
        # quasi-doublon, on l'annonce donc differemment.
        if group_kind == "exact":
            ttk.Label(card, text="Copie exacte (fichier identique)", foreground=SECONDARY_TEXT_COLOR).pack()
        else:
            distance = hashing.hamming_distance(phash_from_sqlite(photo["phash"]), keeper_phash)
            ttk.Label(card, text=f"Distance : {distance}/{self._last_threshold}", foreground=SECONDARY_TEXT_COLOR).pack()

        if is_keeper:
            ttk.Label(card, text="★ Suggeree a garder", foreground=opl_theme.couleur("succes"), font=BODY_FONT).pack(pady=(2, 0))

        # Notation 0-5 etoiles (correctif de l'audit, J1) : la colonne
        # `rating`/`Database.set_rating()` etaient entierement implementees
        # et testees cote donnees mais jamais exposees dans l'interface -
        # utile en pratique pour departager plusieurs quasi-doublons de
        # qualite proche (flou, cadrage...) avant de choisir lesquels
        # deplacer.
        self._build_rating_stars(card, photo["id"], photo["rating"])

        check_var = BooleanVar(value=not is_keeper)
        self._checkbox_vars[photo["id"]] = check_var
        ttk.Checkbutton(card, text="A deplacer", variable=check_var).pack(pady=(4, 0))

    def _build_rating_stars(self, parent, photo_id: int, current_rating: int) -> None:
        """Construit la rangee de RATING_MAX (5) etoiles cliquables d'une
        carte photo (J1 de l'audit) - chaque etoile represente une note de
        1 a son rang ; cliquer sur l'etoile deja active en position `n`
        reinitialise la note a 0 (seul moyen d'annuler une notation, voir
        _set_photo_rating).

        Accessible au clavier (M2 de l'audit du 2026-07-28) : avant ce
        correctif, les etoiles n'etaient ni focusables (`takefocus`
        implicite a 0 sur un ttk.Label) ni reliees a aucun binding clavier -
        seule la souris permettait de noter une photo, Tab sautait purement
        et simplement par-dessus la rangee entiere. Chaque etoile est
        desormais focusable (`takefocus=1`) et se comporte, une fois le
        focus dessus, comme le bouton qu'elle represente deja visuellement :
        Entree/Espace = meme action qu'un clic gauche, Gauche/Droite
        deplacent le focus dans la rangee, et les chiffres 1-5 definissent
        directement la note depuis n'importe quelle etoile focus de la
        carte (sans avoir a naviguer jusqu'a la bonne position). Un contour
        (FocusIn/FocusOut) rend en plus le focus clavier visible, absent par
        defaut sur un ttk.Label."""
        stars_frame = ttk.Frame(parent)
        stars_frame.pack(pady=(4, 0))
        labels = []
        for value in range(1, RATING_MAX + 1):
            star_label = ttk.Label(stars_frame, font=BODY_FONT, cursor="hand2", takefocus=1)
            star_label.pack(side=LEFT)
            # funcid conserve pour chaque binding (voir _rating_star_funcids)
            # pour pouvoir liberer explicitement ces commandes Tcl lorsque
            # cette carte sera reconstruite/detruite - sans quoi les
            # fermetures ci-dessous (qui referment sur `self`) resteraient
            # enregistrees dans l'interpreteur au-dela de la duree de vie
            # reelle de la carte.
            for sequence, handler in (
                ("<Button-1>", lambda event, v=value: self._set_photo_rating(photo_id, v)),
                ("<Return>", lambda event, v=value: self._set_photo_rating(photo_id, v)),
                ("<space>", lambda event, v=value: self._set_photo_rating(photo_id, v)),
                # <Key> generique plutot que 5 bindings "1".."5" par etoile :
                # Tk resout deja <Return>/<space> ci-dessus en priorite sur
                # ce pattern plus generique pour les touches qu'ils couvrent,
                # <Key> ne recoit donc que le reste (dont les chiffres).
                # Deleguee a une methode nommee (_handle_rating_key_press)
                # plutot que de rester une lambda inline : reste testable
                # directement, independamment de la livraison reelle d'un
                # evenement clavier synthetique par Tk (plus fragile a
                # simuler de facon fiable dans les tests qui recreent de
                # nombreuses racines Tk a la suite, voir tests/test_gui.py).
                ("<Key>", lambda event, pid=photo_id: self._handle_rating_key_press(pid, event.char)),
                ("<FocusIn>", lambda event, lbl=star_label: lbl.configure(relief="solid", borderwidth=1)),
                ("<FocusOut>", lambda event, lbl=star_label: lbl.configure(relief="flat", borderwidth=0)),
            ):
                funcid = star_label.bind(sequence, handler)
                self._rating_star_funcids.append((star_label, funcid))
            labels.append(star_label)
        # Navigation Gauche/Droite entre les etoiles d'une meme carte -
        # bindee dans une seconde passe (apres coup) puisqu'elle a besoin de
        # la liste `labels` complete pour s'y deplacer. Deleguee a
        # _focus_star (meme raison que _handle_rating_key_press ci-dessus :
        # testable directement).
        for position, star_label in enumerate(labels):
            left_funcid = star_label.bind("<Left>", lambda event, i=position: self._focus_star(labels, i - 1))
            right_funcid = star_label.bind("<Right>", lambda event, i=position: self._focus_star(labels, i + 1))
            self._rating_star_funcids.append((star_label, left_funcid))
            self._rating_star_funcids.append((star_label, right_funcid))
        self._rating_star_labels[photo_id] = labels
        self._refresh_rating_stars(photo_id, current_rating)

    def _refresh_rating_stars(self, photo_id: int, rating: int) -> None:
        """Redessine les etoiles d'une carte (pleines/vides, couleur) selon
        `rating` - separee de _set_photo_rating pour pouvoir aussi etre
        appelee juste apres la construction initiale de la carte
        (_build_rating_stars), sans dupliquer la logique d'affichage."""
        labels = self._rating_star_labels.get(photo_id)
        if not labels:
            return
        for position, star_label in enumerate(labels, start=1):
            filled = position <= rating
            star_label.configure(
                text="★" if filled else "☆",
                foreground=RATING_STAR_COLOR if filled else SECONDARY_TEXT_COLOR,
            )

    def _set_photo_rating(self, photo_id: int, value: int) -> None:
        """Gestionnaire de clic sur une etoile (J1) : definit la note de
        `photo_id` a `value`, sauf si elle y est deja - dans ce cas la note
        est reinitialisee a 0 (toggle), seul moyen d'annuler une notation
        deja attribuee puisqu'aucune autre commande ne le permet. Persiste
        immediatement via Database.set_rating (deja teste independamment,
        voir tests/test_db.py) puis redessine seulement les etoiles de cette
        carte, sans recalculer/reafficher tout le groupe."""
        row = self.db.get_photo(photo_id)
        current = row["rating"] if row is not None else 0
        new_rating = 0 if current == value else value
        self.db.set_rating(photo_id, new_rating)
        self._refresh_rating_stars(photo_id, new_rating)

    def _handle_rating_key_press(self, photo_id: int, char: str) -> None:
        """Gestionnaire du binding `<Key>` generique de chaque etoile (M2 de
        l'audit du 2026-07-28, voir _build_rating_stars) : un chiffre 1-5
        definit directement la note de `photo_id` a cette valeur (via
        _set_photo_rating, meme toggle-a-0 si c'est deja la note courante),
        n'importe quelle autre touche est ignoree silencieusement (les
        touches deja couvertes par des bindings plus specifiques - Entree,
        Espace, fleches - ne declenchent de toute facon jamais celui-ci,
        voir le commentaire dans _build_rating_stars)."""
        if char in "12345":
            self._set_photo_rating(photo_id, int(char))

    def _focus_star(self, labels: list, index: int) -> None:
        """Deplace le focus clavier vers l'etoile a `index` dans `labels`
        (M2 de l'audit du 2026-07-28, voir les bindings `<Left>`/`<Right>`
        de _build_rating_stars), en le bornant aux limites de la rangee -
        jamais d'IndexError sur la premiere/derniere etoile, le focus y
        reste simplement plutot que de sortir de la rangee ou de sauter
        ailleurs dans l'interface."""
        clamped_index = max(0, min(len(labels) - 1, index))
        labels[clamped_index].focus_set()

    # -- deplacement non destructif -------------------------------------------------

    def _check_duplicates_except_keeper(self, all_groups: bool = False) -> None:
        """Coche en un clic toutes les photos "a deplacer" SAUF la photo
        suggeree a garder (le keeper), soit pour le seul groupe affiche
        (all_groups=False), soit pour l'ensemble des groupes calcules
        (all_groups=True).

        Se branche sur le mecanisme de cases EXISTANT plutot que d'en creer
        un nouveau : pour le groupe affiche, ce sont directement les
        BooleanVar des cartes (self._checkbox_vars) qui sont mises a jour, ce
        qui rafraichit l'etat visuel des Checkbutton immediatement. Pour les
        AUTRES groupes (non affiches, donc sans carte ni BooleanVar en
        memoire), les ids retenus sont memorises dans self._marked_ids ;
        _move_checked_photos les reunit ensuite avec les cases cochees du
        groupe visible pour tout deplacer en une fois.

        Le keeper n'est JAMAIS coche par cette action : il est meme
        explicitement retire de self._marked_ids s'il y figurait."""
        if self._selected_group is None:
            return
        groups = list(self._groups) if all_groups else [self._selected_group]
        for group in groups:
            photos = [p for p in (self.db.get_photo(pid) for pid in group.photo_ids) if p is not None]
            if len(photos) < 2:
                continue
            keeper_id = grouping.suggest_keeper(photos)
            for photo in photos:
                if photo["id"] == keeper_id:
                    self._marked_ids.discard(photo["id"])
                else:
                    self._marked_ids.add(photo["id"])
        # Reflete la selection sur les cases du groupe actuellement affiche
        # (les seules dont les BooleanVar existent en memoire) - le keeper du
        # groupe visible reste explicitement decoche.
        for pid, var in self._checkbox_vars.items():
            if pid == self._current_keeper_id:
                var.set(False)
            elif pid in self._marked_ids:
                var.set(True)

    def _move_checked_photos(self):
        if self._selected_group is None:
            return
        # Meme garde-fou que _refresh_groups : ne jamais lancer un
        # deplacement pendant qu'un scan ou un regroupement ecrit deja sur
        # sa propre connexion SQLite (voir docstring de module) - le bouton
        # Deplacer est deja desactive dans ces deux cas via
        # _set_scan_controls_state, mais cette garde protege aussi les
        # appels directs (tests) qui contournent l'etat du bouton, et evite
        # tout double-declenchement pendant qu'un deplacement precedent
        # tourne encore.
        if self._scanning or self._grouping_in_progress or self._moving:
            return
        checked_visible = {pid for pid, var in self._checkbox_vars.items() if var.get()}
        # Reunit les cases cochees du groupe affiche avec les photos marquees
        # dans les AUTRES groupes via "Cocher ... dans tous les groupes"
        # (self._marked_ids) - ces dernieres n'ont pas de BooleanVar en
        # memoire tant que leur groupe n'est pas affiche. Pour le groupe
        # visible, la case fait foi (une case decochee a la main prime sur un
        # marquage anterieur), d'ou l'exclusion des pid deja presents dans
        # _checkbox_vars. Selection vide par defaut : comportement inchange.
        checked_hidden = {pid for pid in self._marked_ids if pid not in self._checkbox_vars}
        checked_ids = sorted(checked_visible | checked_hidden)
        if not checked_ids:
            messagebox.showinfo(APP_TITLE, "Aucune photo cochee.")
            return
        if len(checked_ids) == len(self._selected_group.photo_ids):
            if not messagebox.askyesno(
                APP_TITLE,
                "Toutes les photos du groupe sont cochees - aucune ne restera a cet "
                "emplacement d'origine. Continuer quand meme ?",
            ):
                return

        review_dir = Path(self.review_folder_var.get())
        try:
            review_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._show_friendly_error("Impossible de creer le dossier de revision", exc)
            return

        # Verification prealable de l'espace disque disponible sur le
        # volume de destination (bug trouve a l'audit, E5) : avant ce
        # correctif, aucune verification n'avait lieu avant de lancer la
        # boucle de shutil.move ci-dessous - un lot de grosses photos qui
        # epuise l'espace disque a mi-chemin suit le meme chemin de code
        # (et la meme famille de risque de copie partielle) que E4. Les
        # photos deja disparues de l'index (already_gone, comptees dans la
        # boucle plus bas) ne sont pas connues a ce stade ; les compter
        # dans l'estimation ferait sous-estimer legerement l'espace
        # necessaire, jamais le sur-estimer - au pire un avertissement
        # legerement trop prudent, jamais un oubli de verification.
        try:
            review_dev = review_dir.stat().st_dev
        except OSError:
            review_dev = None
        required_bytes = 0
        for photo_id in checked_ids:
            row = self.db.get_photo(photo_id)
            if row is None:
                continue
            try:
                source_stat = Path(row["path"]).stat()
            except OSError:
                # Fichier deja disparu du disque : sera de toute facon
                # compte comme "deja disparu" dans la boucle de deplacement
                # ci-dessous, sans consommer d'espace disque.
                continue
            if review_dev is not None and source_stat.st_dev == review_dev:
                # Meme volume que la source : shutil.move empruntera le
                # chemin os.rename() atomique (voir commentaire plus bas sur
                # E4), qui ne copie rien et ne consomme donc pas d'espace
                # disque supplementaire - ne pas le compter evite un
                # avertissement "espace insuffisant" sans objet dans le cas
                # le plus courant (dossier de revision place par defaut a
                # cote du dossier scanne, meme volume que lui).
                continue
            required_bytes += source_stat.st_size
        try:
            free_bytes = shutil.disk_usage(review_dir).free
        except OSError:
            free_bytes = None
        if free_bytes is not None and required_bytes > free_bytes:
            if not messagebox.askyesno(
                APP_TITLE,
                "Espace disque insuffisant sur le volume de destination :\n"
                f"{_format_size(required_bytes)} necessaire(s), {_format_size(free_bytes)} disponible(s) "
                f"dans :\n{review_dir}\n\nContinuer quand meme ?",
            ):
                return

        # -- deplacement effectif sur un thread dedie ------------------------------
        # Bug trouve a l'audit : la boucle de shutil.move ci-dessous tournait
        # auparavant entierement en synchrone sur le thread principal
        # Tkinter (aucun thread, aucune barre de progression), contrairement
        # au scan (deja threade, voir _scan_worker/_start_scan) - sur un
        # gros lot de photos, ca gelait l'interface pendant tout le
        # deplacement. Meme pattern ici : thread + queue.Queue + root.after
        # pour le polling (_poll_move_queue), jamais de widget Tkinter
        # touche depuis le thread (_move_worker) lui-meme - seul
        # _poll_move_queue/_on_move_finished, sur le thread principal, le
        # font. Les verifications ci-dessus (confirmation, dossier de
        # revision, espace disque) restent volontairement sur le thread
        # principal : ce sont de vraies boites de dialogue MODALES
        # (messagebox), qui doivent rester sur le thread Tk.
        self._moving = True
        self.scan_button.configure(state="disabled")
        self._set_scan_controls_state("disabled")
        self.progress_bar.configure(mode="determinate", value=0, maximum=max(len(checked_ids), 1))
        self.status_var.set(f"Deplacement en cours... 0 / {len(checked_ids)}")

        def on_progress(done, total):
            self._move_queue.put(("progress", done, total))

        thread = threading.Thread(
            target=self._move_worker, args=(checked_ids, review_dir, on_progress), daemon=True,
        )
        self._move_thread = thread
        thread.start()
        self.root.after(100, self._poll_move_queue)

    def _move_worker(self, checked_ids: list, review_dir: Path, on_progress) -> None:
        """Deplacement effectif (shutil.move + mise a jour de l'index),
        appele sur le thread cree par _move_checked_photos - jamais sur le
        thread principal. Ouvre sa PROPRE connexion SQLite (`worker_db`)
        plutot que de partager self.db : sqlite3 interdit de reutiliser une
        connexion depuis un autre thread que celui qui l'a creee (meme
        raison que _scan_worker, voir docstring de module)."""
        worker_db = None
        try:
            worker_db = Database(self.db_path)
            moved, failed, already_gone, orphan_copies = 0, [], 0, []
            total = len(checked_ids)
            for index, photo_id in enumerate(checked_ids, start=1):
                row = worker_db.get_photo(photo_id)
                if row is None:
                    # Deja disparue de l'index (deplacee/supprimee hors de
                    # PhotoTri entre l'affichage du groupe et ce clic) - rien
                    # a deplacer, ce n'est pas un echec a proprement parler.
                    already_gone += 1
                else:
                    source = Path(row["path"])
                    dest = _unique_destination(review_dir, source.name)
                    try:
                        shutil.move(str(source), str(dest))
                    except OSError as exc:
                        # Entre volumes differents, shutil.move ne peut pas
                        # se contenter d'un os.rename() atomique : il copie
                        # d'abord le fichier vers `dest` puis supprime la
                        # source. Si SEULE cette suppression finale echoue
                        # (source en lecture seule/permissions restreintes -
                        # assez courant avec des outils de synchronisation
                        # cloud ou des cartes SD protegees), une copie
                        # complete et valide existe deja dans le dossier de
                        # revision au moment ou cette exception est levee.
                        # Avant le correctif d'origine (E4), ce cas etait
                        # toujours rapporte comme un pur "Echec" - message
                        # trompeur, puisqu'une copie orpheline restait en
                        # realite sur le disque, jamais indexee (mark_moved
                        # jamais appele) et donc invisible a tout scan futur
                        # (le dossier de revision est exclu du parcours).
                        #
                        # On distingue ce cas (copie complete, seule la
                        # suppression a echoue) d'un veritable echec de
                        # copie (source toujours intacte car c'est
                        # precisement elle qui n'a pas pu etre supprimee) en
                        # comparant la taille du fichier copie a celle de la
                        # source.
                        try:
                            copy_is_complete = (
                                source.exists() and dest.exists()
                                and dest.stat().st_size == source.stat().st_size
                            )
                        except OSError:
                            copy_is_complete = False

                        if copy_is_complete:
                            # Le fichier physique est bel et bien range dans
                            # le dossier de revision : on le traite comme
                            # deplace pour rester coherent avec la realite
                            # du disque (index a jour, plus jamais
                            # reproposee comme active), et on avertit
                            # separement que l'original n'a pas pu etre
                            # supprime plutot que de pretendre qu'aucune
                            # action n'a eu lieu.
                            worker_db.mark_moved(photo_id, str(dest))
                            moved += 1
                            # L'exception elle-meme est conservee (pas
                            # seulement son texte) : elle est traduite en
                            # phrase francaise generique au moment de
                            # construire le message final, sur le thread
                            # principal (_on_move_finished) - le detail
                            # technique brut part dans erreurs.log plutot
                            # que dans la boite de dialogue (bug trouve a
                            # l'audit, C3).
                            orphan_copies.append((source.name, exc))
                        else:
                            # Echec de copie proprement dit (pas seulement
                            # de la suppression) : on ne laisse pas trainer
                            # une copie partielle/corrompue non indexee dans
                            # le dossier de revision plutot que de revenir a
                            # un etat coherent.
                            if dest.exists():
                                try:
                                    dest.unlink()
                                except OSError:
                                    pass
                            failed.append((source.name, exc))
                    else:
                        worker_db.mark_moved(photo_id, str(dest))
                        moved += 1
                on_progress(index, total)
            self._move_queue.put(("done", moved, failed, already_gone, orphan_copies, review_dir))
        except Exception as exc:
            # Filet de securite symetrique a _scan_worker : si l'ouverture
            # de worker_db ou une erreur inattendue survient, l'exception
            # part dans la file plutot que de tuer silencieusement le
            # thread (aucune console dans l'exe package pour le voir),
            # laissant sinon l'app bloquee indefiniment sur "Deplacement en
            # cours..." avec les boutons desactives a vie.
            self._move_queue.put(("error", exc))
        finally:
            if worker_db is not None:
                worker_db.close()

    def _poll_move_queue(self):
        try:
            while True:
                message = self._move_queue.get_nowait()
                kind = message[0]
                if kind == "progress":
                    _, done, total = message
                    self.progress_bar.configure(value=done, maximum=max(total, 1))
                    self.status_var.set(f"Deplacement en cours... {done} / {total}")
                elif kind == "done":
                    _, moved, failed, already_gone, orphan_copies, review_dir = message
                    self._on_move_finished(moved, failed, already_gone, orphan_copies, review_dir)
                    return
                elif kind == "error":
                    self._moving = False
                    self.scan_button.configure(state="normal")
                    self._set_scan_controls_state("normal")
                    self.progress_bar.configure(value=0)
                    self.status_var.set("")
                    self._show_friendly_error("Le deplacement a echoue", message[1])
                    return
        except queue.Empty:
            pass
        if self._moving:
            self.root.after(100, self._poll_move_queue)

    def _on_move_finished(self, moved: int, failed: list, already_gone: int, orphan_copies: list, review_dir: Path) -> None:
        """Reprend la main sur le thread principal une fois _move_worker
        termine : reactive les controles, construit le message recapitulatif
        (texte francais uniquement, voir _friendly_error_text/C3) et
        rafraichit les groupes - la meme logique qu'avant le passage au
        thread dedie, simplement deplacee ici plutot qu'executee en ligne a
        la fin de _move_checked_photos."""
        self._moving = False
        self.scan_button.configure(state="normal")
        self._set_scan_controls_state("normal")
        self.status_var.set("")

        message = f"{moved} photo(s) deplacee(s) vers :\n{review_dir}"
        if already_gone:
            message += f"\n\n{already_gone} photo(s) etaient deja disparues de l'index (ignoree(s))."
        # Le texte brut de chaque exception (souvent anglais/technique) part
        # dans erreurs.log via _log_technical_error plutot que d'etre
        # affiche tel quel (bug trouve a l'audit, C3) - seule une phrase
        # francaise generique (_friendly_error_text) apparait a cote du nom
        # de fichier concerne dans la boite de dialogue.
        log_path = None
        if orphan_copies:
            for name, exc in orphan_copies:
                log_path = self._log_technical_error(f"{name}: {type(exc).__name__}: {exc}")
            message += (
                "\n\nCopiees dans le dossier de revision, mais l'original n'a PAS pu etre "
                "supprime (verifiez les droits d'acces sur la source) :\n"
                + "\n".join(f"- {name} ({_friendly_error_text(exc)})" for name, exc in orphan_copies)
            )
        if failed:
            for name, exc in failed:
                log_path = self._log_technical_error(f"{name}: {type(exc).__name__}: {exc}")
            message += (
                "\n\nEchec pour :\n"
                + "\n".join(f"- {name} ({_friendly_error_text(exc)})" for name, exc in failed)
            )
        if log_path is not None:
            message += f"\n\n(Details techniques enregistres dans {log_path})"
        if failed or orphan_copies:
            messagebox.showwarning(APP_TITLE, message)
        else:
            messagebox.showinfo(APP_TITLE, message)
        self._refresh_groups()

    # -- fermeture -------------------------------------------------------------------

    def _on_close(self):
        if self._scanning:
            self._stop_event.set()
            if self._scan_thread is not None:
                # Attend (borne a 3s) que le thread de scan termine son
                # ecriture SQLite en cours avant de fermer self.db et de
                # detruire la fenetre - bug trouve a l'audit : sans ce join,
                # le thread daemon pouvait etre tue brutalement par l'arret
                # du process en pleine transaction sur `worker_db`.
                self._scan_thread.join(timeout=3)
        if self._moving and self._move_thread is not None:
            # Meme raisonnement que ci-dessus pour _scan_thread, applique a
            # _move_worker depuis son passage sur un thread dedie : le
            # deplacement n'a pas d'equivalent de _stop_event (pas
            # interrompable proprement au milieu d'un shutil.move), donc ce
            # join se contente d'attendre (borne a 3s) la fin de l'ecriture
            # SQLite en cours sur `worker_db` plutot que de laisser le
            # thread daemon se faire tuer brutalement par la fermeture du
            # process pendant une transaction.
            self._move_thread.join(timeout=3)
        self.db.close()
        self.root.destroy()

    def _log_technical_error(self, detail: str) -> Path:
        """Enregistre un detail technique complet (texte brut d'exception,
        trace le cas echeant) dans erreurs.log, horodate - mecanisme
        d'origine introduit pour _handle_callback_exception, factorise ici
        (bug trouve a l'audit, C3) pour etre reutilise par toute boite de
        dialogue d'erreur qui ne doit plus exposer ce texte brut
        directement a l'utilisateur (voir _show_friendly_error).
        Reutilise le dossier de donnees deja resolu a l'ouverture
        (self.db_path.parent) plutot que de rappeler _data_dir() - cette
        derniere renvoie toujours le meme resultat en usage reel, mais
        architecturalement il n'y a aucune raison de re-deriver un chemin
        deja connu, et ca evite toute divergence si ce point d'entree est
        un jour rendu configurable."""
        log_path = self.db_path.parent / "erreurs.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n--- {datetime.now().isoformat()} ---\n{detail}\n")
        except OSError:
            pass
        return log_path

    def _show_friendly_error(self, title_prefix: str, exc: BaseException) -> None:
        """Affiche une erreur a l'utilisateur en francais courant plutot
        que le texte brut (souvent anglais et technique) de l'exception
        (bug trouve a l'audit, C3 : un message comme "decompression bomb
        DOS attack" est anxiogene hors contexte pour un public non
        technique) - le detail technique complet part dans erreurs.log via
        _log_technical_error, jamais perdu, simplement plus impose en
        premiere lecture."""
        detail = f"{type(exc).__name__}: {exc}"
        log_path = self._log_technical_error(detail)
        friendly = _friendly_error_text(exc)
        messagebox.showerror(
            APP_TITLE,
            f"{title_prefix} :\n{friendly}\n\n(Details techniques enregistres dans {log_path})",
        )

    def _handle_callback_exception(self, exc_type, exc_value, exc_traceback):
        """Filet de securite global : par defaut, Tkinter avale
        silencieusement toute exception levee dans un callback (clic de
        bouton, etc.) - invisible dans l'exe package sans console (bug
        trouve a l'audit : plusieurs acces non defensifs pouvaient planter
        un callback sans le moindre message pour l'utilisateur). On
        journalise le detail dans un fichier ET on avertit l'utilisateur au
        lieu de laisser l'application paraitre figee sans explication."""
        message = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        log_path = self._log_technical_error(message)
        try:
            messagebox.showerror(
                APP_TITLE,
                "Une erreur inattendue s'est produite. Les details ont ete "
                f"enregistres dans :\n{log_path}",
            )
        except Exception:
            pass


def main():
    root = Tk()
    app = PhotoTriApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
