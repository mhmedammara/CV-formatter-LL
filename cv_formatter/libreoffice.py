"""Rendu PDF par LibreOffice identique à celui de PowerPoint (serveur Linux, Google Cloud Run).

Sans PowerPoint, le PDF est produit par LibreOffice, qui ne place pas le texte comme PowerPoint :
- largeur : texte rendu jusqu'à 0,8 % plus étroit (crénage, arrondis différents) : une ligne pleine accueille
  parfois un mot de plus, et tout ce qui suit remonte d'une ligne ;
- hauteur : interligne arrondi au 1/100 mm (jusqu'à 0,8 mm d'écart cumulé en bas d'une colonne) et première
  ligne de chaque zone 0,1 à 0,6 mm plus bas.

Le PDF est donc produit à partir d'une copie du PPTX (le PPTX livré, modifiable, n'est pas touché) :
- chaque ligne calculée par le modèle de mise en page (layout.lines, celui qui a dimensionné les zones) devient
  un paragraphe : la césure est exactement celle prévue, quel que soit le moteur ;
- chaque ligne reçoit un interligne exact, calculé pour que sa ligne de base tombe là où PowerPoint la place
  (config.POWERPOINT_BASELINE) ; le décalage est corrigé ligne par ligne, il ne se cumule pas.

Calibrage (tests/test_libreoffice.py, outils/calibrage_libreoffice.py) : lignes de base à 0,05 mm des PDF
produits par PowerPoint sur les 14 CV de test.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree
from pptx import Presentation

from .config import EMU_PER_CM, LINE_HEIGHT_FACTOR, POWERPOINT_BASELINE
from .layout import PT_PER_CM, Line, Para, Run, lines

# Élément XML de lxml : sa classe publique porte un nom « privé » (_Element) dans les annotations de lxml.
Element = etree._Element  # pyright: ignore[reportPrivateUsage]

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"a": A, "p": P}
DEFAULT_INSET = 91440  # EMU : marge interne par défaut d'une zone de texte (0,254 cm)
EMU_PER_100MM = 360  # LibreOffice travaille au 1/100 mm
BULLET_TAGS = {f"{{{A}}}{t}" for t in ("buClrTx", "buClr", "buSzTx", "buSzPct", "buSzPts", "buFontTx", "buFont", "buNone", "buAutoNum", "buChar", "buBlip")}
SPACING_TAGS = {f"{{{A}}}{t}" for t in ("lnSpc", "spcBef", "spcAft")}
FACES = {"Lexend Medium": "medium", "Lexend SemiBold": "semibold"}
# La ligne de base mesurée dans les PDF LibreOffice est 0,48/100 mm au-dessus de la valeur entière calculée.
LIBREOFFICE_BASELINE_SHIFT = 0.5  # 1/100 mm
# Puces : LibreOffice place la puce d'un paragraphe 0,18 mm au-dessus de la ligne de base du texte (PowerPoint :
# sur la ligne de base). Dans la copie calibrée, la puce devient donc un caractère du texte, dont l'avance est
# portée au retrait du paragraphe par un espacement de caractères : la puce reste au bord du paragraphe et le
# texte commence exactement au retrait, comme dans PowerPoint. Avance de « • » dans Arial et dans Liberation Sans
# (métriques identiques) : 716 / 2048 em.
BULLET_ADVANCES = {("•", "Arial"): 716 / 2048}


class Unsupported(ValueError):
    """Zone de texte que le modèle ne sait pas mesurer (police inconnue, saut de ligne manuel…) : laissée telle quelle."""


@dataclass
class _Paragraph:
    element: Element
    para: Para
    run_elements: list[Element]  # a:r d'origine, dans l'ordre, avec leur texte


def q(tag: str) -> str:
    prefix, name = tag.split(":")
    return f"{{{NS[prefix]}}}{name}"


def baseline_factor(line_spacing: float) -> float:
    """Ligne de base PowerPoint (em depuis le haut de la ligne) ; interligne non calibré : valeur la plus proche."""
    nearest = min(POWERPOINT_BASELINE, key=lambda known: abs(known - line_spacing))
    return POWERPOINT_BASELINE[nearest]


# --- Lecture d'une zone de texte -------------------------------------------------------------------------


def _style(rpr: Element | None) -> str:
    latin = rpr.find("a:latin", NS) if rpr is not None else None
    face = latin.get("typeface") if latin is not None else None
    bold = rpr is not None and rpr.get("b") == "1"
    if face == "Lexend":
        return "bold" if bold else "regular"
    if face in FACES and not bold:
        return FACES[face]
    raise Unsupported(f"police non calibrée : {face!r}{' gras' if bold else ''}")


def _points(parent: Element | None, tag: str) -> float:
    """Espacement en points (spcBef / spcAft) ; un espacement en pourcentage n'est pas pris en charge."""
    node = parent.find(tag, NS) if parent is not None else None
    if node is None:
        return 0.0
    pts = node.find("a:spcPts", NS)
    if pts is None:
        raise Unsupported(f"{tag} en pourcentage")
    return int(pts.get("val", "0")) / 100


