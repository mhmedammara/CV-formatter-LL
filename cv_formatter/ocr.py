"""OCR des pages scannées : OCR intégré de Windows (français), Tesseract en secours.

Le modèle lit lui-même les images des pages ; l'OCR local sert à obtenir le texte de
référence du contrôle anti-invention (et aide l'extraction) quand le PDF n'a pas de
couche texte.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import pymupdf

if TYPE_CHECKING:
    from winrt.windows.media.ocr import OcrEngine

TESSERACT_DIRS = (
    Path(r"C:\Program Files\Tesseract-OCR"),
    Path(r"C:\Program Files (x86)\Tesseract-OCR"),
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR",
)


@lru_cache(maxsize=1)
def _windows_engine() -> OcrEngine | None:
    try:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine
    except ImportError:
        return None
    for tag in ("fr-FR", "fr"):
        try:
            language = Language(tag)
            if OcrEngine.is_language_supported(language):
                return OcrEngine.try_create_from_language(language)
        except Exception:
            continue
    return OcrEngine.try_create_from_user_profile_languages()


async def _windows_ocr_async(png: bytes) -> str:
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png)
    await writer.store_async()
    writer.detach_stream()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    engine = _windows_engine()
    if engine is None:
        return ""
    result = await engine.recognize_async(bitmap)
    return "\n".join(line.text for line in result.lines)


@lru_cache(maxsize=1)
def _tessdata() -> tuple[str, str] | None:
    candidates = [Path(os.environ["TESSDATA_PREFIX"])] if os.environ.get("TESSDATA_PREFIX") else []
    candidates += [d / "tessdata" for d in TESSERACT_DIRS]
    exe = shutil.which("tesseract")
    if exe:
        candidates.append(Path(exe).parent / "tessdata")
    for folder in candidates:
        if (folder / "fra.traineddata").exists():
            return str(folder), "fra"
        if (folder / "eng.traineddata").exists():
            return str(folder), "eng"
    return None


def engine_name() -> str | None:
    if _windows_engine() is not None:
        return "OCR Windows"
    tess = _tessdata()
    if tess is not None:
        return f"Tesseract ({tess[1]})"
    return None


def ocr_page(page: pymupdf.Page, dpi: int = 300) -> str:
    """Texte OCR d'une page ; chaîne vide si aucun moteur n'est disponible."""
    if _windows_engine() is not None:
        zoom = min(dpi / 72, 9000 / max(page.rect.width, page.rect.height))
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        try:
            return asyncio.run(_windows_ocr_async(pix.tobytes("png")))
        except Exception:
            pass
    tess = _tessdata()
    if tess is not None:
        folder, language = tess
        try:
            textpage = page.get_textpage_ocr(language=language, dpi=dpi, full=True, tessdata=folder)
            return page.get_text("text", textpage=textpage)
        except Exception:
            return ""
    return ""
