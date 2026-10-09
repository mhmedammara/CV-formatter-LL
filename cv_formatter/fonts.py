"""Polices Lexend : téléchargement, mesure, installation et intégration dans les PPTX.

Le modèle Google Slides n'embarque que des sous-ensembles de Lexend : sans la police
installée, PowerPoint remplacerait les glyphes manquants. On installe donc la famille
complète (licence SIL OFL) pour l'utilisateur courant, sans droits administrateur :
registre Windows, ou fontconfig sous Linux (l'image Docker l'installe pour tout le système).

Le PPTX livré embarque les polices complètes, pour s'afficher correctement sur un poste sans
Lexend : sous Windows, PowerPoint les intègre en ré-enregistrant le fichier (export_pdf.py) ;
ailleurs, embed_in_pptx les intègre au format EOT, comme PowerPoint.
"""

from __future__ import annotations

import ctypes
import io
import shutil
import struct
import subprocess
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

# Polices intégrées au PPTX : (style interne, emplacement), rangées comme PowerPoint les range lui-même
# (Lexend SemiBold, de graisse 600, dans l'emplacement « gras » de sa famille).
EMBEDDED_SLOTS = (("regular", "regular"), ("bold", "bold"), ("medium", "regular"), ("semibold", "bold"))


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

    if sys.platform == "win32":
        return Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Fonts"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "fonts" / "lexend"


def _fontconfig_files() -> set[str]:
    """Noms des fichiers de police connus de fontconfig (Linux)."""
    try:
        listed = subprocess.run(["fc-list", "--format", "%{file}\n"], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {Path(line.strip()).name for line in listed.splitlines() if line.strip()}


def fonts_installed() -> bool:
    if sys.platform != "win32":
        if sys.platform == "darwin":
            return True
        known = _fontconfig_files()
        return all(filename in known for filename, _, _ in FONTS.values())
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
    if sys.platform == "darwin" or fonts_installed():
        return False
    target_dir = _user_fonts_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":  # Linux : dossier de polices de l'utilisateur, puis cache fontconfig
        for filename, _, _ in FONTS.values():
            shutil.copy2(FONTS_DIR / filename, target_dir / filename)
        subprocess.run(["fc-cache", "-f", str(target_dir)], capture_output=True, timeout=120, check=True)
        return True
    import winreg

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


# --- Intégration dans le PPTX (sans PowerPoint) ------------------------------------------------------------


def eot(ttf: bytes) -> bytes:
    """Police TrueType au format Embedded OpenType (version 0x00020001, non compressée), celui des fichiers
    ppt/fonts/*.fntdata. PowerPoint écrit des EOT compressés (MicroType Express) mais lit aussi cette forme :
    vérifié avec une police absente du poste, que PowerPoint affiche bien depuis le PPTX."""
    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(ttf))
    os2, head, names = font["OS/2"], font["head"], font["name"]

    def name(name_id: int) -> str:
        return names.getDebugName(name_id) or ""

    def string(text: str, nul: bool = False) -> bytes:
        data = (text + ("\x00" if nul else "")).encode("utf-16-le")
        return struct.pack("<H", len(data)) + data

    panose = os2.panose
    header = bytes(
        [panose.bFamilyType, panose.bSerifStyle, panose.bWeight, panose.bProportion, panose.bContrast,
         panose.bStrokeVariation, panose.bArmStyle, panose.bLetterForm, panose.bMidline, panose.bXHeight]
    )
    header += struct.pack("<BBIHH", 0, os2.fsSelection & 1, os2.usWeightClass, os2.fsType, 0x504C)
    header += struct.pack("<IIII", os2.ulUnicodeRange1, os2.ulUnicodeRange2, os2.ulUnicodeRange3, os2.ulUnicodeRange4)
    header += struct.pack("<II", os2.ulCodePageRange1, os2.ulCodePageRange2)
    header += struct.pack("<I", head.checkSumAdjustment) + bytes(16)
    # Noms tels que PowerPoint les écrit : famille et style terminés par un caractère nul.
    header += bytes(2) + string(name(1), True) + bytes(2) + string(name(2), True)
    header += bytes(2) + string(name(5)) + bytes(2) + string(name(4)) + bytes(2) + string("")
    return struct.pack("<IIII", 16 + len(header) + len(ttf), len(ttf), 0x00020001, 0) + header + ttf


def embed_in_pptx(path: Path, files: dict[str, Path] | None = None) -> None:
    """Intègre les polices Lexend complètes dans le PPTX (remplace d'éventuelles polices déjà intégrées).
    `files` (style interne -> fichier) sert aux tests ; par défaut, les polices de assets/fonts."""
    from lxml import etree
    from pptx import Presentation
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT
    from pptx.opc.package import Part
    from pptx.opc.packuri import PackURI

    p_ns = "http://schemas.openxmlformats.org/presentationml/2006/main"
    r_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    prs = Presentation(str(path))
    pres = prs.element
    old = pres.find(f"{{{p_ns}}}embeddedFontLst")
    if old is not None:
        for rel_id in {rel for el in old.iter() if (rel := el.get(f"{{{r_ns}}}id"))}:
            prs.part.drop_rel(rel_id)
        pres.remove(old)
    font_list = etree.Element(f"{{{p_ns}}}embeddedFontLst")
    entries: dict[str, etree._Element] = {}  # pyright: ignore[reportPrivateUsage]
    for number, (style, slot) in enumerate(EMBEDDED_SLOTS, start=1):
        source = (files or {}).get(style) or font_file(style)
        part = Part(PackURI(f"/ppt/fonts/font{number}.fntdata"), "application/x-fontdata", prs.part.package, eot(source.read_bytes()))
        rel_id = prs.part.relate_to(part, RT.FONT)
        family = typeface(style) if files is None else _family(source)
        entry = entries.get(family)
        if entry is None:
            entry = entries[family] = etree.SubElement(font_list, f"{{{p_ns}}}embeddedFont")
            etree.SubElement(entry, f"{{{p_ns}}}font", typeface=family, pitchFamily="2", charset="0")
        etree.SubElement(entry, f"{{{p_ns}}}{slot}", {f"{{{r_ns}}}id": rel_id})
    # Ordre du schéma : … sldSz, notesSz, smartTags, embeddedFontLst, custShowLst, … defaultTextStyle …
    anchor = pres.find(f"{{{p_ns}}}smartTags")
    if anchor is None:
        anchor = pres.find(f"{{{p_ns}}}notesSz")
    if anchor is None:
        pres.insert(0, font_list)
    else:
        anchor.addnext(font_list)
    pres.set("embedTrueTypeFonts", "1")
    pres.set("saveSubsetFonts", "0")
    prs.save(str(path))


def _family(source: Path) -> str:
    from fontTools.ttLib import TTFont

    return TTFont(str(source))["name"].getDebugName(1) or ""
