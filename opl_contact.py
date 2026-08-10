"""Fenêtre de contact commune aux applications Open Projects Lab.

But : un point de contact intégré (bug / suggestion / question) dans chaque
appli, relié au micro-service contact-intake (POST JSON).

EN LIGNE depuis le 2026-08-10 : le message est envoyé à
`https://contact.openprojectslab.com/api/contact` (URL de prod par défaut, cf.
FILONAUT_CONTACT_URL plus bas ; override possible via OPL_CONTACT_URL). En cas
d'échec réseau — ou si l'URL est vidée — repli automatique sur une « boîte
d'envoi » locale (outbox JSONL) : AUCUN message n'est jamais perdu, et
l'utilisateur en est informé honnêtement.

Module stdlib-only, vendored à la racine de chaque dépôt à côté de opl_theme.py.
"""
from __future__ import annotations

import json
import os
import platform
import sys
import tkinter as tk
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from tkinter import ttk

try:  # le thème est optionnel : la fenêtre marche même sans lui
    import opl_theme
except Exception:  # pragma: no cover
    opl_theme = None

# ---------------------------------------------------------------------------
# Endpoint du service contact — EN PRODUCTION depuis le 2026-08-10.
# L'URL de prod est le défaut (indispensable : une app distribuée en .exe n'a pas
# de variable d'environnement). OPL_CONTACT_URL reste un override pour le dev/test.
# Passer à "" (chaîne vide) désactiverait l'envoi -> repli boîte d'envoi locale.
FILONAUT_CONTACT_URL: str | None = (
    os.environ.get("OPL_CONTACT_URL", "").strip()
    or "https://contact.openprojectslab.com/api/contact"
)

# Délai réseau court : le contact ne doit jamais figer l'appli.
_TIMEOUT_S = 8


def _poster_filonaut(payload: dict) -> None:
    """Envoie `payload` vers FILONAUT_CONTACT_URL (POST JSON, stdlib). Lève sur
    échec — ce qui déclenche le repli boîte d'envoi locale dans ouvrir().

    Le schéma interne (app/type/…) est mappé vers celui du micro-service
    contact-intake : { message, email, produit, version, os }. Le type de
    demande est préfixé au message pour ne rien perdre (le service n'a pas de
    champ « type » dédié).
    """
    if not FILONAUT_CONTACT_URL:
        raise RuntimeError("URL de contact non configurée")
    type_ = str(payload.get("type", "")).strip()
    message = payload.get("message", "")
    corps = json.dumps({
        "message": (f"[{type_}] {message}" if type_ else message),
        "email": payload.get("email", ""),
        "produit": payload.get("app", ""),
        "version": payload.get("version", ""),
        "os": payload.get("os", ""),
    }).encode("utf-8")
    req = urllib.request.Request(
        FILONAUT_CONTACT_URL, data=corps, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "refus du serveur"))


TYPES = ["Signaler un bug", "Suggestion d'amélioration", "Question", "Autre"]


def _outbox(app: str) -> Path:
    base = os.environ.get("APPDATA") if sys.platform.startswith("win") else None
    racine = Path(base) if base else Path.home() / ".config"
    dossier = racine / "OpenProjectsLab"
    dossier.mkdir(parents=True, exist_ok=True)
    return dossier / "contact-outbox.jsonl"


