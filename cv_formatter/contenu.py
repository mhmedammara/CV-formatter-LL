"""Mise en forme du contenu : dates en français, années d'expérience, paragraphes stylés."""

from __future__ import annotations

import datetime as dt
import re
import unicodedata

from .config import BLACK, BLUE, DARK_GREY, GREY, ORANGE
from .layout import Block, Para, Run
from .schema import CV, Experience, Projet

MOIS = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]

# Mois reconnus dans les périodes écrites (français et anglais, abrégés ou non).
_MONTH_TOKENS = {
    1: ("jan", "janv", "janvier", "january"),
    2: ("fev", "fevr", "fevrier", "feb", "february"),
    3: ("mar", "mars", "march"),
    4: ("avr", "avril", "apr", "april"),
    5: ("mai", "may"),
    6: ("juin", "jun", "june"),
    7: ("juil", "juillet", "jul", "july"),
    8: ("aout", "aug", "august"),
    9: ("sep", "sept", "septembre", "september"),
    10: ("oct", "octobre", "october"),
    11: ("nov", "novembre", "november"),
    12: ("dec", "decembre", "december"),
}
_MONTH_LOOKUP = {token: month for month, tokens in _MONTH_TOKENS.items() for token in tokens}

PRO_TYPES = {"emploi", "mission", "freelance", "creation_entreprise"}  # alternance et stages exclus


def strip_accents(text: str) -> str:
    text = unicodedata.normalize("NFD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


_LOWER_WORDS = {
    "a", "à", "au", "aux", "d", "de", "des", "du", "en", "et", "l", "la", "le", "les", "ou", "par", "pour", "sur",
    "un", "une", "avec", "dans", "chez", "the", "of", "and", "for",
}


def tame_caps(text: str | None) -> str | None:
    """« CHEF DE PROJET SI JUNIOR » -> « Chef de projet SI junior » (casse seulement, aucun mot changé).

    Ne s'applique qu'à un texte entièrement en majuscules ; les sigles courts (SI, PO, MBA, IT/IS…)
    restent en majuscules. Utilisé pour les titres, postes et diplômes, pas pour les noms d'entreprise."""
    if not text or not any(c.isalpha() for c in text) or text != text.upper() or len(text) < 8:
        return text
    words = re.split(r"(\s+)", text)
    out, first = [], True
    for word in words:
        if not word.strip():
            out.append(word)
            continue
        parts = [re.sub(r"[^A-ZÀ-ÖØ-Þ]", "", p) for p in re.split(r"[/-]", word)]
        core = word.lower().strip("’'.,;:()&|-")
        if core in _LOWER_WORDS and not first:
            out.append(word.lower())
        elif any(parts) and all(len(p) <= 3 for p in parts) and core not in _LOWER_WORDS:
            out.append(word)  # sigle : SI, PO, MBA, IT/IS, MER…
        else:
            low = word.lower()
            out.append(low[:1].upper() + low[1:] if first else low)
        first = False
    return "".join(out)


def clean_item(text: str, capitalize: bool = True) -> str:
    """Nettoyage purement typographique : espaces, puce parasite, ponctuation finale, majuscule.

    La majuscule initiale n'est ajoutée que si le premier mot est entièrement en minuscules
    (on ne touche pas aux graphies de marque comme « spaCy », « eMI3 », « iOS »). Les listes
    d'outils passent `capitalize=False` : leur orthographe est conservée telle quelle.
    """
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"^[-–—•●○▪■·*>›]+\s*", "", text)
    text = re.sub(r"[\s,;:]+$", "", text)
    text = re.sub(r"(?<!\.)(?<!\betc)\.$", "", text)  # point final isolé (on garde « etc. » et « ... »)
    first = text.split(" ", 1)[0] if text else ""
    if capitalize and first.isalpha() and first.islower():
        text = text[0].upper() + text[1:]
    return text


# --- Dates -----------------------------------------------------------------------------


def parse_date(value: str | None) -> tuple[int, int | None] | None:
    if not value:
        return None
    match = re.fullmatch(r"\s*(\d{4})(?:-(\d{1,2}))?\s*", value)
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2)) if match.group(2) else None
    if month is not None and not 1 <= month <= 12:
        return None
    return year, month


def _months_in_text(text: str) -> list[int]:
    tokens = re.findall(r"[a-z]+", strip_accents(text.lower()))
    return [_MONTH_LOOKUP[t] for t in tokens if t in _MONTH_LOOKUP]


