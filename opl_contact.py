"""Fenêtre de contact commune aux applications Open Projects Lab.

But : un point de contact intégré (bug / suggestion / question) dans chaque
appli, relié au micro-service contact-intake (POST JSON).

EN LIGNE depuis le 2026-08-10 : le message est envoyé à
`https://contact.openprojectslab.com/api/contact` (URL de prod par défaut, cf.
FILONAUT_CONTACT_URL plus bas ; override possible via OPL_CONTACT_URL). En cas
d'échec réseau — ou si l'URL est vidée — repli automatique sur une « boîte
d'envoi » locale (outbox JSONL) : AUCUN message n'est jamais perdu, et
l'utilisateur en est informé honnêtement.

ON MONTRE EXACTEMENT CE QUI PART, AVANT QUE ÇA PARTE (depuis le 2026-08-23)
---------------------------------------------------------------------------
Avant, « Envoyer » envoyait. Une ligne disait « Joint automatiquement : app,
OS » — mais ni la destination, ni le corps exact, ni ce qui n'est PAS envoyé.
Désormais « Envoyer » ouvre d'abord un aperçu : l'adresse de destination, le
corps JSON tel qu'il sera transmis (le MÊME objet, sérialisé — pas une copie
qui pourrait diverger), et ce que le serveur fait de la requête. Rien ne part
tant que l'utilisateur n'a pas cliqué « Envoyer tel quel ». Un bouton qui
envoie sans montrer demande une confiance qu'il n'a pas gagnée.

Module stdlib-only, vendored à la racine de chaque dépôt à côté de opl_theme.py.
"""
from __future__ import annotations

import json
import os
import platform
import queue
import re
import sys
import threading
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

# Les limites du service contact-intake (config.js, maxLen), recopiées ICI pour
# refuser AVANT d'envoyer ce que le serveur refuserait après — un message de
# 6 000 caractères mérite d'être prévenu avant le clic, pas après. Le serveur
# tronque produit/version/os sans rejeter ; il REJETTE un message vide ou trop
# long, et un e-mail non vide mal formé (même expression que la sienne).
LIMITES = {"message": 5000, "email": 200, "produit": 80, "version": 40, "os": 120}
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

TYPES = ["Signaler un bug", "Suggestion d'amélioration", "Question", "Autre"]


def _outbox(app: str = "") -> Path:
    """La boite d'envoi locale, COMMUNE aux applications d'Open Projects Lab :
    un seul fichier sous %APPDATA%\\OpenProjectsLab, pas un par application —
    l'application dont vient le message est deja dans chaque ligne (champ
    "app" du payload). `app` n'est donc pas utilise pour construire le chemin ;
    il reste accepte pour ne pas casser les appelants et rend l'intention
    lisible sur le site des appels."""
    base = os.environ.get("APPDATA") if sys.platform.startswith("win") else None
    racine = Path(base) if base else Path.home() / ".config"
    dossier = racine / "OpenProjectsLab"
    dossier.mkdir(parents=True, exist_ok=True)
    return dossier / "contact-outbox.jsonl"


