"""Polices Lexend : téléchargement, mesure et installation utilisateur sous Windows.

Le modèle Google Slides n'embarque que des sous-ensembles de Lexend : sans la police
installée, PowerPoint remplacerait les glyphes manquants. On installe donc la famille
complète (licence SIL OFL) pour l'utilisateur courant, sans droits administrateur.
"""

from __future__ import annotations

import ctypes
import shutil
import sys
import urllib.request
from pathlib import Path

from .config import FONTS_DIR

BASE_URL = "https://raw.githubusercontent.com/googlefonts/lexend/main/fonts/lexend/ttf/"

# style interne -> (fichier, nom de famille vu par PowerPoint, nom de valeur de registre)
FONTS = {
    "regular": ("Lexend-Regular.ttf", "Lexend", "Lexend (TrueType)"),
    "bold": ("Lexend-Bold.ttf", "Lexend", "Lexend Bold (TrueType)"),
    "medium": ("Lexend-Medium.ttf", "Lexend Medium", "Lexend Medium (TrueType)"),
    "semibold": ("Lexend-SemiBold.ttf", "Lexend SemiBold", "Lexend SemiBold (TrueType)"),
}

REGISTRY_KEY = r"Software\Microsoft\Windows NT\CurrentVersion\Fonts"


def font_file(style: str) -> Path:
    return FONTS_DIR / FONTS[style][0]


def typeface(style: str) -> str:
    return FONTS[style][1]


def ensure_font_files() -> None:
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    for filename, _, _ in FONTS.values():
        target = FONTS_DIR / filename
        if target.exists() and target.stat().st_size > 10_000:
            continue
        with urllib.request.urlopen(BASE_URL + filename, timeout=60) as response:
            target.write_bytes(response.read())


def _user_fonts_dir() -> Path:
    import os

    return Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Fonts"


def fonts_installed() -> bool:
    if sys.platform != "win32":
        return True
    import winreg

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, REGISTRY_KEY) as key:
                found = 0
                for _, _, value_name in FONTS.values():
                    try:
                        value, _ = winreg.QueryValueEx(key, value_name)
                    except FileNotFoundError:
                        continue
                    path = Path(value)
                    if not path.is_absolute():
                        path = Path(r"C:\Windows\Fonts") / path
                    if path.exists():
                        found += 1
                if found == len(FONTS):
                    return True
        except OSError:
            continue
    return False


def install_fonts() -> bool:
    """Installe Lexend pour l'utilisateur courant. Renvoie True si une installation a eu lieu."""
    ensure_font_files()
    if sys.platform != "win32" or fonts_installed():
        return False
    import winreg

    target_dir = _user_fonts_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    gdi32 = ctypes.windll.gdi32
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
        for filename, _, value_name in FONTS.values():
            target = target_dir / filename
            if not target.exists():
                shutil.copy2(FONTS_DIR / filename, target)
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, str(target))
            gdi32.AddFontResourceW(str(target))
    # Prévient les applications ouvertes qu'une police a été ajoutée.
    HWND_BROADCAST, WM_FONTCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x001D, 0x0002
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST, WM_FONTCHANGE, 0, 0, SMTO_ABORTIFHUNG, 1000, None
    )
    return True