def _dates_consistent(exp: Experience) -> bool:
    """Vérifie que début/fin normalisés correspondent à la période écrite dans le CV."""
    if not exp.periode_texte:
        return False
    start = parse_date(exp.debut)
    end = parse_date(exp.fin) if exp.fin != "present" else None
    if start is None:
        return False
    years_text = {int(y) for y in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", exp.periode_texte)}
    years_dates = {start[0]} | ({end[0]} if end else set())
    if years_text != years_dates:
        return False
    months_dates = [m for m in (start[1], end[1] if end else None) if m]
    return _months_in_text(exp.periode_texte) == months_dates


def _fmt(date: tuple[int, int | None]) -> str:
    year, month = date
    return f"{MOIS[month - 1].capitalize()} {year}" if month else str(year)


def clean_period_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*[-–—]\s*", " – ", text)
    text = re.sub(r"\b(?:présent|present|aujourd['’]hui|now|current|today)\b", "Aujourd’hui", text, flags=re.I)
    return text[:1].upper() + text[1:]


def format_period(exp: Experience) -> str | None:
    if _dates_consistent(exp):
        start = parse_date(exp.debut)
        if exp.fin == "present":
            return f"{_fmt(start)} – Aujourd’hui"
        end = parse_date(exp.fin)
        if end is None or end == start:
            return _fmt(start)
        return f"{_fmt(start)} – {_fmt(end)}"
    return clean_period_text(exp.periode_texte) if exp.periode_texte else None


_ALTERNANCE = re.compile(r"\b(alternance|alternant|alternante|apprenti|apprentie|apprentissage|professionnalisation|work[- ]study)\b", re.I)
_STAGE = re.compile(r"\b(stage|stagiaire|intern|internship|pfe)\b", re.I)
TYPE_LABELS = {"alternance": "alternance", "stage": "stage", "benevolat": "bénévolat", "autre": "hors emploi (tutorat, job étudiant…)"}


def effective_type(exp: Experience) -> str:
    """Type retenu pour le décompte : une mention « alternance » ou « stage » dans l'intitulé, le
    contexte ou la période l'emporte sur la classification (sécurité contre la surestimation)."""
    text = " ".join(x for x in (exp.poste, exp.contexte, exp.periode_texte) if x)
    if _ALTERNANCE.search(text):
        return "alternance"
    if _STAGE.search(text):
        return "stage"
    return exp.type


def _index(date: tuple[int, int | None], is_end: bool, other_has_month: bool) -> int:
    """Indice de mois (début inclus, fin exclue). Année seule : « 2020 – 2022 » vaut 2 ans ; face à
    une date précise au mois, l'année seule est lue au milieu de l'année (ni janvier, ni décembre)."""
    year, month = date
    if month is None:
        return year * 12 + (6 if other_has_month else 0)
    return year * 12 + (month - 1) + (1 if is_end else 0)


def computed_years(cv: CV, document_date: dt.date | None = None) -> tuple[int | None, str, bool, str | None]:
    """Années d'expérience professionnelle prouvées par les dates du CV.

    Comptent : emploi, mission, freelance, création d'entreprise.
    Ne comptent pas : alternance, stage, bénévolat, autre, projets. Un poste « en cours » est compté
    jusqu'à aujourd'hui ; si le CV (date du PDF) a plus d'un an, un avertissement invite à vérifier
    qu'il l'est toujours. Union des périodes (les trous ne comptent pas), arrondi à l'année supérieure
    dès qu'au moins un mois est prouvé (aucune expérience prouvée : pas de pastille).
    Renvoie (années, détail, complet, avertissement) ; complet = toutes les expériences pro sont datées.
    """
    today = dt.date.today()
    now_index = today.year * 12 + today.month  # mois en cours inclus
    intervals, excluded, complete, current = [], [], True, []
    for exp in cv.experiences:
        name = " — ".join(x for x in (exp.poste, exp.entreprise) if x) or "expérience"
        kind = effective_type(exp)
        if kind not in PRO_TYPES:
            excluded.append(f"{name} ({TYPE_LABELS.get(kind, kind)})")
            continue
        start = parse_date(exp.debut)
        end = None if exp.fin == "present" else parse_date(exp.fin)
        if start is None or (end is None and exp.fin != "present"):
            complete = False
            continue
        end_is_precise = exp.fin == "present" or (end is not None and end[1] is not None)
        start_index = _index(start, False, end_is_precise)
        if exp.fin == "present":
            end_index = now_index
            current.append(name)
        else:
            end_index = min(_index(end, True, start[1] is not None), now_index)
        if end_index > start_index:
            intervals.append((start_index, end_index))
    stale = None
    if current and document_date and (today - document_date).days > 365:
        stale = (
            f"CV daté de {MOIS[document_date.month - 1]} {document_date.year} : le poste en cours "
            f"({', '.join(dict.fromkeys(current))}) est compté jusqu'à aujourd'hui — vérifier qu'il est toujours d'actualité"
        )
    detail_ref = "postes en cours comptés jusqu'à aujourd'hui" if current else "aucun poste en cours"
    excl = f" ; non comptés : {', '.join(excluded)}" if excluded else ""
    if not intervals:
        return None, f"aucune période professionnelle datée hors alternance/stage{excl}", complete, None
    intervals.sort()
    total, (cur_start, cur_end) = 0, intervals[0]
    for start, end in intervals[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
    total += cur_end - cur_start
    if total <= 0:  # aucune expérience prouvée : pas de pastille
        return None, f"aucune période professionnelle datée hors alternance/stage{excl}", complete, None
    years = -(-total // 12)  # arrondi à l'année supérieure (demande de l'équipe commerciale)
    detail = f"{total} mois prouvés par les dates, arrondis à l'année supérieure, {detail_ref}{excl}"
    return years, detail, complete, stale


def experience_label(cv: CV, document_date: dt.date | None = None) -> tuple[str | None, str, str | None]:
    """Texte de la pastille, explication pour le rapport et éventuel avertissement.

    Un nombre écrit dans le CV est vérifié : si les dates en justifient moins, c'est le nombre
    justifié qui est affiché (jamais de surestimation)."""
    computed, detail, complete, stale = computed_years(cv, document_date)
    stated = cv.annees_experience.valeur
    warning = stale
    if stated:
        quote = f"« {cv.annees_experience.citation} »" if cv.annees_experience.citation else str(stated)
        if computed is not None and complete and computed < stated:
            years = computed
            origin = f"le CV annonce {stated} ans ({quote}) mais ses dates n'en justifient que {computed} : {computed} retenu ({detail})"
            warning = " ; ".join(x for x in (f"Années d'expérience ramenées de {stated} (annoncé) à {computed} (justifié par les dates)", stale) if x)
        elif computed is None or not complete:
            years = stated
            origin = f"mentionné dans le CV : {quote} — non vérifiable par les dates ({detail})"
            warning = " ; ".join(x for x in (f"Années d'expérience ({stated}) annoncées par le CV mais non vérifiables : à confirmer", stale) if x)
        else:
            years = stated
            origin = f"mentionné dans le CV : {quote}, cohérent avec les dates ({detail})"
    else:
        if computed is None:
            return None, f"non affiché ({detail})", None
        years, origin = computed, f"calculé à partir des dates : {detail}"
    label = "1 an d’expérience" if years == 1 else f"{years} ans d’expérience"
    return label, origin, warning


def sort_experiences(experiences: list[Experience]) -> list[Experience]:
    """Tri antichronologique si toutes les expériences sont datées, sinon ordre du CV."""
    keys = []
    for exp in experiences:
        start = parse_date(exp.debut)
        if start is None:
            return experiences
        end = (9999, 12) if exp.fin == "present" else (parse_date(exp.fin) or start)
        keys.append(((end[0], end[1] or 0), (start[0], start[1] or 0)))
    order = sorted(range(len(experiences)), key=lambda i: keys[i], reverse=True)
    return [experiences[i] for i in order]


# --- Paragraphes stylés --------------------------------------------------------------

BODY = 10.0


def experience_block(exp: Experience, condensed: bool = False) -> Block:
    head_text = tame_caps(exp.poste) or exp.entreprise or exp.client or "Expérience"
    paras = [Para([Run(head_text, "semibold", 12, BLACK)], space_before=11, keep_with_next=True)]

    org: list[Run] = []
    if exp.poste and exp.entreprise:
        org.append(Run(exp.entreprise, "medium", BODY, BLUE))
    if exp.client and exp.client != head_text:
        org.append(Run(("Client : " if org else "") + exp.client, "regular", BODY, BLUE))
    if exp.lieu:
        org.append(Run(exp.lieu, "regular", BODY, BLUE))
    if org:
        runs: list[Run] = []
        for i, run in enumerate(org):
            if i:
                runs.append(Run("  ·  ", "regular", BODY, BLUE))
            runs.append(run)
        paras.append(Para(runs, space_before=1, keep_with_next=True))

    period = format_period(exp)
    if period:
        label = period + ("  ·  Alternance" if exp.type == "alternance" else "  ·  Stage" if exp.type == "stage" else "")
        paras.append(Para([Run(label, "medium", 9.5, ORANGE)], space_before=1, keep_with_next=True))

    if exp.contexte:
        paras.append(Para([Run(clean_item(exp.contexte), "regular", BODY, DARK_GREY)], space_before=3, line_spacing=1.1))
    for item in [] if condensed else exp.realisations:
        text = clean_item(item)
        if text:
            paras.append(Para([Run(text, "regular", BODY, BLACK)], bullet="•", indent=0.4, space_before=2, line_spacing=1.1))
    env = [clean_item(e, False) for e in exp.environnement if clean_item(e, False)]
    if env:
        paras.append(
            Para(
                [Run("Environnement : ", "semibold", 9, GREY), Run(", ".join(env), "regular", 9, GREY)],
                space_before=4,
                line_spacing=1.1,
            )
        )
    suite_title = " — ".join(x for x in (tame_caps(exp.poste), exp.entreprise) if x) or head_text
    continuation = Para([Run(f"{suite_title} (suite)", "medium", BODY, GREY)], space_before=0, keep_with_next=True)
    return Block(paras, continuation)


def project_block(prj: Projet, titles_only: bool = False) -> Block:
    paras = [Para([Run(clean_item(tame_caps(prj.nom)), "semibold", 11, BLACK)], space_before=10, keep_with_next=True)]
    meta = [x for x in (prj.cadre, clean_period_text(prj.periode_texte) if prj.periode_texte else None) if x]
    if meta:
        paras.append(Para([Run("  ·  ".join(meta), "medium", 9.5, ORANGE)], space_before=1, keep_with_next=True))
    if titles_only:
        paras[-1].keep_with_next = False
        return Block(paras, None)
    for line in prj.description:
        text = clean_item(line)
        if text:
            paras.append(Para([Run(text, "regular", BODY, BLACK)], bullet="•", indent=0.4, space_before=2, line_spacing=1.1))
    env = [clean_item(e, False) for e in prj.environnement if clean_item(e, False)]
    if env:
        paras.append(
            Para([Run("Environnement : ", "semibold", 9, GREY), Run(", ".join(env), "regular", 9, GREY)], space_before=4, line_spacing=1.1)
        )
    continuation = Para([Run(f"{clean_item(tame_caps(prj.nom))} (suite)", "medium", BODY, GREY)], keep_with_next=True)
    return Block(paras, continuation)


def label_para(text: str) -> Para:
    """Libellé de bloc de la colonne droite (style du modèle : Lexend gras 10 pt)."""
    return Para([Run(text, "bold", BODY, BLACK)], line_spacing=1.15, keep_with_next=True)


def bullet_paras(items: list[str], size: float = BODY) -> list[Para]:
    return [
        Para([Run(clean_item(item), "regular", size, BLACK)], bullet="•", indent=0.35, line_spacing=1.1, space_before=1)
        for item in items
        if clean_item(item)
    ]


def inline_para(items: list[str], size: float = BODY) -> list[Para]:
    cleaned = [clean_item(i, False) for i in items if clean_item(i, False)]
    return [Para([Run("  ·  ".join(cleaned), "regular", size, BLACK)], line_spacing=1.15, space_before=1)] if cleaned else []


def langues_paras(langues: list) -> list[Para]:
    paras = []
    for lang in langues:
        runs = [Run(clean_item(lang.langue), "semibold", BODY, BLACK)]
        if lang.niveau:
            runs.append(Run(" : " + lang.niveau.strip(), "regular", BODY, BLACK))
        paras.append(Para(runs, line_spacing=1.1, space_before=1))
    return paras


def certification_paras(certifications: list) -> list[Para]:
    paras = []
    for i, cert in enumerate(certifications):
        paras.append(Para([Run(clean_item(tame_caps(cert.intitule)), "semibold", BODY, BLACK)], line_spacing=1.05, space_before=0 if i == 0 else 5, keep_with_next=True))
        meta = [x.strip() for x in (cert.organisme, cert.date) if x and x.strip()]
        if meta:
            paras.append(Para([Run("  ·  ".join(meta), "regular", 9, GREY)], line_spacing=1.05, space_before=1))
    if paras:
        paras[-1].keep_with_next = False
    return paras


def formation_paras(formations: list, with_details: bool = True) -> list[Para]:
    paras = []
    for i, form in enumerate(formations):
        details = form.details if with_details else None
        paras.append(Para([Run(clean_item(tame_caps(form.diplome)), "semibold", BODY, BLACK)], line_spacing=1.05, space_before=0 if i == 0 else 5, keep_with_next=True))
        place = ", ".join(x.strip() for x in (form.etablissement, form.lieu) if x and x.strip())
        meta = "  ·  ".join(x for x in (place, form.periode.strip() if form.periode else "") if x)
        if meta:
            paras.append(Para([Run(meta, "regular", 9, GREY)], line_spacing=1.05, space_before=1, keep_with_next=bool(details)))
        if details:
            paras.append(Para([Run(clean_item(details), "regular", 9, GREY)], line_spacing=1.05, space_before=1))
    if paras:
        paras[-1].keep_with_next = False
    return paras