def _payload(app: str, version: str, type_: str, email: str, message: str) -> dict:
    """Ce que l'application SAIT du message (avec le type et l'horodatage, pour
    la boîte d'envoi locale). Ce n'est PAS ce qui est envoyé : voir corps_envoye."""
    return {
        "app": app,
        "version": version,
        "type": type_,
        "email": email.strip(),
        "message": message.strip(),
        "os": f"{platform.system()} {platform.release()}",
        "horodatage": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def corps_envoye(payload: dict) -> dict:
    """Le corps JSON EXACT transmis au service — cinq champs, pas un de plus.

    Le schéma interne (app/type/horodatage) est mappé vers celui du service :
    { message, email, produit, version, os }. Le type est préfixé au message
    (le service n'a pas de champ « type »). L'horodatage NE PART PAS : le
    serveur date lui-même la réception. C'est ce dict, et lui seul, que
    l'aperçu montre et que _poster_corps sérialise."""
    type_ = str(payload.get("type", "")).strip()
    message = str(payload.get("message", "")).strip()
    # Pas de préfixe sur un message VIDE : « [Signaler un bug] » tout seul
    # n'est pas vide aux yeux de verifier(), et l'aperçu s'ouvrait sur rien.
    # Trouvé par le test, pas par relecture.
    return {
        "message": (f"[{type_}] {message}" if type_ and message else message),
        "email": str(payload.get("email", "")).strip(),
        "produit": str(payload.get("app", "")),
        "version": str(payload.get("version", "")),
        "os": str(payload.get("os", "")),
    }


def verifier(corps: dict) -> str | None:
    """La première raison pour laquelle le service refuserait ce corps, ou None.
    Même règles que contact-intake : on ne fait pas voyager un refus."""
    if not corps.get("message", "").strip():
        return "Merci d'écrire un message avant d'envoyer."
    if len(corps["message"]) > LIMITES["message"]:
        return f"Message trop long : {len(corps['message'])} caractères, maximum {LIMITES['message']}."
    email = corps.get("email", "")
    if email and (len(email) > LIMITES["email"] or not _EMAIL_RE.match(email)):
        return "L'adresse e-mail n'a pas l'air valide (laissez-la vide si vous ne voulez pas de réponse)."
    return None


def apercu(corps: dict, url: str | None, outbox: Path | None = None) -> str:
    """Le texte montré AVANT l'envoi : la destination, le corps exact, et ce
    que le serveur fait de la requête. Honnête dans les deux sens — ce qui
    part, et ce qui ne part pas."""
    lignes = [f"Destination : {url or '(aucune — envoi désactivé, le message sera gardé sur cet ordinateur)'}", "",
              "Corps envoyé, tel quel :", json.dumps(corps, ensure_ascii=False, indent=2), "",
              "Rien d'autre n'est envoyé : pas d'identifiant de machine, pas de nom",
              "d'utilisateur, pas de fichier, pas de contenu de l'application.", "",
              "Votre adresse IP parvient au serveur, comme pour toute requête ; il n'en",
              "garde qu'une empreinte tronquée (pour limiter les abus), pas l'adresse."]
    if outbox is not None:
        lignes += ["", f"Si l'envoi échoue, le message est gardé ici et n'est jamais renvoyé",
                   f"automatiquement : {outbox}"]
    return "\n".join(lignes)


def _poster_corps(corps: dict) -> None:
    """Envoie `corps` — le dict de corps_envoye, le même objet que l'aperçu —
    vers FILONAUT_CONTACT_URL (POST JSON, stdlib). Lève sur échec, ce qui
    déclenche le repli boîte d'envoi locale dans ouvrir()."""
    if not FILONAUT_CONTACT_URL:
        raise RuntimeError("URL de contact non configurée")
    req = urllib.request.Request(
        FILONAUT_CONTACT_URL, data=json.dumps(corps).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "refus du serveur"))


def _poster_filonaut(payload: dict) -> None:
    """Compatibilité : l'ancien point d'entrée, qui passe par corps_envoye."""
    _poster_corps(corps_envoye(payload))


def _enregistrer_localement(app: str, payload: dict) -> Path:
    """Ajoute le message à la boîte d'envoi locale (une ligne JSON). Aucun
    réseau. Renvoie le chemin du fichier pour l'afficher à l'utilisateur."""
    chemin = _outbox(app)
    with chemin.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return chemin


