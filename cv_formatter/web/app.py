"""Service web du CV Formatter : page de dépôt des CV, suivi du traitement, téléchargement des résultats.

Sur Cloud Run, l'accès est filtré en amont par IAP (connexion Google, comptes Logiclever uniquement) : la page
ne gère donc pas de mot de passe ; l'utilisateur est lu dans l'en-tête X-Goog-Authenticated-User-Email posé par
IAP. Le service ne doit jamais être déployé en accès public (--no-allow-unauthenticated + --iap).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import FONTS_DIR
from . import lancement, lots

STATIC = Path(__file__).resolve().parent / "static"
app = FastAPI(title="CV Formatter Logiclever", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/polices", StaticFiles(directory=FONTS_DIR), name="polices")


def utilisateur(request: Request) -> str:
    """Adresse de l'utilisateur connecté (IAP : « accounts.google.com:prenom.nom@logiclever.com »)."""
    entete = request.headers.get("x-goog-authenticated-user-email", "")
    return entete.split(":", 1)[-1] if entete else "local"


def _lot(lot_id: str) -> lots.Lot:
    try:
        return lots.charger(lot_id)
    except lots.LotIntrouvable:
        raise HTTPException(404, "Lot introuvable" + (f" (les lots sont supprimés au bout de {lots.RETENTION_JOURS} jours)."
                                                      if lots.RETENTION_JOURS else ".")) from None


