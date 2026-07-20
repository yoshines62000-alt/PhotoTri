"""Interface Tkinter de PhotoTri : recherche de photos en double (doublons
exacts et quasi-doublons - rafales, recompressions) dans un dossier, avec
rangement non destructif (deplacement, jamais de suppression automatique).

Le scan (parcours disque + hachage) tourne sur un thread separe pour ne
jamais geler l'interface sur une grosse bibliotheque : sqlite3 interdisant
de reutiliser une connexion depuis un autre thread que celui qui l'a
creee, le thread de scan ouvre sa PROPRE connexion vers le meme fichier
(voir _scan_worker) plutot que de partager self.db - les deux connexions
ne sont jamais actives en meme temps (self.db reste inutilisee pendant
toute la duree du scan), donc aucun risque d'acces concurrent reel."""

from __future__ import annotations

import queue
import shutil
import sys
import threading
import webbrowser
from pathlib import Path
from tkinter import (
    BOTH, END, HORIZONTAL, LEFT, RIGHT, TOP, X, Y, VERTICAL,
    BooleanVar, Canvas, IntVar, StringVar, Tk, Toplevel, ttk, filedialog, messagebox,
)

from PIL import Image, ImageTk

import grouping
import scanner
from db import Database

APP_TITLE = "PhotoTri"
DONATE_URL = "https://ko-fi.com/yoshines62000"
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
        self._groups: list = []
        self._groups_by_iid: dict = {}
        self._selected_group = None
        self._thumbnail_refs: list = []
        self._checkbox_vars: dict = {}

        self._build_layout()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- construction de l'interface ---------------------------------------------

    def _build_layout(self):
        top = ttk.Frame(self.root)
        top.pack(fill=X, padx=10, pady=10)

        ttk.Button(top, text="Choisir un dossier a analyser...", command=self._choose_folder).pack(side=LEFT)
        self.folder_label_var = StringVar(value="Aucun dossier choisi")
        ttk.Label(top, textvariable=self.folder_label_var, foreground="black", font=BODY_FONT).pack(side=LEFT, padx=10)

        right_controls = ttk.Frame(top)
        right_controls.pack(side=RIGHT)
        ttk.Label(right_controls, text="Sensibilite quasi-doublons :", foreground="black", font=BODY_FONT).pack(side=LEFT)
        ttk.Spinbox(right_controls, from_=0, to=20, textvariable=self.threshold_var, width=4).pack(side=LEFT, padx=(4, 10))
        self.recompute_button = ttk.Button(right_controls, text="Recalculer les groupes", command=self._refresh_groups)
        self.recompute_button.pack(side=LEFT, padx=(0, 10))
        self.scan_button = ttk.Button(right_controls, text="Analyser (scanner)", command=self._start_scan, state="disabled")
        self.scan_button.pack(side=LEFT)
        self.stop_button = ttk.Button(right_controls, text="Arreter", command=self._request_stop, state="disabled")
        self.stop_button.pack(side=LEFT, padx=(6, 0))

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
        columns = ("kind", "count", "size")
        self.groups_tree = ttk.Treeview(left, columns=columns, show="headings", height=20)
        for col, label, width in [("kind", "Type", 90), ("count", "Photos", 60), ("size", "Poids total", 100)]:
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
        donate_label = ttk.Label(bottom_bar, text="☕ Soutenir le projet", foreground="#0645AD", cursor="hand2")
        donate_label.pack(side=RIGHT, padx=8, pady=4)
        donate_label.bind("<Button-1>", lambda event: webbrowser.open(DONATE_URL))

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
        ttk.Button(action_bar, text="Changer...", command=self._choose_review_folder).pack(side=LEFT)
        ttk.Button(
            action_bar, text="Deplacer les photos cochees vers le dossier de revision",
            command=self._move_checked_photos,
        ).pack(side=RIGHT)

    # -- choix des dossiers --------------------------------------------------------

    def _choose_folder(self):
        chosen = filedialog.askdirectory(title="Choisir le dossier de photos a analyser")
        if not chosen:
            return
        self.selected_folder = Path(chosen)
        self.folder_label_var.set(str(self.selected_folder))
        self.scan_button.configure(state="normal")
        if not self.review_folder_var.get():
            self.review_folder_var.set(str(self.selected_folder / "PhotoTri_a_revoir"))

    def _choose_review_folder(self):
        chosen = filedialog.askdirectory(title="Choisir le dossier de revision (destination des doublons deplaces)")
        if chosen:
            self.review_folder_var.set(chosen)

    # -- scan (thread separe) ------------------------------------------------------

    def _start_scan(self):
        if self._scanning or self.selected_folder is None:
            return
        self._scanning = True
        self._stop_event.clear()
        self.scan_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.recompute_button.configure(state="disabled")
        self.progress_bar.configure(value=0, maximum=1)
        self.status_var.set("Analyse en cours...")

        thread = threading.Thread(target=self._scan_worker, args=(self.selected_folder,), daemon=True)
        thread.start()
        self.root.after(100, self._poll_scan_queue)

    def _request_stop(self):
        self._stop_event.set()
        self.status_var.set("Arret demande...")

    def _scan_worker(self, folder: Path):
        worker_db = Database(self.db_path)
        try:
            def on_progress(done, total, path):
                self._queue.put(("progress", done, total, path))

            result = scanner.scan_folder(
                folder, worker_db, progress_callback=on_progress, should_stop=self._stop_event.is_set,
            )
            self._queue.put(("done", result))
        except Exception as exc:
            self._queue.put(("error", str(exc)))
        finally:
            worker_db.close()

    def _poll_scan_queue(self):
        try:
            while True:
                message = self._queue.get_nowait()
                kind = message[0]
                if kind == "progress":
                    _, done, total, path = message
                    self.progress_bar.configure(value=done, maximum=max(total, 1))
                    self.status_var.set(f"{done} / {total} - {path}")
                elif kind == "done":
                    self._on_scan_finished(message[1])
                    return
                elif kind == "error":
                    self._scanning = False
                    self.scan_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.recompute_button.configure(state="normal")
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
        self.recompute_button.configure(state="normal")
        summary = (
            f"Analyse terminee : {result.scanned} photo(s) indexee(s), "
            f"{result.skipped_unchanged} inchangee(s), {result.pruned} disparue(s) retiree(s) de l'index."
        )
        if result.errors:
            summary += f" {len(result.errors)} fichier(s) illisible(s) ignore(s)."
        self.status_var.set(summary)
        self._refresh_groups()

    # -- regroupement et affichage --------------------------------------------------

    def _refresh_groups(self):
        photos = self.db.list_active_photos()
        self._groups = grouping.group_photos(list(photos), near_duplicate_threshold=self.threshold_var.get())
        self._groups.sort(key=lambda g: (g.kind != "exact", -len(g.photo_ids)))

        self.groups_tree.delete(*self.groups_tree.get_children())
        self._groups_by_iid = {}
        for index, group in enumerate(self._groups):
            iid = str(index)
            self._groups_by_iid[iid] = group
            total_size = sum((self.db.get_photo(pid)["size"] or 0) for pid in group.photo_ids)
            label = "Exact" if group.kind == "exact" else "Quasi-doublon"
            self.groups_tree.insert("", END, iid=iid, values=(label, len(group.photo_ids), _format_size(total_size)))

        self._show_group_detail(None)
        if not self._groups:
            self.detail_placeholder.configure(
                text="Aucun doublon ni quasi-doublon trouve dans les photos indexees.",
            )

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

        photos = [self.db.get_photo(pid) for pid in group.photo_ids]
        keeper_id = grouping.suggest_keeper(photos)

        for column, photo in enumerate(photos):
            self._build_photo_card(self.cards_inner, column, photo, is_keeper=(photo["id"] == keeper_id))

    def _build_photo_card(self, parent, column, photo, is_keeper: bool):
        card = ttk.Frame(parent, relief="groove", borderwidth=1, padding=6)
        card.grid(row=0, column=column, padx=6, pady=6, sticky="n")

        thumb_label = ttk.Label(card)
        thumb_label.pack()
        try:
            with Image.open(photo["path"]) as img:
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

        moved, failed = 0, []
        for photo_id in checked_ids:
            row = self.db.get_photo(photo_id)
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
        self.db.close()
        self.root.destroy()


def main():
    root = Tk()
    app = PhotoTriApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
