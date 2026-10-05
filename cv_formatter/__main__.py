"""Ligne de commande : python -m cv_formatter [dossiers | fichiers | .zip] [options]."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__, contenu, fonts
from .anonymisation import anonymize, initials
from .config import DATA_DIR, DEFAULT_EFFORT, DEFAULT_MODEL, DEFAULT_OUTPUT_DIR, EFFORT_CHOICES, INPUT_DIR, TEMPLATE_PATH
from .controle import CONTROLE_VERSION, apply_verdicts, fingerprint, fingerprint_data, photo_verdict, run_cross_check, verdicts_from_json, verdicts_to_json
from .export_pdf import export_pdfs
from .extraction import EXTRACTION_VERSION, api_key_available, extract_cv, make_client
from .pdf_source import SUPPORTED_SUFFIXES, SourceDocument, load_source, save_photo, square_photo
from .rapport import CVReport, write_report
from .render_pptx import display_name, render_cv
from .schema import CV
from .verification import Finding, verify
from .visage import FaceCheck, check_face

if TYPE_CHECKING:
    from openai import OpenAI

Stored = dict[str, Any]  # contenu d'un fichier sortie/_donnees/*.json (_meta, cv, controle)


@dataclass
class Job:
    path: Path
    report: CVReport
    data_file: Path
    source: SourceDocument | None = None
    stored: Stored | None = None
    cv: CV | None = None
    pptx: Path | None = None
    stats: dict[str, int | None] = field(default_factory=dict[str, int | None])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m cv_formatter",
        description="Met des CV (PDF, Word, images) au format Logiclever (PPTX + PDF), sans rien inventer.",
    )
    parser.add_argument("entrees", nargs="*", type=Path, help="Dossiers, fichiers ou .zip (défaut : le dossier Input du projet)")
    parser.add_argument("--sortie", type=Path, default=DEFAULT_OUTPUT_DIR, help="Dossier de sortie (défaut : ./sortie)")
    parser.add_argument("--anonymiser", action="store_true", help="CV anonyme : initiales, ni photo, ni e-mail, ni téléphone, ni LinkedIn")
    parser.add_argument("--sans-coordonnees", action="store_true", help="Retire e-mail, téléphone, localisation et LinkedIn (nom et photo conservés)")
    parser.add_argument("--modele", default=DEFAULT_MODEL, help=f"Modèle OpenAI (défaut : {DEFAULT_MODEL})")
    parser.add_argument("--effort", default=DEFAULT_EFFORT, choices=EFFORT_CHOICES, help=f"Effort de raisonnement (défaut : {DEFAULT_EFFORT})")
    parser.add_argument("--forcer", action="store_true", help="Ré-extrait les CV même si une extraction existe déjà")
    parser.add_argument("--depuis-json", action="store_true", help="N'appelle pas l'API : réutilise les JSON de sortie/_donnees (après correction manuelle)")
    parser.add_argument("--sans-controle", action="store_true", help="Désactive la contre-vérification par un second appel au modèle")
    parser.add_argument("--pages-max", type=int, default=None, metavar="N", help="Nombre de pages maximal (défaut : 1 page si le CV d'origine tient sur une page ; sinon pages de suite réservées aux expériences)")
    parser.add_argument("--sans-pdf", action="store_true", help="Ne génère que les PPTX")
    parser.add_argument("--template", type=Path, default=None, help="Modèle PowerPoint à utiliser")
    parser.add_argument("--ouvrir", action="store_true", help="Ouvre le dossier de sortie à la fin")
    parser.add_argument("--version", action="version", version=f"cv_formatter {__version__}")
    return parser.parse_args(argv)


def default_input() -> Path:
    """Dossier d'entrée par défaut : Input, à la racine du projet (créé s'il n'existe pas)."""
    INPUT_DIR.mkdir(exist_ok=True)
    return INPUT_DIR


def _unzip(archive_path: Path, output: Path) -> list[Path]:
    target = output / "_entree_zip" / archive_path.stem
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(target)
    return sorted(p for p in target.rglob("*") if p.suffix.lower() in SUPPORTED_SUFFIXES and not p.name.startswith("~$"))