def _vue(lot: lots.Lot) -> dict[str, Any]:
    """Tout ce que la page affiche pour un lot."""
    statut = lots.statut(lot)
    rapports: dict[str, Any] = {}
    sortie = lot.dossier / "sortie"
    for version in lot.options.versions:
        rapport = lots.lire_json(sortie / version / "rapport.json")
        if rapport is not None:
            rapports[version] = rapport
    if statut.get("etat") in ("en_attente", "en_cours"):
        maj = dt.datetime.fromisoformat(statut.get("maj") or lot.lance_le or lots.maintenant())
        statut["silence_min"] = int((dt.datetime.now(dt.timezone.utc) - maj).total_seconds() // 60)
    return {
        "id": lot.id,
        "cree_le": lot.cree_le,
        "lance_le": lot.lance_le,
        "auteur": lot.auteur,
        "options": asdict(lot.options),
        "fichiers": lot.fichiers,
        "statut": statut,
        "rapports": rapports,
        "zip": (sortie / "resultats.zip").exists(),
    }


@app.get("/", response_class=HTMLResponse)
def page() -> HTMLResponse:
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


@app.get("/api/moi")
def moi(request: Request) -> dict[str, Any]:
    return {"email": utilisateur(request), "version": __version__, "retention_jours": lots.RETENTION_JOURS,
            "max_fichiers": lots.MAX_FICHIERS, "max_taille_mo": lots.MAX_TAILLE // (1024 * 1024)}


@app.get("/api/lots")
def historique(request: Request, page: int = 1) -> dict[str, Any]:
    return lots.lots_de(utilisateur(request), page)


@app.post("/api/lots")
async def nouveau(request: Request) -> dict[str, Any]:
    try:
        donnees = await request.json()
    except ValueError:
        donnees = {}
    lot = lots.creer(utilisateur(request), lots.Options.lire(donnees.get("options") if isinstance(donnees, dict) else None))
    return _vue(lot)


@app.get("/api/lots/{lot_id}")
def consulter(lot_id: str) -> dict[str, Any]:
    return _vue(_lot(lot_id))


@app.post("/api/lots/{lot_id}/fichiers")
async def deposer(lot_id: str, fichier: UploadFile = File(...)) -> dict[str, Any]:
    lot = _lot(lot_id)
    if lot.lance_le:
        raise HTTPException(409, "Ce lot est déjà lancé : créez-en un nouveau pour ajouter des CV.")
    if len(lot.fichiers) >= lots.MAX_FICHIERS:
        raise HTTPException(413, f"{lots.MAX_FICHIERS} fichiers au plus par lot.")
    nom = lots.nom_de_fichier(fichier.filename or "cv", {f["nom"] for f in lot.fichiers})
    if Path(nom).suffix.lower() not in lots.EXTENSIONS:
        raise HTTPException(415, "Format non pris en charge : PDF, Word (DOCX, DOC, ODT, RTF), image ou .zip.")
    contenu = await fichier.read(lots.MAX_TAILLE + 1)
    if len(contenu) > lots.MAX_TAILLE:
        raise HTTPException(413, f"Fichier trop volumineux (plus de {lots.MAX_TAILLE // (1024 * 1024)} Mo).")
    if not contenu:
        raise HTTPException(400, "Fichier vide.")
    cible = lot.dossier / "entree" / nom
    cible.parent.mkdir(parents=True, exist_ok=True)
    cible.write_bytes(contenu)
    lot = _lot(lot_id)  # relu : deux dépôts simultanés ne doivent pas s'écraser la liste
    lot.fichiers = [f for f in lot.fichiers if f["nom"] != nom] + [{"nom": nom, "taille": len(contenu)}]
    lot.enregistrer()
    return {"nom": nom, "taille": len(contenu)}


@app.delete("/api/lots/{lot_id}/fichiers/{nom}")
def retirer(lot_id: str, nom: str) -> dict[str, Any]:
    lot = _lot(lot_id)
    if lot.lance_le:
        raise HTTPException(409, "Ce lot est déjà lancé.")
    if nom not in {f["nom"] for f in lot.fichiers}:
        raise HTTPException(404, "Fichier introuvable.")
    (lot.dossier / "entree" / nom).unlink(missing_ok=True)
    lot.fichiers = [f for f in lot.fichiers if f["nom"] != nom]
    lot.enregistrer()
    return _vue(lot)


@app.post("/api/lots/{lot_id}/lancer")
async def lancer(lot_id: str, request: Request) -> dict[str, Any]:
    lot = _lot(lot_id)
    if lot.lance_le:
        return _vue(lot)
    try:
        donnees = await request.json()
    except ValueError:
        donnees = {}
    if isinstance(donnees, dict) and "options" in donnees:
        lot.options = lots.Options.lire(donnees["options"])
    # Liste refaite d'après les fichiers réellement déposés (dépôts simultanés sur deux instances du service),
    # dans l'ordre de dépôt.
    entree = lot.dossier / "entree"
    sur_disque = {p.name: p.stat().st_size for p in entree.iterdir() if p.is_file()} if entree.is_dir() else {}
    ordre = [f["nom"] for f in lot.fichiers if f["nom"] in sur_disque]
    ordre += sorted(nom for nom in sur_disque if nom not in ordre)
    lot.fichiers = [{"nom": nom, "taille": sur_disque[nom]} for nom in ordre]
    if not lot.fichiers:
        raise HTTPException(400, "Aucun CV déposé.")
    lot.lance_le = lots.maintenant()
    lot.enregistrer()
    lots.indexer(lot)
    lots.ecrire_statut(lot_id, "en_attente")
    try:
        lancement.lancer(lot_id)
    except Exception as exc:
        lots.ecrire_statut(lot_id, "echec", message=f"Lancement impossible : {exc}")
    return _vue(lot)


def _fichier_du_lot(lot: lots.Lot, chemin: str) -> Path:
    sortie = (lot.dossier / "sortie").resolve()
    cible = (sortie / chemin).resolve()
    if sortie not in cible.parents or not cible.is_file():
        raise HTTPException(404, "Fichier introuvable.")
    return cible


@app.get("/api/lots/{lot_id}/resultats/{chemin:path}")
def telecharger(lot_id: str, chemin: str, apercu: bool = False) -> FileResponse:
    lot = _lot(lot_id)
    cible = _fichier_du_lot(lot, chemin)
    nom = cible.name if cible.name != "resultats.zip" else f"CV Logiclever - {lot.cree_le[:10]}.zip"
    return FileResponse(cible, filename=nom, content_disposition_type="inline" if apercu else "attachment")


@app.get("/api/lots/{lot_id}/originaux/{nom}")
def original(lot_id: str, nom: str, apercu: bool = False) -> FileResponse:
    """CV tel qu'il a été déposé (conservé avec le lot)."""
    lot = _lot(lot_id)
    cible = lot.dossier / "entree" / nom
    if nom not in {f["nom"] for f in lot.fichiers} or not cible.is_file():
        raise HTTPException(404, "Fichier introuvable.")
    return FileResponse(cible, filename=nom, content_disposition_type="inline" if apercu else "attachment")


@app.exception_handler(lots.LotIntrouvable)
def _introuvable(_: Request, __: lots.LotIntrouvable) -> JSONResponse:
    return JSONResponse({"detail": "Lot introuvable."}, status_code=404)


@app.get("/sante")
def sante() -> dict[str, str]:
    return {"etat": "ok", "version": __version__}
