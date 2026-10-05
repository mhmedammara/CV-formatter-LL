"""Contre-vérification par un second appel au modèle.

Chaque affirmation extraite (poste, dates, employeur, client et lieu de chaque expérience, chaque
réalisation, compétence, certification, diplôme…) est numérotée et confrontée au PDF d'origine.
Ce contrôle complète le contrôle déterministe : il repère notamment les réalisations rattachées à
la mauvaise expérience, les dates déformées et les employeurs ou clients déduits.

Les CV produits partent souvent sans relecture : une affirmation contestée est retirée (ou
l'employeur détaché), pas seulement signalée.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from openai.types.responses import ResponseInputContentParam

from .config import Effort
from .extraction import check_response, pdf_part
from .pdf_source import SourceDocument
from .schema import CV, Controle, Experience, Verdict
from .verification import FieldPath, Finding

if TYPE_CHECKING:
    from openai import OpenAI

PROMPT = """\
Tu es contrôleur qualité chez Logiclever. On te donne un CV (PDF) et une liste d'affirmations \
numérotées extraites de ce CV (parfois traduites en français ou légèrement retouchées : \
orthographe, ponctuation). Pour CHAQUE identifiant, vérifie l'affirmation dans le CV :
- "confirme" : l'information figure dans le CV (formulation proche ou traduction fidèle) ;
- "absent" : l'information ne figure nulle part dans le CV ;
- "inexact" : elle figure mais est déformée (chiffre, date, nom, entreprise, rattachement à une \
autre expérience, sens modifié).
Sois strict sur les chiffres, les dates, les noms et les rattachements. En revanche, une traduction \
fidèle, un intitulé recopié d'une autre langue ou une information regroupée comme demandé (un score TOEIC, \
TOEFL ou IELTS, qui atteste de l'anglais, rattaché à l'anglais) ne sont pas des erreurs. Une compétence ou \
un domaine que le CV ne présente que comme un souhait ou un objectif (« je cherche », « je souhaite ») \
n'est pas confirmé : réponds "inexact".
Employeur et client : confirme seulement si le bloc de l'expérience lui-même (ou le titre de rubrique \
qui regroupe ses missions) nomme cette organisation dans ce rôle. Un nom présent seulement dans \
l'en-tête du CV (nom, titre du consultant), le logo, les coordonnées, une adresse e-mail ou le pied de \
page ne prouve pas que la personne a travaillé pour cette organisation : réponds "absent". Si le CV \
nomme l'organisation dans un autre rôle (un client présenté comme employeur, ou l'inverse), réponds \
"inexact".
Réponds pour chaque identifiant, sans en oublier, avec une explication courte quand ce n'est pas confirmé.
"""


CONTROLE_VERSION = 8  # à incrémenter quand la liste des affirmations change (invalide le cache)


@dataclass
class Claim:
    id: str
    text: str
    path: FieldPath  # emplacement dans le CV (pour le retrait)
    label: str


PHOTO_CLAIM = "PHOTO"  # affirmation ajoutée seulement si une photo est retenue (n'invalide pas le cache)

# Champs de l'en-tête d'une expérience contrôlés un à un : un verdict négatif vide le champ concerné
# (l'expérience elle-même n'est jamais retirée par la contre-vérification).
EXPERIENCE_FIELDS = ("poste", "periode_texte", "entreprise", "client", "lieu")


def normalize_id(raw: str) -> str:
    """Le modèle recopie parfois l'identifiant avec ses crochets (« [E0] ») : on les retire."""
    return raw.strip().strip("[]").strip()


def experience_name(exp: Experience, index: int) -> str:
    return " — ".join(x for x in (exp.poste, exp.entreprise or exp.client) if x) or f"expérience {index + 1}"


def _where(exp: Experience, index: int, without: tuple[str, ...] = ()) -> str:
    """Désigne l'expérience pour le contrôleur sans utiliser le fait contrôlé (pas de raisonnement circulaire)."""
    parts: list[str] = []
    if "poste" not in without and exp.poste:
        parts.append(f"« {exp.poste} »")
    if "org" not in without:
        # « chez » pour l'employeur seulement : « chez <client> » ferait croire que le client employait la personne.
        if exp.entreprise:
            parts.append(f"chez « {exp.entreprise} »")
        elif exp.client:
            parts.append(f"pour le client « {exp.client} »")
    if "periode" not in without and exp.periode_texte:
        parts.append(f"({exp.periode_texte})")
    return "l'expérience " + (" ".join(parts) if parts else f"n° {index + 1} de la liste")


