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

from PIL import Image

from .config import ASSETS_DIR

MODEL_PATH = ASSETS_DIR / "face_detection_yunet_2023mar.onnx"
MODEL_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
)
SCORE_THRESHOLD = 0.6  # confiance minimale du détecteur
MIN_FACE_RATIO = 0.12  # le visage doit occuper au moins 12 % de la largeur de la photo


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


def check_face(image: Image.Image) -> FaceCheck:
    """Cherche un visage net et suffisamment grand dans la photo recadrée."""
    os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")  # OpenCV 5 est bavard sur stderr
    try:
        import cv2
        import numpy as np
    except ImportError:
        return FaceCheck(None, detail="OpenCV non installé")
    if not _model_ready():
        return FaceCheck(None, detail="modèle de détection de visage indisponible")
    rgb = image.convert("RGB")
    if max(rgb.size) > 640:  # YuNet est plus fiable sur une image de taille modeste
        rgb.thumbnail((640, 640))
    frame = cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2BGR)
    height, width = frame.shape[:2]
    detector = cv2.FaceDetectorYN.create(str(MODEL_PATH), "", (width, height), SCORE_THRESHOLD, 0.3, 50)
    _, faces = detector.detect(frame)
    if faces is None or len(faces) == 0:
        return FaceCheck(False, detail="aucun visage détecté")
    best = max(faces, key=lambda f: float(f[-1]))
    score, face_width = float(best[-1]), float(best[2])
    if face_width < MIN_FACE_RATIO * width:
        return FaceCheck(False, score, f"visage trop petit pour une photo de profil ({face_width / width:.0%} de la largeur)")
    return FaceCheck(True, score, f"visage détecté (confiance {score:.0%})")
