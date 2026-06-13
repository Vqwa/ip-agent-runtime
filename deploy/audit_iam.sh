#!/usr/bin/env bash
# Deploy-blocking IAM/isolation audit for the Hosted Agents runtime (DC-1 + C9 / H38(a)).
#
# Asserts the load-bearing isolation invariants that a Knative manifest cannot
# carry. Exits non-zero on ANY violation so CI blocks the deploy.
#
#   (a) run.invoker policy contains NEITHER allUsers NOR allAuthenticatedUsers.
#   (b) run.invoker members are EXACTLY agent-control@ (+ optional EXPECTED_TEST_INVOKER).
#   (c) the runtime SA's secretAccessor bindings are EXACTLY {e2b-api-key}
#       and on NO other secret (post-H1 the JWT public key is plain env, not a secret).
#   (d) the service is --no-allow-unauthenticated in effect (IAM policy is private).
#   (e) the service's serviceAccountName is the dedicated runtime SA (H1).
#   (f) the runtime SA holds ZERO project-level role bindings (H1).
#   (g) the legacy project-admin SA agent-runtime@ no longer exists (H2).
#   (h) project auditConfigs cover secretmanager DATA_READ+DATA_WRITE and
#       cloudkms DATA_READ (H5, extended for H29).
#
# Usage: deploy/audit_iam.sh   (defaults match the live project; envs override)
set -euo pipefail

PROJECT="${PROJECT:-ip-agent-runtime}"
REGION="${REGION:-europe-west1}"
SERVICE="${SERVICE:-ip-agent-runtime}"
RUNTIME_SA="${RUNTIME_SA:-runtime-svc@${PROJECT}.iam.gserviceaccount.com}"
# The pre-H1 project-admin identity — must stay deleted (H2).
LEGACY_SA="${LEGACY_SA:-agent-runtime@${PROJECT}.iam.gserviceaccount.com}"

# The control-plane (Django) SA — the ONLY principal allowed to invoke (H3).
EXPECTED_INVOKER="${EXPECTED_INVOKER:-serviceAccount:agent-control@${PROJECT}.iam.gserviceaccount.com}"
# Optional CI/integration-test SA also permitted to invoke.
EXPECTED_TEST_INVOKER="${EXPECTED_TEST_INVOKER:-}"

# The ONLY secret the runtime SA may read (post-H1).
ALLOWED_SECRETS=("e2b-api-key")

GCLOUD_COMMON=(--project="$PROJECT")
fail=0
note() { printf '  - %s\n' "$1"; }

echo "== Hosted Agents runtime IAM audit =="
echo "project=$PROJECT region=$REGION service=$SERVICE"
echo

# ----------------------------------------------------------------------------
# Pull the run.invoker policy once.
# ----------------------------------------------------------------------------
run_policy="$(gcloud run services get-iam-policy "$SERVICE" \
  --region="$REGION" "${GCLOUD_COMMON[@]}" --format=json)"

# Members holding roles/run.invoker (empty if the binding is absent).
invoker_members="$(jq -r \
  '.bindings // [] | map(select(.role=="roles/run.invoker")) | .[].members[]?' \
  <<<"$run_policy" | sort -u)"

# (a) No public invoker.
echo "[a] no public run.invoker (allUsers / allAuthenticatedUsers)"
if grep -qxE 'allUsers|allAuthenticatedUsers' <<<"$invoker_members"; then
  note "PUBLIC invoker present on run.invoker — service is internet-open"
  fail=1
else
  echo "    OK: no allUsers/allAuthenticatedUsers"
fi

# (d) Private == no public binding. With ingress=all, the IAM policy is the
#     boundary; (a) passing means --no-allow-unauthenticated is in effect.
echo "[d] --no-allow-unauthenticated in effect (IAM policy is non-public)"
if grep -qxE 'allUsers|allAuthenticatedUsers' <<<"$invoker_members"; then
  note "service is effectively public — --allow-unauthenticated was applied"
  fail=1
else
  echo "    OK: ingress=all but invocation is IAM-gated"
fi

# (b) Invoker members are exactly the expected set.
echo "[b] run.invoker members are only the expected SA(s)"
expected_members="$(printf '%s\n' "$EXPECTED_INVOKER" "$EXPECTED_TEST_INVOKER" \
  | sed '/^$/d' | sort -u)"
unexpected="$(comm -23 <(printf '%s\n' "$invoker_members" | sed '/^$/d') \
                       <(printf '%s\n' "$expected_members"))"
missing="$(comm -13 <(printf '%s\n' "$invoker_members" | sed '/^$/d') \
                    <(printf '%s\n' "$expected_members"))"
if [[ -n "$unexpected" ]]; then
  note "UNEXPECTED run.invoker member(s): $(tr '\n' ' ' <<<"$unexpected")"
  fail=1
fi
if [[ -n "$missing" ]]; then
  note "expected invoker(s) NOT bound: $(tr '\n' ' ' <<<"$missing")"
  fail=1
fi
[[ -z "$unexpected" && -z "$missing" ]] && \
  echo "    OK: invoker set == {$(tr '\n' ' ' <<<"$expected_members")}"

