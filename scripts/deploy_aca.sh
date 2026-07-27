#!/usr/bin/env bash
# Deploy Episteme to Azure Container Apps, refusing to point traffic at an image that is not there.
#
# Written after a deploy did exactly that. `docker push` failed with `authentication required` — the
# ACR login had expired — and the `containerapp update` ran anyway, putting **100% of traffic on a
# revision whose image did not exist**. The service stayed up only because Container Apps kept the
# previous revision answering. That is the platform being forgiving, not the deploy being safe.
#
# Two failures made it possible and both are closed here:
#
#   1. The push's exit status was never checked, so a failed push still reached the update.
#   2. Success was judged by the public endpoint, which returned 200 throughout — served by the OLD
#      revision. The endpoint cannot tell you whether the revision you just shipped is running.
#
# So: fail on any error, prove the tag exists in the registry before updating, and afterwards check
# the REVISION's health, rolling traffic back if it did not come up.
set -euo pipefail

TAG="${1:?usage: deploy_aca.sh <tag>   e.g. deploy_aca.sh q34}"
REGISTRY="epistemeacr5675"
IMAGE="${REGISTRY}.azurecr.io/episteme:${TAG}"
APP="episteme"
RG="episteme-ci"
AZ="${AZ:-/c/Program Files/Microsoft SDKs/Azure/CLI2/wbin/az.cmd}"

echo "==> tests must pass before anything is built"
python -m pytest -q -p no:warnings

echo "==> recording the revision currently serving, so there is something to go back to"
PREVIOUS="$("$AZ" containerapp revision list --name "$APP" --resource-group "$RG" \
  --query "[?properties.trafficWeight>\`0\`].name | [0]" -o tsv)"
echo "    currently serving: ${PREVIOUS:-none}"

echo "==> build"
docker build -q -t "$IMAGE" .

echo "==> login and push"
"$AZ" acr login --name "$REGISTRY" >/dev/null
docker push -q "$IMAGE"

# The check that was missing. A push can fail for reasons that do not stop the script — an expired
# token being the one that actually happened — so the registry is asked directly rather than trusted.
echo "==> confirming ${TAG} is really in the registry"
if ! "$AZ" acr repository show-tags --name "$REGISTRY" --repository episteme -o tsv | grep -qx "$TAG"; then
  echo "REFUSING TO DEPLOY: ${TAG} is not in the registry. Nothing was changed." >&2
  exit 1
fi

echo "==> update"
"$AZ" containerapp update --name "$APP" --resource-group "$RG" --image "$IMAGE" \
  --query "properties.latestRevisionName" -o tsv

echo "==> waiting for the new revision to report healthy"
for _ in $(seq 1 30); do
  sleep 6
  STATE="$("$AZ" containerapp revision list --name "$APP" --resource-group "$RG" \
    --query "[?properties.trafficWeight>\`0\`].properties.healthState | [0]" -o tsv || true)"
  [ "$STATE" = "Healthy" ] && { echo "    healthy"; break; }
done

if [ "${STATE:-}" != "Healthy" ]; then
  echo "NEW REVISION IS ${STATE:-unknown} — rolling traffic back to ${PREVIOUS}" >&2
  [ -n "${PREVIOUS:-}" ] && "$AZ" containerapp ingress traffic set --name "$APP" --resource-group "$RG" \
    --revision-weight "${PREVIOUS}=100" >/dev/null
  exit 1
fi

# Deliberately last and deliberately not the only check: a 200 here proves *something* is serving,
# not that it is the revision just shipped.
echo "==> endpoint responds"
curl -fsS -o /dev/null -w "    /healthz %{http_code}\n" \
  "https://episteme.blacksky-e393132e.centralindia.azurecontainerapps.io/healthz"
echo "==> done: ${IMAGE}"
