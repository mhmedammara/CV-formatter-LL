#!/usr/bin/env bash
# Déploiement du CV Formatter Logiclever sur Google Cloud Run (à lancer dans Cloud Shell, depuis la racine du dépôt).
#
#   bash deploiement/deployer.sh
#
# Relançable à volonté : crée ce qui manque, met à jour le reste (nouvelle version du code = relancer le script).
# Paramètres (variables d'environnement, toutes facultatives) :
#   PROJET     projet Google Cloud              (défaut : projet courant de gcloud)
#   REGION     région                           (défaut : europe-west9, Paris — tarif « Tier 1 »)
#   DOMAINE    domaine Google Workspace autorisé (défaut : logiclever.com)
#   BUCKET     bucket des lots et du cache      (défaut : <projet>-cv-formatter)
#   RETENTION  jours de conservation des lots   (défaut : 30)
set -euo pipefail

PROJET="${PROJET:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-europe-west9}"
DOMAINE="${DOMAINE:-logiclever.com}"
BUCKET="${BUCKET:-${PROJET}-cv-formatter}"
RETENTION="${RETENTION:-30}"
SERVICE="cv-formatter"
JOB="cv-formatter-traitement"
DEPOT="cv-formatter"
SECRET="openai-api-key"
SA_WEB="cv-formatter-web"
SA_JOB="cv-formatter-job"

[ -n "$PROJET" ] || { echo "Projet inconnu : gcloud config set project MON-PROJET (ou PROJET=… bash $0)"; exit 1; }
[ -f Dockerfile ] || { echo "Lancer depuis la racine du dépôt (dossier contenant le Dockerfile)."; exit 1; }
NUMERO="$(gcloud projects describe "$PROJET" --format='value(projectNumber)')"
COMPTE_WEB="${SA_WEB}@${PROJET}.iam.gserviceaccount.com"
COMPTE_JOB="${SA_JOB}@${PROJET}.iam.gserviceaccount.com"
VERSION="$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)"
IMAGE="${REGION}-docker.pkg.dev/${PROJET}/${DEPOT}/cv-formatter:${VERSION}"
# Bucket monté en /donnees. Sans cache de métadonnées : la page voit aussitôt l'avancement écrit par le job.
# Dossiers implicites (défaut de Cloud Run, rendu explicite) : la règle de suppression à 30 jours peut effacer
# les marqueurs de dossier sans masquer les lots récents. uid/gid : utilisateur « app » de l'image.
VOLUME="name=donnees,type=cloud-storage,bucket=${BUCKET},mount-options=implicit-dirs=true;metadata-cache-ttl-secs=0;uid=1000;gid=1000"
# Un compte de service ou une API tout juste créés mettent jusqu'à une minute à être visibles partout : on réessaie.
reessayer() {
  local essai
  for essai in 1 2 3 4 5 6 7 8 9 10 11 12; do
    "$@" && return 0
    echo "  … pas encore prêt, nouvel essai dans 10 s (${essai}/12)"; sleep 10
  done
  "$@"
}
etape() { printf '\n\033[1;33m== %s\033[0m\n' "$*"; }

echo "Projet ${PROJET} (n° ${NUMERO}) · région ${REGION} · domaine ${DOMAINE} · bucket ${BUCKET} · conservation ${RETENTION} j"

etape "1/8 API Google Cloud"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com iap.googleapis.com storage.googleapis.com --project "$PROJET"

etape "2/8 Dépôt d'images (Artifact Registry) avec nettoyage automatique"
if ! gcloud artifacts repositories describe "$DEPOT" --location "$REGION" --project "$PROJET" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$DEPOT" --repository-format docker --location "$REGION" --project "$PROJET" \
    --description "Images du CV Formatter"
fi
# Seules les 2 dernières images sont gardées (la version en service et la précédente, pour revenir en arrière) :
# le stockage reste proche de la part gratuite (0,5 Go).
cat > /tmp/nettoyage-images.json <<'JSON'
[
  {"name": "garder-les-2-dernieres", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 2}},
  {"name": "supprimer-les-autres", "action": {"type": "Delete"}, "condition": {"olderThan": "1d"}}
]
JSON
gcloud artifacts repositories set-cleanup-policies "$DEPOT" --location "$REGION" --project "$PROJET" \
  --policy /tmp/nettoyage-images.json --no-dry-run >/dev/null

etape "3/8 Construction de l'image (Cloud Build) : ${IMAGE}"
# Déjà construite pour cette version (relance après un échec plus loin) : rien à refaire.
if gcloud artifacts docker images describe "$IMAGE" --project "$PROJET" >/dev/null 2>&1; then
  echo "Image déjà construite pour la version ${VERSION}."
else
  # Cloud Build ne garde rien d'une construction à l'autre : on repart de l'image précédente (« derniere »), dont
  # les couches LibreOffice et Python sont réutilisées tant que le Dockerfile et les dépendances n'ont pas changé.
  # Une modification du code seul ne reconstruit que la dernière couche (≈ 1 à 2 min au lieu de 6).
  DERNIERE="${REGION}-docker.pkg.dev/${PROJET}/${DEPOT}/cv-formatter:derniere"
  cat > /tmp/cloudbuild-cv.yaml <<YAML
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
  gcloud builds submit --project "$PROJET" --config /tmp/cloudbuild-cv.yaml .