def build_claims(cv: CV, with_photo: bool = False) -> list[Claim]:
    claims: list[Claim] = []
    if with_photo:
        claims.append(Claim(PHOTO_CLAIM, "L'image jointe (photo retenue pour le CV) est une photographie du visage d'une personne, et non un logo, un badge, une icône ou une illustration.", ("photo_candidate",), "Photo"))
    if cv.titre:
        claims.append(Claim("T", f"Titre du consultant : « {cv.titre} »", ("titre",), "Titre"))
    if cv.annees_experience.valeur:
        claims.append(Claim("A", f"Le CV indique {cv.annees_experience.valeur} ans d'expérience (« {cv.annees_experience.citation} »)", ("annees_experience",), "Années d'expérience"))
    for i, exp in enumerate(cv.experiences):
        name = experience_name(exp, i)
        base = ("experiences", i)
        where = _where(exp, i)
        if exp.poste:
            claims.append(Claim(f"E{i}.P", f"Dans {_where(exp, i, ('poste',))}, le poste occupé est « {exp.poste} ».", base + ("poste",), f"{name} — Poste"))
        if exp.periode_texte:
            claims.append(Claim(f"E{i}.D", f"Dans le CV, {_where(exp, i, ('periode',))} est datée « {exp.periode_texte} ».", base + ("periode_texte",), f"{name} — Période"))
        if exp.entreprise:
            claims.append(Claim(f"E{i}.E", f"« {exp.entreprise} » est présentée comme l'organisation de {_where(exp, i, ('org',))} (employeur, société ou association), dans l'en-tête de l'expérience ou dans le titre de rubrique qui la regroupe — et non seulement citée dans sa description ou comme client d'une autre organisation.", base + ("entreprise",), f"{name} — Employeur"))
        if exp.client:
            claims.append(Claim(f"E{i}.K", f"Le bloc de {_where(exp, i, ('org',))} nomme « {exp.client} » comme client (entreprise pour laquelle la mission a été réalisée).", base + ("client",), f"{name} — Client"))
        if exp.lieu:
            claims.append(Claim(f"E{i}.L", f"Selon le CV, {where} s'est déroulée à « {exp.lieu} ».", base + ("lieu",), f"{name} — Lieu"))
        # Nature contrôlée à part (jamais de retrait) : on cherche une alternance ou un stage non repéré,
        # qui gonflerait les années d'expérience.
        if exp.type not in ("alternance", "stage"):
            claims.append(Claim(f"E{i}.T", f"Le CV ne présente pas {where} comme une alternance, un apprentissage ou un stage.", base + ("type",), f"{name} — Nature"))
        if exp.type == "freelance":  # affiché « Freelance » à côté des dates
            claims.append(Claim(f"E{i}.F", f"Le CV présente {where} comme une activité en freelance / indépendant.", base + ("freelance",), f"{name} — Freelance"))
        if exp.contexte:
            claims.append(Claim(f"E{i}.C", f"Dans {where}, contexte : « {exp.contexte} »", base + ("contexte",), f"{name} — Contexte"))
        for j, item in enumerate(exp.realisations):
            claims.append(Claim(f"E{i}.R{j}", f"Dans {where} : « {item} »", base + ("realisations", j), f"{name} — Réalisation"))
    for i, prj in enumerate(cv.projets):
        claims.append(Claim(f"P{i}", f"Projet « {prj.nom} »" + (f" ({prj.cadre})" if prj.cadre else "") + (f", période « {prj.periode_texte} »" if prj.periode_texte else ""), ("projets", i), f"Projet {prj.nom}"))
        for j, item in enumerate(prj.description):
            claims.append(Claim(f"P{i}.D{j}", f"Dans le projet « {prj.nom} » : « {item} »", ("projets", i, "description", j), f"Projet {prj.nom}"))
    for key, label, attr in (("X", "Domaine d'expertise", "expertise"), ("S", "Outil / technologie", "outils_si")):
        for k, item in enumerate(getattr(cv, attr)):
            claims.append(Claim(f"{key}{k}", f"{label} cité dans le CV : « {item} »", (attr, k), label))
    for i, cat in enumerate(cv.competences_detaillees):
        for j, item in enumerate(cat.elements):
            claims.append(Claim(f"D{i}.{j}", f"Compétence de la catégorie « {cat.categorie} » : « {item} »", ("competences_detaillees", i, "elements", j), f"Compétences « {cat.categorie} »"))
    for i, lang in enumerate(cv.langues):  # la langue et son niveau à part : un niveau contesté ne retire pas la langue
        claims.append(Claim(f"L{i}", f"Le CV mentionne la langue « {lang.langue} »", ("langues", i), "Langue"))
        if lang.niveau:
            claims.append(Claim(f"L{i}.N", f"Niveau en {lang.langue} selon le CV : « {lang.niveau} »", ("langues", i, "niveau"), f"Langue {lang.langue} — niveau"))
    for i, cert in enumerate(cv.certifications):
        extra = ", ".join(x for x in (cert.organisme, cert.date) if x)
        claims.append(Claim(f"C{i}", f"Certification « {cert.intitule} »" + (f" ({extra})" if extra else ""), ("certifications", i), "Certification"))
    for i, form in enumerate(cv.formations):
        extra = ", ".join(x for x in (form.etablissement, form.lieu, form.periode) if x)
        claims.append(Claim(f"F{i}", f"Formation « {form.diplome} »" + (f" ({extra})" if extra else ""), ("formations", i), "Formation"))
    return claims