def ouvrir(parent: tk.Misc, *, app: str, version: str = "") -> tk.Toplevel:
    """Ouvre la fenêtre de contact modale, thémée comme le reste de l'appli.
    Renvoie la fenêtre (ses boutons et son aperçu sont exposés comme attributs,
    pour les tests : bouton_envoyer, bouton_confirmer, bouton_modifier, apercu)."""
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

    titre_style = "Titre.TLabel" if opl_theme else "TLabel"
    doux_style = "SousTitre.TLabel" if opl_theme else "TLabel"
    mono_style = "Mono.TLabel" if opl_theme else "TLabel"
    style_envoyer = "Accent.TButton" if opl_theme else "TButton"

    # Deux pages dans la même fenêtre : le FORMULAIRE, puis l'APERÇU. Seule
    # la seconde envoie. On bascule par pack()/pack_forget(), rien n'est détruit.
    formulaire = ttk.Frame(win, padding=20)
    page_apercu = ttk.Frame(win, padding=20)
    formulaire.pack(fill="both", expand=True)

    # -- page 1 : le formulaire ------------------------------------------------
    ttk.Label(formulaire, text="Nous contacter", style=titre_style).grid(row=0, column=0, columnspan=2, sticky="w")
    ttk.Label(formulaire, text="Bug, idée ou question — votre retour arrive directement à l'auteur.",
              style=doux_style).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 14))

    ttk.Label(formulaire, text="Type").grid(row=2, column=0, sticky="w", pady=(0, 2))
    type_var = tk.StringVar(value=TYPES[0])
    combo = ttk.Combobox(formulaire, textvariable=type_var, values=TYPES, state="readonly", width=34)
    combo.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 10))

    ttk.Label(formulaire, text="Votre e-mail (optionnel, pour la réponse)").grid(row=4, column=0, sticky="w", pady=(0, 2))
    email_var = tk.StringVar()
    ttk.Entry(formulaire, textvariable=email_var, width=36).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(0, 10))

    ttk.Label(formulaire, text="Message").grid(row=6, column=0, sticky="w", pady=(0, 2))
    txt = tk.Text(formulaire, width=44, height=8, wrap="word")
    txt.grid(row=7, column=0, columnspan=2, sticky="ew")

    meta = f"Joint automatiquement : {app} v{version} · {platform.system()} {platform.release()}"
    ttk.Label(formulaire, text=meta, style=mono_style).grid(row=8, column=0, columnspan=2, sticky="w", pady=(8, 14))

    etat = ttk.Label(formulaire, text="", style=doux_style)
    etat.grid(row=9, column=0, columnspan=2, sticky="w")

    barre = ttk.Frame(formulaire)
    barre.grid(row=10, column=0, columnspan=2, sticky="e", pady=(10, 0))
    formulaire.columnconfigure(0, weight=1)
    formulaire.columnconfigure(1, weight=1)

    # -- page 2 : l'aperçu -----------------------------------------------------
    ttk.Label(page_apercu, text="Voici exactement ce qui sera envoyé", style=titre_style).grid(row=0, column=0, sticky="w")
    ttk.Label(page_apercu, text="Rien ne part tant que vous n'avez pas cliqué « Envoyer tel quel ».",
              style=doux_style).grid(row=1, column=0, sticky="w", pady=(2, 10))
    zone = tk.Text(page_apercu, width=64, height=18, wrap="word")
    zone.grid(row=2, column=0, sticky="ew")
    etat_apercu = ttk.Label(page_apercu, text="", style=doux_style)
    etat_apercu.grid(row=3, column=0, sticky="w", pady=(8, 0))
    barre_apercu = ttk.Frame(page_apercu)
    barre_apercu.grid(row=4, column=0, sticky="e", pady=(10, 0))
    page_apercu.columnconfigure(0, weight=1)

    en_attente: dict = {}   # le payload et le corps de l'aperçu en cours

    def fermer():
        try:
            win.grab_release()
        except Exception:
            pass
        win.destroy()

    def montrer_apercu():
        """« Envoyer » sur le formulaire : on VÉRIFIE, on MONTRE, on n'envoie pas."""
        message = txt.get("1.0", "end").strip()
        payload = _payload(app, version, type_var.get(), email_var.get(), message)
        corps = corps_envoye(payload)
        refus = verifier(corps)
        if refus:
            etat.configure(text=refus, foreground=P.get("danger", "#DC2626"))
            txt.focus_set()
            return
        en_attente.clear()
        en_attente.update(payload=payload, corps=corps)
        zone.configure(state="normal")
        zone.delete("1.0", "end")
        zone.insert("1.0", apercu(corps, FILONAUT_CONTACT_URL, _outbox(app)))
        zone.configure(state="disabled")
        etat_apercu.configure(text="")
        formulaire.pack_forget()
        page_apercu.pack(fill="both", expand=True)

    def modifier():
        page_apercu.pack_forget()
        formulaire.pack(fill="both", expand=True)
        txt.focus_set()

    resultat_envoi: "queue.Queue" = queue.Queue()

    def _relever_resultat():
        """Relève le résultat de l'envoi DEPUIS le thread Tk. Le worker se
        contente de déposer dans une queue ; c'est le thread principal qui
        vient chercher, exactement comme partout ailleurs dans cette suite
        d'outils (thread + queue.Queue + after). Aucun appel à Tk n'est jamais
        fait depuis le thread d'envoi : un `after()` appelé depuis un autre
        thread attend que la boucle d'événements le prenne, ce qui bloque le
        worker aussi longtemps que le thread principal ne pompe pas.

        Un échec total (envoi ET boîte d'envoi impossibles) rend la main pour
        réessayer ; un succès ferme la fenêtre au bout de 2,2 s."""
        if not win.winfo_exists():
            return
        try:
            info, echec = resultat_envoi.get_nowait()
        except queue.Empty:
            win.after(80, _relever_resultat)
            return
        if echec:
            for w in barre_apercu.winfo_children():
                w.configure(state="normal")
            etat_apercu.configure(text=info, foreground=P.get("danger", "#DC2626"))
            return
        etat_apercu.configure(text=info, foreground=P.get("emeraude_fonce", "#10B981"))
        win.after(2200, fermer)

    def envoyer_tel_quel():
        """Le seul chemin qui envoie — et il envoie le dict de l'aperçu, pas un
        autre. Un corps reconstruit ici pourrait diverger de ce qui a été montré.

        L'ENVOI PART SUR UN THREAD. Jusqu'au 2026-08-26, _poster_corps était
        appelé directement dans ce callback, donc sur le thread Tk : toute
        l'application gelait le temps de la requête — jusqu'à _TIMEOUT_S
        secondes, et ce délai ne borne que la connexion et la lecture, la
        résolution DNS s'y ajoute. Mesuré à l'audit : un battement d'interface
        au lieu d'une soixantaine sur trois secondes, soit une fenêtre blanche
        et un « ne répond pas » de Windows au moment précis où l'utilisateur
        signale un bug. C'est la règle appliquée partout ailleurs dans cette
        suite d'outils (voir update_checker.start_update_check) : le réseau vit
        sur un thread, seul win.after touche aux widgets.

        Renvoie le thread lancé — exposé aussi en `win.envoi`, pour les tests,
        comme update_checker.ouvrir_mise_a_jour le fait. Un second clic pendant
        un envoi ne relance rien (les boutons sont désactivés dès le départ)."""
        if en_attente.get("envoi"):
            return None
        payload, corps = en_attente["payload"], en_attente["corps"]
        for w in barre_apercu.winfo_children():
            w.configure(state="disabled")
        etat_apercu.configure(text="Envoi en cours…", foreground=P.get("texte_doux", "#5A6B80"))

        def worker():
            try:
                if FILONAUT_CONTACT_URL:
                    _poster_corps(corps)
                    info, echec = "Message envoyé. Merci !", False
                else:
                    chemin = _enregistrer_localement(app, payload)
                    info, echec = f"Envoi désactivé dans cette version : message gardé localement.\n({chemin})", False
            except Exception as exc:  # repli : ne jamais perdre le message
                try:
                    chemin = _enregistrer_localement(app, payload)
                    info, echec = f"Message conservé localement (envoi indisponible : {exc}).\n({chemin})", False
                except Exception:
                    info, echec = f"Échec : {exc}", True
            en_attente["envoi"] = None
            resultat_envoi.put((info, echec))   # aucun appel Tk depuis ce thread

        fil = threading.Thread(target=worker, daemon=True)
        en_attente["envoi"] = fil
        win.envoi = fil
        fil.start()
        win.after(0, _relever_resultat)         # la relève, elle, part du thread Tk
        return fil

    ttk.Button(barre, text="Annuler", command=fermer).pack(side="left", padx=(0, 8))
    win.bouton_envoyer = ttk.Button(barre, text="Envoyer", command=montrer_apercu, style=style_envoyer)
    win.bouton_envoyer.pack(side="left")
    win.bouton_modifier = ttk.Button(barre_apercu, text="Modifier", command=modifier)
    win.bouton_modifier.pack(side="left", padx=(0, 8))
    win.bouton_confirmer = ttk.Button(barre_apercu, text="Envoyer tel quel", command=envoyer_tel_quel, style=style_envoyer)
    win.bouton_confirmer.pack(side="left")
    win.apercu = zone
    win.message = txt
    win.envoi = None   # le thread d'envoi, une fois « Envoyer tel quel » cliqué

    win.update_idletasks()
    # centre la fenêtre sur le parent
    try:
        top = parent.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - win.winfo_reqwidth()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
    except Exception:
        pass
    try:
        win.grab_set()
    except Exception:
        pass
    txt.focus_set()
    return win
