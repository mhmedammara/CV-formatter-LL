"""Contre-vérification par un second appel au modèle.

Chaque affirmation extraite (poste + entreprise + dates, chaque réalisation, compétence,
certification, diplôme…) est numérotée et confrontée au PDF d'origine. Ce contrôle
complète le contrôle déterministe : il repère notamment les réalisations rattachées à la
mauvaise expérience ou les dates déformées.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from .extraction import check_response, pdf_part
from .pdf_source import SourceDocument
from .schema import CV, Controle, Verdict
from .verification import Finding

PROMPT = """\
Tu es contrôleur qualité chez Logiclever. On te donne un CV (PDF) et une liste d'affirmations \
numérotées extraites de ce CV (parfois traduites en français ou légèrement retouchées : \
orthographe, ponctuation). Pour CHAQUE identifiant, vérifie l'affirmation dans le CV :
- "confirme" : l'information figure dans le CV (formulation proche ou traduction fidèle) ;
- "absent" : l'information ne figure nulle part dans le CV ;
- "inexact" : elle figure mais est déformée (chiffre, date, nom, entreprise, rattachement à une \
autre expérience, sens modifié).
Sois strict sur les chiffres, les dates, les noms et les rattachements. Réponds pour chaque \
identifiant, sans en oublier, avec une explication courte quand ce n'est pas confirmé.
"""


CONTROLE_VERSION = 3  # à incrémenter quand la liste des affirmations change (invalide le cache)


@dataclass
class Claim:
    id: str
    text: str
    path: tuple  # emplacement dans le CV (pour le retrait)
    label: str


PHOTO_CLAIM = "PHOTO"  # affirmation ajoutée seulement si une photo est retenue (n'invalide pas le cache)


def normalize_id(raw: str) -> str:
    """Le modèle recopie parfois l'identifiant avec ses crochets (« [E0] ») : on les retire."""
    return raw.strip().strip("[]").strip()


def build_claims(cv: CV, with_photo: bool = False) -> list[Claim]:
    claims: list[Claim] = []
    if with_photo:
        claims.append(Claim(PHOTO_CLAIM, "L'image jointe (photo retenue pour le CV) est une photographie du visage d'une personne, et non un logo, un badge, une icône ou une illustration.", ("photo_candidate",), "Photo"))
    if cv.titre:
        claims.append(Claim("T", f"Titre du consultant : « {cv.titre} »", ("titre",), "Titre"))
    if cv.annees_experience.valeur:
        claims.append(Claim("A", f"Le CV indique {cv.annees_experience.valeur} ans d'expérience (« {cv.annees_experience.citation} »)", ("annees_experience",), "Années d'expérience"))
    for i, exp in enumerate(cv.experiences):
        name = " — ".join(x for x in (exp.poste, exp.entreprise) if x) or f"expérience {i + 1}"
        head = ", ".join(
            x for x in (
                f"poste « {exp.poste} »" if exp.poste else "",
                f"employeur « {exp.entreprise} »" if exp.entreprise else "",
                f"client « {exp.client} »" if exp.client else "",
                f"lieu « {exp.lieu} »" if exp.lieu else "",
                f"période « {exp.periode_texte} »" if exp.periode_texte else "",
            ) if x
        )
        claims.append(Claim(f"E{i}", f"Expérience : {head}", ("experiences", i), name))
        # Nature contrôlée à part (jamais de retrait) : on cherche une alternance ou un stage non repéré,
        # qui gonflerait les années d'expérience.
        if exp.type not in ("alternance", "stage"):
            claims.append(Claim(f"E{i}.T", f"Le CV ne présente pas l'expérience « {name} » comme une alternance, un apprentissage ou un stage.", ("experiences", i, "type"), f"{name} — Nature"))
        if exp.contexte:
            claims.append(Claim(f"E{i}.C", f"Dans l'expérience « {name} », contexte : « {exp.contexte} »", ("experiences", i, "contexte"), f"{name} — Contexte"))
        for j, item in enumerate(exp.realisations):
            claims.append(Claim(f"E{i}.R{j}", f"Dans l'expérience « {name} » : « {item} »", ("experiences", i, "realisations", j), f"{name} — Réalisation"))
        for j, item in enumerate(exp.environnement):
            claims.append(Claim(f"E{i}.V{j}", f"Outil ou technologie cité pour l'expérience « {name} » : « {item} »", ("experiences", i, "environnement", j), f"{name} — Environnement"))
    for i, prj in enumerate(cv.projets):
        claims.append(Claim(f"P{i}", f"Projet « {prj.nom} »" + (f" ({prj.cadre})" if prj.cadre else "") + (f", période « {prj.periode_texte} »" if prj.periode_texte else ""), ("projets", i), f"Projet {prj.nom}"))
        for j, item in enumerate(prj.description):
            claims.append(Claim(f"P{i}.D{j}", f"Dans le projet « {prj.nom} » : « {item} »", ("projets", i, "description", j), f"Projet {prj.nom}"))
    for key, label, items in (("X", "Domaine d'expertise", cv.expertise), ("M", "Méthode", cv.methodes), ("S", "Outil / technologie", cv.outils_si)):
        attr = {"X": "expertise", "M": "methodes", "S": "outils_si"}[key]
        for k, item in enumerate(items):
            claims.append(Claim(f"{key}{k}", f"{label} cité dans le CV : « {item} »", (attr, k), label))
    for i, cat in enumerate(cv.competences_detaillees):
        for j, item in enumerate(cat.elements):
            claims.append(Claim(f"D{i}.{j}", f"Compétence de la catégorie « {cat.categorie} » : « {item} »", ("competences_detaillees", i, "elements", j), f"Compétences « {cat.categorie} »"))
    for i, lang in enumerate(cv.langues):
        claims.append(Claim(f"L{i}", f"Langue : {lang.langue}" + (f", niveau « {lang.niveau} »" if lang.niveau else ""), ("langues", i), "Langue"))
    for i, cert in enumerate(cv.certifications):
        extra = ", ".join(x for x in (cert.organisme, cert.date) if x)
        claims.append(Claim(f"C{i}", f"Certification « {cert.intitule} »" + (f" ({extra})" if extra else ""), ("certifications", i), "Certification"))
    for i, form in enumerate(cv.formations):
        extra = ", ".join(x for x in (form.etablissement, form.lieu, form.periode) if x)
        claims.append(Claim(f"F{i}", f"Formation « {form.diplome} »" + (f" ({extra})" if extra else ""), ("formations", i), "Formation"))
    return claims


