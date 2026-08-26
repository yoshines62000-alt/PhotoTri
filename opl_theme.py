"""Thème visuel « OPL Pro » commun aux applications Open Projects Lab.

Un seul appel, juste après la création de la fenêtre racine, AVANT de
construire l'interface :

    import opl_theme
    root = Tk()
    opl_theme.apply(root, "PhotoTri")

CE QUE FAIT CE MODULE
---------------------
1. Il pose des styles ttk **pilotés par images** (PNG 9-patch dans ./assets) :
   boutons à coins arrondis, champs avec focus ring, cases/radios/interrupteurs
   dessinés — le rendu « produit 2024 » que `clam` seul ne sait pas produire.
   C'est la technique des thèmes de référence (sv-ttk, azure-ttk). Les PNG sont
   lus par `tk.PhotoImage` (stdlib, Tk 8.6+) : AUCUNE dépendance à l'exécution.
2. Il configure aussi des *valeurs par défaut* pour les widgets Tk classiques
   (`Text`, `Listbox`, `Canvas`, `Menu`) que ttk ne stylise pas.

Il ne parcourt jamais l'arbre des widgets et n'écrase jamais une couleur posée
explicitement par l'appli : une intention locale gagne toujours.

PERFORMANCE (défaut rapide, arrondi en opt-in)
----------------------------------------------
Les éléments-images ÉTIRÉS (boutons, champs) sont trop coûteux à dessiner en
nombre : jusqu'à ~6,5 s de premier rendu sur l'app la plus dense, contre ~0,1 s
sans eux. Par défaut on garde donc boutons/champs PLATS (clam recoloré, rapide)
et on n'active en images que les indicateurs de taille fixe (cases, radios,
interrupteurs), qui, eux, ne coûtent rien. Pour retrouver les boutons/champs
arrondis sur une machine qui l'assume : variable d'environnement OPL_ROUNDED=1.
Détails et mesures dans le commentaire de _appliquer_images().

DÉGRADATION HONNÊTE
-------------------
Si le dossier assets/ manque ou qu'un PNG est illisible, on retombe sur un thème
`clam` recoloré — fonctionnel, juste moins arrondi — et on émet UN avertissement
sur stderr (jamais un échec muet : une appli qui a l'air « presque bonne » sans
qu'on sache pourquoi est pire qu'un message clair).

POURQUOI UN THÈME CLAIR alors que la charte OPL est sombre : ttk ne stylise pas
`Text`/`Listbox`/`Canvas`/`Menu`, très utilisés ici ; un thème sombre global =
texte noir sur fond noir. La marque entre par les ACCENTS (cyan, émeraude).

`option_add` n'agit que sur les widgets créés APRÈS l'appel : d'où l'obligation
d'appeler apply() immédiatement après Tk().
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk

__all__ = ["apply", "entete", "carte", "Rail", "etat_vide", "Erreur",
           "PALETTE", "couleur", "police"]

# --- Palette (deux modes) -------------------------------------------------
# Les accents de marque (cyan/émeraude/encre) sont communs aux deux modes : ils
# ressortent aussi bien sur clair que sur sombre. Seules changent les surfaces
# et le texte. `PALETTE` est le dictionnaire ACTIF ; apply() le bascule selon le
# mode AVANT que l'appli ne construise ses widgets — c'est pourquoi couleur(),
# lu à la création de chaque widget, rend la bonne teinte dans les deux modes.
_LIGHT = {
    # — L'ACCENT, ET SES DEUX RÔLES ------------------------------------------
    # `cyan` est l'APLAT : des fonds, des traits, la barre de l'entrée active.
    # Il ne sert JAMAIS de texte — 1,82:1 sur blanc, illisible. Pour écrire en
    # accent, c'est `cyan_fonce` (5,15:1). C'est la distinction que le site
    # tient sous les noms --accent / --accent-texte, et la confondre est
    # l'erreur qui rend une interface jolie et inutilisable.
    "cyan": "#30D2EB",            # --accent (aplat), identique en clair et sombre
    "cyan_fonce": "#1E759C",      # --accent-texte : l'accent ÉCRIT
    "accent_survol": "#5FD8F5",   # cyan-400 — survol d'un aplat
    "accent_presse": "#38BDDC",   # cyan-500 — aplat enfoncé
    "encre": "#04222E",           # --sur-accent : ce qu'on écrit SUR l'aplat (9,07:1)
    # — Marque (fonds navy du site, pour un bandeau ou un aplat sombre)
    "ardoise": "#10141C",
    "ardoise_clair": "#171D27",
    # — Surfaces
    "fond": "#EDF1F8",            # clair-100
    "surface": "#FFFFFF",         # clair-000
    "surface_2": "#F5F8FC",       # clair-050
    "survol": "#F5F8FC",
    "bordure": "#DCE3EF",         # clair-200
    "bordure_forte": "#808899",   # clair-300
    # — Texte
    "texte": "#10141C",           # navy-900 — 18,44:1 sur surface
    "texte_doux": "#3C4A68",      # encre-600 — 8,86:1
    "gris_texte": "#54648A",      # encre-400 — 5,89:1
    # — États. Les rampes CLAIRES : ambre-400 sur blanc rend 1,89:1, d'où les
    # variantes -700. Les fonds sont dérivés comme sur le site (mélange de la
    # teinte -400 sur le fond moyen), au taux qui laisse le texte au-dessus de
    # 4,5:1 — mesuré, pas approché.
    "danger": "#AC495A",          # rouge-700 — 5,45:1
    "danger_bg": "#F3E8EE",       # texte danger dessus : 4,56:1
    "succes": "#217756",          # vert-700 — 5,47:1
    "emeraude_fonce": "#217756",
    "emeraude": "#37C68F",        # vert-400 — aplat, pas du texte
    "vert_lab": "#2F9E68",
    "avertissement": "#826535",   # ambre-700 — 5,44:1
    "avertissement_bg": "#F2EEE5",
    "surbrillance": "#F2EBDF",
    "lien": "#1E759C",
}
_DARK = {
    # L'APLAT NE CHANGE PAS : c'est la couleur de la teinte, pas un rôle de
    # surface. Seule l'encre s'adapte — vif sur fond sombre, assombrie sur
    # fond clair. Le bouton principal a donc exactement le même cyan dans les
    # deux thèmes, et c'est voulu.
    "cyan": "#30D2EB",
    "cyan_fonce": "#59E2FD",      # --accent-texte version vive (12,05:1 sur navy)
    "accent_survol": "#5FD8F5",
    "accent_presse": "#38BDDC",
    "encre": "#04222E",           # inchangé : on écrit toujours sombre sur l'aplat
    "ardoise": "#0C1422",
    "ardoise_clair": "#1B2740",
    "fond": "#0A0D13",            # navy-950
    "surface": "#10141C",         # navy-900
    "surface_2": "#171D27",       # navy-850
    "survol": "#171D27",
    "bordure": "#232A36",         # navy-700
    "bordure_forte": "#637294",   # navy-600
    "texte": "#EEF2F8",           # ardoise-100
    "texte_doux": "#A7B6CF",      # ardoise-300
    "gris_texte": "#8798B5",      # ardoise-400
    "danger": "#ED657C",          # rouge-400
    "danger_bg": "#2F1F29",
    "succes": "#37C68F",          # vert-400
    "emeraude_fonce": "#37C68F",
    "emeraude": "#37C68F",
    "vert_lab": "#3DDC97",
    "avertissement": "#E8B45F",   # ambre-400
    "avertissement_bg": "#2E2A25",
    "surbrillance": "#373128",
    "lien": "#59E2FD",
}
PALETTE = dict(_LIGHT)   # dictionnaire ACTIF, basculé par apply()


def _resoudre_mode(mode):
    """mode explicite ('light'/'dark') sinon préférence enregistrée sinon clair."""
    if mode in ("light", "dark"):
        return mode
    try:
        val = _pref_path().read_text(encoding="utf-8").strip().lower()
        return "dark" if val == "dark" else "light"
    except Exception:
        return "light"


def _pref_path() -> Path:
    base = os.environ.get("APPDATA") if sys.platform.startswith("win") else None
    racine = Path(base) if base else Path.home() / ".config"
    d = racine / "OpenProjectsLab"
    d.mkdir(parents=True, exist_ok=True)
    return d / "theme.txt"


def mode_actuel() -> str:
    """Renvoie le mode réellement appliqué au dernier apply() ('light'/'dark')."""
    return _MODE[0]


def definir_mode(mode: str) -> None:
    """Enregistre la préférence de thème (prise en compte au prochain apply())."""
    try:
        _pref_path().write_text("dark" if mode == "dark" else "light", encoding="utf-8")
    except Exception:
        pass


_MODE = ["light"]  # mode réellement actif, mis à jour par apply()

# polices résolues au premier apply(), réutilisables par les applis via police()
_POLICES: dict[str, tuple] = {}


def couleur(nom: str) -> str:
    """Accès nommé à la palette, pour que les applis n'écrivent pas de hex."""
    return PALETTE[nom]