def run_cross_check(client: OpenAI, source: SourceDocument, cv: CV, model: str, effort: Effort) -> list[Verdict]:
    photo = None
    if cv.photo_candidate and 1 <= cv.photo_candidate <= len(source.candidates):
        photo = source.candidates[cv.photo_candidate - 1]
    claims = build_claims(cv, with_photo=photo is not None)
    if not claims:
        return []
    listing = "\n".join(f"[{c.id}] {c.text}" for c in claims)
    content: list[ResponseInputContentParam] = [pdf_part(source), {"type": "input_text", "text": "Affirmations à contrôler :\n" + listing}]
    if photo is not None:
        data = base64.b64encode(photo.thumbnail_png()).decode("ascii")
        content.append({"type": "input_text", "text": f"Image jointe pour l'affirmation [{PHOTO_CLAIM}] :"})
        content.append({"type": "input_image", "image_url": f"data:image/png;base64,{data}", "detail": "auto"})
    response = client.responses.parse(
        model=model,
        instructions=PROMPT,
        input=[{"role": "user", "content": content}],
        text_format=Controle,
        reasoning={"effort": effort},
        max_output_tokens=64000,
        store=False,
    )
    check_response(response)
    return list(response.output_parsed.verdicts) if response.output_parsed else []


def _delete(cv: CV, path: FieldPath) -> None:
    """Supprime l'élément désigné par `path` (liste) ou met le champ à None."""
    data: Any = cv  # parcours générique du modèle (attributs puis éléments de liste)
    for step in path[:-1]:
        data = data[step] if isinstance(step, int) else getattr(data, step)
    last = path[-1]
    if isinstance(last, int):
        data.pop(last)
    elif last == "annees_experience":
        cv.annees_experience.valeur, cv.annees_experience.citation = None, None
    else:
        setattr(data, last, None)


def _reason(verdict: Verdict) -> str:
    base = "contre-vérification : introuvable dans le CV" if verdict.statut == "absent" else "contre-vérification : inexact"
    return base + (f" ({verdict.explication})" if verdict.explication else "")


def _correct_experience(exp: Experience, bad: dict[str, tuple[Claim, Verdict]], apply: bool) -> list[Finding]:
    """Vide les champs contestés de l'en-tête d'une expérience.

    Employeur non nommé dans l'expérience (ex. l'ESN qui a mis le CV en forme, citée seulement dans le
    titre) : il est retiré, et le client — que le bloc nomme, lui — devient l'entreprise affichée."""
    findings: list[Finding] = []
    action = "retiré" if apply else "à vérifier"

    def note(attr: str, extra: str = "") -> None:
        claim, verdict = bad[attr]
        findings.append(Finding(claim.path, claim.label, getattr(exp, attr) or "", _reason(verdict) + extra, action))

    if "entreprise" in bad:
        client_absent = "client" in bad and bad["client"][1].statut == "absent"
        promote = bool(exp.client) and not client_absent
        note("entreprise", f" — employeur retiré ; « {exp.client} » affiché comme entreprise" if promote else " — employeur retiré")
        if "client" in bad and not promote:
            note("client")
        if apply:
            if promote:
                exp.entreprise, exp.client = exp.client, None
            else:
                exp.entreprise = None
                if "client" in bad:
                    exp.client = None
    elif "client" in bad:
        note("client")
        if apply:
            exp.client = None
    if "periode_texte" in bad:
        note("periode_texte", " — dates retirées")
        if apply:
            exp.periode_texte, exp.debut, exp.fin = None, None, None
    for attr in ("lieu", "poste"):
        if attr in bad:
            note(attr)
            if apply:
                setattr(exp, attr, None)
    return findings


