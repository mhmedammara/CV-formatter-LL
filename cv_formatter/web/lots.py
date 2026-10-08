"""Lots de CV du service web : fichiers déposés, état du traitement, résultats.

Tout est stocké sous forme de fichiers dans DONNEES (sur Cloud Run : un bucket Cloud Storage monté comme
dossier, partagé par le service web et le job de traitement ; en local : un dossier du projet) :

    lots/<id>/lot.json        qui, quand, options, fichiers déposés          (écrit par le service web)
    lots/<id>/entree/…        CV déposés
    lots/<id>/statut.json     avancement du traitement                        (écrit par le job)
    lots/<id>/sortie/…        nominatif/ et anonyme/ : PPTX, PDF, rapport.md, rapport.json, photos/
    lots/<id>/sortie/resultats.zip
    cache/<sha256>.json       extractions et contre-vérifications, partagées entre lots (un CV déjà
                              traité n'est pas renvoyé à OpenAI)

Le bucket supprime tout automatiquement au bout de RETENTION_JOURS (règle de cycle de vie, cf. deploiement/).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT

DONNEES = Path(os.environ.get("CV_FORMATTER_DONNEES") or PROJECT_ROOT / "donnees-web")
RETENTION_JOURS = int(os.environ.get("CV_FORMATTER_RETENTION_JOURS", "30"))
MAX_FICHIERS = int(os.environ.get("CV_FORMATTER_MAX_FICHIERS", "50"))
MAX_TAILLE = 30 * 1024 * 1024  # Cloud Run refuse les requêtes de plus de 32 Mio
EXTENSIONS = {".pdf", ".docx", ".doc", ".odt", ".rtf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".zip"}
VERSIONS = {"nominatif": "CV nominatifs", "anonyme": "CV anonymes"}
_ID = re.compile(r"^[0-9a-f]{32}$")


class LotIntrouvable(LookupError):
    pass


@dataclass
class Options:
    versions: list[str] = field(default_factory=lambda: ["nominatif"])  # « nominatif » et/ou « anonyme »
    sans_coordonnees: bool = False

    @classmethod
    def lire(cls, data: dict[str, Any] | None) -> Options:
        data = data or {}
        versions = [v for v in data.get("versions", ["nominatif"]) if v in VERSIONS] or ["nominatif"]
        return cls(sorted(set(versions), key=list(VERSIONS).index), bool(data.get("sans_coordonnees")))


@dataclass
class Lot:
    id: str
    auteur: str
    cree_le: str
    options: Options
    fichiers: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])  # nom, taille
    lance_le: str | None = None

    @property
    def dossier(self) -> Path:
        return dossier_lot(self.id)

    def enregistrer(self) -> None:
        self.dossier.mkdir(parents=True, exist_ok=True)
        ecrire_json(self.dossier / "lot.json", asdict(self))


def maintenant() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def ecrire_json(path: Path, data: dict[str, Any]) -> None:
    """Écriture d'un seul tenant : sur le bucket monté, le fichier n'est publié qu'à la fermeture."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def lire_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def dossier_lot(lot_id: str) -> Path:
    if not _ID.match(lot_id):
        raise LotIntrouvable(lot_id)
    return DONNEES / "lots" / lot_id


def creer(auteur: str, options: Options) -> Lot:
    lot = Lot(uuid.uuid4().hex, auteur, maintenant(), options)
    lot.enregistrer()
    return lot


def charger(lot_id: str) -> Lot:
    data = lire_json(dossier_lot(lot_id) / "lot.json")
    if data is None:
        raise LotIntrouvable(lot_id)
    return Lot(data["id"], data.get("auteur", ""), data.get("cree_le", ""), Options.lire(data.get("options")),
               data.get("fichiers", []), data.get("lance_le"))


def statut(lot: Lot) -> dict[str, Any]:
    if lot.lance_le is None:
        return {"etat": "brouillon"}
    return lire_json(lot.dossier / "statut.json") or {"etat": "en_attente", "maj": lot.lance_le}


def ecrire_statut(lot_id: str, etat: str, **details: Any) -> None:
    ecrire_json(dossier_lot(lot_id) / "statut.json", {"etat": etat, "maj": maintenant(), **details})


def nom_de_fichier(nom: str, existants: set[str]) -> str:
    """Nom sûr (ni dossier, ni caractère de contrôle), unique dans le lot, extension conservée."""
    nom = unicodedata.normalize("NFC", nom).replace("\\", "/").split("/")[-1]
    nom = re.sub(r'[\x00-\x1f<>:"|?*]', "", nom).strip().lstrip(".") or "cv"
    racine, extension = os.path.splitext(nom)
    racine, extension = racine[:120].strip() or "cv", extension.lower()
    candidat, n = f"{racine}{extension}", 2
    # Noms distincts sans tenir compte de la casse ni de l'extension (« CV.pdf » et « cv.docx »).
    pris = {os.path.splitext(e)[0].lower() for e in existants}
    while os.path.splitext(candidat)[0].lower() in pris:
        candidat, n = f"{racine} ({n}){extension}", n + 1
    return candidat


def lots_de(auteur: str, limite: int = 30) -> list[dict[str, Any]]:
    """Lots récents d'un utilisateur, du plus récent au plus ancien."""
    racine = DONNEES / "lots"
    if not racine.is_dir():
        return []
    trouves: list[dict[str, Any]] = []
    for dossier in racine.iterdir():
        data = lire_json(dossier / "lot.json")
        if data and data.get("auteur") == auteur and data.get("lance_le"):
            lot = charger(data["id"])
            trouves.append({"id": lot.id, "cree_le": lot.cree_le, "fichiers": len(lot.fichiers),
                            "options": asdict(lot.options), "statut": statut(lot).get("etat")})
    trouves.sort(key=lambda d: d["cree_le"], reverse=True)
    return trouves[:limite]
