"""Chemins, paramètres par défaut et constantes de mise en page."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, get_args

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
ASSETS_DIR = PACKAGE_DIR / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
TEMPLATE_PATH = ASSETS_DIR / "modele_cv_logiclever.pptx"
INPUT_DIR = PROJECT_ROOT / "Input"  # dossier où déposer les CV à traiter (CV ou .zip)
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "sortie"
DATA_DIR = DEFAULT_OUTPUT_DIR / "_donnees"  # extractions mises en cache, partagées par toutes les sorties

load_dotenv(PROJECT_ROOT / ".env")

Effort = Literal["none", "low", "medium", "high", "xhigh", "max"]  # effort de raisonnement accepté par l'API
EFFORT_CHOICES: tuple[Effort, ...] = get_args(Effort)


def parse_effort(value: str) -> Effort:
    """Effort de raisonnement lu dans .env ou sur la ligne de commande ; « high » si la valeur est inconnue."""
    for choice in EFFORT_CHOICES:
        if choice == value:
            return choice
    return "high"


DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-6-luna")
DEFAULT_EFFORT: Effort = parse_effort(os.environ.get("OPENAI_EFFORT", "high"))

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
LEFT_TOP_PAGE1 = 7.98            # titre Expériences du modèle (6,93) + HEADER_TO_CONTENT, comme la colonne droite

RIGHT_X = 13.81                 # colonne Compétences / Certifications / Formation
RIGHT_WIDTH = 6.47

# Titres de section (groupe icône + titre) : l'icône est calée sur la marge du texte de la colonne et
# centrée sur la hauteur de capitale du titre ; le titre commence à HEADER_TEXT_OFFSET du bord de l'icône,
# comme le texte des coordonnées (même alignement dans les deux colonnes).
HEADER_TEXT_OFFSET = 1.3
HEADER_TO_CONTENT = 1.05        # du haut du titre de section au haut de la zone de texte qui le suit
SECTION_GAP = 0.3               # du bas d'une section au titre suivant : plus d'air au-dessus d'un titre qu'en dessous
CONTINUATION_HEADER_Y = 1.75    # en-tête « Expériences (suite) » sur les pages suivantes
FULL_WIDTH = PAGE_WIDTH - 2 * LEFT_X

NO_PHOTO_X = LEFT_X + TEXT_INSET  # sans photo, le nom s'aligne sur la marge du texte de la colonne gauche
PHOTO_X = 1.21                  # photo alignée sur la marge du texte (et non collée au bord)
PHOTO_SIZE = 3.9
PHOTO_BOTTOM = 6.25             # bas de la photo aligné sur le bas de la pastille
PHOTO_GAP = 0.55                # espace entre la photo et le nom

# Calibrage des hauteurs de ligne mesuré sur le rendu PowerPoint (voir layout.py) : interligne simple de
# 1,2 em, ligne de base à 0,96 em sous le haut de la ligne ; capitales de Lexend : 0,70 em.
LINE_HEIGHT_FACTOR = 1.2
BASELINE_FACTOR = 0.96
CAP_HEIGHT = 0.70
# Hauteur : le modèle reproduit PowerPoint au centième de millimètre près (75 zones de 14 CV contrôlées) ;
# la marge ne sert plus qu'aux imprévus (glyphe absent de Lexend rendu dans une autre police, etc.).
SAFETY_MARGIN = 0.005
# Largeur : PowerPoint arrondit tailles et avances au 1/600 de pouce ; mesuré sur 772 lignes, le texte rendu
# est jusqu'à 0,75 % plus large que calculé (8 pt rendu à 8,04 pt). Sans marge, une ligne pleine passait à
# la ligne suivante et le bloc débordait sur le titre de section suivant.
WIDTH_SAFETY_MARGIN = 0.01

# Position de la ligne de base dans une ligne PowerPoint (en em, depuis le haut de la ligne), selon
# l'interligne du paragraphe. Mesuré sur une diapositive de calibrage (6,5 à 14 pt, 4 graisses), puis validé
# sur les PDF PowerPoint des 14 CV de test : 1 023 lignes retrouvées à 0,044 mm près. Sert au rendu PDF par
# LibreOffice (libreoffice.py), qui doit reproduire ces positions.
POWERPOINT_BASELINE = {0.9: 0.8404, 1.0: 0.9648, 1.05: 0.9518, 1.1: 0.9964, 1.15: 1.0407}
