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
from pathlib import Path
from tkinter import ttk

__all__ = ["apply", "entete", "carte", "PALETTE", "couleur", "police"]

# --- Palette (deux modes) -------------------------------------------------
# Les accents de marque (cyan/émeraude/encre) sont communs aux deux modes : ils
# ressortent aussi bien sur clair que sur sombre. Seules changent les surfaces
# et le texte. `PALETTE` est le dictionnaire ACTIF ; apply() le bascule selon le
# mode AVANT que l'appli ne construise ses widgets — c'est pourquoi couleur(),
# lu à la création de chaque widget, rend la bonne teinte dans les deux modes.
_LIGHT = {
    "encre": "#0A0F1A",
    "ardoise": "#131C2E",
    "ardoise_clair": "#1E2B44",
    "cyan": "#22D3EE",
    "cyan_fonce": "#0EA5C4",
    "emeraude": "#34D399",
    "emeraude_fonce": "#10B981",
    "vert_lab": "#3DDC97",
    "gris_texte": "#8A9EB8",
    "fond": "#F5F7FA",
    "surface": "#FFFFFF",
    "surface_2": "#EEF3FA",
    "bordure": "#D6DEE8",
    "bordure_forte": "#B9C6D8",
    "texte": "#16202E",
    "texte_doux": "#5A6B80",
    "survol": "#E8F0FC",
    "danger": "#DC2626",
    "danger_bg": "#FEE2E2",
    "lien": "#0EA5C4",
    "succes": "#10B981",
    "avertissement": "#B45309",
    "avertissement_bg": "#FEF3C7",
    "surbrillance": "#FEF9C3",
}
_DARK = {
    "encre": "#0A0F1A",           # reste le texte SUR les boutons cyan (lisible)
    "ardoise": "#0C1422",         # bandeau, un cran sous le fond
    "ardoise_clair": "#1B2740",
    "cyan": "#22D3EE",
    "cyan_fonce": "#38BEE0",
    "emeraude": "#34D399",
    "emeraude_fonce": "#34D399",
    "vert_lab": "#3DDC97",
    "gris_texte": "#8A9EB8",
    "fond": "#0F1626",            # fond général sombre
    "surface": "#16203A",         # champs, listes, cartes
    "surface_2": "#1B2740",
    "bordure": "#2A3A57",
    "bordure_forte": "#3A4E70",
    "texte": "#EAF2FF",
    "texte_doux": "#9DB0C9",
    "survol": "#22304C",
    "danger": "#F87171",
    "danger_bg": "#3A1D1D",
    "lien": "#38BEE0",
    "succes": "#34D399",
    "avertissement": "#FBBF24",
    "avertissement_bg": "#3A2E12",
    "surbrillance": "#3A340F",
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
    titre = _police_dispo(root, "Bahnschrift SemiBold", "Bahnschrift", "Segoe UI Semibold", "Segoe UI", "TkDefaultFont")
    titre_reg = _police_dispo(root, "Bahnschrift", "Segoe UI Semibold", "Segoe UI", "TkDefaultFont")
    corps = _police_dispo(root, "Segoe UI", "Inter", "DejaVu Sans", "TkDefaultFont")
    mono = _police_dispo(root, "Cascadia Mono", "Consolas", "DejaVu Sans Mono", "TkFixedFont")
    _POLICES.update({
        "display": (titre, 22), "titre": (titre, 15), "soustitre": (corps, 11),
        "corps": (corps, 10), "corps_gras": (titre_reg, 10), "petit": (corps, 9),
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
    style.configure("TButton", background=p["surface"], foreground=p["texte"],
                    bordercolor=p["bordure"], focuscolor=p["cyan"], padding=(9, 5), relief="flat")
    style.map("TButton",
              background=[("pressed", p["bordure"]), ("active", p["survol"]), ("disabled", p["fond"])],
              foreground=[("disabled", p["texte_doux"])],
              bordercolor=[("focus", p["cyan"]), ("active", p["cyan"])])
    style.configure("Accent.TButton", background=p["cyan"], foreground=p["encre"],
                    bordercolor=p["cyan"], padding=(11, 6), relief="flat", font=(titre, 10))
    style.map("Accent.TButton",
              background=[("pressed", p["emeraude_fonce"]), ("active", p["emeraude"]), ("disabled", p["bordure"])],
              foreground=[("disabled", p["texte_doux"])])
    style.configure("Danger.TButton", background=p["surface"], foreground=p["danger"],
                    bordercolor=p["bordure"], padding=(9, 5), relief="flat")
    style.map("Danger.TButton", background=[("pressed", p["danger_bg"]), ("active", p["danger_bg"])])

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

    # En-tête de marque : bandeau ardoise, texte clair.
    style.configure("Entete.TFrame", background=p["ardoise"])
    style.configure("EnteteTitre.TLabel", background=p["ardoise"], foreground="#EAF2FF", font=(titre, 15))
    style.configure("EnteteSousTitre.TLabel", background=p["ardoise"], foreground=p["gris_texte"], font=(mono, 9))
    style.configure("EntetePuce.TLabel", background=p["ardoise_clair"], foreground=p["cyan"], font=(mono, 8), padding=(8, 3))
    style.configure("EnteteLien.TLabel", background=p["ardoise"], foreground=p["cyan"], font=(corps, 10))

    # --- montage des images (avec repli par widget) -----------------------
    imgs = _charger_images(root)
    if imgs is not None:
        echecs = _appliquer_images(style, imgs, titre, corps)
        if echecs:
            print("[opl_theme] rendu plat conservé pour : " + ", ".join(echecs), file=sys.stderr)

    _ = nom_appli  # réservé au bandeau entete()
    return style


def entete(parent: tk.Misc, nom_appli: str, accroche: str = "", *,
           badge: str = "OPEN PROJECTS LAB", on_contact=None) -> ttk.Frame:
    """Bandeau de marque à empaqueter en haut de la fenêtre (fill='x').

    `on_contact` : si fourni (callable sans argument), ajoute un lien « Contact »
    cliquable à droite du bandeau — typiquement `lambda: opl_contact.ouvrir(...)`.
    """
    cadre = ttk.Frame(parent, style="Entete.TFrame", padding=(20, 14))
    ligne = ttk.Frame(cadre, style="Entete.TFrame")
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
        lab.bind("<Enter>", lambda _e: lab.configure(foreground="#EAF2FF"))
        lab.bind("<Leave>", lambda _e: lab.configure(foreground=PALETTE["cyan"]))
        return lab

    # Bascule clair/sombre : toujours présente. Le libellé annonce la cible.
    _lien("☀  Mode clair" if _MODE[0] == "dark" else "☾  Mode sombre",
          lambda: basculer(parent))
    if on_contact is not None:
        _lien("✉  Contact", on_contact)
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