def _read_paragraph(p: Element) -> _Paragraph:
    ppr = p.find("a:pPr", NS)
    for node in p:
        if node.tag in (q("a:br"), q("a:fld")):
            raise Unsupported("saut de ligne ou champ dans le texte")
    line_spacing = 1.0
    ln_spc = ppr.find("a:lnSpc", NS) if ppr is not None else None
    if ln_spc is not None:
        pct = ln_spc.find("a:spcPct", NS)
        if pct is None:
            raise Unsupported("interligne exact déjà fixé")
        line_spacing = int(pct.get("val", "100000")) / 100000
    runs: list[Run] = []
    run_elements: list[Element] = []
    for r in p.findall("a:r", NS):
        text = "".join(t.text or "" for t in r.findall("a:t", NS))
        if not text:
            continue
        rpr = r.find("a:rPr", NS)
        if rpr is None or rpr.get("sz") is None:
            raise Unsupported("taille de police héritée")
        runs.append(Run(text, _style(rpr), int(rpr.get("sz", "1000")) / 100))
        run_elements.append(r)
    bullet = ppr.find("a:buChar", NS) if ppr is not None else None
    para = Para(
        runs,
        bullet=bullet.get("char") if bullet is not None else None,
        indent=int(ppr.get("marL", "0")) / EMU_PER_CM if ppr is not None else 0.0,
        space_before=_points(ppr, "a:spcBef"),
        space_after=_points(ppr, "a:spcAft"),
        line_spacing=line_spacing,
        align=ppr.get("algn", "l") if ppr is not None else "l",
    )
    return _Paragraph(p, para, run_elements)


# --- Positions PowerPoint ----------------------------------------------------------------------------------


@dataclass
class PlacedLine:
    paragraph: int
    line: Line
    baseline: float  # cm, depuis le haut de la zone de texte


@dataclass
class TextBox:
    """Zone de texte lue dans le PPTX : géométrie (cm), ancrage et paragraphes."""

    width: float
    height: float
    anchor: str
    top_inset: float
    bottom_inset: float
    paragraphs: list[Para] = field(default_factory=list[Para])
    shape_inset: float = 0.0  # retrait de la zone de texte imposé par la forme (coins arrondis), en haut et en bas

    def powerpoint_lines(self) -> list[PlacedLine]:
        """Lignes et lignes de base telles que PowerPoint les affiche (modèle calibré, cf. config)."""
        placed: list[PlacedLine] = []
        y = 0.0  # points, depuis le haut du bloc de texte
        for index, para in enumerate(self.paragraphs):
            y += para.space_before  # PowerPoint l'applique même au premier paragraphe (set_text l'y annule)
            factor = baseline_factor(para.line_spacing)
            for line in lines(para, self.width):
                placed.append(PlacedLine(index, line, y + factor * line.size))
                y += LINE_HEIGHT_FACTOR * line.size * para.line_spacing
            y += para.space_after
        inner = (self.height - 2 * self.shape_inset - self.top_inset - self.bottom_inset) * PT_PER_CM
        offset = (self.shape_inset + self.top_inset) * PT_PER_CM
        if self.anchor == "b":
            offset += inner - y
        elif self.anchor == "ctr":
            offset += (inner - y) / 2
        for item in placed:
            item.baseline = (item.baseline + offset) / PT_PER_CM
        return placed


def _body_properties(sp: Element) -> tuple[Element, Element]:
    body = sp.find("p:txBody", NS)
    body_pr = body.find("a:bodyPr", NS) if body is not None else None
    if body is None or body_pr is None:
        raise Unsupported("pas de zone de texte")
    return body, body_pr


def _shape_inset(sp: Element, width: float, height: float) -> float:
    """Retrait (cm) de la zone de texte dû à la forme, d'après les formes prédéfinies OOXML : nul pour un
    rectangle ; pour un rectangle à coins arrondis, 29,289 % du rayon (« il » de presetShapeDefinitions)."""
    geometry = sp.find("p:spPr/a:prstGeom", NS)
    preset = geometry.get("prst") if geometry is not None else None
    if preset == "rect":
        return 0.0
    if preset == "roundRect" and geometry is not None:
        guide = geometry.find("a:avLst/a:gd[@name='adj']", NS)
        adj = int(guide.get("fmla", "val 16667").split()[-1]) if guide is not None else 16667
        radius = min(width, height) * min(max(adj, 0), 50000) / 100000
        return radius * 29289 / 100000
    raise Unsupported(f"forme {preset or 'personnalisée'}")


