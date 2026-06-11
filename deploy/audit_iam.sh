#!/usr/bin/env bash
# Deploy-blocking IAM/isolation audit for the Hosted Agents runtime (DC-1).
#
# Asserts the load-bearing isolation invariants that a Knative manifest cannot
# carry. Exits non-zero on ANY violation so CI blocks the deploy.
#
#   (a) run.invoker policy contains NEITHER allUsers NOR allAuthenticatedUsers.
#   (b) run.invoker members are only EXPECTED_INVOKER (+ optional EXPECTED_TEST_INVOKER).
#   (c) the agent-runtime SA's secretAccessor bindings are EXACTLY
#       {jwt-public-key, e2b-api-key} and on NO other secret.
#   (d) the service is --no-allow-unauthenticated in effect (IAM policy is private).
#
# Usage:
#   EXPECTED_INVOKER=serviceAccount:django-dispatch@<proj>.iam.gserviceaccount.com \
#     deploy/audit_iam.sh
set -euo pipefail

PROJECT="${PROJECT:-ip-agent-runtime}"
REGION="${REGION:-europe-west1}"
SERVICE="${SERVICE:-ip-agent-runtime}"
RUNTIME_SA="${RUNTIME_SA:-agent-runtime@ip-agent-runtime.iam.gserviceaccount.com}"

# The Django dispatch SA that is allowed to invoke. Not yet created — must be
# passed in (e.g. serviceAccount:django-dispatch@<proj>.iam.gserviceaccount.com).
EXPECTED_INVOKER="${EXPECTED_INVOKER:-}"
# Optional CI/integration-test SA also permitted to invoke.
EXPECTED_TEST_INVOKER="${EXPECTED_TEST_INVOKER:-}"

# The ONLY secrets the runtime SA may read.
ALLOWED_SECRETS=("jwt-public-key" "e2b-api-key")

GCLOUD_COMMON=(--project="$PROJECT")
fail=0
note() { printf '  - %s\n' "$1"; }

if [[ -z "$EXPECTED_INVOKER" ]]; then
  echo "FATAL: set EXPECTED_INVOKER (e.g. serviceAccount:django-dispatch@${PROJECT}.iam.gserviceaccount.com)" >&2
  exit 2
fi

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
# (c) Runtime SA secretAccessor must be EXACTLY {jwt-public-key, e2b-api-key}.
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
# Summary.
# ----------------------------------------------------------------------------
echo
if [[ "$fail" -ne 0 ]]; then
  echo "AUDIT RESULT: FAIL — isolation invariant(s) violated; deploy blocked."
  exit 1
fi
echo "AUDIT RESULT: PASS — invoker private + scoped, runtime SA secret-scope intact."