def collect_files(inputs: list[Path], output: Path) -> list[Path]:
    files: list[Path] = []
    for item in inputs:
        if item.is_dir():
            files += sorted(p for p in item.rglob("*") if p.suffix.lower() in SUPPORTED_SUFFIXES and not p.name.startswith("~$"))
            for archive in sorted(item.rglob("*.zip")):  # export Google Drive déposé tel quel
                files += _unzip(archive, output)
        elif item.suffix.lower() == ".zip" and item.exists():
            files += _unzip(item, output)
        elif item.exists() and item.suffix.lower() in SUPPORTED_SUFFIXES:
            files.append(item)
        else:
            print(f"  ! Ignoré (introuvable ou format non pris en charge) : {item}")
    seen: set[Path] = set()
    unique: list[Path] = []
    for f in files:
        key = f.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def safe_name(text: str) -> str:
    text = re.sub(r"\s*[/\\]\s*", "-", text)  # « AI/ML » -> « AI-ML » (caractère interdit sous Windows)
    return re.sub(r'[<>:"|?*\x00-\x1f]', "", text).strip().rstrip(".") or "CV"


def load_stored(job: Job) -> None:
    if job.data_file.exists():
        try:
            stored: Stored = json.loads(job.data_file.read_text(encoding="utf-8"))
            job.stored, job.cv = stored, CV.model_validate(stored["cv"])
        except Exception as exc:
            job.report.warnings.append(f"JSON existant illisible, ignoré ({exc})")
            job.stored, job.cv = None, None


def json_edited(stored: Stored | None) -> bool:
    """Le JSON enregistré a-t-il été modifié à la main depuis l'extraction ?"""
    if not stored:
        return False
    meta: dict[str, Any] = stored.get("_meta", {})
    return meta.get("empreinte") not in (None, fingerprint_data(stored.get("cv", {})))


def needs_extraction(job: "Job", forcer: bool, depuis_json: bool) -> bool:
    """Extraction à (re)faire : CV nouveau ou modifié, --forcer, ou extraction faite avec une consigne
    antérieure (sauf JSON corrigé à la main, dont les corrections priment)."""
    if job.report.error:
        return False
    if depuis_json:
        return job.cv is None
    if forcer or job.cv is None or job.stored is None or job.source is None:
        return True
    meta: dict[str, Any] = job.stored.get("_meta", {})
    if meta.get("sha256") != job.source.sha256:
        return True
    return meta.get("version_extraction") != EXTRACTION_VERSION and not json_edited(job.stored)


def photo_decision(local: FaceCheck, llm_ok: bool | None, llm_why: str, edited: bool) -> tuple[bool, str | None]:
    """Garde-t-on la photo choisie ? Renvoie (garder, avertissement éventuel).

    Détection locale d'un visage (YuNet) + avis du modèle (contre-vérification). Un logo dans le
    cadre photo est pire qu'une absence de photo : sans visage détecté ni confirmé, la photo est
    retirée (sauf JSON corrigé à la main, où elle est gardée et signalée)."""
    why = f" ({llm_why})" if llm_why else ""
    if local.found is True:
        if llm_ok is False:
            return True, f"le modèle doute qu'il s'agisse d'un visage{why}, mais un visage est détecté : vérifier"
        return True, None
    if llm_ok is True:
        return True, f"{local.detail} automatiquement, mais le modèle confirme un visage : vérifier"
    if edited:
        return True, f"{local.detail} — conservée car le JSON a été modifié à la main : vérifier"
    if local.found is None and llm_ok is None:
        return True, f"photo non contrôlée ({local.detail}) : vérifier"
    reason = local.detail if local.found is False else f"le modèle indique que ce n'est pas un visage{why}"
    return False, f"photo retirée ({reason}) — voir la vignette dans le rapport"


def cached_controle(job: Job) -> dict[str, Any] | None:
    """Contre-vérification enregistrée, si elle porte sur ces données et la version courante des contrôles."""
    if job.stored is None:
        return None
    cached: dict[str, Any] | None = job.stored.get("controle")
    if cached and cached.get("version") == CONTROLE_VERSION and cached.get("empreinte") == fingerprint_data(job.stored.get("cv", {})):
        return cached
    return None


