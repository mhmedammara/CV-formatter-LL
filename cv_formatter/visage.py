"""Contrôle de la photo : la photo retenue contient-elle bien un visage ?

Détecteur YuNet d'OpenCV (modèle ONNX de 230 Ko, licence MIT, fourni dans assets/), exécuté en
local : gratuit, hors ligne et indépendant du modèle de langage qui a choisi la photo. Il évite
qu'un logo, un badge ou une illustration se retrouve dans le cadre photo du CV.
"""

from __future__ import annotations

import os
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from PIL import Image

from .config import ASSETS_DIR

if TYPE_CHECKING:
    import cv2

MODEL_PATH = ASSETS_DIR / "face_detection_yunet_2023mar.onnx"
MODEL_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
)
SCORE_THRESHOLD = 0.6  # confiance minimale du détecteur
MIN_FACE_RATIO = 0.12  # le visage doit occuper au moins 12 % de la largeur de la photo


@dataclass
class Face:
    score: float
    box: tuple[float, float, float, float]  # x, y, largeur, hauteur, en pixels de l'image analysée


@dataclass
class FaceCheck:
    found: bool | None  # None : contrôle impossible (OpenCV ou modèle indisponible)
    score: float = 0.0
    detail: str = ""


@lru_cache(maxsize=1)
def _model_ready() -> bool:
    if MODEL_PATH.exists() and MODEL_PATH.stat().st_size > 100_000:
        return True
    try:
        with urllib.request.urlopen(MODEL_URL, timeout=60) as response:
            MODEL_PATH.write_bytes(response.read())
        return True
    except OSError:
        return False


def _detect_faces(detector: cv2.FaceDetectorYN, frame: cv2.typing.MatLike) -> cv2.typing.MatLike | None:
    """Visages trouvés par YuNet, ou None : OpenCV renvoie None quand il n'y en a aucun (ses annotations l'ignorent)."""
    return detector.detect(frame)[1]


def _faces(image: Image.Image) -> list[Face] | str:
    """Visages trouvés dans l'image (coordonnées de l'image), ou la raison pour laquelle on n'a pas pu chercher."""
    os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")  # OpenCV 5 est bavard sur stderr
    try:
        import cv2
        import numpy as np
    except ImportError:
        return "OpenCV non installé"
    if not _model_ready():
        return "modèle de détection de visage indisponible"
    rgb = image.convert("RGB")
    if max(rgb.size) > 640:  # YuNet est plus fiable sur une image de taille modeste
        rgb.thumbnail((640, 640))
    ratio = image.width / rgb.width
    frame = cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2BGR)
    height, width = frame.shape[:2]
    detector = cv2.FaceDetectorYN.create(str(MODEL_PATH), "", (width, height), SCORE_THRESHOLD, 0.3, 50)
    found = _detect_faces(detector, frame)
    if found is None:
        return []
    return [Face(float(f[-1]), (float(f[0]) * ratio, float(f[1]) * ratio, float(f[2]) * ratio, float(f[3]) * ratio)) for f in found]


def find_face(image: Image.Image) -> Face | None:
    """Visage le plus net de l'image, ou None."""
    faces = _faces(image)
    return max(faces, key=lambda f: f.score) if isinstance(faces, list) and faces else None


def check_face(image: Image.Image) -> FaceCheck:
    """Cherche un visage net et suffisamment grand dans la photo recadrée."""
    faces = _faces(image)
    if isinstance(faces, str):
        return FaceCheck(None, detail=faces)
    if not faces:
        return FaceCheck(False, detail="aucun visage détecté")
    best = max(faces, key=lambda f: f.score)
    face_width = best.box[2]
    if face_width < MIN_FACE_RATIO * image.width:
        return FaceCheck(False, best.score, f"visage trop petit pour une photo de profil ({face_width / image.width:.0%} de la largeur)")
    return FaceCheck(True, best.score, f"visage détecté (confiance {best.score:.0%})")
