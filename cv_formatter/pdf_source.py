"""Lecture du CV source : conversion en PDF, texte, liens, OCR et photo du consultant."""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pymupdf
from numpy.typing import NDArray
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from . import ocr
from .export_pdf import libreoffice, soffice_convert
from .visage import find_face

PDF_SUFFIXES = {".pdf"}
OFFICE_SUFFIXES = {".docx", ".doc", ".odt", ".rtf"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
SUPPORTED_SUFFIXES = PDF_SUFFIXES | OFFICE_SUFFIXES | IMAGE_SUFFIXES

_SPACING_ACCENTS = {"´": "́", "`": "̀", "ˆ": "̂", "¨": "̈", "˜": "̃"}

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
MARGIN = 0.07  # haut et bas de page (fraction de la hauteur) où se trouvent en-têtes et pieds de page


def fix_text(text: str) -> str:
    """Normalise le texte extrait (ligatures, accents LaTeX séparés « Ing´enieur »)."""
    text = unicodedata.normalize("NFKC", text)
    # NFKC transforme « ´ » en espace + accent combinant : on recolle à la lettre suivante.
    text = re.sub(r" ?([́̀̂̈̃])\s?([A-Za-z])", lambda m: m.group(2) + m.group(1), text)
    text = re.sub(r"([´`ˆ¨˜])\s?([A-Za-z])", lambda m: m.group(2) + _SPACING_ACCENTS[m.group(1)], text)
    text = re.sub(r"([cC]) ?̧|([cC]) ?¸", lambda m: (m.group(1) or m.group(2)) + "̧", text)
    text = unicodedata.normalize("NFC", text)
    text = text.replace(" ", " ").replace("​", "")
    return text


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFD", unicodedata.normalize("NFKC", text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower().replace("’", "'")
    return re.sub(r"\s+", " ", text).strip(" .,;:|-–—")


@dataclass
class TextLine:
    """Ligne de texte du PDF avec sa position (fraction de la page : 0 = haut / gauche) et sa taille de police."""

    page: int
    block: int
    top: float
    bottom: float
    text: str
    left: float = 0.0
    size: float = 0.0


@dataclass
class LinkedInRole:
    """Poste lu dans un export LinkedIn (« Enregistrer au format PDF »), avec l'entreprise sous laquelle il est listé."""

    company: str
    title: str
    dates: str
    text: str  # intitulé, dates, lieu et description du poste


def linkedin_roles(lines: list[TextLine]) -> list[LinkedInRole]:
    """Export LinkedIn : entreprises et postes lus d'après la mise en page (vide si ce n'est pas un export LinkedIn).

    LinkedIn n'écrit le nom d'une entreprise qu'une fois, au-dessus de tous les postes qu'on y a occupés, dans une
    police un peu plus grande que l'intitulé des postes : sans cette structure, un deuxième poste peut sembler
    sans employeur, ou attribué au client cité dans sa description."""
    if not any(line.text.rstrip().endswith("(LinkedIn)") for line in lines):
        return []
    main = sorted((line for line in lines if line.left > 0.25), key=lambda line: (line.page, line.top))
    start = next((i for i, line in enumerate(main) if line.size >= 14 and _fold(line.text) in ("experience", "experiences")), None)
    if start is None:
        return []
    section: list[TextLine] = []
    for line in main[start + 1:]:
        if line.size >= 14:  # rubrique suivante (Formation…)
            break
        section.append(line)
    sizes = sorted({round(line.size, 1) for line in section if line.size >= 11}, reverse=True)
    if len(sizes) < 2:
        return []
    company_size, role_size = sizes[0], sizes[1]
    roles: list[LinkedInRole] = []
    company = ""
    previous = None
    for line in section:
        size = round(line.size, 1)
        if size == company_size:
            company = f"{company} {line.text}".strip() if previous == "company" else line.text
            previous = "company"
        elif size == role_size:
            if previous == "role" and roles:  # intitulé sur deux lignes
                roles[-1].title += " " + line.text
                roles[-1].text += " " + line.text
            else:
                roles.append(LinkedInRole(company, line.text, "", line.text))
            previous = "role"
        else:
            if roles and roles[-1].company == company:
                if not roles[-1].dates and re.search(r"(?:19|20)\d{2}", line.text):
                    roles[-1].dates = line.text
                roles[-1].text += "\n" + line.text
            previous = "text"
    return roles


@dataclass
class PhotoCandidate:
    index: int
    page: int
    xref: int
    image: Image.Image  # zone réellement visible, hors-masque blanchi
    fill_ratio: float

    def thumbnail_png(self, size: int = 320) -> bytes:
        thumb = self.image.copy()
        thumb.thumbnail((size, size))
        buffer = io.BytesIO()
        thumb.save(buffer, format="PNG")
        return buffer.getvalue()


@dataclass
class SourceDocument:
    path: Path
    pdf_bytes: bytes
    sha256: str
    page_count: int
    text: str  # texte dans l'ordre du flux PDF (fourni au modèle)
    reference_text: str  # texte de référence pour le contrôle (flux + ordre de lecture + liens)
    links: list[str]
    ocr_pages: list[int] = field(default_factory=list[int])
    ocr_engine: str | None = None
    candidates: list[PhotoCandidate] = field(default_factory=list[PhotoCandidate])
    document_date: dt.date | None = None  # date du PDF : sert de « aujourd'hui » pour les postes « en cours »
    lines: list[TextLine] = field(default_factory=list[TextLine])  # pages avec couche texte uniquement

    @property
    def has_text_layer(self) -> bool:
        return not self.ocr_pages

    @property
    def text_is_reliable(self) -> bool:
        return bool(self.text.strip()) and not self.ocr_pages

    def body_text(self, prenom: str | None, nom: str | None, titre: str | None) -> str | None:
        """Texte du CV sans son en-tête (bloc du nom, titre du consultant), sans e-mails ni URL et sans les
        en-têtes / pieds de page répétés : c'est là qu'un employeur ou un client doit être nommé pour être
        attribué à une expérience. None si la position du texte est inconnue (OCR)."""
        if not self.lines or self.ocr_pages:
            return None
        excluded: set[int] = set()
        blocks: dict[tuple[int, int], list[int]] = {}
        for i, line in enumerate(self.lines):
            blocks.setdefault((line.page, line.block), []).append(i)
        # 1. Bloc d'identité : premier bloc de la page 1 (de haut en bas) qui contient le nom complet.
        name = set(re.findall(r"[a-z0-9]+", _fold(f"{prenom or ''} {nom or ''}")))
        if name:
            first_page = sorted((k for k in blocks if k[0] == 0), key=lambda k: self.lines[blocks[k][0]].top)
            for key in first_page:
                words = set(re.findall(r"[a-z0-9]+", _fold(" ".join(self.lines[i].text for i in blocks[key]))))
                if name <= words and len(blocks[key]) <= 8:
                    excluded.update(blocks[key])
                    break
        # 2. Titre du consultant, s'il est écrit à part (page 1) : ligne identique au titre, ou morceau de titre.
        title = _fold(titre or "")
        if title:
            for i, line in enumerate(self.lines):
                folded = _fold(line.text)
                if line.page == 0 and folded and (folded == title or (len(folded) >= 12 and folded in title)):
                    excluded.add(i)
        # 3. En-têtes et pieds de page répétés sur plusieurs pages (nom de l'ESN, mention « confidentiel »…).
        margin = [i for i, line in enumerate(self.lines) if line.bottom < MARGIN or line.top > 1 - MARGIN]
        pages: dict[str, set[int]] = {}
        for i in margin:
            pages.setdefault(_fold(self.lines[i].text), set()).add(self.lines[i].page)
        excluded.update(i for i in margin if len(pages[_fold(self.lines[i].text)]) >= 2)
        text = "\n".join(line.text for i, line in enumerate(self.lines) if i not in excluded)
        return URL_RE.sub(" ", EMAIL_RE.sub(" ", text))

    @property
    def linkedin_roles(self) -> list[LinkedInRole]:
        return [] if self.ocr_pages else linkedin_roles(self.lines)


def _office_to_pdf(path: Path) -> bytes:
    soffice = libreoffice()
    with tempfile.TemporaryDirectory() as tmp:
        if soffice:
            try:
                soffice_convert(soffice, [path], Path(tmp), Path(tmp) / "profil", timeout=180)
            except subprocess.TimeoutExpired:
                pass
            produced = Path(tmp) / (path.stem + ".pdf")
            if produced.exists():
                return produced.read_bytes()
        try:  # secours : Word via COM
            import pythoncom

            from .export_pdf import office_application

            pythoncom.CoInitialize()
            word = office_application("Word.Application")
            try:
                document = word.Documents.Open(str(path.resolve()), ReadOnly=True)
                target = Path(tmp) / "converted.pdf"
                document.SaveAs2(str(target), FileFormat=17)
                document.Close(False)
                return target.read_bytes()
            finally:
                word.Quit()
        except Exception as exc:
            raise RuntimeError(f"Impossible de convertir {path.name} en PDF (LibreOffice/Word) : {exc}") from exc


def _to_pdf_bytes(path: Path) -> bytes:
    suffix = path.suffix.lower()
    if suffix in PDF_SUFFIXES:
        return path.read_bytes()
    if suffix in IMAGE_SUFFIXES:
        with pymupdf.open(str(path)) as image_doc:
            return image_doc.convert_to_pdf()
    if suffix in OFFICE_SUFFIXES:
        return _office_to_pdf(path)
    raise ValueError(f"Format non pris en charge : {path.suffix}")


def _visible_photo(pdf_bytes: bytes, page_index: int, xref: int, rect: pymupdf.Rect) -> tuple[Image.Image, float] | None:
    """Rend la zone avec et sans l'image : la différence donne exactement la partie visible."""
    zoom = 5.0
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        page = doc[page_index]
        clip = (rect + (-2, -2, 2, 2)) & page.rect
        matrix = pymupdf.Matrix(zoom, zoom)
        with_image = page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
        page.delete_image(xref)
        without_image = page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
    a = Image.frombytes("RGB", (with_image.width, with_image.height), with_image.samples)
    b = Image.frombytes("RGB", (without_image.width, without_image.height), without_image.samples)
    mask = ImageChops.difference(a, b).convert("L").point([255 if v > 12 else 0 for v in range(256)])
    mask = mask.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
    bbox = mask.getbbox()
    if bbox is None:
        return None
    image, mask = a.crop(bbox), mask.crop(bbox)
    fill = mask.histogram()[255] / float(mask.width * mask.height)
    if fill < 0.92:  # forme non rectangulaire (cercle, ovale, détourage)
        image = _inscribed_circle(image, mask) or Image.composite(image, Image.new("RGB", image.size, "white"), mask)
    return image, fill


def best_circle(inside: NDArray[np.bool_], keep: tuple[float, float, float, float]) -> tuple[int, int, int] | None:
    """Plus grand cercle entièrement dans la zone `inside` qui contient le rectangle `keep` (x0, y0, x1, y1).
    Renvoie (cx, cy, rayon) en pixels, ou None si aucun cercle de la zone ne contient le rectangle."""
    import cv2

    # Rayon maximal d'un cercle centré en chaque point de la zone (distance au bord le plus proche).
    distance = np.asarray(cv2.distanceTransform(inside.astype(np.uint8), cv2.DIST_L2, 5), dtype=np.float64)
    height, width = distance.shape
    ys = np.arange(height, dtype=np.float64)[:, None]
    xs = np.arange(width, dtype=np.float64)[None, :]
    x0, y0, x1, y1 = keep
    reach = np.zeros_like(distance)  # distance au coin le plus éloigné du rectangle
    for x in (x0, x1):
        for y in (y0, y1):
            reach = np.maximum(reach, np.hypot(xs - x, ys - y))
    # Marge de 2 px : le rayon est ensuite arrondi à l'entier inférieur.
    candidate = distance - 2
    radius = candidate * (candidate >= reach)  # 0 là où le cercle ne contiendrait pas le rectangle
    index = int(radius.argmax())
    cy, cx = divmod(index, width)
    r = int(radius.flat[index])
    return (cx, cy, r) if r > 0 else None


def _inscribed_circle(image: Image.Image, mask: Image.Image) -> Image.Image | None:
    """Photo coupée par une forme quelconque (ovale, « galet ») : on garde le plus grand cercle entièrement rempli
    par la photo ET contenant tout le visage (cheveux et menton compris), extérieur blanchi. Sans cela, les bords
    blancs de la forme apparaîtraient dans le cadre. None (forme d'origine conservée) si aucun visage n'est trouvé
    ou si aucun cercle de la forme ne contient le visage."""
    face = find_face(image)
    if face is None:
        return None
    fx, fy, fw, fh = face.box
    keep = (max(fx - 0.25 * fw, 0), max(fy - 0.45 * fh, 0), min(fx + 1.25 * fw, image.width - 1), min(fy + 1.2 * fh, image.height - 1))
    circle = best_circle(np.asarray(mask) > 0, keep)
    if circle is None:
        return None
    cx, cy, r = circle
    if r < 0.3 * min(mask.size):
        return None
    square = image.crop((cx - r, cy - r, cx + r, cy + r))
    disc = Image.new("L", square.size, 0)
    ImageDraw.Draw(disc).ellipse((0, 0, square.width - 1, square.height - 1), fill=255)
    return Image.composite(square, Image.new("RGB", square.size, "white"), disc)


def _photo_candidates(pdf_bytes: bytes, doc: pymupdf.Document) -> list[PhotoCandidate]:
    candidates: list[PhotoCandidate] = []
    seen: set[int] = set()
    for page_index in range(min(2, doc.page_count)):
        page = doc[page_index]
        page_area = page.rect.width * page.rect.height
        for info in page.get_image_info(xrefs=True):
            xref = info.get("xref", 0)
            if not xref or xref in seen:
                continue
            rect = pymupdf.Rect(info["bbox"]) & page.rect
            if rect.is_empty:
                continue
            w, h = rect.width, rect.height
            if min(w, h) < 35 or min(info["width"], info["height"]) < 60:
                continue
            if not 0.55 <= w / h <= 1.8 or not 0.002 <= (w * h) / page_area <= 0.15:
                continue
            seen.add(xref)
            visible = _visible_photo(pdf_bytes, page_index, xref, rect)
            if visible is None or min(visible[0].size) < 80:
                continue
            candidates.append(PhotoCandidate(len(candidates) + 1, page_index, xref, visible[0], visible[1]))
    return candidates


def _text_lines(page: pymupdf.Page, index: int) -> list[TextLine]:
    height = page.rect.height or 1.0
    width = page.rect.width or 1.0
    lines: list[TextLine] = []
    for number, block in enumerate(page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]):
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            text = fix_text("".join(span["text"] for span in line["spans"])).strip()
            if text:
                x0, y0, _, y1 = line["bbox"]
                size = max(span["size"] for span in line["spans"])
                lines.append(TextLine(index, number, y0 / height, y1 / height, text, x0 / width, size))
    return lines


