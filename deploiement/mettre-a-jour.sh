#!/usr/bin/env bash
# Mise à jour rapide du CV Formatter sur Google Cloud Run (Cloud Shell, depuis la racine du dépôt).
#
#   bash deploiement/mettre-a-jour.sh [branche]        (défaut : main)
#
# Pour une nouvelle version du code seulement : le déploiement initial (deployer.sh) doit avoir été fait.
# Construit l'image (couches LibreOffice et Python reprises de la précédente), puis met à jour le job de traitement
# ET le service web en parallèle (c'est le job qui lit les CV et recadre la photo : ne mettre à jour que le service
# ne changerait rien au traitement). Ne touche ni au bucket (la durée de conservation n'est pas à repasser), ni au
# secret OpenAI, ni aux comptes de service, ni à IAP.
# Paramètres facultatifs : PROJET=… REGION=…
set -euo pipefail

BRANCHE="${1:-main}"
PROJET="${PROJET:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-europe-west9}"
SERVICE="cv-formatter"
JOB="cv-formatter-traitement"
DEPOT="cv-formatter"

[ -n "$PROJET" ] || { echo "Projet inconnu : gcloud config set project MON-PROJET (ou PROJET=… bash $0)"; exit 1; }
[ -f Dockerfile ] || { echo "Lancer depuis la racine du dépôt (dossier contenant le Dockerfile)."; exit 1; }

echo "== Récupération de la branche ${BRANCHE}"
git fetch --quiet origin
git checkout --quiet -B "$BRANCHE" "origin/$BRANCHE"
VERSION="$(git rev-parse --short HEAD)"
IMAGE="${REGION}-docker.pkg.dev/${PROJET}/${DEPOT}/cv-formatter:${VERSION}"
DERNIERE="${REGION}-docker.pkg.dev/${PROJET}/${DEPOT}/cv-formatter:derniere"
echo "   version ${VERSION} · projet ${PROJET} · région ${REGION}"

echo "== Construction de l'image"
if gcloud artifacts docker images describe "$IMAGE" --project "$PROJET" >/dev/null 2>&1; then
  echo "   déjà construite pour cette version."
else
  CONFIG="$(mktemp)"
  cat > "$CONFIG" <<YAML
steps:
- name: gcr.io/cloud-builders/docker
  entrypoint: bash
  args: ['-c', 'docker pull ${DERNIERE} || true']
- name: gcr.io/cloud-builders/docker
  env: ['DOCKER_BUILDKIT=1']
  args: ['build', '--cache-from', '${DERNIERE}', '--build-arg', 'BUILDKIT_INLINE_CACHE=1',
         '-t', '${IMAGE}', '-t', '${DERNIERE}', '.']
images: ['${IMAGE}', '${DERNIERE}']
YAML
  gcloud builds submit --project "$PROJET" --config "$CONFIG" .
  rm -f "$CONFIG"
fi

echo "== Mise à jour du job et du service (en parallèle)"
gcloud run jobs update "$JOB" --project "$PROJET" --region "$REGION" --image "$IMAGE" --quiet &
PID_JOB=$!
gcloud run services update "$SERVICE" --project "$PROJET" --region "$REGION" --image "$IMAGE" \
  --update-env-vars "CV_FORMATTER_VERSION=${VERSION}" --quiet &
PID_SERVICE=$!
ECHEC=0
wait "$PID_JOB" || { echo "!! Échec de la mise à jour du job ${JOB}"; ECHEC=1; }
wait "$PID_SERVICE" || { echo "!! Échec de la mise à jour du service ${SERVICE}"; ECHEC=1; }
[ "$ECHEC" = 0 ] || exit 1

URL="$(gcloud run services describe "$SERVICE" --project "$PROJET" --region "$REGION" --format 'value(status.url)')"
printf '\n\033[1;32mEn ligne : version %s.\033[0m %s (le pied de page doit afficher %s)\n' "$VERSION" "$URL" "$VERSION"
[ "$BRANCHE" = main ] || echo "Attention : ce n'est pas main. Fusionnez la branche dans main pour que main corresponde à ce qui est en ligne."
