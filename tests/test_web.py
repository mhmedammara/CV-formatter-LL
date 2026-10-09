"""Service web et job de traitement (données fictives, sans appel à OpenAI ni à Google Cloud)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

import pymupdf
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

import cv_formatter.__main__ as cli
from cv_formatter.controle import fingerprint
from cv_formatter.export_pdf import PdfExport
from cv_formatter.extraction import EXTRACTION_VERSION
from cv_formatter.web import app as web_app
from cv_formatter.web import lancement, lots, traitement
from test_cv_formatter import SOURCE, make_cv

MOI = {"X-Goog-Authenticated-User-Email": "accounts.google.com:jeanne.commerciale@logiclever.com"}
PDF = b"%PDF-1.4 fictif"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(lots, "DONNEES", tmp_path)
    lances: list[str] = []
    monkeypatch.setattr(lancement, "lancer", lances.append)
    c = TestClient(web_app.app)
    c.lances = lances  # type: ignore[attr-defined]
    return c


def _nouveau(client: TestClient, **options: Any) -> str:
    return client.post("/api/lots", json={"options": options}, headers=MOI).json()["id"]


def _deposer(client: TestClient, lot: str, nom: str, contenu: bytes = PDF):
    return client.post(f"/api/lots/{lot}/fichiers", files={"fichier": (nom, contenu)}, headers=MOI)


def test_depot_lancement_et_historique(client: TestClient):
    moi = client.get("/api/moi", headers=MOI).json()
    assert moi["email"] == "jeanne.commerciale@logiclever.com" and moi["retention_jours"] == lots.RETENTION_JOURS
    lot = _nouveau(client, versions=["nominatif", "anonyme"])
    assert _deposer(client, lot, "CV Jean.pdf").json()["nom"] == "CV Jean.pdf"
    assert _deposer(client, lot, "cv jean.docx").json()["nom"] == "cv jean (2).docx"  # même nom, autre extension
    assert _deposer(client, lot, "../../../etc/passwd.pdf").json()["nom"] == "passwd.pdf"
    assert client.delete(f"/api/lots/{lot}/fichiers/passwd.pdf").status_code == 200
    vue = client.post(f"/api/lots/{lot}/lancer", json={"options": {"versions": ["anonyme"]}}).json()
    assert [f["nom"] for f in vue["fichiers"]] == ["CV Jean.pdf", "cv jean (2).docx"]
    assert vue["options"]["versions"] == ["anonyme"] and vue["statut"]["etat"] == "en_attente"
    assert client.lances == [lot]  # type: ignore[attr-defined]
    assert client.post(f"/api/lots/{lot}/lancer").status_code == 200 and client.lances == [lot]  # pas relancé  # type: ignore[attr-defined]
    assert [l["id"] for l in client.get("/api/lots", headers=MOI).json()["lots"]] == [lot]
    autre = {"X-Goog-Authenticated-User-Email": "accounts.google.com:autre@logiclever.com"}
    assert client.get("/api/lots", headers=autre).json()["lots"] == []


def test_historique_pagine(client: TestClient):
    ids = []
    for _ in range(45):
        lot = client.post("/api/lots", json={}, headers=MOI).json()["id"]
        _deposer(client, lot, "cv.pdf")
        client.post(f"/api/lots/{lot}/lancer", headers=MOI)
        ids.insert(0, lot)
    page1 = client.get("/api/lots", headers=MOI).json()
    assert (page1["page"], page1["pages"], page1["total"]) == (1, 3, 45)
    assert [l["id"] for l in page1["lots"]] == ids[:20]
    assert [l["id"] for l in client.get("/api/lots?page=3", headers=MOI).json()["lots"]] == ids[40:]
    assert client.get("/api/lots?page=9", headers=MOI).json()["page"] == 3
    # Lot supprimé par la règle de conservation : retiré de l'historique.
    import shutil
    shutil.rmtree(lots.dossier_lot(ids[0]))
    page1 = client.get("/api/lots", headers=MOI).json()
    assert page1["total"] == 44 and page1["lots"][0]["id"] == ids[1]
    # Index absent (lots lancés avant son introduction) : reconstruit en parcourant les lots.
    lots._index("jeanne.commerciale@logiclever.com").unlink()
    assert client.get("/api/lots", headers=MOI).json()["total"] == 44


def test_regles_de_depot(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    lot = _nouveau(client)
    assert _deposer(client, lot, "notes.txt").status_code == 415
    assert _deposer(client, lot, "vide.pdf", b"").status_code == 400
    monkeypatch.setattr(lots, "MAX_TAILLE", 10)
    assert _deposer(client, lot, "gros.pdf", b"x" * 11).status_code == 413
    monkeypatch.setattr(lots, "MAX_TAILLE", 30 * 1024 * 1024)
    assert client.post(f"/api/lots/{lot}/lancer").status_code == 400  # aucun CV
    _deposer(client, lot, "cv.pdf")
    client.post(f"/api/lots/{lot}/lancer")
    assert _deposer(client, lot, "encore.pdf").status_code == 409


def test_telechargement_limite_au_lot(client: TestClient):
    lot = _nouveau(client)
    sortie = lots.dossier_lot(lot) / "sortie" / "nominatif"
    sortie.mkdir(parents=True)
    (sortie / "CV Logiclever - Jean DUPONT.pdf").write_bytes(PDF)
    (lots.DONNEES / "cache").mkdir()
    (lots.DONNEES / "cache" / "secret.json").write_text("{}")
    ok = client.get(f"/api/lots/{lot}/resultats/nominatif/CV%20Logiclever%20-%20Jean%20DUPONT.pdf?apercu=1")
    assert ok.status_code == 200 and ok.content == PDF and ok.headers["content-disposition"].startswith("inline")
    nomme = client.get(f"/api/lots/{lot}/resultats/nominatif/CV%20Logiclever%20-%20Jean%20DUPONT.pdf?nom=Jean.pdf")
    assert 'filename="Jean.pdf"' in nomme.headers["content-disposition"]
    autre_ext = client.get(f"/api/lots/{lot}/resultats/nominatif/CV%20Logiclever%20-%20Jean%20DUPONT.pdf?nom=..%2Fx.exe")
    assert "x.exe" not in autre_ext.headers["content-disposition"]
    for chemin in ("../lot.json", "..%2F..%2F..%2Fcache%2Fsecret.json", "nominatif/../../lot.json"):
        assert client.get(f"/api/lots/{lot}/resultats/{chemin}").status_code == 404
    assert client.get("/api/lots/0123456789abcdef0123456789abcdef").status_code == 404
    _deposer(client, lot, "cv.pdf")
    assert client.get(f"/api/lots/{lot}/originaux/cv.pdf").content == PDF
    for nom in ("..%2Flot.json", "lot.json", "autre.pdf"):
        assert client.get(f"/api/lots/{lot}/originaux/{nom}").status_code == 404
    assert client.get("/api/lots/..%2F..%2Fcache").status_code == 404


def _cv_source(dossier: Path) -> Path:
    """CV PDF fictif (texte de SOURCE) et son extraction en cache, comme si OpenAI l'avait déjà lu."""
    pdf = dossier / "CV Jean DUPONT.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    for i, ligne in enumerate(SOURCE.splitlines()):
        page.insert_text((40, 50 + 14 * i), ligne, fontsize=9)
    doc.save(str(pdf))
    cv = make_cv()
    empreinte = hashlib.sha256(pdf.read_bytes()).hexdigest()
    cache = lots.DONNEES / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / f"{empreinte}.json").write_text(json.dumps({
        "_meta": {"source": pdf.name, "sha256": empreinte, "extracteur": "essai", "effort": "high",
                  "version_extraction": EXTRACTION_VERSION, "date": dt.datetime.now().isoformat(), "empreinte": fingerprint(cv)},
        "cv": cv.model_dump(),
    }, ensure_ascii=False), encoding="utf-8")
    return pdf