# ----------------------------------------------------------------------------
# (c) Runtime SA secretAccessor must be EXACTLY {e2b-api-key} (post-H1).
#     Walk every secret in the project; flag any extra grant and any missing one.
# ----------------------------------------------------------------------------
echo "[c] runtime SA secretAccessor == {${ALLOWED_SECRETS[*]}} and no other secret"
all_secrets="$(gcloud secrets list "${GCLOUD_COMMON[@]}" \
  --format='value(name)' | awk -F/ '{print $NF}' | sort -u)"

is_allowed() { local s; for s in "${ALLOWED_SECRETS[@]}"; do [[ "$1" == "$s" ]] && return 0; done; return 1; }

granted=()
while IFS= read -r secret; do
  [[ -z "$secret" ]] && continue
  members="$(gcloud secrets get-iam-policy "$secret" "${GCLOUD_COMMON[@]}" \
    --format=json | jq -r \
    '.bindings // [] | map(select(.role=="roles/secretmanager.secretAccessor"))
       | .[].members[]?' | sort -u)"
  if grep -qx "serviceAccount:${RUNTIME_SA}" <<<"$members"; then
    granted+=("$secret")
    if ! is_allowed "$secret"; then
      note "runtime SA has secretAccessor on DISALLOWED secret: $secret"
      fail=1
    fi
  fi
done <<<"$all_secrets"

# Every allowed secret must actually be granted (catches drift / missing grant).
for want in "${ALLOWED_SECRETS[@]}"; do
  if ! printf '%s\n' "${granted[@]:-}" | grep -qx "$want"; then
    note "runtime SA is MISSING secretAccessor on required secret: $want"
    fail=1
  fi
done
echo "    granted: ${granted[*]:-<none>}"

# ----------------------------------------------------------------------------
# (e) The service must run as the dedicated runtime SA (H1).
# ----------------------------------------------------------------------------
echo "[e] service serviceAccountName == $RUNTIME_SA"
service_sa="$(gcloud run services describe "$SERVICE" --region="$REGION" "${GCLOUD_COMMON[@]}" \
  --format='value(spec.template.spec.serviceAccountName)')"
if [[ "$service_sa" != "$RUNTIME_SA" ]]; then
  note "serviceAccountName is '${service_sa:-<empty>}' — expected $RUNTIME_SA"
  fail=1
else
  echo "    OK: service runs as $RUNTIME_SA"
fi

# ----------------------------------------------------------------------------
# (f) + (h) need the project IAM policy (includes auditConfigs); fetch once.
# ----------------------------------------------------------------------------
project_policy="$(gcloud projects get-iam-policy "$PROJECT" --format=json)"

# (f) The runtime SA must hold ZERO project-level role bindings (H1).
echo "[f] runtime SA has no project-level role bindings"
runtime_roles="$(jq -r --arg m "serviceAccount:${RUNTIME_SA}" \
  '.bindings // [] | map(select(.members | index($m))) | .[].role' \
  <<<"$project_policy" | sort -u)"
if [[ -n "$runtime_roles" ]]; then
  note "runtime SA holds PROJECT-level role(s): $(tr '\n' ' ' <<<"$runtime_roles")"
  fail=1
else
  echo "    OK: zero project bindings for $RUNTIME_SA"
fi

# ----------------------------------------------------------------------------
# (g) The legacy project-admin SA must stay deleted (H2).
# ----------------------------------------------------------------------------
echo "[g] legacy SA $LEGACY_SA does not exist"
if gcloud iam service-accounts describe "$LEGACY_SA" "${GCLOUD_COMMON[@]}" >/dev/null 2>&1; then
  note "legacy SA STILL EXISTS: $LEGACY_SA — H2 teardown incomplete"
  fail=1
else
  echo "    OK: $LEGACY_SA is gone"
fi

# ----------------------------------------------------------------------------
# (h) Data Access audit logs (H5; cloudkms added for H29's KMS signer).
# ----------------------------------------------------------------------------
echo "[h] auditConfigs: secretmanager DATA_READ+DATA_WRITE, cloudkms DATA_READ"
audit_has() {  # $1=service $2=logType — allServices coverage counts too
  jq -e --arg svc "$1" --arg lt "$2" \
    '.auditConfigs // [] | map(select(.service==$svc or .service=="allServices"))
       | map(.auditLogConfigs // [] | map(.logType)) | flatten | index($lt)' \
    <<<"$project_policy" >/dev/null
}
for want in "secretmanager.googleapis.com DATA_READ" \
            "secretmanager.googleapis.com DATA_WRITE" \
            "cloudkms.googleapis.com DATA_READ"; do
  svc="${want% *}" lt="${want#* }"
  if audit_has "$svc" "$lt"; then
    echo "    OK: $svc $lt"
  else
    note "auditConfigs MISSING $lt for $svc"
    fail=1
  fi
done

# ----------------------------------------------------------------------------
# Summary.
# ----------------------------------------------------------------------------
echo
if [[ "$fail" -ne 0 ]]; then
  echo "AUDIT RESULT: FAIL — isolation invariant(s) violated; deploy blocked."
  exit 1
fi
echo "AUDIT RESULT: PASS — invoker private + scoped, runtime SA secret-scope intact."
