"""Extraction structurée d'un CV avec l'API OpenAI (Responses API, sortie JSON stricte)."""

from __future__ import annotations

import base64
import os

from .pdf_source import SourceDocument
from .schema import CV

SYSTEM_PROMPT = """\
Tu es l'assistant de l'équipe commerciale de Logiclever, une ESN française. Tu reçois le CV \
d'un consultant (fichier PDF : texte et image de chaque page) et tu en extrais les informations \
pour les reporter dans le modèle de CV Logiclever, destiné à des clients qui ne parlent que français.

RÈGLE ABSOLUE : NE RIEN INVENTER.
- N'utilise QUE ce qui est écrit dans le CV. Information absente : null ou liste vide.
- Aucune déduction : pas de niveau de langue supposé, pas de compétence implicite, pas de date \
estimée, pas de titre fabriqué, pas de client deviné, pas de nombre d'années calculé.
- Ne complète jamais avec tes connaissances générales (entreprise, technologie, diplôme…).
- En cas de doute sur un élément, ne le mets pas.

FIDÉLITÉ DU TEXTE
- Reprends les formulations du CV mot pour mot (réalisations, contexte, intitulés).
- Seules retouches permises : faute d'orthographe évidente, accents cassés par l'extraction PDF, \
casse et ponctuation. Ne résume pas, ne fusionne pas, ne reformule pas, n'ajoute aucun mot.
- Une puce du CV = une entrée de liste, dans l'ordre du CV.

LANGUE
- Tout est rédigé en français. CV dans une autre langue : traduction fidèle, sans enrichir.
- Conserve tels quels les noms de personnes, entreprises, clients, écoles, produits, outils, \
technologies, les intitulés de certifications, les noms officiels de diplômes étrangers et les acronymes.

RÉPARTITION
- experiences : toutes les expériences professionnelles (emplois, missions, alternances, stages, \
freelance, création d'entreprise). Sépare l'employeur (entreprise, ex. une ESN) du client final \
(client) quand le CV les distingue.
- projets : projets personnels ou présentés dans une section à part ; ne duplique pas une expérience.
- expertise : jusqu'à 8 domaines d'expertise exprimés avec des termes présents dans le CV.
- methodes : méthodologies et référentiels explicitement cités.
- outils_si : jusqu'à 15 outils, logiciels ou technologies parmi les plus représentatifs du CV.
- competences_detaillees : recopie complète des compétences techniques catégorisées, seulement si \
le CV contient une telle section.
- langues : les scores TOEIC / TOEFL / IELTS vont dans le niveau de la langue concernée.
- certifications : certifications professionnelles uniquement (ni diplômes, ni scores de langue).
- annees_experience : uniquement si le CV écrit explicitement un nombre d'années, avec la citation exacte.
- type de chaque expérience : classe rigoureusement les alternances (apprentissage, contrat pro) et les \
stages (internship, PFE), même si la mention n'apparaît qu'à côté des dates ; dans le doute, alternance \
ou stage plutôt qu'emploi (les années d'expérience affichées excluent alternances et stages).
- contact.localisation : ville (et code postal ou pays s'ils sont écrits), jamais l'adresse complète.
- sections_non_reprises : titres des sections du CV que tu n'as reprises nulle part.

TEXTE ET LIENS FOURNIS EN COMPLÉMENT
Le texte extrait et les liens hypertextes du PDF t'aident à lire l'orthographe exacte. Le texte \
visible du CV prime : un lien ne sert qu'à compléter une information affichée (ex. l'URL LinkedIn \
derrière une icône). Si un lien contredit le texte visible (ex. un autre e-mail), ignore-le.

PHOTO
Si des images candidates numérotées sont jointes, photo_candidate = numéro de celle qui est une \
photo du visage du consultant ; null si aucune ne l'est (logo, badge, illustration).
"""


class ExtractionError(RuntimeError):
    pass


def api_key_available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def make_client():
    from openai import OpenAI

    if not api_key_available():
        raise ExtractionError(
            "Clé OpenAI absente : créez un fichier .env à la racine du projet contenant OPENAI_API_KEY=sk-..."
        )
    return OpenAI(timeout=900, max_retries=3)


def pdf_part(source: SourceDocument) -> dict:
    data = base64.b64encode(source.pdf_bytes).decode("ascii")
    return {
        "type": "input_file",
        "filename": source.path.with_suffix(".pdf").name,
        "file_data": f"data:application/pdf;base64,{data}",
    }


def check_response(response) -> None:
    if getattr(response, "status", None) == "incomplete":
        reason = getattr(getattr(response, "incomplete_details", None), "reason", "inconnue")
        raise ExtractionError(f"Réponse incomplète du modèle (raison : {reason})")
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", []) or []:
            if getattr(part, "type", None) == "refusal":
                raise ExtractionError(f"Le modèle a refusé de traiter ce CV : {part.refusal}")


def extract_cv(client, source: SourceDocument, model: str, effort: str) -> tuple[CV, dict]:
    supplement = "Texte extrait du PDF (aide à la lecture, peut être dans le désordre) :\n" + source.text
    if source.ocr_pages:
        supplement += f"\n\n(Pages {source.ocr_pages} scannées : texte obtenu par OCR, peut contenir des erreurs ; fie-toi à l'image.)"
    supplement += "\n\nLiens hypertextes du PDF :\n" + ("\n".join(source.links) if source.links else "(aucun)")
    content: list[dict] = [pdf_part(source), {"type": "input_text", "text": supplement}]
    if source.candidates:
        content.append({"type": "input_text", "text": "Images candidates pour la photo du consultant :"})
        for candidate in source.candidates:
            data = base64.b64encode(candidate.thumbnail_png()).decode("ascii")
            content.append({"type": "input_text", "text": f"Image candidate n° {candidate.index} :"})
            content.append({"type": "input_image", "image_url": f"data:image/png;base64,{data}"})
    else:
        content.append({"type": "input_text", "text": "Aucune image candidate : photo_candidate = null."})

    response = client.responses.parse(
        model=model,
        instructions=SYSTEM_PROMPT,
        input=[{"role": "user", "content": content}],
        text_format=CV,
        reasoning={"effort": effort},
        max_output_tokens=64000,
        store=False,
    )
    check_response(response)
    parsed = response.output_parsed
    if parsed is None:
        raise ExtractionError("Le modèle n'a pas renvoyé de données exploitables.")
    usage = getattr(response, "usage", None)
    stats = {
        "tokens_entree": getattr(usage, "input_tokens", None),
        "tokens_sortie": getattr(usage, "output_tokens", None),
    }
    return parsed, stats
