"""Extraction structurée d'un CV avec l'API OpenAI (Responses API, sortie JSON stricte)."""

from __future__ import annotations

import base64
import os
from typing import TYPE_CHECKING

from openai.types.responses import ResponseInputContentParam, ResponseInputFileParam

from .config import Effort
from .pdf_source import SourceDocument
from .schema import CV

if TYPE_CHECKING:
    from openai import OpenAI

# À incrémenter quand la consigne ou le schéma changent : les extractions enregistrées avec une version
# antérieure sont refaites au lancement suivant (sauf JSON corrigé à la main ou --depuis-json).
EXTRACTION_VERSION = 9

SYSTEM_PROMPT = """\
Tu es l'assistant de l'équipe commerciale de Logiclever, une ESN française. Tu reçois le CV \
d'un consultant (fichier PDF : texte et image de chaque page) et tu en extrais les informations \
pour les reporter dans le modèle de CV Logiclever, destiné à des clients qui ne parlent que français.

RÈGLE ABSOLUE : NE RIEN INVENTER.
- N'utilise QUE ce qui est écrit dans le CV. Information absente : null ou liste vide.
- Aucune déduction : pas de niveau de langue supposé, pas de compétence implicite, pas de date \
estimée, pas de titre fabriqué, pas d'employeur ni de client deviné, pas de nombre d'années calculé.
- Ne complète jamais avec tes connaissances générales (entreprise, technologie, diplôme…).
- Ce que le consultant dit rechercher ou vouloir développer (« je cherche », « je souhaite », objectif) \
n'est ni une compétence, ni une expertise, ni une expérience.
- En cas de doute sur un élément, ne le mets pas.

FIDÉLITÉ DU TEXTE
- Reprends les formulations du CV mot pour mot (réalisations, contexte, intitulés).
- Seules retouches permises : faute d'orthographe évidente, accents cassés par l'extraction PDF, \
casse et ponctuation. Ne résume pas, ne fusionne pas, ne reformule pas, n'ajoute aucun mot.
- Une puce du CV = une entrée de liste, dans l'ordre du CV.

LANGUE
- Tout est rédigé en français. CV dans une autre langue : traduction fidèle, sans enrichir.
- Conserve tels quels les noms de personnes, entreprises, clients, écoles, lieux (villes, régions, \
pays), produits, outils, technologies, les intitulés de certifications, les noms officiels de diplômes \
étrangers et les acronymes.

RÉPARTITION
- experiences : toutes les expériences professionnelles (emplois, missions, alternances, stages, \
freelance, création d'entreprise). Employeur et client : voir la section dédiée ci-dessous.
- projets : projets personnels ou présentés dans une section à part ; ne duplique pas une expérience.
- expertise : jusqu'à 8 domaines d'expertise exprimés avec des termes présents dans le CV ; les normes et \nréglementations maîtrisées (ex. RGPD, DSP2, ISO 27001) vont ici.
- outils_si : jusqu'à 15 outils et technologies (logiciels, ERP, plateformes, langages…) que le CV \
présente comme utilisés ou maîtrisés, y compris les méthodologies (Scrum, SAFe, cycle en V…) ; pas \nde normes ni de réglementations (elles vont dans expertise).
- environnement d'une expérience : les lignes « Environnement », « Env. technique », « Tech. »… vont \
dans ce champ (non affiché), jamais dans les réalisations.
- competences_detaillees : recopie complète des compétences techniques catégorisées, seulement si \
le CV contient une telle section.
- langues : les scores TOEIC / TOEFL / IELTS vont dans le niveau de la langue concernée.
- certifications : certifications professionnelles uniquement (ni diplômes, ni scores de langue).
- annees_experience : uniquement si le CV écrit explicitement un nombre d'années d'expérience au total, \
avec la citation exacte. La durée d'un poste ou d'une entreprise (ex. « (3 ans 2 mois) » dans un export \
LinkedIn) n'en est pas un.
- type de chaque expérience : classe rigoureusement les alternances (apprentissage, contrat pro) et les \
stages (internship, PFE), même si la mention n'apparaît qu'à côté des dates ; dans le doute, alternance \
ou stage plutôt qu'emploi (les années d'expérience affichées excluent alternances et stages).
- contact.localisation : ville (et code postal ou pays s'ils sont écrits), jamais l'adresse complète.
- sections_non_reprises : titres des sections du CV que tu n'as reprises nulle part.

EMPLOYEUR ET CLIENT (point critique : une erreur ici fait mentir le CV)
- entreprise = l'organisation que le bloc de l'expérience nomme lui-même, ou le titre de rubrique qui \
regroupe plusieurs missions (un employeur et ses dates, suivis de plusieurs missions).
- client = seulement si le CV distingue explicitement un client de l'employeur pour cette expérience. \
Exemples, avec A l'employeur et B le client : « A (Client : B) », « A, mission chez B », « B (via A) », \
« A - Chef de projet » suivi d'une ligne « B (2022 - 2024) » → entreprise = A, client = B.
- Export LinkedIn : quand une entreprise a eu plusieurs postes, son nom n'apparaît qu'une fois, suivi de \
la durée totale (ex. « A » puis « 3 ans 10 mois »), puis chaque poste avec ses dates : TOUS ces postes ont A \
pour employeur (entreprise = A), même si la description d'un poste cite un client (« Consultante au \
Ministère B » → entreprise = A, client = Ministère B).
- Freelance : « B (Freelance) » → entreprise = null, client = B, type = freelance.
- Une seule organisation nommée → entreprise = cette organisation, client = null.
- Deux organisations sans rôle explicite (« B – A ») : sépare-les seulement si le CV montre laquelle \
emploie la personne (ex. A apparaît seule comme employeur dans une autre expérience) ; sinon recopie-les \
ensemble dans entreprise, dans l'ordre du CV, et client = null.
- Ne prends JAMAIS l'employeur dans l'en-tête, le titre du consultant, le logo, les coordonnées, l'adresse \
e-mail ou le pied de page. Un CV mis en forme par une ESN (Logiclever ou une autre, ex. un titre \
« Consultant confirmé A ») ne signifie pas que les expériences listées ont été faites pour cette ESN : \
le consultant a pu les vivre avant de la rejoindre.
- Plusieurs missions sous un même emploi (un employeur et ses dates, puis des missions ou des clients sans \
dates ni durée propres) : UNE seule entrée pour cet emploi ; client = les clients nommés, dans l'ordre du \
CV, séparés par des virgules ; chaque intitulé de mission va dans les réalisations, terminé par « : », suivi \
de ses puces, dans l'ordre du CV. Un poste ou une mission qui a ses propres dates ou sa propre durée (ex. \
plusieurs postes successifs dans la même entreprise, « Depuis 4 ans ») reste une entrée séparée \
(entreprise = l'employeur ou l'organisation, client = le client éventuel). Une puce marquée d'un nom de \
client (ex. « [B] … ») reste une puce, recopiée telle quelle. Un projet interne n'est pas un client.
- type « mission » seulement si un employeur (ESN, cabinet) ET un client sont nommés ; « freelance » \
seulement si le CV écrit freelance, indépendant, auto-entrepreneur ou portage.

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


def make_client() -> OpenAI:
    from openai import OpenAI

    if not api_key_available():
        raise ExtractionError(
            "Clé OpenAI absente : créez un fichier .env à la racine du projet contenant OPENAI_API_KEY=sk-..."
        )
    return OpenAI(timeout=900, max_retries=3)


def pdf_part(source: SourceDocument) -> ResponseInputFileParam:
    data = base64.b64encode(source.pdf_bytes).decode("ascii")
    return {
        "type": "input_file",
        "filename": source.path.with_suffix(".pdf").name,
        "file_data": f"data:application/pdf;base64,{data}",
    }


def check_response(response: object) -> None:
    """Réponse tronquée ou refus du modèle : erreur explicite (accès par getattr : vaut pour tout type de réponse)."""
    if getattr(response, "status", None) == "incomplete":
        reason = getattr(getattr(response, "incomplete_details", None), "reason", "inconnue")
        raise ExtractionError(f"Réponse incomplète du modèle (raison : {reason})")
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", []) or []:
            if getattr(part, "type", None) == "refusal":
                raise ExtractionError(f"Le modèle a refusé de traiter ce CV : {part.refusal}")


def extract_cv(client: OpenAI, source: SourceDocument, model: str, effort: Effort) -> tuple[CV, dict[str, int | None]]:
    supplement = "Texte extrait du PDF (aide à la lecture, peut être dans le désordre) :\n" + source.text
    if source.ocr_pages:
        supplement += f"\n\n(Pages {source.ocr_pages} scannées : texte obtenu par OCR, peut contenir des erreurs ; fie-toi à l'image.)"
    supplement += "\n\nLiens hypertextes du PDF :\n" + ("\n".join(source.links) if source.links else "(aucun)")
    roles = source.linkedin_roles
    if roles:
        listing = "\n".join(f"- {role.company or '?'} → {role.title} ({role.dates})" for role in roles)
        supplement += (
            "\n\nExport LinkedIn : structure des expériences lue d'après la mise en page (entreprise → poste). "
            "Chaque poste a pour employeur l'entreprise sous laquelle il est listé :\n" + listing
        )
    content: list[ResponseInputContentParam] = [pdf_part(source), {"type": "input_text", "text": supplement}]
    if source.candidates:
        content.append({"type": "input_text", "text": "Images candidates pour la photo du consultant :"})
        for candidate in source.candidates:
            data = base64.b64encode(candidate.thumbnail_png()).decode("ascii")
            content.append({"type": "input_text", "text": f"Image candidate n° {candidate.index} :"})
            content.append({"type": "input_image", "image_url": f"data:image/png;base64,{data}", "detail": "auto"})
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
    usage = response.usage
    stats: dict[str, int | None] = {
        "tokens_entree": usage.input_tokens if usage else None,
        "tokens_sortie": usage.output_tokens if usage else None,
    }
    return parsed, stats
