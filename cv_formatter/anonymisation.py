"""Anonymisation d'un CV : initiales, pas de photo ni de coordonnées personnelles."""

from __future__ import annotations

import re
from typing import Any, cast

from .contenu import strip_accents
from .schema import CV

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
LINKEDIN = re.compile(r"(?:https?://)?(?:[\w-]+\.)?linkedin\.com/\S*", re.I)
PHONE_CANDIDATE = re.compile(r"(?:\(\s*\+?\d{1,3}\s*\)|\+)?[\d(][\d\s.\-()]{7,}\d")


def initials(cv: CV) -> str:
    parts: list[str] = []
    for name in (cv.prenom, cv.nom):
        for word in (name or "").split():
            pieces = [seg for seg in word.split("-") if seg]
            if pieces:
                parts.append("-".join(seg[0].upper() + "." for seg in pieces))
    return " ".join(parts) or "Consultant"


def _scrub_phones(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        chunk = match.group(0)
        digits = re.sub(r"\D", "", chunk)
        looks_like_phone = chunk.lstrip("( ").startswith(("+", "0")) and 9 <= len(digits) <= 13
        return "" if looks_like_phone else chunk

    return PHONE_CANDIDATE.sub(replace, text)


def anonymize(cv: CV) -> CV:
    data = cv.model_dump()
    short = initials(cv)
    names: list[str] = []
    if cv.prenom and cv.nom:
        names += [f"{cv.prenom} {cv.nom}", f"{cv.nom} {cv.prenom}"]
    names += [n for n in (cv.nom, cv.prenom) if n and len(n) >= 3]
    patterns: list[re.Pattern[str]] = []
    for name in names:
        letters = [re.escape(c) if c.strip() else r"\s+" for c in name.strip()]
        patterns.append(re.compile(r"(?<!\w)" + "".join(letters) + r"(?!\w)", re.I))
    folded = [re.compile(re.escape(strip_accents(n)), re.I) for n in names]

    def clean(text: str) -> str:
        text = EMAIL.sub("", text)
        text = LINKEDIN.sub("", text)
        text = _scrub_phones(text)
        for pattern in patterns:
            text = pattern.sub(short, text)
        for pattern in folded:
            text = pattern.sub(short, text)
        return re.sub(r"\s{2,}", " ", text).strip(" |,;")

    def walk(value: Any) -> Any:  # données JSON du CV (model_dump) : chaînes, listes, dictionnaires
        if isinstance(value, str):
            return clean(value)
        if isinstance(value, list):
            return [walk(v) for v in cast(list[Any], value)]
        if isinstance(value, dict):
            return {k: walk(v) for k, v in cast(dict[str, Any], value).items()}
        return value

    for key in list(data):
        if key in ("prenom", "nom", "contact", "langue_source", "photo_candidate"):
            continue
        data[key] = walk(data[key])
    # Le nom complet ne doit subsister nulle part dans les données anonymisées (défense en profondeur).
    parts = short.split(" ", 1)
    data["prenom"], data["nom"] = parts[0], (parts[1] if len(parts) > 1 else None)
    data["contact"]["email"] = None
    data["contact"]["telephone"] = None
    data["contact"]["linkedin"] = None
    data["photo_candidate"] = None
    return CV.model_validate(data)