def _payload(app: str, version: str, type_: str, email: str, message: str) -> dict:
    return {
        "app": app,
        "version": version,
        "type": type_,
        "email": email.strip(),
        "message": message.strip(),
        "os": f"{platform.system()} {platform.release()}",
        "horodatage": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _enregistrer_localement(app: str, payload: dict) -> Path:
    """Ajoute le message à la boîte d'envoi locale (une ligne JSON). Aucun
    réseau. Renvoie le chemin du fichier pour l'afficher à l'utilisateur."""
    chemin = _outbox(app)
    with chemin.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return chemin


def ouvrir(parent: tk.Misc, *, app: str, version: str = "") -> None:
    """Ouvre la fenêtre de contact modale, thémée comme le reste de l'appli."""
    P = opl_theme.PALETTE if opl_theme else {
        "fond": "#F5F7FA", "surface": "#FFFFFF", "texte": "#16202E",
        "texte_doux": "#5A6B80", "emeraude_fonce": "#10B981", "danger": "#DC2626",
    }

    win = tk.Toplevel(parent)
    win.title(f"Contact — {app}")
    win.configure(background=P["fond"])
    win.resizable(False, False)
    try:
        win.transient(parent.winfo_toplevel())
    except Exception:
        pass

    cadre = ttk.Frame(win, padding=20)
    cadre.pack(fill="both", expand=True)

    titre_style = "Titre.TLabel" if opl_theme else "TLabel"
    doux_style = "SousTitre.TLabel" if opl_theme else "TLabel"
    ttk.Label(cadre, text="Nous contacter", style=titre_style).grid(row=0, column=0, columnspan=2, sticky="w")
    ttk.Label(cadre, text="Bug, idée ou question — votre retour arrive directement à l'auteur.",
              style=doux_style).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 14))

    ttk.Label(cadre, text="Type").grid(row=2, column=0, sticky="w", pady=(0, 2))
    type_var = tk.StringVar(value=TYPES[0])
    combo = ttk.Combobox(cadre, textvariable=type_var, values=TYPES, state="readonly", width=34)
    combo.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 10))

    ttk.Label(cadre, text="Votre e-mail (optionnel, pour la réponse)").grid(row=4, column=0, sticky="w", pady=(0, 2))
    email_var = tk.StringVar()
    ttk.Entry(cadre, textvariable=email_var, width=36).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(0, 10))

    ttk.Label(cadre, text="Message").grid(row=6, column=0, sticky="w", pady=(0, 2))
    txt = tk.Text(cadre, width=44, height=8, wrap="word")
    txt.grid(row=7, column=0, columnspan=2, sticky="ew")

    meta = f"Joint automatiquement : {app} v{version} · {platform.system()} {platform.release()}"
    ttk.Label(cadre, text=meta, style=("Mono.TLabel" if opl_theme else "TLabel")).grid(
        row=8, column=0, columnspan=2, sticky="w", pady=(8, 14))

    etat = ttk.Label(cadre, text="", style=doux_style)
    etat.grid(row=9, column=0, columnspan=2, sticky="w")

    barre = ttk.Frame(cadre)
    barre.grid(row=10, column=0, columnspan=2, sticky="e", pady=(10, 0))

    def fermer():
        win.grab_release()
        win.destroy()

    def envoyer():
        message = txt.get("1.0", "end").strip()
        if not message:
            etat.configure(text="Merci d'écrire un message avant d'envoyer.",
                           foreground=P.get("danger", "#DC2626"))
            txt.focus_set()
            return
        payload = _payload(app, version, type_var.get(), email_var.get(), message)
        try:
            if FILONAUT_CONTACT_URL:
                _poster_filonaut(payload)          # branché plus tard
                info = "Message envoyé. Merci !"
            else:
                chemin = _enregistrer_localement(app, payload)
                info = ("Message enregistré localement. L'envoi vers le panneau "
                        f"sera activé prochainement.\n({chemin})")
        except Exception as exc:  # repli : ne jamais perdre le message
            try:
                chemin = _enregistrer_localement(app, payload)
                info = f"Message conservé localement (envoi indisponible : {exc}).\n({chemin})"
            except Exception:
                etat.configure(text=f"Échec : {exc}", foreground=P.get("danger", "#DC2626"))
                return
        for w in barre.winfo_children():
            w.configure(state="disabled")
        etat.configure(text=info, foreground=P.get("emeraude_fonce", "#10B981"))
        win.after(2200, fermer)

    style_annuler = "TButton"
    style_envoyer = "Accent.TButton" if opl_theme else "TButton"
    ttk.Button(barre, text="Annuler", command=fermer, style=style_annuler).pack(side="left", padx=(0, 8))
    ttk.Button(barre, text="Envoyer", command=envoyer, style=style_envoyer).pack(side="left")

    cadre.columnconfigure(0, weight=1)
    cadre.columnconfigure(1, weight=1)

    win.update_idletasks()
    # centre la fenêtre sur le parent
    try:
        top = parent.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - win.winfo_reqwidth()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
    except Exception:
        pass
    win.grab_set()
    txt.focus_set()
