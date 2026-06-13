#!/usr/bin/env bash
# H013 deploy gate: verify the runtime image was signed by our cosign KMS key BEFORE
# deploying it. Run this in the deploy path (deploy/release.sh) — a non-zero exit blocks
# the rollout, so an unsigned or tampered image can never reach Cloud Run. This is the
# enforcement layer for the cosign signature the build attaches; platform-level Binary
# Authorization (below) is the documented further-hardening.
#
#   ./deploy/verify_signature.sh <image>@<digest>
#
# Requires: cosign on PATH, gcloud auth (read access to the cosign KMS public key).
set -euo pipefail

IMAGE="${1:?usage: verify_signature.sh <image-ref-with-or-without-digest>}"
# Verify against the committed PUBLIC key (deploy/cosign.pub) — no KMS API call, so no
# quota-project / ADC pitfalls, and the verifier needs no KMS access. The private half
# lives only in Cloud KMS (cosign-image-signer), used only by the build's sign step.
PUBKEY="${COSIGN_PUBKEY:-$(dirname "$0")/cosign.pub}"

echo "[verify_signature] verifying cosign signature on ${IMAGE} against ${PUBKEY}"
cosign verify --key "${PUBKEY}" "${IMAGE}" >/dev/null
echo "[verify_signature] OK: image is signed by the runtime cosign key"

# Confirm the SPDX SBOM attestation is present (visibility, not blocking).
if cosign verify-attestation --key "${PUBKEY}" --type spdxjson "${IMAGE}" >/dev/null 2>&1; then
  echo "[verify_signature] OK: SPDX SBOM attestation present"
else
  echo "[verify_signature] WARN: no SPDX SBOM attestation found (non-blocking)"
fi

# ---------------------------------------------------------------------------
# FURTHER HARDENING (documented, not yet enforced): platform-level Binary
# Authorization on the Cloud Run service makes the signature check mandatory
# even for an out-of-band deploy that skips this script. To enable:
#   1. gcloud services enable binaryauthorization.googleapis.com
#   2. Create a sigstore-signing check policy referencing the cosign public key
#      (gcloud kms keys versions get-public-key 1 --key cosign-image-signer ...).
#   3. Attach in DRY_RUN first (audit-log-only) to confirm it would pass, then flip
#      to enforced and deploy with --binary-authorization=default.
# Left as a deliberate follow-up: a misconfigured enforced policy can wedge all
# deploys, so it needs a live dry-run soak before enforcement.
# ---------------------------------------------------------------------------