def police(role: str = "corps") -> tuple:
    """Police d'un rôle typographique : display, titre, soustitre, corps,
    corps_gras, petit, mono. Vide tant qu'apply() n'a pas tourné."""
    return _POLICES.get(role, ("TkDefaultFont", 10))


def _police_dispo(root: tk.Misc, *noms: str) -> str:
    from tkinter import font as tkfont
    try:
        dispo = {f.lower() for f in tkfont.families(root)}
    except tk.TclError:
        return noms[-1]
    for n in noms:
        if n.lower() in dispo:
            return n
    return noms[-1]


# --- chargement des assets ------------------------------------------------
_ASSETS = (
    "btn_accent_normal btn_accent_active btn_accent_pressed btn_accent_disabled "
    "btn_default_normal btn_default_active btn_default_pressed btn_default_disabled btn_default_focus "
    "btn_danger_normal btn_danger_active btn_danger_pressed "
    "field_normal field_focus field_disabled card card_soft "
    "check_off check_on radio_off radio_on switch_off switch_on"
).split()


def _charger_images(root: tk.Misc) -> dict | None:
    """Charge tous les PNG en tk.PhotoImage. Renvoie None (et prévient) si l'un
    manque — on gardera alors le thème plat. Les images sont ancrées sur `root`
    pour survivre au ramasse-miettes (sinon Tk affiche du vide)."""
    # Chemin des assets, compatible PyInstaller : en exécutable gelé, les
    # données embarquées sont sous sys._MEIPASS (même schéma que le
    # _resource_path() de chaque appli) ; en source, à côté de ce fichier. Le
    # .spec doit inclure opl_assets ET opl_assets_dark dans `datas` — sinon
    # repli plat honnête. Le sous-dossier dépend du mode actif.
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    dossier = base / ("opl_assets_dark" if _MODE[0] == "dark" else "opl_assets")
    if not dossier.exists():                       # pas d'assets sombres ? on
        dossier = base / "opl_assets"              # retombe sur les clairs
    imgs: dict = {}
    try:
        for nom in _ASSETS:
            chemin = dossier / f"{nom}.png"
            imgs[nom] = tk.PhotoImage(master=root, file=str(chemin))
    except (tk.TclError, OSError) as exc:
        print(f"[opl_theme] assets indisponibles ({exc}) — thème plat de repli.",
              file=sys.stderr)
        return None
    root._opl_imgs = imgs  # type: ignore[attr-defined]  # garde une référence vivante
    return imgs


def _elem_image(style, nom, imgs, defaut, etats, *, border, sticky="nsew", padding=None):
    spec = [imgs[defaut]]
    for etat, cle in etats:
        spec.append((etat, imgs[cle]))
    kw = {"border": border, "sticky": sticky}
    if padding is not None:
        kw["padding"] = padding
    style.element_create(nom, "image", *spec, **kw)