def test_job_traite_un_lot_sans_appel_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(lots, "DONNEES", tmp_path / "donnees")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cli, "export_pdfs", lambda pptx: PdfExport())  # moteur PDF hors sujet ici
    source = _cv_source(tmp_path)
    lot = lots.creer("jeanne@logiclever.com", lots.Options(["nominatif", "anonyme"]))
    (lot.dossier / "entree").mkdir(parents=True)
    (lot.dossier / "entree" / source.name).write_bytes(source.read_bytes())
    lot.fichiers, lot.lance_le = [{"nom": source.name, "taille": source.stat().st_size}], lots.maintenant()
    lot.enregistrer()
    assert traitement.traiter(lot.id) == 0
    assert lots.statut(lots.charger(lot.id))["etat"] == "termine"
    sortie = lot.dossier / "sortie"
    nominatif = json.loads((sortie / "nominatif" / "rapport.json").read_text(encoding="utf-8"))
    anonyme = json.loads((sortie / "anonyme" / "rapport.json").read_text(encoding="utf-8"))
    assert [c["nom"] for c in nominatif["cv"]] == ["Jean DUPONT"] and nominatif["cv"][0]["erreur"] is None
    assert (sortie / "nominatif" / nominatif["cv"][0]["pptx"]).exists()
    # Version anonyme : ni le nom ni le prénom dans le PPTX (texte, métadonnées). Le rapport, interne, cite le fichier source.
    assert anonyme["cv"][0]["nom"] == "J. D."
    with zipfile.ZipFile(sortie / "anonyme" / anonyme["cv"][0]["pptx"]) as pptx:
        xml = " ".join(pptx.read(n).decode("utf-8", "ignore") for n in pptx.namelist() if n.endswith(".xml"))
    assert "DUPONT" not in xml and "Jean" not in xml
    # CV d'origine : copie gardée avec chaque version (bouton « Original »), une seule fois dans l'archive.
    for version, rapport in (("nominatif", nominatif), ("anonyme", anonyme)):
        assert rapport["cv"][0]["original"] == "originaux/CV Jean DUPONT.pdf"
        assert (sortie / version / "originaux" / "CV Jean DUPONT.pdf").read_bytes() == source.read_bytes()
    with zipfile.ZipFile(sortie / "resultats.zip") as archive:
        noms = archive.namelist()
    assert "CV nominatifs/CV Logiclever - Jean DUPONT.pptx" in noms and "CV anonymes/rapport.md" in noms
    assert [n for n in noms if "originaux" in n or n.startswith("CV d'origine/")] == ["CV d'origine/CV Jean DUPONT.pdf"]
    assert not any(n.endswith("rapport.json") for n in noms)
    assert "CV nominatifs/CV Logiclever - Jean DUPONT.json" in noms
    donnees_anonymes = json.loads((sortie / "anonyme" / anonyme["cv"][0]["json"]).read_text(encoding="utf-8"))
    assert "DUPONT" not in json.dumps(donnees_anonymes, ensure_ascii=False) and donnees_anonymes["nom_affiche"] == "J. D."


