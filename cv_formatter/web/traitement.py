"""Traitement d'un lot : python -m cv_formatter.web.traitement [LOT_ID]  (sinon variable d'environnement LOT_ID).

Exécuté par le job Cloud Run (une exécution par lot, lancée par le service web) ou, en local, par un
processus fils du service web. Même chaîne que la ligne de commande (cv_formatter.__main__.run) :
extraction (en cache sous lots/../cache, partagé entre lots), contrôles, mise en page, PDF, rapport.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
import time
import traceback
import zipfile
from pathlib import Path

from . import lots

ETAPES = {
    "lecture": "Lecture des CV",
    "extraction": "Extraction des informations",
    "controle": "Contre-vérification",
    "mise_en_page": "Mise en page",
    "pdf": "Export PDF",
    "rapport": "Rapport",
}


class Avancement:
    """Écrit statut.json, au plus toutes les 2 secondes (chaque écriture est un envoi vers le bucket)."""

    def __init__(self, lot_id: str, versions: list[str]):
        self.lot_id, self.versions = lot_id, versions
        self.version = versions[0]
        self.derniere = 0.0
        self.etape = ""

    def __call__(self, etape: str, fait: int, total: int) -> None:
        if etape == self.etape and time.monotonic() - self.derniere < 2 and fait < total:
            return
        self.etape, self.derniere = etape, time.monotonic()
        rang = self.versions.index(self.version)
        lots.ecrire_statut(
            self.lot_id, "en_cours", etape=etape, libelle=ETAPES.get(etape, etape), fait=fait, total=total,
            version=self.version, version_rang=rang + 1, versions=len(self.versions),
        )


def _copier_resultats(source: Path, cible: Path) -> list[Path]:
    """Copie les fichiers utiles du dossier de travail vers le lot : résultats et CV d'origine (copies de
    originaux/, quel que soit leur format), sans les .zip décompressés (_entree_zip/)."""
    copies: list[Path] = []
    for fichier in sorted(source.rglob("*")):
        relatif = fichier.relative_to(source)
        dossiers = relatif.parts[1:-1]  # sous le dossier de la version
        if not fichier.is_file() or any(d.startswith("_") for d in dossiers):
            continue
        if fichier.suffix.lower() not in {".pptx", ".pdf", ".md", ".json", ".jpg"} and dossiers != ("originaux",):
            continue
        destination = cible / relatif
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(fichier, destination)
        copies.append(relatif)
    return copies


def traiter(lot_id: str) -> int:
    from ..__main__ import parse_args, run

    lot = lots.charger(lot_id)
    versions = lot.options.versions
    avancement = Avancement(lot_id, versions)
    lots.ecrire_statut(lot_id, "en_cours", etape="lecture", libelle=ETAPES["lecture"], fait=0, total=len(lot.fichiers),
                       version=versions[0], version_rang=1, versions=len(versions))
    sortie_lot = lot.dossier / "sortie"
    codes: list[int] = []
    with tempfile.TemporaryDirectory(prefix="lot_") as tmp:
        travail = Path(tmp)
        entree = travail / "entree"
        shutil.copytree(lot.dossier / "entree", entree)
        for version in versions:
            avancement.version = version
            sortie = travail / "sortie" / version
            arguments = [str(entree), "--sortie", str(sortie), "--donnees", str(lots.DONNEES / "cache"),
                         "--cache-par-empreinte", "--exporter-json", "--photos", str(sortie / "photos"),
                         "--originaux", str(sortie / "originaux")]
            if version == "anonyme":
                arguments.append("--anonymiser")
            if lot.options.sans_coordonnees:
                arguments.append("--sans-coordonnees")
            # Le détail (noms des consultants) va dans le rapport du lot, pas dans les journaux Cloud Logging.
            with contextlib.redirect_stdout(io.StringIO()):
                codes.append(run(parse_args(arguments), progress=avancement))
        copies = _copier_resultats(travail / "sortie", sortie_lot)
    # Archive de tout le lot : PPTX, PDF, rapports et photos du rapport, rangés par version, puis les CV d'origine
    # (un fichier par CV, y compris ceux extraits d'un .zip ; identiques d'une version à l'autre : une seule fois).
    # Photo retenue : nommée d'après le CV (« CV Logiclever - Jean DUPONT.jpg ») plutôt que l'empreinte du fichier.
    noms_photos: dict[Path, Path] = {}
    for version in versions:
        for cv in (lots.lire_json(sortie_lot / version / "rapport.json") or {}).get("cv", []):
            if cv.get("photo_fichier") and cv.get("pptx") and not cv.get("photo_retiree"):
                noms_photos[Path(version, cv["photo_fichier"])] = Path(version, Path(cv["pptx"]).with_suffix(".jpg"))
    with zipfile.ZipFile(sortie_lot / "resultats.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for relatif in copies:
            if relatif.parts[1:-1] == ("originaux",):
                if relatif.parts[0] == versions[0]:
                    archive.write(sortie_lot / relatif, f"CV d'origine/{relatif.name}")
            elif relatif.name != "rapport.json":
                dans_zip = noms_photos.get(relatif, relatif)
                archive.write(sortie_lot / relatif, f"{lots.VERSIONS[dans_zip.parts[0]]}/{Path(*dans_zip.parts[1:])}")
    reussi = all(code in (0, 1) for code in codes)  # 1 : au moins un CV en échec (détaillé dans le rapport)
    lots.ecrire_statut(lot_id, "termine" if reussi else "echec", **({} if reussi else {"message": "aucun CV n'a pu être traité"}))
    print(f"lot {lot_id} : {len(lot.fichiers)} fichier(s), versions {'+'.join(versions)}, codes {codes}")
    return 0 if reussi else 1


def main() -> None:
    lot_id = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("LOT_ID", "")
    try:
        code = traiter(lot_id)
    except Exception as exc:
        traceback.print_exc()
        try:
            lots.ecrire_statut(lot_id, "echec", message=f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