def run_cross_check(client, source: SourceDocument, cv: CV, model: str, effort: str) -> list[Verdict]:
    photo = None
    if cv.photo_candidate and 1 <= cv.photo_candidate <= len(source.candidates):
        photo = source.candidates[cv.photo_candidate - 1]
    claims = build_claims(cv, with_photo=photo is not None)
    if not claims:
        return []
    listing = "\n".join(f"[{c.id}] {c.text}" for c in claims)
    content = [pdf_part(source), {"type": "input_text", "text": "Affirmations à contrôler :\n" + listing}]
    if photo is not None:
        data = base64.b64encode(photo.thumbnail_png()).decode("ascii")
        content += [
            {"type": "input_text", "text": f"Image jointe pour l'affirmation [{PHOTO_CLAIM}] :"},
            {"type": "input_image", "image_url": f"data:image/png;base64,{data}"},
        ]
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


def _delete(cv: CV, path: tuple) -> None:
    """Supprime l'élément désigné par `path` (liste) ou met le champ à None."""
    data = cv
    for step in path[:-1]:
        data = data[step] if isinstance(step, int) else getattr(data, step)
    last = path[-1]
    if isinstance(last, int):
        data.pop(last)
    elif last == "annees_experience":
        cv.annees_experience.valeur, cv.annees_experience.citation = None, None
    else:
        setattr(data, last, None)


def apply_verdicts(cv: CV, verdicts: list[Verdict], apply_removals: bool) -> list[Finding]:
    claims = {c.id: c for c in build_claims(cv)}
    findings: list[Finding] = []
    to_delete: list[tuple] = []
    for verdict in verdicts:
        claim = claims.get(normalize_id(verdict.id))
        if claim is None or verdict.statut == "confirme":
            continue
        if claim.path[-1] == "type":
            if verdict.statut == "inexact":  # le CV la présente comme alternance/stage : hors décompte
                cv.experiences[claim.path[1]].type = "autre"
                findings.append(Finding(claim.path, claim.label, claim.text, "contre-vérification : présentée comme alternance ou stage dans le CV — exclue du calcul des années d'expérience" + (f" ({verdict.explication})" if verdict.explication else ""), "à vérifier"))
            continue
        # Une expérience ou un projet entier regroupe plusieurs faits (poste, employeur, dates) : un
        # verdict négatif est seulement signalé ; les champs inventés sont retirés un à un par le
        # contrôle déterministe. Idem pour la nature (alternance/stage), qui n'est jamais retirée.
        whole_entry = len(claim.path) == 2 and claim.path[0] in ("experiences", "projets")
        nature = claim.path[-1] == "type"
        remove = verdict.statut == "absent" and apply_removals and not whole_entry and not nature
        reason = ("contre-vérification : introuvable dans le CV" if verdict.statut == "absent" else "contre-vérification : inexact")
        if verdict.explication:
            reason += f" ({verdict.explication})"
        findings.append(Finding(claim.path, claim.label, claim.text, reason, "retiré" if remove else "à vérifier"))
        if remove:
            to_delete.append(claim.path)
    # Un élément dont le parent est déjà retiré n'a pas à être traité ; on supprime ensuite
    # dans l'ordre inverse pour que les indices restants ne se décalent pas.
    paths = set(to_delete)
    paths = {p for p in paths if not any(len(o) < len(p) and p[: len(o)] == o for o in paths)}
    sort_key = lambda p: [(1, x) if isinstance(x, int) else (0, x) for x in p]  # noqa: E731
    for path in sorted(paths, key=sort_key, reverse=True):
        try:
            _delete(cv, path)
        except (IndexError, AttributeError, KeyError):
            continue
    return findings


def photo_verdict(verdicts: list[Verdict]) -> tuple[bool | None, str]:
    """Avis du modèle sur la photo retenue : True (visage), False (pas un visage), None (pas d'avis)."""
    for verdict in verdicts:
        if normalize_id(verdict.id) == PHOTO_CLAIM:
            return verdict.statut == "confirme", verdict.explication
    return None, ""


def verdicts_to_json(verdicts: list[Verdict]) -> list[dict]:
    return [v.model_dump() for v in verdicts]


def verdicts_from_json(data: list[dict]) -> list[Verdict]:
    return [Verdict.model_validate(d) for d in data]


def fingerprint(cv: CV) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(cv.model_dump(), sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