def _appliquer_images(style: ttk.Style, imgs: dict, titre: str, corps: str) -> list[str]:
    """Remplace la géométrie des widgets clés par des éléments-images. Chaque
    widget est isolé : si son montage échoue (Tcl capricieux), il garde le rendu
    plat sans casser les autres. Renvoie la liste des widgets retombés à plat."""
    echecs: list[str] = []
    p = PALETTE
    # PERF (mesuré le 2026-08-10) — les éléments-images ÉTIRÉS (boutons, champs :
    # 9-patch `sticky=nsew`) coûtent extrêmement cher à dessiner dès qu'une
    # fenêtre en compte beaucoup : jusqu'à 6,5 s de PREMIER RENDU sur l'app la
    # plus dense (DownloadOrganizer), contre ~0,1 s sans eux. Le coût vient de
    # l'étirement 9-patch recalculé par widget, pas de l'alpha (rendre les coins
    # opaques ne l'enlève qu'à moitié). Les INDICATEURS (cases/radios/switch :
    # taille FIXE, `sticky=""`) ne s'étirent pas → négligeables, on les garde
    # toujours. Les boutons/champs restent donc PLATS par défaut (rendu clam
    # déjà posé dans apply(), rapide) ; leur version arrondie est un opt-in
    # assumé via OPL_ROUNDED=1. NE PAS réactiver les images de bouton/champ par
    # défaut sans re-mesurer le premier rendu de DownloadOrganizer.
    rounded = os.environ.get("OPL_ROUNDED") == "1"

    def bouton(style_nom, prefixe, focus_img=None):
        elem = f"{style_nom}.img"
        etats = [("pressed", f"{prefixe}_pressed"), ("active", f"{prefixe}_active")]
        if focus_img:
            etats.append(("focus", focus_img))
        if f"{prefixe}_disabled" in imgs:
            etats.append(("disabled", f"{prefixe}_disabled"))
        _elem_image(style, elem, imgs, f"{prefixe}_normal", etats,
                    border=9, padding=(13, 7))
        style.layout(style_nom, [(elem, {"sticky": "nsew", "children": [
            ("Button.label", {"sticky": "nsew"})]})])

    if rounded:
        try:
            bouton("TButton", "btn_default", focus_img="btn_default_focus")
            style.configure("TButton", foreground=p["texte"], font=(corps, 10), anchor="center")
            style.map("TButton", foreground=[("disabled", p["texte_doux"])])
        except tk.TclError:
            echecs.append("TButton")

        try:
            bouton("Accent.TButton", "btn_accent", focus_img="btn_accent_active")
            style.configure("Accent.TButton", foreground=p["encre"], font=(titre, 10), anchor="center")
            style.map("Accent.TButton", foreground=[("disabled", p["texte_doux"])])
        except tk.TclError:
            echecs.append("Accent.TButton")

        try:
            bouton("Danger.TButton", "btn_danger", focus_img="btn_danger_active")
            style.configure("Danger.TButton", foreground=p["danger"], font=(corps, 10), anchor="center")
        except tk.TclError:
            echecs.append("Danger.TButton")

        # Champs : Entry + Combobox partagent l'élément field arrondi.
        try:
            _elem_image(style, "OPL.field", imgs, "field_normal",
                        [("focus", "field_focus"), ("disabled", "field_disabled")],
                        border=8, padding=(9, 6))
            style.layout("TEntry", [("OPL.field", {"sticky": "nsew", "children": [
                ("Entry.textarea", {"sticky": "nsew"})]})])
            style.configure("TEntry", foreground=p["texte"], fieldbackground=p["surface"],
                            insertcolor=p["cyan"])
        except tk.TclError:
            echecs.append("TEntry")

        try:
            style.layout("TCombobox", [("OPL.field", {"sticky": "nsew", "children": [
                ("Combobox.downarrow", {"side": "right", "sticky": "ns"}),
                ("Combobox.padding", {"sticky": "nsew", "children": [
                    ("Combobox.textarea", {"sticky": "nsew"})]})]})])
            style.configure("TCombobox", foreground=p["texte"], arrowcolor=p["texte_doux"])
            style.map("TCombobox", arrowcolor=[("active", p["cyan"])])
        except tk.TclError:
            echecs.append("TCombobox")

    # Case à cocher, radio, interrupteur : indicateur dessiné (taille fixe,
    # non étiré → coût négligeable, toujours actif même sans OPL_ROUNDED).
    try:
        _elem_image(style, "OPL.check", imgs, "check_off",
                    [("selected", "check_on")], border=0, sticky="")
        style.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nsew", "children": [
            ("OPL.check", {"side": "left", "sticky": ""}),
            ("Checkbutton.label", {"side": "left", "sticky": "nsew"})]})])
        style.configure("TCheckbutton", background=p["fond"], foreground=p["texte"], padding=(2, 3))
    except tk.TclError:
        echecs.append("TCheckbutton")

    try:
        _elem_image(style, "OPL.radio", imgs, "radio_off",
                    [("selected", "radio_on")], border=0, sticky="")
        style.layout("TRadiobutton", [("Radiobutton.padding", {"sticky": "nsew", "children": [
            ("OPL.radio", {"side": "left", "sticky": ""}),
            ("Radiobutton.label", {"side": "left", "sticky": "nsew"})]})])
        style.configure("TRadiobutton", background=p["fond"], foreground=p["texte"], padding=(2, 3))
    except tk.TclError:
        echecs.append("TRadiobutton")

    try:
        _elem_image(style, "OPL.switch", imgs, "switch_off",
                    [("selected", "switch_on")], border=0, sticky="")
        style.layout("Switch.TCheckbutton", [("Checkbutton.padding", {"sticky": "nsew", "children": [
            ("OPL.switch", {"side": "left", "sticky": ""}),
            ("Checkbutton.label", {"side": "left", "sticky": "nsew"})]})])
        style.configure("Switch.TCheckbutton", background=p["fond"], foreground=p["texte"], padding=(2, 3))
    except tk.TclError:
        echecs.append("Switch.TCheckbutton")

    # Carte arrondie = image ÉTIRÉE (même classe de coût que les boutons) :
    # réservée à OPL_ROUNDED. Sans lui, carte() retombe sur le style plat bordé
    # défini dans apply() (rapide). Peu de cartes par écran, mais on reste
    # cohérent avec la décision boutons/champs.
    if rounded:
        try:
            style.element_create("OPL.card", "image", imgs["card"], border=12, sticky="nsew")
            style.layout("Card.TFrame", [("OPL.card", {"sticky": "nsew"})])
            style.configure("Card.TFrame", padding=16)
            style.element_create("OPL.card_soft", "image", imgs["card_soft"], border=12, sticky="nsew")
            style.layout("CarteDouce.TFrame", [("OPL.card_soft", {"sticky": "nsew"})])
            style.configure("CarteDouce.TFrame", padding=16)
        except tk.TclError:
            echecs.append("Card.TFrame")

    return echecs