fi

etape "4/8 Bucket des lots (suppression automatique après ${RETENTION} jours)"
if ! gcloud storage buckets describe "gs://${BUCKET}" --project "$PROJET" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" --project "$PROJET" --location "$REGION" \
    --uniform-bucket-level-access --public-access-prevention
fi
cat > /tmp/cycle-de-vie.json <<JSON
{"rule": [{"action": {"type": "Delete"}, "condition": {"age": ${RETENTION}}}]}
JSON
gcloud storage buckets update "gs://${BUCKET}" --lifecycle-file /tmp/cycle-de-vie.json >/dev/null

etape "5/8 Clé OpenAI (Secret Manager)"
if ! gcloud secrets describe "$SECRET" --project "$PROJET" >/dev/null 2>&1; then
  gcloud secrets create "$SECRET" --project "$PROJET" --replication-policy user-managed --locations "$REGION"
  read -rsp "Collez la clé OpenAI (sk-…), elle ne s'affiche pas : " CLE; echo
  printf '%s' "$CLE" | gcloud secrets versions add "$SECRET" --project "$PROJET" --data-file=-
  unset CLE
else
  echo "Secret ${SECRET} déjà présent (nouvelle clé : gcloud secrets versions add ${SECRET} --data-file=-)."
fi

etape "6/8 Comptes de service et droits (le service web ne voit pas la clé OpenAI)"
for SA in "$SA_WEB" "$SA_JOB"; do
  gcloud iam service-accounts describe "${SA}@${PROJET}.iam.gserviceaccount.com" --project "$PROJET" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "$SA" --project "$PROJET" --display-name "CV Formatter (${SA#cv-formatter-})"
done
for COMPTE in "$COMPTE_WEB" "$COMPTE_JOB"; do
  reessayer gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" --member "serviceAccount:${COMPTE}" \
    --role roles/storage.objectUser >/dev/null
done
reessayer gcloud secrets add-iam-policy-binding "$SECRET" --project "$PROJET" --member "serviceAccount:${COMPTE_JOB}" \
  --role roles/secretmanager.secretAccessor >/dev/null

etape "7/8 Job de traitement des lots (${JOB})"
gcloud run jobs deploy "$JOB" --project "$PROJET" --region "$REGION" --image "$IMAGE" \
  --service-account "$COMPTE_JOB" \
  --command python --args=-m,cv_formatter.web.traitement \
  --set-secrets OPENAI_API_KEY="${SECRET}:latest" \
  --set-env-vars CV_FORMATTER_DONNEES=/donnees \
  --cpu 1 --memory 2Gi --tasks 1 --max-retries 1 --task-timeout 3600 \
  --add-volume "$VOLUME" --add-volume-mount volume=donnees,mount-path=/donnees
reessayer gcloud run jobs add-iam-policy-binding "$JOB" --project "$PROJET" --region "$REGION" \
  --member "serviceAccount:${COMPTE_WEB}" --role roles/run.jobsExecutorWithOverrides >/dev/null

etape "8/8 Service web (${SERVICE}) protégé par IAP : comptes @${DOMAINE} uniquement"
gcloud run deploy "$SERVICE" --project "$PROJET" --region "$REGION" --image "$IMAGE" \
  --service-account "$COMPTE_WEB" \
  --no-allow-unauthenticated --iap \
  --execution-environment gen2 --cpu 1 --memory 512Mi --min-instances 0 --max-instances 2 --concurrency 40 \
  --timeout 300 \
  --set-env-vars "CV_FORMATTER_EXECUTION=cloudrun,CV_FORMATTER_JOB=${JOB},CV_FORMATTER_REGION=${REGION},CV_FORMATTER_DONNEES=/donnees,CV_FORMATTER_RETENTION_JOURS=${RETENTION}" \
  --add-volume "$VOLUME" --add-volume-mount volume=donnees,mount-path=/donnees
# IAP appelle le service avec son propre compte de service : il doit pouvoir l'invoquer.
gcloud beta services identity create --service iap.googleapis.com --project "$PROJET" >/dev/null
gcloud run services add-iam-policy-binding "$SERVICE" --project "$PROJET" --region "$REGION" \
  --member "serviceAccount:service-${NUMERO}@gcp-sa-iap.iam.gserviceaccount.com" --role roles/run.invoker >/dev/null
gcloud iap web add-iam-policy-binding --project "$PROJET" --region "$REGION" \
  --resource-type cloud-run --service "$SERVICE" \
  --member "domain:${DOMAINE}" --role roles/iap.httpsResourceAccessor >/dev/null

URL="$(gcloud run services describe "$SERVICE" --project "$PROJET" --region "$REGION" --format 'value(status.url)')"
printf '\n\033[1;32mDéployé (version %s).\033[0m Adresse à partager avec l'"'"'équipe : %s\n' "$VERSION" "$URL"
echo "Accès : comptes Google @${DOMAINE}. Lots et cache supprimés au bout de ${RETENTION} jours."
