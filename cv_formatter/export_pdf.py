"""Conversion PPTX -> PDF : PowerPoint (COM) en priorité, LibreOffice en secours."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PP_SAVE_AS_PDF = 32
PP_SAVE_AS_PPTX = 24
MSO_TRUE, MSO_FALSE = -1, 0

LIBREOFFICE_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
)


def _libreoffice() -> str | None:
    for candidate in LIBREOFFICE_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return shutil.which("soffice") or shutil.which("libreoffice")


def _export_with_powerpoint(jobs: list[tuple[Path, Path]]) -> dict[Path, str]:
    import pythoncom
    import win32com.client

    errors: dict[Path, str] = {}
    pythoncom.CoInitialize()
    try:
        app = win32com.client.DispatchEx("PowerPoint.Application")
        already_open = app.Presentations.Count > 0
        try:
            for pptx, pdf in jobs:
                try:
                    presentation = app.Presentations.Open(str(pptx), MSO_TRUE, MSO_FALSE, MSO_FALSE)
                    embedded = pptx.with_name(pptx.stem + ".~embed.pptx")
                    try:
                        presentation.SaveAs(str(pdf), PP_SAVE_AS_PDF)
                        # Ré-enregistrement par PowerPoint avec les polices Lexend complètes intégrées :
                        # le PPTX s'affiche correctement même là où Lexend n'est pas installée.
                        try:
                            presentation.SaveAs(str(embedded), PP_SAVE_AS_PPTX, MSO_TRUE)
                        except Exception:
                            embedded = None
                    finally:
                        presentation.Close()
                    if embedded is not None and embedded.exists():
                        os.replace(embedded, pptx)
                except Exception as exc:  # erreur COM sur un fichier : on continue le lot
                    errors[pptx] = str(exc)
        finally:
            if not already_open and app.Presentations.Count == 0:
                app.Quit()
    finally:
        pythoncom.CoUninitialize()
    return errors


def _export_with_libreoffice(jobs: list[tuple[Path, Path]], soffice: str) -> dict[Path, str]:
    errors: dict[Path, str] = {}
    for pptx, pdf in jobs:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf", "--outdir", tmp, str(pptx)],
                capture_output=True,
                text=True,
                timeout=180,
            )
            produced = Path(tmp) / (pptx.stem + ".pdf")
            if result.returncode != 0 or not produced.exists():
                errors[pptx] = (result.stderr or result.stdout or "échec LibreOffice").strip()
                continue
            shutil.move(str(produced), pdf)
    return errors


def export_pdfs(pptx_files: list[Path]) -> dict[Path, str]:
    """Convertit chaque PPTX en PDF à côté (et, via PowerPoint, y intègre les polices).

    Renvoie {pptx: message d'erreur} pour les échecs.
    """
    jobs = [(p.resolve(), p.with_suffix(".pdf").resolve()) for p in pptx_files]
    if not jobs:
        return {}
    errors: dict[Path, str] = {pptx: "non converti" for pptx, _ in jobs}
    if sys.platform == "win32":
        try:
            errors = _export_with_powerpoint(jobs)
        except Exception as exc:  # PowerPoint absent ou COM indisponible
            errors = {pptx: f"PowerPoint indisponible : {exc}" for pptx, _ in jobs}
    remaining = [(pptx, pdf) for pptx, pdf in jobs if pptx in errors]
    soffice = _libreoffice() if remaining else None
    if soffice:
        lo_errors = _export_with_libreoffice(remaining, soffice)
        errors = {p: msg for p, msg in errors.items() if p in lo_errors}
        errors.update(lo_errors)
    return errors