def _document_date(doc: pymupdf.Document, path: Path) -> dt.date | None:
    """Date de rédaction du CV d'après les métadonnées PDF (modification, sinon création).
    Ignorée pour un document converti à l'instant (Word, image) : elle ne dit rien du CV."""
    if path.suffix.lower() not in PDF_SUFFIXES:
        return None
    for key in ("modDate", "creationDate"):
        match = re.match(r"D:(\d{4})(\d{2})(\d{2})", doc.metadata.get(key) or "")
        if match:
            try:
                date = dt.date(int(match[1]), int(match[2]), int(match[3]))
            except ValueError:
                continue
            if dt.date(1995, 1, 1) <= date <= dt.date.today():
                return date
    return None


def load_source(path: Path, with_photos: bool = True) -> SourceDocument:
    pdf_bytes = _to_pdf_bytes(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    parts: list[str] = []
    sorted_parts: list[str] = []
    links: list[str] = []
    ocr_pages: list[int] = []
    lines: list[TextLine] = []
    for index, page in enumerate(doc):
        page_text = page.get_text("text")
        sorted_text = page.get_text("text", sort=True)
        if len(page_text.strip()) < 40 and (page.get_images() or page.get_drawings()):
            ocr_text = ocr.ocr_page(page)
            if ocr_text.strip():
                page_text = sorted_text = ocr_text
                ocr_pages.append(index + 1)
        if index + 1 not in ocr_pages:
            lines += _text_lines(page, index)
        parts.append(f"--- Page {index + 1} ---\n{fix_text(page_text).strip()}")
        sorted_parts.append(fix_text(sorted_text))
        for link in page.get_links():
            uri = link.get("uri")
            if uri and uri not in links:
                links.append(uri)
    candidates = _photo_candidates(pdf_bytes, doc) if with_photos else []
    source = SourceDocument(
        path=path,
        pdf_bytes=pdf_bytes,
        sha256=sha,
        page_count=doc.page_count,
        text="\n".join(parts),
        reference_text="\n".join(parts + sorted_parts),
        links=links,
        ocr_pages=ocr_pages,
        ocr_engine=ocr.engine_name() if ocr_pages else None,
        candidates=candidates,
        document_date=_document_date(doc, path),
        lines=lines,
    )
    doc.close()
    return source


def square_photo(image: Image.Image, max_size: int = 800) -> Image.Image:
    """Recadrage carré ; pour un portrait vertical on garde plutôt le haut (visage)."""
    w, h = image.size
    if w > h:
        left = (w - h) // 2
        image = image.crop((left, 0, left + h, h))
    elif h > w:
        top = int((h - w) * 0.2)
        image = image.crop((0, top, w, top + w))
    if image.width > max_size:
        image = image.resize((max_size, max_size), Image.Resampling.LANCZOS)
    return image


def save_photo(candidate: PhotoCandidate, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    square_photo(candidate.image).save(target, format="JPEG", quality=92)
    return target


def copy_input(path: Path, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / path.name
    shutil.copy2(path, target)
    return target