def test_job_garde_l_original_d_un_cv_extrait_d_un_zip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Export Google Drive déposé en .zip : chaque CV qu'il contient a son original, téléchargeable seul et rangé
    dans « CV d'origine » ; ce qui n'est pas un CV (un PowerPoint) et le dossier décompressé ne sont pas repris."""
    monkeypatch.setattr(lots, "DONNEES", tmp_path / "donnees")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cli, "export_pdfs", lambda pptx: PdfExport())
    source = _cv_source(tmp_path)
    lot = lots.creer("jeanne@logiclever.com", lots.Options())
    (lot.dossier / "entree").mkdir(parents=True)
    with zipfile.ZipFile(lot.dossier / "entree" / "export.zip", "w") as archive:
        archive.writestr(f"Candidats/{source.name}", source.read_bytes())
        archive.writestr("Candidats/Présentation.pptx", b"pas un CV")
    lot.fichiers, lot.lance_le = [{"nom": "export.zip", "taille": 1}], lots.maintenant()
    lot.enregistrer()
    assert traitement.traiter(lot.id) == 0
    sortie = lot.dossier / "sortie"
    cv = json.loads((sortie / "nominatif" / "rapport.json").read_text(encoding="utf-8"))["cv"][0]
    assert cv["source"] == source.name and cv["original"] == f"originaux/{source.name}"
    assert not (sortie / "nominatif" / "_entree_zip").exists()
    client = TestClient(web_app.app)
    reponse = client.get(f"/api/lots/{lot.id}/resultats/nominatif/originaux/{source.name}?apercu=1")
    assert reponse.status_code == 200 and reponse.content == source.read_bytes()
    with zipfile.ZipFile(sortie / "resultats.zip") as archive:
        noms = archive.namelist()
    assert f"CV d'origine/{source.name}" in noms
    assert not any("_entree_zip" in n or n.endswith((".zip", "Présentation.pptx")) for n in noms)


def test_job_signale_une_archive_illisible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(lots, "DONNEES", tmp_path / "donnees")
    lot = lots.creer("jeanne@logiclever.com", lots.Options())
    (lot.dossier / "entree").mkdir(parents=True)
    (lot.dossier / "entree" / "export.zip").write_bytes(b"pas une archive")
    lot.lance_le = lots.maintenant()
    lot.enregistrer()
    traitement.traiter(lot.id)
    rapport = json.loads((lot.dossier / "sortie" / "nominatif" / "rapport.json").read_text(encoding="utf-8"))
    assert rapport["cv"][0]["source"] == "export.zip" and "archive ignorée" in rapport["cv"][0]["erreur"]


def test_lot_termine_garde_en_memoire(client: TestClient):
    """Un lot terminé ne change plus : il est servi sans relire le bucket ; un lot en cours est toujours relu."""
    lot = _nouveau(client)
    _deposer(client, lot, "cv.pdf")
    client.post(f"/api/lots/{lot}/lancer")
    lots.ecrire_statut(lot, "en_cours", etape="extraction")
    assert client.get(f"/api/lots/{lot}").json()["statut"]["etat"] == "en_cours"
    lots.ecrire_statut(lot, "termine")
    assert client.get(f"/api/lots/{lot}").json()["statut"]["etat"] == "termine"
    (lots.dossier_lot(lot) / "statut.json").unlink()  # plus relu : la vue en mémoire suffit
    assert client.get(f"/api/lots/{lot}").json()["statut"]["etat"] == "termine"
    assert client.get("/api/lots", headers=MOI).json()["lots"][0]["statut"] == "termine"
