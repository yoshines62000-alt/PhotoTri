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

from PIL import Image, ImageTk

import grouping
import hashing
import scanner
import update_checker
from db import Database, phash_from_sqlite

APP_TITLE = "PhotoTri"
DONATE_URL = "https://ko-fi.com/yoshines62000"
APP_VERSION = "1.0.6"
UPDATE_REPO = "yoshines62000-alt/PhotoTri"
RELEASES_URL = f"https://github.com/{UPDATE_REPO}/releases/latest"
THUMBNAIL_SIZE = (150, 150)
BODY_FONT = ("Segoe UI", 10)


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
            tw, text=text, foreground="black", font=BODY_FONT,
            background="#ffffe0", relief="solid", borderwidth=1, padding=(4, 2),
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
        ttk.Style(self.root).theme_use("alt")

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
            messagebox.showerror(APP_TITLE, f"Impossible d'ouvrir l'index de PhotoTri :\n{exc}")
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
        self._thumbnail_refs: list = []
        self._checkbox_vars: dict = {}
        self._grouping_in_progress = False
        self._grouping_thread = None
        self._grouping_queue: "queue.Queue" = queue.Queue()
        self._pending_status_prefix = None
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
        self.folder_label = ttk.Label(top, textvariable=self.folder_label_var, foreground="black", font=BODY_FONT)
        self.folder_label.pack(side=LEFT, padx=10)
        self._folder_font = tkfont.Font(font=BODY_FONT)
        self._folder_tooltip = _Tooltip(self.folder_label, lambda: self._folder_full_path)

        right_controls = ttk.Frame(top)
        right_controls.pack(side=RIGHT)
        self.right_controls_frame = right_controls
        ttk.Label(right_controls, text="Sensibilite quasi-doublons :", foreground="black", font=BODY_FONT).pack(side=LEFT)
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
        self.scan_button = ttk.Button(right_controls, text="Analyser (scanner)", command=self._start_scan, state="disabled")
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
        ttk.Label(progress_frame, textvariable=self.status_var, foreground="#666").pack(anchor="w", pady=(2, 8))

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
        ttk.Label(bottom_bar, text=f"v{APP_VERSION}", foreground="#666").pack(side=LEFT, padx=(8, 0), pady=4)
        self.update_status_var = StringVar(value="")
        self.update_status_label = ttk.Label(bottom_bar, textvariable=self.update_status_var, foreground="#666")
        self.update_status_label.pack(side=LEFT, padx=(6, 0), pady=4)
        donate_label = ttk.Label(bottom_bar, text="☕ Soutenir le projet", foreground="#0645AD", cursor="hand2")
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
            self.update_status_label.configure(foreground="#0645AD", cursor="hand2")
            self.update_status_label.bind("<Button-1>", lambda event: webbrowser.open(RELEASES_URL))
        elif status == "up_to_date":
            self.update_status_var.set("A jour")
            self.update_status_label.configure(foreground="#1B7A1B", cursor="")
        # "check_failed" (hors ligne, GitHub inaccessible...) : on ne
        # revendique rien plutot que d'afficher a tort "a jour".

    def _build_detail_panel(self, parent):
        self.detail_placeholder = ttk.Label(
            parent, text="Choisissez un dossier, lancez une analyse, puis selectionnez un groupe a gauche.",
            foreground="black", font=BODY_FONT, wraplength=500, justify="left",
        )
        self.detail_placeholder.pack(padx=15, pady=15, anchor="w")

        self.detail_frame = ttk.Frame(parent)

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
        ttk.Label(action_bar, text="Dossier de revision :", foreground="black", font=BODY_FONT).pack(side=LEFT)
        ttk.Entry(action_bar, textvariable=self.review_folder_var, width=45, state="readonly").pack(side=LEFT, padx=5)
        self.change_review_button = ttk.Button(action_bar, text="Changer...", command=self._choose_review_folder)
        self.change_review_button.pack(side=LEFT)
        self.move_button = ttk.Button(
            action_bar, text="Deplacer les photos cochees vers le dossier de revision",
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
            self._queue.put(("error", str(exc)))
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
                    messagebox.showerror(APP_TITLE, f"L'analyse a echoue :\n{message[1]}")
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
            groups = grouping.group_photos(photos, near_duplicate_threshold=threshold, progress_callback=on_progress)
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
        if not self._groups:
            self.detail_placeholder.configure(
                text="Aucun doublon ni quasi-doublon trouve dans les photos indexees.",
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
        self._selected_group = group
        self._thumbnail_refs = []
        self._checkbox_vars = {}
        for widget in self.cards_inner.winfo_children():
            widget.destroy()

        if group is None:
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
                foreground="black", font=BODY_FONT, wraplength=500,
            ).grid(row=0, column=0, padx=15, pady=15)
            return
        keeper_id = grouping.suggest_keeper(photos)
        keeper_phash = phash_from_sqlite(next(p["phash"] for p in photos if p["id"] == keeper_id))

        for column, photo in enumerate(photos):
            self._build_photo_card(
                self.cards_inner, column, photo, is_keeper=(photo["id"] == keeper_id),
                group_kind=group.kind, keeper_phash=keeper_phash,
            )

    def _build_photo_card(self, parent, column, photo, is_keeper: bool, group_kind: str, keeper_phash: int):
        card = ttk.Frame(parent, relief="groove", borderwidth=1, padding=6)
        card.grid(row=0, column=column, padx=6, pady=6, sticky="n")

        thumb_label = ttk.Label(card)
        thumb_label.pack()
        try:
            with Image.open(photo["path"], formats=hashing.ALLOWED_PILLOW_FORMATS) as img:
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
            thumb_label.configure(text="[image indisponible]", foreground="black", font=BODY_FONT, width=20)

        filename = Path(photo["path"]).name
        ttk.Label(card, text=filename, foreground="black", font=BODY_FONT, wraplength=THUMBNAIL_SIZE[0]).pack()
        dims = f"{photo['width'] or '?'} x {photo['height'] or '?'}" if photo["width"] else "Dimensions inconnues"
        ttk.Label(card, text=f"{dims} - {_format_size(photo['size'])}", foreground="#666").pack()
        ttk.Label(card, text=photo["taken_at"][:10] if photo["taken_at"] else "Date inconnue", foreground="#666").pack()

        # Distance par rapport a la photo suggeree gardee (A2 de l'audit) :
        # avant ce correctif, rien sur une carte ne permettait de savoir a
        # quel point elle ressemblait vraiment aux autres membres du groupe.
        # Un groupe "exact" est une copie bit a bit (sha256 identique) - la
        # notion de distance de Hamming n'y a pas la meme portee qu'un
        # quasi-doublon, on l'annonce donc differemment.
        if group_kind == "exact":
            ttk.Label(card, text="Copie exacte (fichier identique)", foreground="#666").pack()
        else:
            distance = hashing.hamming_distance(phash_from_sqlite(photo["phash"]), keeper_phash)
            ttk.Label(card, text=f"Distance : {distance}/{self._last_threshold}", foreground="#666").pack()

        if is_keeper:
            ttk.Label(card, text="★ Suggeree a garder", foreground="#1B7A1B", font=BODY_FONT).pack(pady=(2, 0))

        check_var = BooleanVar(value=not is_keeper)
        self._checkbox_vars[photo["id"]] = check_var
        ttk.Checkbutton(card, text="A deplacer", variable=check_var).pack(pady=(4, 0))

    # -- deplacement non destructif -------------------------------------------------

    def _move_checked_photos(self):
        if self._selected_group is None:
            return
        checked_ids = [pid for pid, var in self._checkbox_vars.items() if var.get()]
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
            messagebox.showerror(APP_TITLE, f"Impossible de creer le dossier de revision :\n{exc}")
            return

        moved, failed, already_gone = 0, [], 0
        for photo_id in checked_ids:
            row = self.db.get_photo(photo_id)
            if row is None:
                # Deja disparue de l'index (deplacee/supprimee hors de
                # PhotoTri entre l'affichage du groupe et ce clic) - rien a
                # deplacer, ce n'est pas un echec a proprement parler.
                already_gone += 1
                continue
            source = Path(row["path"])
            dest = _unique_destination(review_dir, source.name)
            try:
                shutil.move(str(source), str(dest))
            except OSError as exc:
                failed.append((source.name, str(exc)))
                continue
            self.db.mark_moved(photo_id, str(dest))
            moved += 1

        message = f"{moved} photo(s) deplacee(s) vers :\n{review_dir}"
        if already_gone:
            message += f"\n\n{already_gone} photo(s) etaient deja disparues de l'index (ignoree(s))."
        if failed:
            message += "\n\nEchec pour :\n" + "\n".join(f"- {name} ({err})" for name, err in failed)
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
        self.db.close()
        self.root.destroy()

    def _handle_callback_exception(self, exc_type, exc_value, exc_traceback):
        """Filet de securite global : par defaut, Tkinter avale
        silencieusement toute exception levee dans un callback (clic de
        bouton, etc.) - invisible dans l'exe package sans console (bug
        trouve a l'audit : plusieurs acces non defensifs pouvaient planter
        un callback sans le moindre message pour l'utilisateur). On
        journalise le detail dans un fichier ET on avertit l'utilisateur au
        lieu de laisser l'application paraitre figee sans explication."""
        message = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        # Reutilise le dossier de donnees deja resout a l'ouverture
        # (self.db_path.parent) plutot que de rappeler _data_dir() - cette
        # derniere renvoie toujours le meme resultat en usage reel, mais
        # architecturalement il n'y a aucune raison de re-deriver un chemin
        # deja connu, et ca evite toute divergence si ce point d'entree est
        # un jour rendu configurable.
        log_path = self.db_path.parent / "erreurs.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n--- {datetime.now().isoformat()} ---\n{message}\n")
        except OSError:
            pass
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
