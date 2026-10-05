"""Structure des données extraites d'un CV.

Tous les champs sont obligatoires mais peuvent valoir null / liste vide : c'est ce
qu'exige le mode « structured outputs » strict d'OpenAI, et cela force le modèle à dire
explicitement qu'une information est absente plutôt que de l'inventer.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

TypeExperience = Literal[
    "emploi", "mission", "freelance", "creation_entreprise", "alternance", "stage", "benevolat", "autre"
]


class Contact(BaseModel):
    email: Optional[str] = Field(description="Adresse e-mail visible dans le CV, sinon null.")
    telephone: Optional[str] = Field(description="Téléphone tel qu'écrit dans le CV, sinon null.")
    localisation: Optional[str] = Field(
        description="Ville (avec code postal ou pays s'ils figurent à côté). Jamais le numéro ni le nom de rue. null si absente."
    )
    linkedin: Optional[str] = Field(
        description="URL ou identifiant LinkedIn tel qu'il apparaît dans le CV ou dans ses liens hypertextes, sinon null."
    )


class AnneesExperience(BaseModel):
    valeur: Optional[int] = Field(
        description="Nombre d'années d'expérience UNIQUEMENT s'il est écrit explicitement dans le CV (ex. « 16 ans d'expérience », « depuis trois ans »). Sinon null : ne jamais le calculer."
    )
    citation: Optional[str] = Field(description="Extrait exact du CV qui mentionne ce nombre, sinon null.")


class Langue(BaseModel):
    langue: str = Field(description="Nom de la langue en français (ex. « Anglais »).")
    niveau: Optional[str] = Field(
        description="Niveau tel qu'écrit (ex. « Courant », « B2/C1 », « Langue maternelle »), avec le score TOEIC/TOEFL s'il est indiqué. null si aucun niveau n'est écrit."
    )


class Certification(BaseModel):
    intitule: str = Field(description="Intitulé exact de la certification.")
    organisme: Optional[str] = Field(description="Organisme certificateur s'il est écrit, sinon null.")
    date: Optional[str] = Field(description="Date ou année telle qu'écrite, sinon null.")


class Formation(BaseModel):
    diplome: str = Field(description="Intitulé du diplôme ou de la formation tel qu'écrit.")
    etablissement: Optional[str] = Field(description="École / université, sinon null.")
    lieu: Optional[str] = Field(description="Ville / pays s'ils sont écrits, sinon null.")
    periode: Optional[str] = Field(description="Année(s) telle(s) qu'écrite(s) (ex. « 2018 – 2019 »), sinon null.")
    details: Optional[str] = Field(description="Spécialité, option ou mention écrite sous le diplôme, sinon null.")


class Experience(BaseModel):
    poste: Optional[str] = Field(description="Intitulé du poste ou du rôle tel qu'écrit, sinon null.")
    entreprise: Optional[str] = Field(
        description=(
            "Organisation nommée dans le bloc de CETTE expérience (employeur, société, ESN) ou dans le titre de "
            "rubrique qui regroupe plusieurs missions, telle qu'écrite. Jamais une entreprise citée seulement dans "
            "l'en-tête du CV, le titre du consultant, le logo, les coordonnées, l'e-mail ou le pied de page. "
            "null si le bloc n'en nomme aucune (ou pour un freelance, cf. client)."
        )
    )
    client: Optional[str] = Field(
        description=(
            "Client final, seulement si le CV le distingue explicitement de l'employeur pour cette expérience "
            "(A employeur, B client : « A (Client : B) », « mission chez B », « B (via A) », ligne « B » sous "
            "« A - poste »). Freelance : l'entreprise pour laquelle la mission a été faite. Sinon null."
        )
    )
    lieu: Optional[str] = Field(description="Ville / pays de l'expérience s'ils sont écrits, sinon null.")
    periode_texte: Optional[str] = Field(description="Période exactement comme dans le CV (ex. « Juin 2022 – Présent »), sinon null.")
    debut: Optional[str] = Field(
        description="Début normalisé « AAAA-MM » si le mois est écrit, « AAAA » si seule l'année est écrite, sinon null."
    )
    fin: Optional[str] = Field(
        description="Fin normalisée « AAAA-MM » ou « AAAA » ; « present » si le CV indique Présent / Aujourd'hui / Depuis ; null si absente."
    )
    type: TypeExperience = Field(
        description=(
            "Nature de l'expérience d'après le CV : alternance = alternance, apprentissage, contrat de "
            "professionnalisation (même si le mot n'apparaît que près des dates) ; stage = stage, internship, PFE ; "
            "emploi = poste salarié ; mission = mission chez un client pour le compte d'un employeur (ESN, cabinet) "
            "nommé dans le CV ; freelance = le CV écrit freelance, indépendant, auto-entrepreneur ou portage ; "
            "creation_entreprise = fondateur / cofondateur ; benevolat ; "
            "autre = tutorat, job étudiant, associatif. En cas de doute entre alternance/stage et emploi, "
            "choisir alternance ou stage."
        )
    )
    contexte: Optional[str] = Field(
        description="Phrase de contexte, objectif ou projet telle qu'écrite (ex. « Projet eMAT : gestion du matériel roulant »), sinon null."
    )
    realisations: list[str] = Field(
        description="Chaque puce / tâche / réalisation reprise mot pour mot, une entrée par puce, dans l'ordre du CV."
    )
    environnement: list[str] = Field(
        description=(
            "Outils ou technologies listés pour CETTE expérience (ligne « Environnement », « Env. technique », "
            "« Tech. »…), un par entrée, sinon liste vide. Non affichés sur le CV Logiclever : les ranger ici évite "
            "de les mêler aux réalisations."
        )
    )


class Projet(BaseModel):
    nom: str = Field(description="Nom du projet tel qu'écrit.")
    cadre: Optional[str] = Field(description="Cadre du projet tel qu'écrit (ex. « Projet personnel », nom d'entreprise), sinon null.")
    periode_texte: Optional[str] = Field(description="Période telle qu'écrite, sinon null.")
    description: list[str] = Field(description="Lignes de description reprises mot pour mot.")
    environnement: list[str] = Field(description="Technologies listées pour ce projet (non affichées), sinon liste vide.")


class CategorieCompetences(BaseModel):
    categorie: str = Field(description="Titre de la catégorie tel qu'écrit dans le CV (ex. « Data Engineering »).")
    elements: list[str] = Field(description="Éléments de la catégorie, mot pour mot, un par entrée.")


class CV(BaseModel):
    langue_source: str = Field(description="Code ISO 639-1 de la langue principale du CV d'origine (ex. « fr », « en »).")
    prenom: Optional[str] = Field(description="Prénom(s) du consultant, sinon null.")
    nom: Optional[str] = Field(description="Nom de famille du consultant, sinon null.")
    titre: Optional[str] = Field(
        description="Intitulé de poste / titre affiché en en-tête du CV, tel qu'écrit. null s'il n'y en a pas (ne pas en fabriquer un)."
    )
    contact: Contact
    annees_experience: AnneesExperience
    expertise: list[str] = Field(
        description="Jusqu'à 8 domaines d'expertise (métier, fonctionnels ou techniques) formulés avec des termes présents dans le CV. Liste vide si rien d'explicite."
    )
    outils_si: list[str] = Field(
        description=(
            "Jusqu'à 15 outils et technologies que le CV présente comme utilisés ou maîtrisés : logiciels, ERP, "
            "plateformes, langages, bases de données, frameworks, outils de gestion (ex. Jira) — les plus "
            "représentatifs. Pas de méthodologies (Agile, Scrum, SAFe, cycle en V…), ni de normes ou "
            "réglementations, ni de connaissances métier. Liste vide si le CV n'en cite pas."
        )
    )
    competences_detaillees: list[CategorieCompetences] = Field(
        description="Liste complète des compétences techniques par catégorie, UNIQUEMENT si le CV contient une section de compétences catégorisée ; sinon liste vide."
    )
    langues: list[Langue] = Field(description="Langues parlées citées dans le CV (les scores TOEIC/TOEFL vont ici).")
    certifications: list[Certification] = Field(
        description="Certifications professionnelles (pas les diplômes, pas les scores TOEIC/TOEFL)."
    )
    formations: list[Formation] = Field(description="Diplômes et formations, du plus récent au plus ancien comme dans le CV.")
    experiences: list[Experience] = Field(
        description="Toutes les expériences professionnelles (emplois, missions, alternances, stages…), dans l'ordre du CV."
    )
    projets: list[Projet] = Field(
        description="Projets personnels ou projets présentés à part des expériences, sinon liste vide. Ne pas dupliquer une expérience."
    )
    photo_candidate: Optional[int] = Field(
        description="Numéro de l'image candidate qui est une photo du visage du consultant, ou null si aucune ne l'est (ou si aucune image n'est fournie)."
    )
    sections_non_reprises: list[str] = Field(
        description="Titres des sections du CV non reprises ailleurs (ex. « Centres d'intérêt », « Bénévolat », « À propos »)."
    )


# --- Contre-vérification (2e appel) ---------------------------------------------------

StatutControle = Literal["confirme", "absent", "inexact"]


class Verdict(BaseModel):
    id: str = Field(description="Identifiant de l'affirmation contrôlée, recopié tel quel.")
    statut: StatutControle = Field(
        description="confirme = présent dans le CV ; absent = introuvable dans le CV ; inexact = présent mais déformé (date, rattachement, chiffre…)."
    )
    explication: str = Field(description="Justification courte (vide si confirmé).")


class Controle(BaseModel):
    verdicts: list[Verdict]
