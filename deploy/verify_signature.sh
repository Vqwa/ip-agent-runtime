#!/usr/bin/env bash
# H013 deploy gate: verify the runtime image was signed by our cosign key BEFORE deploying it,
# and PIN to the immutable digest so a mutable tag can't be swapped between verify and rollout.
# Run this in the deploy path; it prints the verified `…@sha256:…` ref on stdout — feed THAT to
# `gcloud run deploy --image`, never the tag:
#
#   IMG="$(./deploy/verify_signature.sh europe-west1-docker.pkg.dev/ip-agent-runtime/runtime/ip-agent-runtime:v10)"
#   gcloud run deploy ip-agent-runtime --image "$IMG" …
#
# A non-zero exit blocks the rollout, so an unsigned/tampered image can never reach Cloud Run.
# Requires: cosign on PATH; gcloud (or crane) to resolve a tag -> digest.
set -euo pipefail

IMAGE="${1:?usage: verify_signature.sh <image-ref>  # prints the verified @sha256 digest ref}"
# Verify against the committed PUBLIC key (deploy/cosign.pub) — no KMS API call, so no
# quota-project / ADC pitfalls, and the verifier needs no KMS access. The private half lives
# only in Cloud KMS (cosign-image-signer), used only by the build's sign step.
PUBKEY="${COSIGN_PUBKEY:-$(dirname "$0")/cosign.pub}"

# ---------------------------------------------------------------------------
# 1. Pin to an immutable digest FIRST. If the caller already passed a digest, use it; otherwise
#    resolve the tag exactly once via the registry. We then verify AND return this digest, so the
#    deploy uses the same bytes we verified — a tag retag (needs Artifact Registry write) between
#    verify and Cloud Run's own resolve can no longer swap in an unsigned image.
# ---------------------------------------------------------------------------
if [[ "${IMAGE}" == *@sha256:* ]]; then
  DIGEST_REF="${IMAGE}"
else
  digest="$(gcloud artifacts docker images describe "${IMAGE}" --format='value(image_summary.digest)' 2>/dev/null || true)"
  if [[ -z "${digest}" ]] && command -v crane >/dev/null 2>&1; then
    digest="$(crane digest "${IMAGE}" 2>/dev/null || true)"
  fi
  if [[ -z "${digest}" ]]; then
    echo "[verify_signature] FATAL: could not resolve ${IMAGE} to a digest (need gcloud or crane)" >&2
    exit 1
  fi
  DIGEST_REF="${IMAGE%:*}@${digest}"   # strip the :tag, append @sha256:…
fi

# All human output goes to stderr so stdout carries ONLY the digest ref (for $(...) capture).
echo "[verify_signature] verifying ${DIGEST_REF} against ${PUBKEY}" >&2
cosign verify --key "${PUBKEY}" "${DIGEST_REF}" >/dev/null
echo "[verify_signature] OK: image digest is signed by the runtime cosign key" >&2

# Confirm the SPDX SBOM attestation is present on the SAME digest (visibility, not blocking).
if cosign verify-attestation --key "${PUBKEY}" --type spdxjson "${DIGEST_REF}" >/dev/null 2>&1; then
  echo "[verify_signature] OK: SPDX SBOM attestation present" >&2
else
  echo "[verify_signature] WARN: no SPDX SBOM attestation found (non-blocking)" >&2
fi

# The ONLY thing on stdout: the immutable, verified ref the deploy must use verbatim.
echo "${DIGEST_REF}"

# ---------------------------------------------------------------------------
# FURTHER HARDENING (documented, not yet enforced): platform-level Binary Authorization on the
# Cloud Run service makes the signature check mandatory even for an out-of-band deploy that skips
# this script. Digest-pinning above closes the verify->deploy race for the scripted path; Binary
# Authorization closes it for every path. To enable:
#   1. gcloud services enable binaryauthorization.googleapis.com
#   2. Create a sigstore-signing check policy referencing the cosign public key
#      (gcloud kms keys versions get-public-key 1 --key cosign-image-signer ...).
#   3. Attach in DRY_RUN first (audit-log-only) to confirm it would pass, then flip to enforced and
#      deploy with --binary-authorization=default.
# Left as a deliberate follow-up: a misconfigured enforced policy can wedge all deploys, so it needs
# a live dry-run soak before enforcement.
# ---------------------------------------------------------------------------
