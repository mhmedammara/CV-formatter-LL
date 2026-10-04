"""Rapport de traitement (Markdown) : ce qui a été retiré, ce qu'il faut vérifier."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from .verification import Finding


@dataclass
class CVReport:
    source: str
    name: str = ""
    pptx: Path | None = None
    pdf: Path | None = None
    pages: int = 0
    extractor: str = ""
    photo: str = ""
    experience: str = ""
    cross_check: str = ""
    skipped_sections: list[str] = field(default_factory=list)
    layout_notes: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def removed(self) -> list[Finding]:
        return [f for f in self.findings if f.action == "retiré"]

    @property
    def to_check(self) -> list[Finding]:
        return [f for f in self.findings if f.action != "retiré"]


def _line(f: Finding) -> str:
    text = f.text if len(f.text) <= 160 else f.text[:157] + "…"
    return f"- **{f.label}** : « {text} » — {f.reason}"


def write_report(reports: list[CVReport], target: Path, settings: str) -> Path:
    ok = [r for r in reports if not r.error]
    lines = [
        "# Rapport — mise au format Logiclever",
        "",
        f"Généré le {dt.datetime.now():%d/%m/%Y à %H:%M} · {settings}",
        "",
        f"**{len(ok)} CV générés**, {len(reports) - len(ok)} en échec. "
        "Relisez en priorité les sections « À vérifier » avant tout envoi à un client.",
        "",
    ]
    for r in reports:
        lines.append(f"## {r.name or r.source}")
        lines.append("")
        lines.append(f"- Source : `{r.source}`")
        if r.error:
            lines.append(f"- ❌ **Échec** : {r.error}")
            lines.append("")
            continue
        if r.pptx:
            files = f"`{r.pptx.name}`" + (f" et `{r.pdf.name}`" if r.pdf else " (PDF non généré)")
            lines.append(f"- Fichiers : {files} — {r.pages} page(s)")
        lines.append(f"- Extraction : {r.extractor}")
        if r.cross_check:
            lines.append(f"- Contre-vérification : {r.cross_check}")
        lines.append(f"- Photo : {r.photo}")
        lines.append(f"- Pastille d'expérience : {r.experience}")
        if r.skipped_sections:
            lines.append(f"- Sections du CV non reprises (absentes du modèle) : {', '.join(r.skipped_sections)}")
        for note in r.layout_notes:
            lines.append(f"- Mise en page : {note}")
        for warning in r.warnings:
            lines.append(f"- ⚠️ {warning}")
        if r.removed:
            lines += ["", "**Éléments retirés (non retrouvés dans le CV d'origine)**", ""]
            lines += [_line(f) for f in r.removed]
        if r.to_check:
            lines += ["", "**À vérifier**", ""]
            lines += [_line(f) for f in r.to_check]
        if not r.findings:
            lines.append("- ✅ Tous les éléments ont été retrouvés dans le CV d'origine")
        lines.append("")
    target.write_text("\n".join(lines), encoding="utf-8")
    return target
