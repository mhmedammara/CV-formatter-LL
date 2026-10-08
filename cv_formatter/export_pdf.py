"""Conversion PPTX -> PDF : PowerPoint (COM) sous Windows, LibreOffice calibré ailleurs (serveur Linux).

Sous Linux, LibreOffice convertit une copie du PPTX calée sur le rendu de PowerPoint (libreoffice.py), puis
les polices complètes sont intégrées au PPTX livré, comme le fait PowerPoint en le ré-enregistrant.
La variable d'environnement CV_FORMATTER_PDF=libreoffice force LibreOffice (essais sous Windows).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PP_SAVE_AS_PDF = 32
PP_SAVE_AS_PPTX = 24
MSO_TRUE, MSO_FALSE = -1, 0

LIBREOFFICE_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
)


def libreoffice() -> str | None:
    for candidate in LIBREOFFICE_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return shutil.which("soffice") or shutil.which("libreoffice")


def soffice_convert(soffice: str, files: list[Path], outdir: Path, profile: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    """Une seule instance LibreOffice pour tout le lot, avec un profil temporaire (pas de conflit entre
    conversions simultanées, rien d'écrit dans le dossier personnel)."""
    return subprocess.run(
        [soffice, f"-env:UserInstallation={profile.resolve().as_uri()}", "--headless", "--norestore",
         "--convert-to", "pdf", "--outdir", str(outdir), *(str(f) for f in files)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def office_application(prog_id: str) -> Any:
    """Application Office pilotée par COM (« PowerPoint.Application »…) : objet dynamique, donc typé Any."""
    import win32com.client

    return getattr(win32com.client, "DispatchEx")(prog_id)


def _export_with_powerpoint(jobs: list[tuple[Path, Path]]) -> dict[Path, str]:
    import pythoncom

    errors: dict[Path, str] = {}
    pythoncom.CoInitialize()
    try:
        app = office_application("PowerPoint.Application")
        already_open = app.Presentations.Count > 0
        try:
            for pptx, pdf in jobs:
                try:
                    presentation = app.Presentations.Open(str(pptx), MSO_TRUE, MSO_FALSE, MSO_FALSE)
                    embedded: Path | None = pptx.with_name(pptx.stem + ".~embed.pptx")
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


def _export_with_libreoffice(jobs: list[tuple[Path, Path]], soffice: str, notes: dict[Path, list[str]]) -> dict[Path, str]:
    from . import libreoffice as calage

    errors: dict[Path, str] = {}
    with tempfile.TemporaryDirectory(prefix="cv_pdf_") as tmp:
        work = Path(tmp)
        copies: list[Path] = []
        for index, (pptx, _) in enumerate(jobs):
            copy = work / f"{index:04d}.pptx"  # noms courts et uniques : LibreOffice nomme le PDF d'après le fichier
            try:
                result = calage.prepare(pptx, copy)
                if result.skipped:
                    notes.setdefault(pptx, []).append("zones rendues sans calage : " + " ; ".join(result.skipped))
            except Exception as exc:  # rendu LibreOffice brut plutôt qu'aucun PDF, et on le dit
                shutil.copy2(pptx, copy)
                notes.setdefault(pptx, []).append(f"rendu LibreOffice non calé sur PowerPoint ({type(exc).__name__}: {exc})")
            copies.append(copy)
        try:
            result = soffice_convert(soffice, copies, work / "pdf", work / "profil", timeout=120 + 30 * len(copies))
            message = (result.stderr or result.stdout or "échec LibreOffice").strip()[-500:]
        except subprocess.TimeoutExpired:
            message = "LibreOffice n'a pas répondu à temps"
        for copy, (pptx, pdf) in zip(copies, jobs):
            produced = work / "pdf" / (copy.stem + ".pdf")
            if not produced.exists():
                errors[pptx] = message
                continue
            shutil.move(str(produced), pdf)
    return errors


def embed_fonts(pptx_files: list[Path]) -> dict[Path, str]:
    """Polices complètes intégrées aux PPTX que PowerPoint n'a pas ré-enregistrés. Renvoie {pptx: erreur}."""
    from . import fonts

    errors: dict[Path, str] = {}
    for pptx in pptx_files:
        try:
            fonts.embed_in_pptx(pptx)
        except Exception as exc:
            errors[pptx] = f"{type(exc).__name__}: {exc}"
    return errors


@dataclass
class PdfExport:
    errors: dict[Path, str] = field(default_factory=dict[Path, str])  # PPTX -> raison de l'échec du PDF
    engines: dict[Path, str] = field(default_factory=dict[Path, str])  # PPTX -> moteur qui a produit le PDF
    notes: dict[Path, list[str]] = field(default_factory=dict[Path, list[str]])  # remarques pour le rapport


def export_pdfs(pptx_files: list[Path]) -> PdfExport:
    """Convertit chaque PPTX en PDF à côté, et intègre les polices complètes au PPTX."""
    jobs = [(p.resolve(), p.with_suffix(".pdf").resolve()) for p in pptx_files]
    export = PdfExport()
    if not jobs:
        return export
    errors: dict[Path, str] = {pptx: "non converti" for pptx, _ in jobs}
    if sys.platform == "win32" and os.environ.get("CV_FORMATTER_PDF", "").lower() != "libreoffice":
        try:
            errors = _export_with_powerpoint(jobs)
        except Exception as exc:  # PowerPoint absent ou COM indisponible
            errors = {pptx: f"PowerPoint indisponible : {exc}" for pptx, _ in jobs}
        export.engines.update({pptx: "PowerPoint" for pptx, _ in jobs if pptx not in errors})
    remaining = [(pptx, pdf) for pptx, pdf in jobs if pptx in errors]
    soffice = libreoffice() if remaining else None
    if soffice:
        lo_errors = _export_with_libreoffice(remaining, soffice, export.notes)
        errors = {p: msg for p, msg in errors.items() if p in lo_errors}
        errors.update(lo_errors)
        export.engines.update({pptx: "LibreOffice (calé sur PowerPoint)" for pptx, _ in remaining if pptx not in lo_errors})
    # Sans PowerPoint, personne n'a intégré les polices au PPTX livré : on le fait ici.
    for pptx, message in embed_fonts([pptx for pptx, _ in jobs if export.engines.get(pptx) != "PowerPoint"]).items():
        export.notes.setdefault(pptx, []).append(f"polices non intégrées au PPTX ({message})")
    export.errors = errors
    return export