def apply(root: tk.Misc, nom_appli: str = "", *, base: str = "clam", mode=None) -> ttk.Style:
    """Applique le thème OPL Pro à `root`. Renvoie le ttk.Style pour affinage.

    `mode` : 'light' | 'dark' | None. None ⇒ préférence enregistrée (défaut
    clair). Le mode doit être fixé AVANT que l'appli ne construise ses widgets
    (contrat identique à celui d'option_add) : PALETTE actif est basculé ici,
    donc chaque couleur() lue à la création rend la bonne teinte.
    """
    mode = _resoudre_mode(mode)
    _MODE[0] = mode
    PALETTE.clear()
    PALETTE.update(_DARK if mode == "dark" else _LIGHT)

    style = ttk.Style(root)
    try:
        style.theme_use(base)
    except tk.TclError:
        pass

    p = PALETTE
    # Segoe UI Semibold EN TETE : c'est la police du site et de BRAND.md.
    # Bahnschrift reste en repli — elle passait devant, ce qui donnait aux
    # apps une graisse condensee que le site n'a nulle part.
    titre = _police_dispo(root, "Segoe UI Semibold", "Bahnschrift SemiBold", "Segoe UI", "TkDefaultFont")
    titre_reg = _police_dispo(root, "Segoe UI Semibold", "Bahnschrift", "Segoe UI", "TkDefaultFont")
    corps = _police_dispo(root, "Segoe UI", "Inter", "DejaVu Sans", "TkDefaultFont")
    mono = _police_dispo(root, "Cascadia Mono", "Consolas", "DejaVu Sans Mono", "TkFixedFont")
    # SIX PAS, UN ROLE PAR PAS. L'echelle en comptait neuf (9, 10, 11, 13,
    # 15, 22...) sans qu'aucun role ne les distingue : c'est ce qui fait
    # qu'une fenetre parait bricolee sans qu'on sache dire pourquoi.
    # `display` est en MONO a chasse fixe : ses deux seuls usages sont des
    # NOMBRES (chronometre, valeurs du tableau de bord), qui doivent
    # s'aligner et ne pas danser quand ils changent.
    _POLICES.update({
        "display": (mono, 26),      # un seul usage par appli, jamais du texte
        "titre": (titre, 12),       # titre de vue
        "soustitre": (titre, 11),   # nom de l'appli dans le bandeau
        "corps_gras": (titre, 10),  # boutons, entree active du rail
        "corps": (corps, 9),        # texte courant, listes
        "petit": (corps, 8),        # barre d'etat, legendes, groupes
        "mono": (mono, 9),
    })

    root.configure(background=p["fond"])

    # --- widgets Tk classiques (non stylables par ttk) --------------------
    for motif, valeur in (
        ("*Font", (corps, 9)),
        ("*Text.background", p["surface"]),
        ("*Text.foreground", p["texte"]),
        ("*Text.insertBackground", p["cyan"]),
        ("*Text.selectBackground", p["survol"]),
        ("*Text.selectForeground", p["texte"]),
        ("*Text.borderWidth", 1),
        ("*Text.relief", "solid"),
        ("*Text.highlightThickness", 0),
        ("*Text.padX", 8),
        ("*Text.padY", 6),
        ("*Listbox.background", p["surface"]),
        ("*Listbox.foreground", p["texte"]),
        ("*Listbox.selectBackground", p["cyan"]),
        ("*Listbox.selectForeground", p["encre"]),
        ("*Listbox.borderWidth", 1),
        ("*Listbox.relief", "solid"),
        ("*Listbox.highlightThickness", 0),
        ("*Listbox.activeStyle", "none"),
        ("*Canvas.background", p["surface"]),
        ("*Canvas.highlightThickness", 0),
        ("*Menu.background", p["surface"]),
        ("*Menu.foreground", p["texte"]),
        ("*Menu.activeBackground", p["cyan"]),
        ("*Menu.activeForeground", p["encre"]),
        ("*Menu.borderWidth", 0),
        ("*Menu.relief", "flat"),
        ("*Menu.activeBorderWidth", 0),
        ("*Toplevel.background", p["fond"]),
        # --- widgets Tk CLASSIQUES (tk.Frame/Label/Button/Entry/…) ---------
        # Les 7 applis en utilisent des centaines. ttk ne les stylise pas et
        # option_add non plus par défaut : en clair ils passaient inaperçus
        # (défauts Windows ≈ palette claire), mais en SOMBRE ils restaient des
        # blocs clairs (ex. la barre de statut de DownloadOrganizer). On pose
        # donc des défauts pour chaque type classique — une couleur explicite
        # de l'appli (via couleur()) reste prioritaire, ces valeurs ne sont que
        # le repli.
        ("*Frame.background", p["fond"]),
        ("*Labelframe.background", p["fond"]),
        ("*Labelframe.foreground", p["texte_doux"]),
        ("*Label.background", p["fond"]),
        ("*Label.foreground", p["texte"]),
        ("*Button.background", p["surface"]),
        ("*Button.foreground", p["texte"]),
        ("*Button.activeBackground", p["survol"]),
        ("*Button.activeForeground", p["texte"]),
        ("*Button.highlightBackground", p["fond"]),
        ("*Button.disabledForeground", p["texte_doux"]),
        ("*Entry.background", p["surface"]),
        ("*Entry.foreground", p["texte"]),
        ("*Entry.insertBackground", p["cyan"]),
        ("*Entry.selectBackground", p["survol"]),
        ("*Entry.selectForeground", p["texte"]),
        ("*Entry.disabledBackground", p["fond"]),
        ("*Entry.readonlyBackground", p["surface_2"]),
        ("*Entry.highlightThickness", 0),
        ("*Checkbutton.background", p["fond"]),
        ("*Checkbutton.foreground", p["texte"]),
        ("*Checkbutton.activeBackground", p["fond"]),
        ("*Checkbutton.activeForeground", p["texte"]),
        ("*Checkbutton.selectColor", p["surface"]),
        ("*Radiobutton.background", p["fond"]),
        ("*Radiobutton.foreground", p["texte"]),
        ("*Radiobutton.activeBackground", p["fond"]),
        ("*Radiobutton.activeForeground", p["texte"]),
        ("*Radiobutton.selectColor", p["surface"]),
        ("*Scale.background", p["fond"]),
        ("*Scale.foreground", p["texte"]),
        ("*Scale.troughColor", p["surface_2"]),
        ("*Scale.activeBackground", p["survol"]),
        ("*Scale.highlightThickness", 0),
        ("*Spinbox.background", p["surface"]),
        ("*Spinbox.foreground", p["texte"]),
        ("*Spinbox.insertBackground", p["cyan"]),
        ("*Spinbox.buttonBackground", p["surface"]),
        ("*Spinbox.readonlyBackground", p["surface_2"]),
    ):
        root.option_add(motif, valeur)

    # --- base ttk plate (fallback ET socle de couleurs/typo) --------------
    style.configure(".", background=p["fond"], foreground=p["texte"],
                    fieldbackground=p["surface"], bordercolor=p["bordure"],
                    lightcolor=p["surface"], darkcolor=p["surface"], font=(corps, 9))

    style.configure("TFrame", background=p["fond"])
    style.configure("TLabel", background=p["fond"], foreground=p["texte"])
    style.configure("TLabelframe", background=p["fond"], bordercolor=p["bordure"], relief="solid")
    style.configure("TLabelframe.Label", background=p["fond"], foreground=p["texte_doux"], font=(titre_reg, 10))
    style.configure("TSeparator", background=p["bordure"])
    style.configure("TSizegrip", background=p["fond"])

    # Rendus plats de repli (utilisés si les images ne montent pas)
    # focuscolor = cyan_fonce et NON cyan : l'aplat #30D2EB ne rend que
    # 2,83:1 sur lui-meme, l'anneau y serait invisible. Mesure, pas suppose.
    style.configure("TButton", background=p["surface_2"], foreground=p["texte"],
                    bordercolor=p["bordure_forte"], focuscolor=p["cyan_fonce"],
                    padding=(11, 5), relief="flat", font=(corps, 10))
    style.map("TButton",
              background=[("pressed", p["bordure"]), ("active", p["fond"]), ("disabled", p["surface_2"])],
              foreground=[("disabled", p["bordure_forte"])],
              bordercolor=[("pressed", p["cyan"]), ("focus", p["cyan_fonce"]),
                           ("disabled", p["bordure"]), ("active", p["bordure_forte"])])
    # LE BOUTON PRINCIPAL DEVENAIT VERT AU SURVOL : sa map pointait sur
    # emeraude/emeraude_fonce, reliquat d'une palette ou l'accent etait teal.
    # Un bouton cyan qui vire au vert sous le curseur, dans les sept applis.
    # Les etats restent desormais dans la rampe cyan du site (cyan-400 au
    # survol, cyan-500 enfonce).
    # focuscolor = encre : sur l'aplat cyan, c'est la SEULE valeur lisible
    # (9,07:1 contre 2,83:1 pour cyan_fonce).
    style.configure("Accent.TButton", background=p["cyan"], foreground=p["encre"],
                    bordercolor=p["cyan"], focuscolor=p["encre"],
                    padding=(12, 6), relief="flat", font=(titre, 10))
    style.map("Accent.TButton",
              background=[("pressed", p["accent_presse"]), ("active", p["accent_survol"]),
                          ("disabled", p["surface_2"])],
              bordercolor=[("pressed", p["accent_presse"]), ("active", p["accent_survol"]),
                           ("disabled", p["bordure"])],
              foreground=[("disabled", p["bordure_forte"])])
    style.configure("Danger.TButton", background=p["surface_2"], foreground=p["danger"],
                    bordercolor=p["danger"], focuscolor=p["danger"],
                    padding=(11, 5), relief="flat", font=(corps, 10))
    style.map("Danger.TButton",
              background=[("pressed", p["danger_bg"]), ("active", p["danger_bg"]),
                          ("disabled", p["surface_2"])],
              foreground=[("disabled", p["bordure_forte"])],
              bordercolor=[("disabled", p["bordure"])])

    style.configure("TEntry", fieldbackground=p["surface"], foreground=p["texte"],
                    bordercolor=p["bordure"], insertcolor=p["cyan"], padding=6, relief="flat")
    # États readonly/disabled : sans ces maps, clam retombe sur SON fond clair
    # par défaut → champ blanc en mode sombre (glitch trouvé à l'audit live).
    style.map("TEntry",
              bordercolor=[("focus", p["cyan"])],
              fieldbackground=[("readonly", p["surface_2"]), ("disabled", p["fond"])],
              foreground=[("readonly", p["texte"]), ("disabled", p["texte_doux"])])
    style.configure("TCombobox", fieldbackground=p["surface"], background=p["surface"],
                    foreground=p["texte"], bordercolor=p["bordure"], arrowcolor=p["texte_doux"], padding=6)
    style.map("TCombobox",
              bordercolor=[("focus", p["cyan"])],
              arrowcolor=[("active", p["cyan"])],
              fieldbackground=[("readonly", p["surface"]), ("disabled", p["fond"])],
              background=[("readonly", p["surface"]), ("disabled", p["fond"])],
              foreground=[("readonly", p["texte"]), ("disabled", p["texte_doux"])])
    # Liste déroulante de la combobox (Listbox interne) : la teindre aussi.
    root.option_add("*TCombobox*Listbox.background", p["surface"])
    root.option_add("*TCombobox*Listbox.foreground", p["texte"])
    root.option_add("*TCombobox*Listbox.selectBackground", p["cyan"])
    root.option_add("*TCombobox*Listbox.selectForeground", p["encre"])
    style.configure("TCheckbutton", background=p["fond"], foreground=p["texte"])
    style.map("TCheckbutton", indicatorcolor=[("selected", p["cyan"])])
    style.configure("TRadiobutton", background=p["fond"], foreground=p["texte"])
    style.map("TRadiobutton", indicatorcolor=[("selected", p["cyan"])])

    # Widgets gardés en config (pas d'image) : onglets, arbre, jauges, ascenseurs
    style.configure("TNotebook", background=p["fond"], bordercolor=p["bordure"], tabmargins=(2, 6, 2, 0))
    style.configure("TNotebook.Tab", background=p["fond"], foreground=p["texte_doux"],
                    padding=(14, 8), font=(corps, 9), bordercolor=p["bordure"])
    style.map("TNotebook.Tab",
              background=[("selected", p["surface"])],
              foreground=[("selected", p["texte"]), ("active", p["texte"])],
              expand=[("selected", (0, 0, 0, 2))])

    style.configure("Treeview", background=p["surface"], fieldbackground=p["surface"],
                    foreground=p["texte"], bordercolor=p["bordure"], relief="flat", rowheight=28)
    style.configure("Treeview.Heading", background=p["fond"], foreground=p["texte_doux"],
                    font=(titre_reg, 10), relief="flat", padding=(10, 8), bordercolor=p["bordure"])
    style.map("Treeview", background=[("selected", p["cyan"])], foreground=[("selected", p["encre"])])
    style.map("Treeview.Heading", background=[("active", p["survol"])])

    style.configure("TProgressbar", background=p["cyan"], troughcolor=p["surface_2"],
                    bordercolor=p["surface_2"], lightcolor=p["cyan"], darkcolor=p["cyan"], thickness=8)
    style.configure("Horizontal.TProgressbar", background=p["cyan"], troughcolor=p["surface_2"],
                    bordercolor=p["surface_2"], lightcolor=p["cyan"], darkcolor=p["cyan"], thickness=8)
    style.configure("TScrollbar", background=p["bordure"], troughcolor=p["fond"],
                    bordercolor=p["fond"], arrowcolor=p["texte_doux"], relief="flat", width=12)
    style.map("TScrollbar", background=[("active", p["bordure_forte"])])
    style.configure("TScale", background=p["fond"], troughcolor=p["surface_2"])
    style.configure("TSpinbox", fieldbackground=p["surface"], foreground=p["texte"],
                    bordercolor=p["bordure"], arrowcolor=p["texte_doux"], padding=6, relief="flat")
    style.map("TSpinbox",
              bordercolor=[("focus", p["cyan"])],
              fieldbackground=[("readonly", p["surface_2"]), ("disabled", p["fond"])],
              foreground=[("readonly", p["texte"]), ("disabled", p["texte_doux"])])

    # --- styles nommés (hiérarchie typographique partagée) ----------------
    style.configure("Display.TLabel", background=p["fond"], foreground=p["texte"], font=(titre, 22))
    style.configure("Titre.TLabel", background=p["fond"], foreground=p["texte"], font=(titre, 15))
    style.configure("SousTitre.TLabel", background=p["fond"], foreground=p["texte_doux"], font=(corps, 11))
    style.configure("Section.TLabel", background=p["fond"], foreground=p["texte_doux"], font=(titre_reg, 10))
    style.configure("Mono.TLabel", background=p["fond"], foreground=p["texte_doux"], font=(mono, 9))
    style.configure("Succes.TLabel", background=p["fond"], foreground=p["emeraude_fonce"], font=(corps, 10))
    style.configure("Erreur.TLabel", background=p["fond"], foreground=p["danger"], font=(corps, 10))
    style.configure("Puce.TLabel", background=p["surface_2"], foreground=p["texte_doux"], font=(mono, 9), padding=(8, 3))
    # Labels posés SUR une carte blanche (fond surface, pas fond général).
    style.configure("Carte.TLabel", background=p["surface"], foreground=p["texte"])
    style.configure("CarteTitre.TLabel", background=p["surface"], foreground=p["texte"], font=(titre, 13))
    style.configure("CarteDoux.TLabel", background=p["surface"], foreground=p["texte_doux"], font=(corps, 10))
    # Barre de statut : un ttk.Label relief="sunken" rend un fond CLAIR sous
    # clam (quirk confirmé à l'audit live, glaring en sombre). Ce style plat sur
    # surface_2 donne le même effet « champ discret » et se teinte correctement
    # dans les deux modes.
    style.configure("Statut.TLabel", background=p["surface_2"], foreground=p["texte_doux"], padding=(8, 3))
    # Cartes en version PLATE (défaut, rapide) : surface blanche + hairline via
    # relief. La version arrondie (image) ne prend le dessus que sous OPL_ROUNDED.
    style.configure("Card.TFrame", background=p["surface"], relief="solid",
                    borderwidth=1, bordercolor=p["bordure"], padding=16)
    style.configure("CarteDouce.TFrame", background=p["surface_2"], relief="solid",
                    borderwidth=1, bordercolor=p["bordure"], padding=16)

    # En-tête de marque. IL SUIT LE THÈME — il était verrouillé sur le navy
    # `ardoise` dans les DEUX modes, avec un texte codé en dur (#EAF2FF) qui
    # n'est lisible que sur du sombre. En thème clair on obtenait donc une
    # barre noire au-dessus d'une application claire, et ses liens tombaient
    # à 3,4:1 une fois la palette du site posée. Ce qui identifie la marque
    # n'est pas un fond noir : c'est le TRAIT CYAN de 2 px en dessous.
    style.configure("Entete.TFrame", background=p["fond"])
    style.configure("EnteteTrait.TFrame", background=p["cyan"])
    style.configure("EnteteTitre.TLabel", background=p["fond"], foreground=p["texte"], font=(titre, 11))
    style.configure("EnteteSousTitre.TLabel", background=p["fond"], foreground=p["gris_texte"], font=(mono, 8))
    style.configure("EntetePuce.TLabel", background=p["surface_2"], foreground=p["cyan_fonce"], font=(mono, 8), padding=(8, 3))
    style.configure("EnteteLien.TLabel", background=p["fond"], foreground=p["cyan_fonce"], font=(corps, 9))

    # --- rail de navigation (remplace ttk.Notebook) -----------------------
    # Le rail vit sur `fond`, la zone de travail sur `surface` : c'est ce seul
    # ecart de valeur qui les separe, sans ombre ni relief.
    style.configure("RailHote.TFrame", background=p["fond"])
    style.configure("Rail.TFrame", background=p["fond"])
    style.configure("RailTrait.TFrame", background=p["bordure"])
    # La zone de travail reste sur `fond`, PAS sur `surface`. La maquette la
    # voulait blanche ; essaye et mesure a l'ecran, le rendu est PAR PLAQUES :
    # seule la page recoit le blanc, ses sous-cadres gardent `fond`, et les
    # applications en imbriquent partout. Un aplat uniforme separe deja le rail
    # du travail par son trait d'un pixel — c'est plus propre qu'un blanc
    # troue.
    style.configure("RailContenu.TFrame", background=p["fond"])
    # La barre de 3 px qui marque l'entree active. `RailBarre` est transparente
    # (couleur du rail) : seule l'entree active recoit `RailBarreActive`.
    style.configure("RailBarre.TFrame", background=p["fond"])
    style.configure("RailBarreActive.TFrame", background=p["cyan"])
    style.configure("RailItem.TLabel", background=p["fond"], foreground=p["texte_doux"],
                    font=(corps, 9), padding=(10, 6))
    style.configure("RailItemSurvol.TLabel", background=p["surface_2"], foreground=p["texte"],
                    font=(corps, 9), padding=(10, 6))
    # DEUX signaux pour l'etat actif — la barre cyan ET le fond. La couleur du
    # texte seule ne se voit pas, et ne se voit pas du tout en daltonisme.
    style.configure("RailItemActif.TLabel", background=p["surface_2"], foreground=p["texte"],
                    font=(titre, 10), padding=(10, 6))
    style.configure("RailGroupe.TLabel", background=p["fond"], foreground=p["gris_texte"],
                    font=(corps, 8), padding=(13, 10, 10, 2))

    # --- etat vide --------------------------------------------------------
    style.configure("EtatVide.TFrame", background=p["fond"])
    style.configure("EtatVideTitre.TLabel", background=p["fond"], foreground=p["texte"], font=(titre, 12))
    style.configure("EtatVideTexte.TLabel", background=p["fond"], foreground=p["texte_doux"], font=(corps, 9))

    # --- erreur en ligne --------------------------------------------------
    # Fond `surface_2` et non `danger_bg` : le bandeau se pose SOUS un champ,
    # dans le flux, pas au milieu d'une page. Un aplat rouge y crierait ; la
    # barre de 3 px suffit a le designer, et le texte reste lisible (le rouge
    # sur `surface_2` tient au-dessus de 4,5:1, mesure).
    style.configure("Erreur.TFrame", background=p["surface_2"])
    style.configure("ErreurBarre.TFrame", background=p["danger"])
    style.configure("ErreurTitre.TLabel", background=p["surface_2"], foreground=p["danger"], font=(titre, 9))
    style.configure("ErreurTexte.TLabel", background=p["surface_2"], foreground=p["texte_doux"], font=(corps, 9))
    # Le champ fautif se signale par son trait, pas par son fond : un fond
    # rouge derriere du texte saisi le rend penible a relire, or c'est
    # justement ce que l'utilisateur doit relire.
    style.configure("Erreur.TEntry", fieldbackground=p["surface"], foreground=p["texte"],
                    bordercolor=p["danger"], insertcolor=p["cyan"], padding=6, relief="flat")

    # --- montage des images (avec repli par widget) -----------------------
    imgs = _charger_images(root)
    if imgs is not None:
        echecs = _appliquer_images(style, imgs, titre, corps)
        if echecs:
            print("[opl_theme] rendu plat conservé pour : " + ", ".join(echecs), file=sys.stderr)

    _ = nom_appli  # réservé au bandeau entete()
    return style