def save_stored(job: Job) -> None:
    job.data_file.parent.mkdir(parents=True, exist_ok=True)
    job.data_file.write_text(json.dumps(job.stored, ensure_ascii=False, indent=2), encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)  # absent si la sortie est redirigée vers un objet quelconque
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")
    output: Path = args.sortie.resolve()
    output.mkdir(parents=True, exist_ok=True)
    data_dir = DATA_DIR
    template = (args.template or TEMPLATE_PATH).resolve()
    if not template.exists():
        print(f"Modèle introuvable : {template}")
        return 2

    inputs = args.entrees or [default_input()]
    files = collect_files(inputs, output)
    if not files:
        print("Aucun CV (PDF, DOCX, image ou .zip) trouvé dans :", ", ".join(str(i) for i in inputs))
        if not args.entrees:
            print(f"Déposez les CV à traiter dans le dossier {INPUT_DIR} puis relancez.")
        return 2

    print(f"CV Formatter Logiclever {__version__} — {len(files)} CV à traiter")
    try:
        if fonts.install_fonts():
            print("  Polices Lexend installées pour l'utilisateur (fermez puis rouvrez PowerPoint s'il était ouvert).")
    except Exception as exc:
        print(f"  ! Polices Lexend non installées ({exc}) : le PDF utilisera une police de remplacement.")

    jobs = [Job(f, CVReport(source=f.name), data_dir / f"{f.stem}.json") for f in files]
    has_key = api_key_available()
    client: OpenAI | None = None

    # 1. Lecture des sources (séquentiel : PyMuPDF n'est pas multi-thread).
    for job in jobs:
        try:
            # Photos toujours détectées : l'extraction mise en cache doit rester valable sans --anonymiser.
            job.source = load_source(job.path)
        except Exception as exc:
            job.report.error = f"lecture impossible : {exc}"
            continue
        load_stored(job)
        meta: dict[str, Any] = (job.stored or {}).get("_meta", {})
        if job.cv is not None and meta.get("version_extraction") != EXTRACTION_VERSION and (args.depuis_json or json_edited(job.stored)):
            job.report.warnings.append(
                "Extraction faite avec une version antérieure des consignes"
                + (" et corrigée à la main" if json_edited(job.stored) else "")
                + " : relancer avec --forcer pour appliquer les dernières règles (les corrections manuelles seraient perdues)."
            )
        if job.source.ocr_pages:
            job.report.warnings.append(
                f"Pages {job.source.ocr_pages} scannées, lues par {job.source.ocr_engine or 'aucun moteur OCR'} : "
                "les éléments douteux sont signalés au lieu d'être retirés, relisez attentivement."
            )

    # 2. Extraction (appels API en parallèle).
    to_extract = [j for j in jobs if needs_extraction(j, args.forcer, args.depuis_json)]
    if to_extract and (args.depuis_json or not has_key):
        for job in to_extract:
            job.report.error = (
                "aucune extraction enregistrée pour ce CV (--depuis-json)" if args.depuis_json
                else "clé OpenAI absente : ajoutez OPENAI_API_KEY=... dans le fichier .env à la racine du projet"
            )
        to_extract = []
    if to_extract:
        client = api = make_client()
        print(f"  Extraction de {len(to_extract)} CV avec {args.modele} (effort {args.effort})…")

        def extract(job: Job) -> None:
            source = job.source
            if source is None:
                return
            try:
                cv, stats = extract_cv(api, source, args.modele, args.effort)
            except Exception as exc:
                job.report.error = f"extraction impossible : {exc}"
                return
            job.cv = cv
            job.stats = stats
            job.stored = {
                "_meta": {
                    "source": job.path.name,
                    "sha256": source.sha256,
                    "extracteur": args.modele,
                    "effort": args.effort,
                    "version_extraction": EXTRACTION_VERSION,
                    "date": dt.datetime.now().isoformat(timespec="seconds"),
                    "empreinte": fingerprint(cv),
                    "outil": f"cv_formatter {__version__}",
                    **stats,
                },
                "cv": cv.model_dump(),
            }
            save_stored(job)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(extract, to_extract))

    # 3. Contre-vérification (second appel, mis en cache dans le JSON).
    cross_enabled = not args.sans_controle
    ready = [j for j in jobs if not j.report.error and j.cv is not None]
    pending: list[Job] = []
    for job in ready:
        if cached_controle(job):
            continue
        if cross_enabled and has_key and not args.depuis_json:
            pending.append(job)
    if pending:
        checker = client or make_client()
        print(f"  Contre-vérification de {len(pending)} CV…")

        def cross(job: Job) -> None:
            if job.source is None or job.cv is None or job.stored is None:
                return
            try:
                verdicts = run_cross_check(checker, job.source, job.cv, args.modele, args.effort)
            except Exception as exc:
                job.report.warnings.append(f"Contre-vérification impossible : {exc}")
                return
            job.stored["controle"] = {
                "version": CONTROLE_VERSION,
                "empreinte": fingerprint_data(job.stored.get("cv", {})),
                "modele": args.modele,
                "date": dt.datetime.now().isoformat(timespec="seconds"),
                "verdicts": verdicts_to_json(verdicts),
            }
            save_stored(job)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(cross, pending))

    # 4. Contrôles, mise en page, PPTX.
    used_names: set[str] = set()
    photo_dir = data_dir / "photos"
    for job in ready:
        report, source, extracted = job.report, job.source, job.cv
        if source is None or extracted is None:
            continue
        meta = (job.stored or {}).get("_meta", {})
        try:
            raw = extracted.model_copy(deep=True)
            edited = json_edited(job.stored)
            apply_removals = source.text_is_reliable and not edited
            extractor = meta.get("extracteur", "?")
            report.extractor = extractor + (f" (effort {meta['effort']})" if meta.get("effort") else "")
            if edited:
                report.extractor += " — JSON modifié à la main : modifications conservées, alertes seulement"

            cross_findings: list[Finding] = []
            cached = cached_controle(job)
            if cached:
                cross_findings = apply_verdicts(raw, verdicts_from_json(cached["verdicts"]), apply_removals)
                report.cross_check = f"effectuée par {cached.get('modele')} le {cached.get('date', '?')[:10]}"
            elif not cross_enabled:
                report.cross_check = "désactivée (--sans-controle)"
            else:
                report.cross_check = "non effectuée (clé OpenAI absente)" if not has_key else "non effectuée"
            # L'employeur et le client doivent être nommés ailleurs que dans l'en-tête, les coordonnées ou
            # les pieds de page (un CV mis en forme par une ESN ne fait pas de l'ESN l'employeur).
            body = source.body_text(raw.prenom, raw.nom, raw.titre)
            checked, findings = verify(raw, source.reference_text, source.links, apply_removals, body, source.linkedin_roles)
            report.findings = cross_findings + findings
            report.skipped_sections = list(checked.sections_non_reprises)

            label, origin, years_warning = contenu.experience_label(checked, source.document_date)
            report.experience = f"« {label} » — {origin}" if label else origin
            if years_warning:
                report.warnings.append(years_warning)

            photo_path = None
            if args.anonymiser:
                report.photo = "retirée (anonymisation)"
            elif checked.photo_candidate and 1 <= checked.photo_candidate <= len(source.candidates):
                candidate = source.candidates[checked.photo_candidate - 1]
                where = f"image n° {candidate.index}, page {candidate.page + 1}"
                saved = save_photo(candidate, photo_dir / f"{job.path.stem}.jpg")
                report.photo_file = saved
                local = check_face(square_photo(candidate.image))
                cached = cached_controle(job)
                llm_ok, llm_why = photo_verdict(verdicts_from_json(cached["verdicts"])) if cached else (None, "")
                keep, warning = photo_decision(local, llm_ok, llm_why, edited)
                if keep:
                    photo_path = saved
                    report.photo = f"reprise du CV ({where}) — {local.detail or 'non contrôlée'}"
                else:
                    report.photo_rejected = True
                    report.photo = f"retirée : l'image choisie ({where}) ne semble pas être un visage"
                if warning:
                    report.warnings.append(f"Photo : {warning}")
            elif source.candidates:
                report.photo = f"aucune retenue ({len(source.candidates)} image(s) écartée(s) : pas une photo du consultant)"
                faces = [c for c in source.candidates if check_face(square_photo(c.image)).found]
                if faces:
                    report.warnings.append(
                        f"Photo : {len(faces)} image(s) écartée(s) contiennent un visage — vérifier qu'il ne manque pas la photo du consultant"
                    )
            else:
                report.photo = "aucune photo dans le CV"

            final = anonymize(checked) if args.anonymiser else checked
            if args.anonymiser:
                name = initials(checked)
                # Le titre court distingue deux consultants aux mêmes initiales.
                short_title = re.split(r" [—–|-] |, ", contenu.tame_caps(contenu.clean_item(checked.titre)))[0][:40].strip() if checked.titre else ""
                base = f"CV Logiclever - {name}" + (f" - {short_title}" if short_title else "") + " (anonyme)"
                contact: dict[str, str | None] = {"email": None, "telephone": None, "linkedin": None, "localisation": final.contact.localisation}
            else:
                name = display_name(checked)
                base = f"CV Logiclever - {name}"
                contact = final.contact.model_dump()
            if args.sans_coordonnees:
                contact = {k: None for k in contact}
            base = safe_name(base)
            candidate_name, counter = base, 2
            while candidate_name.lower() in used_names:
                candidate_name = f"{base} ({counter})"
                counter += 1
            used_names.add(candidate_name.lower())
            report.name = name

            target = output / f"{candidate_name}.pptx"
            result = render_cv(
                final,
                photo_path,
                template,
                target,
                experience_label=label,
                contact=contact,
                name=name,
                document_title="CV Logiclever" + (" (anonyme)" if args.anonymiser else f" - {name}"),
                pages_max=args.pages_max,
                source_pages=source.page_count,
            )
            job.pptx = target
            report.pptx = target
            report.pages = result.pages
            report.layout_notes = result.notes
        except PermissionError:
            report.error = "fichier de sortie verrouillé : fermez-le dans PowerPoint puis relancez"
        except Exception as exc:
            report.error = f"génération impossible : {type(exc).__name__}: {exc}"

    # 5. PDF.
    produced = [j.pptx for j in jobs if j.pptx]
    if produced and not args.sans_pdf:
        print(f"  Export PDF de {len(produced)} CV…")
        errors = export_pdfs(produced)
        for job in jobs:
            if job.pptx is None:
                continue
            if job.pptx.resolve() in errors:
                job.report.warnings.append(f"PDF non généré : {errors[job.pptx.resolve()]}")
            elif job.pptx.with_suffix(".pdf").exists():
                job.report.pdf = job.pptx.with_suffix(".pdf")

    # 6. Rapport.
    cross_state = "activée" if cross_enabled and has_key else ("désactivée" if not cross_enabled else "impossible (clé OpenAI absente)")
    settings = (
        f"modèle : {args.modele} (effort {args.effort}) · contre-vérification : {cross_state}"
        f" · anonymisation : {'oui' if args.anonymiser else 'non'} · le détail par CV figure ci-dessous"
    )
    report_path = write_report([j.report for j in jobs], output / "rapport.md", settings)
    print()
    for job in jobs:
        r = job.report
        if r.error:
            print(f"  ✗ {r.source} : {r.error}")
        else:
            details = f"{r.pages} p., {len(r.removed)} retiré(s), {len(r.to_check)} à vérifier"
            print(f"  ✓ {r.name} ({details}) ← {r.source}")
    print(f"\nRésultats : {output}\nRapport   : {report_path}")
    if args.ouvrir and os.name == "nt":
        os.startfile(output)  # noqa: S606 — ouverture de l'explorateur
    return 0 if all(not j.report.error for j in jobs) else 1


def main() -> None:
    sys.exit(run(parse_args()))


if __name__ == "__main__":
    main()
