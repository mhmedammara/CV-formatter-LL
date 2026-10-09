"""Lancement du traitement d'un lot.

- CV_FORMATTER_EXECUTION=cloudrun : exécution du job Cloud Run CV_FORMATTER_JOB (même image), avec LOT_ID en
  variable d'environnement. Jeton d'accès du compte de service fourni par le serveur de métadonnées : aucune
  bibliothèque Google nécessaire. Le compte du service web doit avoir le rôle « Cloud Run Jobs Executor With
  Overrides » sur le job.
- sinon (poste local, essais Docker) : processus fils, détaché du service web.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

from . import lots

METADATA = "http://metadata.google.internal/computeMetadata/v1/"


class LancementImpossible(RuntimeError):
    pass


def _metadata(chemin: str) -> str:
    requete = urllib.request.Request(METADATA + chemin, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(requete, timeout=5) as reponse:
        return reponse.read().decode()


def _cloud_run(lot_id: str) -> None:
    job = os.environ.get("CV_FORMATTER_JOB", "cv-formatter-traitement")
    projet = os.environ.get("CV_FORMATTER_PROJET") or _metadata("project/project-id")
    region = os.environ.get("CV_FORMATTER_REGION") or _metadata("instance/region").rsplit("/", 1)[-1]
    jeton = json.loads(_metadata("instance/service-accounts/default/token"))["access_token"]
    corps = {"overrides": {"containerOverrides": [{"env": [{"name": "LOT_ID", "value": lot_id}]}]}}
    requete = urllib.request.Request(
        f"https://run.googleapis.com/v2/projects/{projet}/locations/{region}/jobs/{job}:run",
        data=json.dumps(corps).encode(),
        headers={"Authorization": f"Bearer {jeton}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(requete, timeout=30):
            pass
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise LancementImpossible(f"le job {job} n'a pas pu être lancé (HTTP {exc.code}) : {detail}") from exc


def _local(lot_id: str) -> None:
    journal = lots.dossier_lot(lot_id) / "traitement.log"
    with open(journal, "ab") as sortie:
        subprocess.Popen(  # noqa: S603 — arguments fixes, identifiant de lot vérifié
            [sys.executable, "-m", "cv_formatter.web.traitement", lot_id],
            stdout=sortie,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def lancer(lot_id: str) -> None:
    if os.environ.get("CV_FORMATTER_EXECUTION", "local") == "cloudrun":
        _cloud_run(lot_id)
    else:
        _local(lot_id)
