"""Modèle de paragraphes et mesure du texte avec les métriques réelles de Lexend.

PowerPoint ne calcule pas la hauteur du texte pour nous : pour paginer les expériences,
empiler la colonne de droite et réduire les polices, on mesure chaque mot avec la police
TrueType utilisée par le rendu, puis on reproduit la césure ligne par ligne.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

import pymupdf

from . import fonts
from .config import BLACK, LINE_HEIGHT_FACTOR, ORANGE, SAFETY_MARGIN, TEXT_INSET

PT_PER_CM = 72 / 2.54


@dataclass
class Run:
    text: str
    style: str = "regular"  # regular | medium | semibold | bold
    size: float = 10.0  # points
    color: str = BLACK


@dataclass
class Para:
    runs: list[Run]
    bullet: str | None = None
    bullet_color: str = ORANGE
    indent: float = 0.0  # cm : retrait du texte (la puce est dans le retrait)
    space_before: float = 0.0  # points
    space_after: float = 0.0  # points
    line_spacing: float = 1.0  # multiple de l'interligne simple
    align: str = "l"
    keep_with_next: bool = False

    @property
    def text(self) -> str:
        return "".join(run.text for run in self.runs)

    @property
    def max_size(self) -> float:
        return max((run.size for run in self.runs if run.text), default=10.0)


@dataclass
class Block:
    """Groupe de paragraphes paginé ensemble (ex. une expérience)."""

    paras: list[Para] = field(default_factory=list[Para])
    continuation: Para | None = None  # en-tête répété si le bloc est coupé entre deux pages


@lru_cache(maxsize=None)
def _font(style: str) -> pymupdf.Font:
    return pymupdf.Font(fontfile=str(fonts.font_file(style)))


@lru_cache(maxsize=200_000)
def text_width(text: str, style: str, size: float) -> float:
    """Largeur en points."""
    return _font(style).text_length(text, fontsize=size)


def _words(para: Para) -> list[list[tuple[str, Run]]]:
    """Découpe le paragraphe en mots ; un mot peut chevaucher plusieurs runs."""
    words: list[list[tuple[str, Run]]] = [[]]
    for run in para.runs:
        for piece in re.split(r"( )", run.text):
            if piece == "":
                continue
            if piece == " ":
                words.append([])
            else:
                words[-1].append((piece, run))
    return [w for w in words if w]


def wrap(para: Para, width_cm: float) -> list[float]:
    """Renvoie la taille de police dominante de chaque ligne après césure."""
    available = max(1.0, (width_cm - 2 * TEXT_INSET - para.indent) * PT_PER_CM)
    lines: list[float] = []
    line_width = 0.0
    line_size = 0.0
    for word in _words(para):
        word_width = sum(text_width(piece, run.style, run.size) for piece, run in word)
        word_size = max(run.size for _, run in word)
        space = text_width(" ", word[0][1].style, word[0][1].size)
        if line_width == 0.0:
            if word_width <= available:
                line_width, line_size = word_width, word_size
                continue
        elif line_width + space + word_width <= available:
            line_width += space + word_width
            line_size = max(line_size, word_size)
            continue
        else:
            lines.append(line_size)
            line_width, line_size = 0.0, 0.0
            if word_width <= available:
                line_width, line_size = word_width, word_size
                continue
        # Mot plus large que la ligne : PowerPoint le coupe au caractère.
        for piece, run in word:
            for char in piece:
                char_width = text_width(char, run.style, run.size)
                if line_width + char_width > available and line_width > 0:
                    lines.append(line_size)
                    line_width, line_size = 0.0, 0.0
                line_width += char_width
                line_size = max(line_size, run.size)
    if line_width > 0 or not lines:
        lines.append(line_size or para.max_size)
    return lines


def line_count(para: Para, width_cm: float) -> int:
    return len(wrap(para, width_cm))


def para_height(para: Para, width_cm: float, first: bool = False) -> float:
    """Hauteur en cm, interlignes et espacements compris."""
    lines = wrap(para, width_cm)
    height_pt = sum(size * LINE_HEIGHT_FACTOR * para.line_spacing for size in lines)
    if not first:
        height_pt += para.space_before
    height_pt += para.space_after
    return height_pt / PT_PER_CM * (1 + SAFETY_MARGIN)


def paras_height(paras: list[Para], width_cm: float) -> float:
    """Hauteur du contenu d'une zone de texte (sans les marges internes)."""
    return sum(para_height(p, width_cm, first=(i == 0)) for i, p in enumerate(paras))


def box_height(paras: list[Para], width_cm: float) -> float:
    """Hauteur d'une zone de texte, marges internes comprises."""
    return paras_height(paras, width_cm) + 2 * TEXT_INSET


def fits_one_line(text: str, style: str, size: float, width_cm: float) -> bool:
    return text_width(text, style, size) <= (width_cm - 2 * TEXT_INSET) * PT_PER_CM


def scale(paras: list[Para], factor: float) -> list[Para]:
    """Copie des paragraphes avec toutes les tailles multipliées par `factor`."""
    scaled: list[Para] = []
    for para in paras:
        runs = [Run(r.text, r.style, round(r.size * factor * 2) / 2, r.color) for r in para.runs]
        scaled.append(
            Para(
                runs,
                bullet=para.bullet,
                bullet_color=para.bullet_color,
                indent=para.indent * factor,
                space_before=para.space_before * factor,
                space_after=para.space_after * factor,
                line_spacing=para.line_spacing,
                align=para.align,
                keep_with_next=para.keep_with_next,
            )
        )
    return scaled