def apply_verdicts(cv: CV, verdicts: list[Verdict], apply_removals: bool) -> list[Finding]:
    claims = {c.id: c for c in build_claims(cv)}
    findings: list[Finding] = []
    to_delete: list[FieldPath] = []
    headers: dict[int, dict[str, tuple[Claim, Verdict]]] = {}
    for verdict in verdicts:
        claim = claims.get(normalize_id(verdict.id))
        if claim is None or verdict.statut == "confirme":
            continue
        last = claim.path[-1]
        if last == "type":
            if verdict.statut == "inexact":  # le CV la présente comme alternance/stage : hors décompte
                cv.experiences[int(claim.path[1])].type = "autre"
                findings.append(Finding(claim.path, claim.label, claim.text, "contre-vérification : présentée comme alternance ou stage dans le CV — exclue du calcul des années d'expérience" + (f" ({verdict.explication})" if verdict.explication else ""), "à vérifier"))
            continue
        if last == "freelance":  # mention « Freelance » non prouvée : l'expérience reste comptée, sans la mention
            if apply_removals:
                cv.experiences[int(claim.path[1])].type = "emploi"
            findings.append(Finding(claim.path, claim.label, "Freelance", _reason(verdict) + " — mention « Freelance » retirée", "retiré" if apply_removals else "à vérifier"))
            continue
        if claim.path[0] == "experiences" and len(claim.path) == 3 and last in EXPERIENCE_FIELDS:
            headers.setdefault(int(claim.path[1]), {})[str(last)] = (claim, verdict)
            continue
        # Un projet entier regroupe plusieurs faits : un verdict négatif est seulement signalé. Un élément
        # atomique (puce, outil, certification…) absent ou déformé est retiré : mieux vaut un CV plus court
        # qu'une information fausse envoyée au client sans relecture.
        whole_entry = len(claim.path) == 2 and claim.path[0] in ("experiences", "projets")
        remove = apply_removals and not whole_entry
        findings.append(Finding(claim.path, claim.label, claim.text, _reason(verdict), "retiré" if remove else "à vérifier"))
        if remove:
            to_delete.append(claim.path)
    for index, bad in sorted(headers.items()):
        findings += _correct_experience(cv.experiences[index], bad, apply_removals)
    # Un élément dont le parent est déjà retiré n'a pas à être traité ; on supprime ensuite
    # dans l'ordre inverse pour que les indices restants ne se décalent pas.
    paths = set(to_delete)
    paths = {p for p in paths if not any(len(o) < len(p) and p[: len(o)] == o for o in paths)}
    for path in sorted(paths, key=_path_order, reverse=True):
        try:
            _delete(cv, path)
        except (IndexError, AttributeError, KeyError):
            continue
    return findings


def _path_order(path: FieldPath) -> list[tuple[int, str, int]]:
    """Clé de tri d'un chemin : les indices de liste sont comparés comme des nombres."""
    return [(1, "", step) if isinstance(step, int) else (0, step, 0) for step in path]


def photo_verdict(verdicts: list[Verdict]) -> tuple[bool | None, str]:
    """Avis du modèle sur la photo retenue : True (visage), False (pas un visage), None (pas d'avis)."""
    for verdict in verdicts:
        if normalize_id(verdict.id) == PHOTO_CLAIM:
            return verdict.statut == "confirme", verdict.explication
    return None, ""


def verdicts_to_json(verdicts: list[Verdict]) -> list[dict[str, Any]]:
    return [v.model_dump() for v in verdicts]


def verdicts_from_json(data: list[dict[str, Any]]) -> list[Verdict]:
    return [Verdict.model_validate(d) for d in data]


def fingerprint_data(data: dict[str, Any]) -> str:
    """Empreinte du JSON enregistré tel quel : un champ ajouté ou retiré du schéma par une nouvelle
    version de l'outil ne fait pas passer le fichier pour « modifié à la main »."""
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def fingerprint(cv: CV) -> str:
    return fingerprint_data(cv.model_dump())