def read_box(sp: Element) -> tuple[TextBox, list[_Paragraph]]:
    body, body_pr = _body_properties(sp)
    ext = sp.find("p:spPr/a:xfrm/a:ext", NS)
    if ext is None:
        raise Unsupported("géométrie héritée")
    if body_pr.get("vert", "horz") != "horz" or body_pr.get("wrap", "square") != "square":
        raise Unsupported("texte vertical ou sans retour à la ligne")
    if body_pr.find("a:normAutofit", NS) is not None or body_pr.find("a:spAutoFit", NS) is not None:
        raise Unsupported("ajustement automatique")
    paragraphs = [_read_paragraph(p) for p in body.findall("a:p", NS)]
    width, height = int(ext.get("cx", "0")) / EMU_PER_CM, int(ext.get("cy", "0")) / EMU_PER_CM
    box = TextBox(
        width=width,
        height=height,
        anchor=body_pr.get("anchor", "t"),
        top_inset=int(body_pr.get("tIns", str(DEFAULT_INSET))) / EMU_PER_CM,
        bottom_inset=int(body_pr.get("bIns", str(DEFAULT_INSET))) / EMU_PER_CM,
        paragraphs=[p.para for p in paragraphs],
        shape_inset=_shape_inset(sp, width, height),
    )
    return box, paragraphs


# --- Copie calibrée pour LibreOffice ------------------------------------------------------------------------


def _font_height(size: float) -> int:
    """Taille de police telle que LibreOffice la stocke (1/100 mm, arrondi)."""
    return math.floor(size * 2540 / 72 + 0.5)


def _descent(size: float) -> int:
    """Hauteur sous la ligne de base d'une ligne LibreOffice à interligne exact (1/100 mm) : LibreOffice
    applique aux PPTX un interligne indépendant de la police, ligne de base à 80 % de la hauteur de police."""
    height = _font_height(size)
    return height - math.floor(0.8 * height + 0.5)


def _spacing_value(height: int) -> int:
    """Valeur spcPts (1/100 pt) que LibreOffice convertit exactement en `height` (1/100 mm)."""
    value = max(0, math.ceil((height * 720 - 360) / 254))
    while (value * 254 + 360) // 720 < height:
        value += 1
    return value


def _spacing(tag: str, value: int) -> Element:
    node = etree.Element(q(tag))
    etree.SubElement(node, q("a:spcPts"), val=str(value))
    return node


def _line_runs(paragraph: _Paragraph, line: Line) -> list[Element]:
    """Copies des a:r d'origine réduites aux caractères de la ligne (mise en forme conservée)."""
    out: list[Element] = []
    position = 0
    for r, run in zip(paragraph.run_elements, paragraph.para.runs):
        start, end = position, position + len(run.text)
        position = end
        lo, hi = max(start, line.start), min(end, line.end)
        if lo >= hi:
            continue
        piece = copy.deepcopy(r)
        texts = piece.findall("a:t", NS)
        texts[0].text = run.text[lo - start : hi - start]
        for extra in texts[1:]:
            piece.remove(extra)
        out.append(piece)
    return out


def _bullet_run(paragraph: _Paragraph, ppr: Element) -> Element:
    """Puce du paragraphe écrite comme un caractère du texte (cf. BULLET_ADVANCES)."""
    para = paragraph.para
    font = ppr.find("a:buFont", NS)
    face = font.get("typeface", "") if font is not None else ""
    advance = BULLET_ADVANCES.get((para.bullet or "", face))
    if advance is None or not para.runs:
        raise Unsupported(f"puce {para.bullet!r} en {face or 'police du texte'} non calibrée")
    size_pct = ppr.find("a:buSzPct", NS)
    size = para.runs[0].size * (int(size_pct.get("val", "100000")) / 100000 if size_pct is not None else 1.0)
    color = ppr.find("a:buClr/a:srgbClr", NS)
    rpr = etree.Element(q("a:rPr"))
    rpr.set("lang", "fr-FR")
    rpr.set("sz", str(int(round(size * 100))))
    rpr.set("b", "0")
    rpr.set("i", "0")
    # Avance de la puce portée au retrait : le texte commence au retrait (marL), comme derrière une vraie puce.
    rpr.set("spc", str(int(round((para.indent * PT_PER_CM - advance * size) * 100))))
    if color is not None:
        etree.SubElement(etree.SubElement(rpr, q("a:solidFill")), q("a:srgbClr"), val=color.get("val", "000000"))
    for tag in ("a:latin", "a:ea", "a:cs", "a:sym"):
        etree.SubElement(rpr, q(tag), typeface=face)
    run = etree.Element(q("a:r"))
    run.append(rpr)
    etree.SubElement(run, q("a:t")).text = para.bullet
    return run