# ---------------------------------------------------------------------------
# Les pages du site vers lesquelles le menu Aide renvoie. Chemins FRANÇAIS,
# recopiés de site/src/i18n/index.ts (ROUTES) et du préfixe des fiches
# (PREFIXE_LOGICIEL) d'Open Projects Lab : un renommage de route là-bas doit
# être reporté ici — les sept applications installées ne peuvent pas lire la
# table du site à l'exécution. Aucune page « /docs/<app> » n'existe : la FICHE
# du logiciel tient ce rôle (guide, notes de version, téléchargement vérifié).
SITE = "https://openprojectslab.com"
AIDE = {
    "fiche": "/logiciels/{slug}/",
    "glossaire": "/glossaire/",
    "communaute": "/communaute/",
}


def url_aide(cle: str, slug: str = "") -> str:
    return SITE + AIDE[cle].format(slug=slug)


def entete(parent: tk.Misc, nom_appli: str, accroche: str = "", *,
           badge: str = "OPEN PROJECTS LAB", on_contact=None,
           slug: str | None = None, version: str = "") -> ttk.Frame:
    """Bandeau de marque à empaqueter en haut de la fenêtre (fill='x').

    `on_contact` : si fourni (callable sans argument), ajoute un lien « Contact »
    cliquable à droite du bandeau — typiquement `lambda: opl_contact.ouvrir(...)`.

    `slug` : si fourni, ajoute un lien « Aide » qui déroule un petit menu vers
    la fiche du logiciel sur le site, le glossaire et la communauté — plus
    « Signaler un problème » (le contact) et une ligne « À propos ». Le menu est
    exposé en `cadre.menu_aide` (None sans slug), pour les tests.

    POURQUOI UN MENU, ET POURQUOI ICI. Les sept applications n'ont aucune barre
    de menus : l'aide n'avait aucun point d'entrée, et l'existence même du
    glossaire ou de la page Communauté restait invisible depuis le logiciel.
    Le bandeau est le seul chrome commun aux sept — un lien de plus ici, c'est
    une ligne changée dans chaque gui.py, et le même comportement partout.
    """
    # `cadre` n'est plus qu'un conteneur : le rembourrage descend dans
    # `corps`, pour que le trait cyan puisse courir sur TOUTE la largeur,
    # bord à bord, sans être rentré de 20 px comme le reste.
    cadre = ttk.Frame(parent, style="Entete.TFrame")
    corps_entete = ttk.Frame(cadre, style="Entete.TFrame", padding=(20, 12))
    corps_entete.pack(fill="x")
    ligne = ttk.Frame(corps_entete, style="Entete.TFrame")
    ligne.pack(fill="x")
    bloc = ttk.Frame(ligne, style="Entete.TFrame")
    bloc.pack(side="left")
    ttk.Label(bloc, text=nom_appli, style="EnteteTitre.TLabel").pack(anchor="w")
    if accroche:
        ttk.Label(bloc, text=accroche, style="EnteteSousTitre.TLabel").pack(anchor="w", pady=(2, 0))

    droite = ttk.Frame(ligne, style="Entete.TFrame")
    droite.pack(side="right", anchor="ne")
    if badge:
        ttk.Label(droite, text=badge, style="EntetePuce.TLabel").pack(anchor="e")

    def _lien(texte, action):
        lab = ttk.Label(droite, text=texte, style="EnteteLien.TLabel", cursor="hand2")
        lab.pack(anchor="e", pady=(8, 0))
        lab.bind("<Button-1>", lambda _e: action())
        lab.bind("<Enter>", lambda _e: lab.configure(foreground=PALETTE["texte"]))
        lab.bind("<Leave>", lambda _e: lab.configure(foreground=PALETTE["cyan_fonce"]))
        return lab

    # Bascule clair/sombre : toujours présente. Le libellé annonce la cible.
    _lien("☀  Mode clair" if _MODE[0] == "dark" else "☾  Mode sombre",
          lambda: basculer(parent))
    if on_contact is not None:
        _lien("✉  Contact", on_contact)

    cadre.menu_aide = None
    if slug:
        menu = tk.Menu(parent, tearoff=0)
        menu.add_command(label="Guide, notes de version et téléchargement",
                         command=lambda: webbrowser.open(url_aide("fiche", slug)))
        menu.add_command(label="Glossaire — les mots techniques expliqués",
                         command=lambda: webbrowser.open(url_aide("glossaire")))
        menu.add_command(label="Communauté — chat, idées, entraide",
                         command=lambda: webbrowser.open(url_aide("communaute")))
        if on_contact is not None:
            menu.add_separator()
            menu.add_command(label="Signaler un problème…", command=on_contact)
        menu.add_separator()
        # Une ligne d'information, pas une action : la version, et la licence —
        # « libre » est une promesse que l'utilisateur doit pouvoir lire ici.
        menu.add_command(label=f"{nom_appli} v{version or '?'} — logiciel libre, licence MIT", state="disabled")

        def derouler(lab):
            try:
                menu.tk_popup(lab.winfo_rootx(), lab.winfo_rooty() + lab.winfo_height())
            finally:
                menu.grab_release()

        lien_aide = _lien("?  Aide", lambda: None)
        lien_aide.bind("<Button-1>", lambda _e: derouler(lien_aide))
        cadre.menu_aide = menu

    # LA signature de la marque, dans les deux thèmes : deux pixels de cyan
    # bord à bord. Un Frame vide de hauteur 2 — aucun enfant, donc aucune
    # propagation de géométrie à désactiver.
    ttk.Frame(cadre, style="EnteteTrait.TFrame", height=2).pack(fill="x", side="bottom")
    return cadre


