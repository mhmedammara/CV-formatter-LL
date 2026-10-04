"""Chemins, paramètres par défaut et constantes de mise en page."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
ASSETS_DIR = PACKAGE_DIR / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
TEMPLATE_PATH = ASSETS_DIR / "modele_cv_logiclever.pptx"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "sortie"
DATA_DIR = DEFAULT_OUTPUT_DIR / "_donnees"  # extractions mises en cache, partagées par toutes les sorties

load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-6-luna")
DEFAULT_EFFORT = os.environ.get("OPENAI_EFFORT", "high")
EFFORT_CHOICES = ("none", "low", "medium", "high", "xhigh", "max")

# Couleurs du thème Logiclever (theme1.xml du modèle).
ORANGE = "FF5D24"
BLUE = "1F2ADE"
BLACK = "000000"
WHITE = "FFFFFF"
GREY = "595959"
DARK_GREY = "404040"

EMU_PER_CM = 360000
EMU_PER_PT = 12700


def cm(value: float) -> int:
    return int(round(value * EMU_PER_CM))


def emu_to_cm(value: int) -> float:
    return value / EMU_PER_CM


# Géométrie de la page (cm) — diapositive 21 x 29 cm.
PAGE_WIDTH = 21.0
PAGE_HEIGHT = 29.0
CONTENT_BOTTOM = 28.55          # limite basse des zones de texte
TEXT_INSET = 0.254              # marges internes PowerPoint (91 425 EMU)

LEFT_X = 0.96                   # colonne Expériences
LEFT_WIDTH = 12.44
LEFT_TOP_PAGE1 = 7.98            # aligné sur le premier bloc de la colonne droite

RIGHT_X = 13.81                 # colonne Compétences / Certifications / Formation
RIGHT_WIDTH = 6.47
RIGHT_HEADER_X = 14.11

SECTION_HEADER_HEIGHT = 1.15    # groupe icône + titre de section
CONTINUATION_HEADER_Y = 1.75    # en-tête « Expériences (suite) » sur les pages suivantes
FULL_WIDTH = PAGE_WIDTH - 2 * LEFT_X

NO_PHOTO_X = 1.36               # alignement du nom quand il n'y a pas de photo
PHOTO_X = 1.21                  # photo alignée sur la marge du texte (et non collée au bord)
PHOTO_SIZE = 3.9
PHOTO_BOTTOM = 6.25             # bas de la photo aligné sur le bas de la pastille
PHOTO_GAP = 0.55                # espace entre la photo et le nom

# Calibrage des hauteurs de ligne mesuré sur le rendu PowerPoint (voir layout.py).
LINE_HEIGHT_FACTOR = 1.2
SAFETY_MARGIN = 0.01
