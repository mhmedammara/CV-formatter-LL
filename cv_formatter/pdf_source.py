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

import pymupdf
from PIL import Image, ImageChops, ImageFilter

from . import ocr
from .export_pdf import _libreoffice

PDF_SUFFIXES = {".pdf"}
OFFICE_SUFFIXES = {".docx", ".doc", ".odt", ".rtf"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
SUPPORTED_SUFFIXES = PDF_SUFFIXES | OFFICE_SUFFIXES | IMAGE_SUFFIXES

_SPACING_ACCENTS = {"´": "́", "`": "̀", "ˆ": "̂", "¨": "̈", "˜": "̃"}


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
    ocr_pages: list[int] = field(default_factory=list)
    ocr_engine: str | None = None
    candidates: list[PhotoCandidate] = field(default_factory=list)
    document_date: dt.date | None = None  # date du PDF : sert de « aujourd'hui » pour les postes « en cours »

    @property
    def has_text_layer(self) -> bool:
        return not self.ocr_pages

    @property
    def text_is_reliable(self) -> bool:
        return bool(self.text.strip()) and not self.ocr_pages


def _office_to_pdf(path: Path) -> bytes:
    soffice = _libreoffice()
    with tempfile.TemporaryDirectory() as tmp:
        if soffice:
            subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf", "--outdir", tmp, str(path)],
                capture_output=True,
                timeout=180,
            )
            produced = Path(tmp) / (path.stem + ".pdf")
            if produced.exists():
                return produced.read_bytes()
        try:  # secours : Word via COM
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            word = win32com.client.DispatchEx("Word.Application")
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
    mask = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 12 else 0)
    mask = mask.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
    bbox = mask.getbbox()
    if bbox is None:
        return None
    image, mask = a.crop(bbox), mask.crop(bbox)
    fill = mask.histogram()[255] / float(mask.width * mask.height)
    if fill < 0.92:  # forme non rectangulaire (cercle, détourage) : on blanchit l'extérieur
        image = Image.composite(image, Image.new("RGB", image.size, "white"), mask)
    return image, fill


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
    parts, sorted_parts, links, ocr_pages = [], [], [], []
    for index, page in enumerate(doc):
        page_text = page.get_text("text")
        sorted_text = page.get_text("text", sort=True)
        if len(page_text.strip()) < 40 and (page.get_images() or page.get_drawings()):
            ocr_text = ocr.ocr_page(page)
            if ocr_text.strip():
                page_text = sorted_text = ocr_text
                ocr_pages.append(index + 1)
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
        image = image.resize((max_size, max_size), Image.LANCZOS)
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