def basculer(parent: tk.Misc) -> str:
    """Inverse clair/sombre : enregistre la préférence, puis propose un
    redémarrage pour l'appliquer proprement.

    Pourquoi un redémarrage plutôt qu'un basculement à chaud : les couleurs
    passées à la CRÉATION d'un widget (via couleur()) sont figées ; un simple
    re-apply() laisserait du texte « clair » illisible sur fond sombre. Recréer
    l'UI au lancement suivant garantit un rendu correct partout.
    """
    nouveau = "light" if _MODE[0] == "dark" else "dark"
    definir_mode(nouveau)
    from tkinter import messagebox
    libelle = "sombre" if nouveau == "dark" else "clair"
    try:
        relancer = messagebox.askyesno(
            "Thème",
            f"Thème {libelle} enregistré.\nRedémarrer l'application maintenant pour l'appliquer ?",
            parent=parent)
    except tk.TclError:
        relancer = False
    if relancer:
        _relancer(parent)
    return nouveau


def _relancer(parent: tk.Misc) -> None:
    """Relance le processus courant (source ou exécutable gelé)."""
    try:
        parent.winfo_toplevel().destroy()
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
    else:
        os.execv(sys.executable, [sys.executable, *sys.argv])


def carte(parent: tk.Misc, titre: str = "", *, doux: bool = False, **kw) -> ttk.Frame:
    """Conteneur de section : surface blanche arrondie (si les images ont
    monté), sinon un cadre plat bordé. Si `titre`, l'ajoute en tête."""
    style_cadre = "CarteDouce.TFrame" if doux else "Card.TFrame"
    try:
        cadre = ttk.Frame(parent, style=style_cadre, **kw)
    except tk.TclError:
        cadre = ttk.Frame(parent, **kw)
    if titre:
        ttk.Label(cadre, text=titre, style="CarteTitre.TLabel").pack(anchor="w", pady=(0, 10))
    return cadre

