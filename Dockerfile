# CV Formatter Logiclever — image Linux pour Google Cloud Run.
#   service web (commande par défaut) : python -m cv_formatter.web
#   job de traitement d'un lot        : python -m cv_formatter.web.traitement   (LOT_ID en variable d'environnement)
#
# Sous Linux, le PDF est produit par LibreOffice (pas de PowerPoint), à partir d'une copie du PPTX calée sur le
# rendu de PowerPoint (cv_formatter/libreoffice.py). La version de LibreOffice et les polices font partie du
# rendu : après une mise à jour de l'image de base, relancer le contrôle du calage (tests/test_rendu_linux.py).
FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata \
    CV_FORMATTER_DONNEES=/donnees \
    PORT=8080

# LibreOffice : PPTX -> PDF et Word -> PDF ; Tesseract (français) : OCR des CV scannés.
# Liberation Sans : métriques identiques à Arial, police des puces « • » du modèle (sans elle, LibreOffice
# prend DejaVu Sans, plus haute, et décale les lignes à puce).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libreoffice-impress libreoffice-writer \
        tesseract-ocr tesseract-ocr-fra \
        fontconfig fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-cloud.txt ./
RUN pip install -r requirements-cloud.txt

# Polices Lexend installées pour LibreOffice (le modèle Google Slides n'en embarque que des sous-ensembles).
COPY cv_formatter/assets/fonts/ /usr/local/share/fonts/lexend/
RUN fc-cache -f && fc-list | grep -q Lexend

COPY cv_formatter ./cv_formatter

# Utilisateur sans privilèges (LibreOffice ouvre des documents déposés par les utilisateurs).
RUN useradd --uid 1000 --create-home app && mkdir -p /donnees && chown app:app /donnees
USER app

EXPOSE 8080
CMD ["python", "-m", "cv_formatter.web"]