def _line_paragraph(paragraph: _Paragraph, line: Line, first: bool, height: int) -> Element:
    original_ppr = paragraph.element.find("a:pPr", NS)
    ppr = copy.deepcopy(original_ppr) if original_ppr is not None else etree.Element(q("a:pPr"))
    bullet = _bullet_run(paragraph, ppr) if first and paragraph.para.bullet else None
    for node in list(ppr):
        if node.tag in SPACING_TAGS or node.tag in BULLET_TAGS:
            ppr.remove(node)
    ppr.insert(0, _spacing("a:spcAft", 0))
    ppr.insert(0, _spacing("a:spcBef", 0))
    ppr.insert(0, _spacing("a:lnSpc", _spacing_value(height)))
    ppr.insert(3, etree.Element(q("a:buNone")))
    if not first:
        ppr.set("indent", "0")  # ligne suivante d'un paragraphe : le texte reste au retrait du paragraphe
    p = etree.Element(q("a:p"))
    p.append(ppr)
    if bullet is not None:
        p.append(bullet)
    for r in _line_runs(paragraph, line):
        p.append(r)
    end = paragraph.element.find("a:endParaRPr", NS)
    if end is not None:
        p.append(copy.deepcopy(end))
    return p


def has_text(sp: Element) -> bool:
    return any((t.text or "").strip() for t in sp.iterfind("p:txBody/a:p/a:r/a:t", NS))


def calibrate_shape(sp: Element) -> bool:
    """Réécrit le texte d'une forme pour LibreOffice. False si la forme n'a pas de texte."""
    if not has_text(sp):
        return False
    box, paragraphs = read_box(sp)
    placed = box.powerpoint_lines()
    body, body_pr = _body_properties(sp)
    targets = [item.baseline * 1000 for item in placed]  # 1/100 mm depuis le haut de la zone
    sizes = [item.line.size for item in placed]
    spacings = [paragraphs[item.paragraph].para.line_spacing for item in placed]
    # Haut de la première ligne : la ligne garde sa hauteur naturelle, le reste va dans la marge du haut (comptée
    # depuis le bord de la zone de texte de la forme, en retrait du bord pour un rectangle à coins arrondis).
    natural = _font_height(sizes[0]) * LINE_HEIGHT_FACTOR * spacings[0]
    shape_inset = box.shape_inset * 1000
    top = max(shape_inset, targets[0] + _descent(sizes[0]) + LIBREOFFICE_BASELINE_SHIFT - natural)
    inset = math.floor(top - shape_inset)
    new_paragraphs: list[Element] = []
    line_top = shape_inset + inset
    for index, (item, target, size) in enumerate(zip(placed, targets, sizes)):
        # Ligne de base LibreOffice = haut de ligne + hauteur - descente - décalage : on en déduit la hauteur.
        height = max(1, round(target + _descent(size) + LIBREOFFICE_BASELINE_SHIFT - line_top))
        first = index == 0 or placed[index - 1].paragraph != item.paragraph
        new_paragraphs.append(_line_paragraph(paragraphs[item.paragraph], item.line, first, height))
        line_top += height
    for p in body.findall("a:p", NS):
        body.remove(p)
    for p in new_paragraphs:
        body.append(p)
    body_pr.set("anchor", "t")
    body_pr.set("tIns", str(inset * EMU_PER_100MM))
    return True


@dataclass
class Preparation:
    calibrated: int = 0
    skipped: list[str] = field(default_factory=list[str])  # zones laissées telles quelles, avec la raison


def prepare(source: Path, target: Path) -> Preparation:
    """Écrit dans `target` la copie calibrée de `source` pour un rendu PDF par LibreOffice."""
    prs = Presentation(str(source))
    # Polices intégrées retirées de la copie : LibreOffice utilise les polices installées, celles du calibrage.
    font_list = prs.element.find(q("p:embeddedFontLst"))
    if font_list is not None:
        for rel_id in {rel for el in font_list.iter() if (rel := el.get(f"{{{R}}}id"))}:
            prs.part.drop_rel(rel_id)
        prs.element.remove(font_list)
    result = Preparation()
    for number, slide in enumerate(prs.slides, start=1):
        for sp in slide.shapes.element.iter(q("p:sp")):
            if sp.find("p:txBody", NS) is None:
                continue
            try:
                if calibrate_shape(sp):
                    result.calibrated += 1
            except Unsupported as exc:
                name = sp.find("p:nvSpPr/p:cNvPr", NS)
                result.skipped.append(f"diapositive {number}, {name.get('name') if name is not None else '?'} : {exc}")
    prs.save(str(target))
    return result