class Rail(ttk.Frame):
    """Navigation VERTICALE, en remplacement de ttk.Notebook.

    POURQUOI — PdfAtelier a onze onglets horizontaux. A la largeur par defaut
    de sa fenetre ils se serrent jusqu'a ce que huit libelles sur onze soient
    tronques : l'application la plus riche de la suite devient la plus
    illisible. Une liste verticale en tient onze sans effort, se parcourt au
    clavier, et donne aux applications la meme silhouette.

    L'API est celle de ttk.Notebook — `add`, `select`, `tabs`, `tab`, `index`,
    et l'evenement <<NotebookTabChanged>> — pour que le remplacement tienne en
    UNE ligne dans chaque application et que les tests qui pilotent la
    navigation continuent de fonctionner tels quels.

    PIEGE DE PARENTE TK : les pages sont creees comme enfants du Rail
    (`ttk.Frame(rail)`, exactement comme avec un Notebook), mais elles sont
    affichees dans `_contenu`, qui est leur FRERE. Tk l'autorise — le
    gestionnaire de geometrie peut etre un descendant du parent du widget —
    et c'est ce qui permet de garder la meme signature d'appel.
    """

    LARGEUR = 170          # mesure sur le plus long libelle reel de la suite

    def __init__(self, parent, *, largeur: int = LARGEUR, **kw):
        super().__init__(parent, style="RailHote.TFrame", takefocus=True, **kw)
        self._barre = ttk.Frame(self, style="Rail.TFrame", width=largeur)
        self._barre.pack(side="left", fill="y")
        self._barre.pack_propagate(False)      # sinon la barre se retrecit sur son contenu
        ttk.Frame(self, style="RailTrait.TFrame", width=1).pack(side="left", fill="y")
        self._contenu = ttk.Frame(self, style="RailContenu.TFrame")
        self._contenu.pack(side="left", fill="both", expand=True)
        # TOUTES les pages sont empilees dans LA MEME cellule de grille, comme
        # le fait ttk.Notebook : la cellule reclame donc la taille de la plus
        # grande, et la fenetre se calibre sur la vue la plus encombrante et
        # non sur celle qui se trouve ouverte. Changer de vue n'est alors
        # qu'un tkraise(), sans aucun rappel differe — un `after_idle` posait
        # ici un « can't delete Tcl command » a la fermeture, une erreur qui
        # ne dit rien de sa cause.
        self._contenu.grid_rowconfigure(0, weight=1)
        self._contenu.grid_columnconfigure(0, weight=1)
        self._pages: list = []                 # [{page, texte, entree, barre, label}]
        self._courant = None
        self.bind("<Up>", lambda _e: self._voisin(-1))
        self.bind("<Down>", lambda _e: self._voisin(+1))
        self.bind("<Home>", lambda _e: self.select(0))
        self.bind("<End>", lambda _e: self.select(len(self._pages) - 1))

    # -- construction ------------------------------------------------------

    def add(self, page, text: str = "", groupe: str = "") -> None:
        """Ajoute une page. `groupe`, s'il est fourni, insere un intertitre
        AVANT elle — c'est la seule addition a l'API du Notebook, et elle est
        facultative : sans elle, le rail est une simple liste."""
        if groupe:
            ttk.Label(self._barre, text=groupe.upper(), style="RailGroupe.TLabel").pack(fill="x", anchor="w")
        entree = ttk.Frame(self._barre, style="Rail.TFrame", cursor="hand2")
        entree.pack(fill="x")
        barre = ttk.Frame(entree, style="RailBarre.TFrame", width=3)
        barre.pack(side="left", fill="y")
        label = ttk.Label(entree, text=text, style="RailItem.TLabel", cursor="hand2", anchor="w")
        label.pack(side="left", fill="x", expand=True)

        page.grid(in_=self._contenu, row=0, column=0, sticky="nsew")
        fiche = {"page": page, "texte": text, "entree": entree, "barre": barre, "label": label}
        self._pages.append(fiche)

        for w in (entree, label):
            w.bind("<Button-1>", lambda _e, p=page: self.select(p))
            w.bind("<Enter>", lambda _e, f=fiche: self._survol(f, True))
            w.bind("<Leave>", lambda _e, f=fiche: self._survol(f, False))
        if self._courant is None:
            self.select(page)

    # -- API compatible Notebook -------------------------------------------

    def tabs(self) -> tuple:
        """Les chemins Tk des pages, dans l'ordre — comme Notebook.tabs()."""
        return tuple(str(f["page"]) for f in self._pages)

    def tab(self, cible, option=None):
        """tab(cible, "text") rend le libelle ; sans option, le dictionnaire."""
        fiche = self._pages[self._index_de(cible)]
        infos = {"text": fiche["texte"], "state": "normal"}
        if option is None:
            return infos
        return infos[option.lstrip("-")]

    def index(self, cible) -> int:
        return self._index_de(cible)

    def select(self, cible=None):
        """Sans argument : le chemin Tk de la page affichee (comme Notebook).
        Avec : affiche cette page et emet <<NotebookTabChanged>>."""
        if cible is None:
            return str(self._courant) if self._courant is not None else ""
        fiche = self._pages[self._index_de(cible)]
        if self._courant is fiche["page"]:
            return None
        for f in self._pages:
            actif = f is fiche
            f["label"].configure(style="RailItemActif.TLabel" if actif else "RailItem.TLabel")
            f["barre"].configure(style="RailBarreActive.TFrame" if actif else "RailBarre.TFrame")
        fiche["page"].tkraise()
        self._courant = fiche["page"]
        self.event_generate("<<NotebookTabChanged>>")
        return None

    # -- interne -----------------------------------------------------------

    def _index_de(self, cible) -> int:
        """Accepte un index, un widget, ou un chemin Tk — les trois formes que
        ttk.Notebook accepte, parce que les appelants utilisent les trois."""
        if isinstance(cible, int):
            return cible
        chemin = str(cible)
        for i, f in enumerate(self._pages):
            if str(f["page"]) == chemin:
                return i
        raise ValueError(f"page inconnue du rail : {cible!r}")

    def _survol(self, fiche, dedans: bool) -> None:
        if fiche["page"] is self._courant:
            return                              # l'actif ne bouge pas au survol
        fiche["label"].configure(style="RailItemSurvol.TLabel" if dedans else "RailItem.TLabel")

    def _voisin(self, pas: int) -> str:
        """Fleches haut/bas : navigation au clavier, que les onglets
        horizontaux ne donnaient pas (Tab les traversait un par un)."""
        if not self._pages:
            return "break"
        i = self._index_de(self._courant) if self._courant is not None else 0
        self.select(max(0, min(len(self._pages) - 1, i + pas)))
        return "break"


def etat_vide(parent: tk.Misc, titre: str, phrase: str, *,
              action=None, libelle: str = "") -> ttk.Frame:
    """Ce qu'on montre quand il n'y a encore rien.

    Aucune des sept applications ne traitait ce cas : une liste vide etait
    simplement... vide. C'est pourtant le PREMIER ecran qu'un nouvel
    utilisateur voit, et celui ou il decide si l'outil lui parle.

    Un titre, UNE phrase, UNE action — pas davantage. Pas d'illustration
    non plus : Tkinter la rendrait mal, et elle n'apprendrait rien.

    `action` (callable) ajoute un bouton principal portant `libelle`. Sans
    action, l'etat vide se contente de dire ce qui manque.
    """
    cadre = ttk.Frame(parent, style="EtatVide.TFrame", padding=30)
    bloc = ttk.Frame(cadre, style="EtatVide.TFrame")
    bloc.place(relx=0.5, rely=0.5, anchor="center")
    ttk.Label(bloc, text=titre, style="EtatVideTitre.TLabel",
              anchor="center", justify="center").pack()
    # wraplength en pixels : sans lui, une phrase un peu longue etire le
    # cadre au lieu de revenir a la ligne, et l'etat vide devient une barre.
    ttk.Label(bloc, text=phrase, style="EtatVideTexte.TLabel", anchor="center",
              justify="center", wraplength=380).pack(pady=(6, 0))
    if action is not None:
        ttk.Button(bloc, text=libelle or "Commencer", style="Accent.TButton",
                   command=action).pack(pady=(16, 0))
    return cadre


class Erreur(ttk.Frame):
    """Une erreur de saisie, EN LIGNE, sous le champ fautif.

    Les sept applications signalent leurs erreurs de saisie par une
    messagebox : une fenetre modale que Windows dessine, qu'aucun theme
    n'atteint, qui masque le champ dont elle parle et qu'il faut fermer
    avant de pouvoir corriger. Pour « le taux horaire doit etre un nombre »,
    c'est disproportionne.

    Ici : le message reste a cote du champ, dit ce qui ne va pas ET comment
    le corriger, marque le champ, et ne bloque rien. Il disparait des que
    l'utilisateur retouche le champ — corriger fait partie de la correction.

    S'utilise en trois temps :
        self.err = opl_theme.Erreur(parent);  self.err.pack(fill="x")
        ...
        self.err.montrer("Montant", "« 12,4,50 » n'est pas un nombre.", champ=entree)
        self.err.effacer()
    """

    def __init__(self, parent: tk.Misc, *, apres: tk.Misc = None, **kw):
        """`apres` : le widget SOUS lequel l'erreur doit apparaitre —
        typiquement le formulaire. Sans lui, pack() la placerait en fin
        d'ordre d'empilement, donc tout en bas de la vue, a des centaines de
        pixels du champ dont elle parle : toute sa raison d'etre. Constate a
        l'ecran, pas a la relecture."""
        super().__init__(parent, style="Erreur.TFrame", **kw)
        self._apres = apres
        self._barre = ttk.Frame(self, style="ErreurBarre.TFrame", width=3)
        self._barre.pack(side="left", fill="y")
        self._corps = ttk.Frame(self, style="Erreur.TFrame", padding=(9, 7))
        self._corps.pack(side="left", fill="x", expand=True)
        self._quoi = ttk.Label(self._corps, style="ErreurTitre.TLabel")
        self._quoi.pack(side="left", padx=(0, 6))
        self._texte = ttk.Label(self._corps, style="ErreurTexte.TLabel",
                                wraplength=520, justify="left")
        self._texte.pack(side="left", fill="x", expand=True)
        self._champ = None
        self._style_origine = ""
        self._visible = False

    @property
    def visible(self) -> bool:
        return self._visible

    def montrer(self, quoi: str, texte: str, *, champ: tk.Misc = None) -> None:
        """Affiche l'erreur. `quoi` nomme le champ (« Montant »), `texte` dit
        ce qui ne va pas et comment le reparer. `champ`, s'il est fourni, est
        marque, place au focus, et efface l'erreur des qu'il est modifie."""
        self._quoi.configure(text=f"{quoi} :" if quoi else "")
        self._texte.configure(text=texte)
        self._marquer(champ)
        if not self._visible:
            place = {"after": self._apres} if self._apres is not None else {}
            self.pack(fill="x", padx=10, pady=(6, 0), **place)
            self._visible = True

    def effacer(self) -> None:
        """Retire l'erreur et rend au champ son apparence normale."""
        self._demarquer()
        if self._visible:
            self.pack_forget()
            self._visible = False

    # -- interne -----------------------------------------------------------

    def _marquer(self, champ) -> None:
        self._demarquer()
        if champ is None:
            return
        self._champ = champ
        try:
            self._style_origine = champ.cget("style") or "TEntry"
            champ.configure(style="Erreur.TEntry")
            champ.focus_set()
            # <KeyRelease> et non <Key> : sur <Key>, l'erreur disparait AVANT
            # que le caractere ne soit insere, donc avant toute correction
            # reelle — elle clignotait a chaque frappe.
            self._lien = champ.bind("<KeyRelease>", lambda _e: self.effacer(), "+")
        except tk.TclError:
            self._champ = None          # widget sans style (Text, Listbox...)

    def _demarquer(self) -> None:
        if self._champ is None:
            return
        try:
            self._champ.configure(style=self._style_origine or "TEntry")
            self._champ.unbind("<KeyRelease>", self._lien)
        except tk.TclError:
            pass
        self._champ = None
