# Hosted Agents runtime — Cloud Run deploy (Infrastructure-as-Code)

Codifies the live `ip-agent-runtime` Cloud Run service so its isolation flags
stop being uncodified console state (review finding DC-1).

- Project `ip-agent-runtime` (588759994730), region `europe-west1`
- Image `europe-west1-docker.pkg.dev/ip-agent-runtime/runtime/ip-agent-runtime:v3`
  (built/pushed by `../cloudbuild.yaml`)

## Deploy

The Knative manifest covers the container, scaling, concurrency, env, and the
runtime service account — but **not** the IAM invoker binding. Apply both:

```bash
# 1. Service config (idempotent; replaces revision).
gcloud run services replace deploy/service.yaml \
  --region=europe-west1 --project=ip-agent-runtime

# 2. IAM invoker binding (separate; not carried by Knative). Grant ONLY the
#    Django dispatch SA. The service is --no-allow-unauthenticated, so without
#    this binding nobody can invoke it.
gcloud run services add-iam-policy-binding ip-agent-runtime \
  --region=europe-west1 --project=ip-agent-runtime \
  --member=serviceAccount:<django-dispatch-sa>@ip-agent-runtime.iam.gserviceaccount.com \
  --role=roles/run.invoker
```

Replace `<django-dispatch-sa>` with the real Django dispatch SA (see below).

## Rotating secrets

Secrets are mounted from Secret Manager at `:latest`, so rotation needs no
redeploy — add a new version and the next cold start picks it up:

```bash
echo -n "<new value>" | gcloud secrets versions add e2b-api-key \
  --data-file=- --project=ip-agent-runtime
# (The JWT public key is a plain env value on the service since H1 — rotate it
#  by editing deploy/service.yaml's RUNTIME_JWT_PUBLIC_KEY, not via Secret Manager.)
# force fresh containers to pick up :latest immediately
gcloud run services update ip-agent-runtime --region=europe-west1 \
  --project=ip-agent-runtime --no-traffic --tag=rotate && \
gcloud run services update-traffic ip-agent-runtime --region=europe-west1 \
  --project=ip-agent-runtime --to-latest
```

Never grant the runtime SA accessor on any other secret (see invariants).

## Invariants enforced by `audit_iam.sh` (deploy-blocking)

Run in CI after deploy; non-zero exit blocks the rollout:

```bash
EXPECTED_INVOKER=serviceAccount:<django-dispatch-sa>@ip-agent-runtime.iam.gserviceaccount.com \
  deploy/audit_iam.sh
```

- (a) `run.invoker` has NEITHER `allUsers` NOR `allAuthenticatedUsers`.
- (b) `run.invoker` members == only `EXPECTED_INVOKER` (+ optional
  `EXPECTED_TEST_INVOKER`).
- (c) the runtime SA (`runtime-svc@`) holds `secretAccessor` on EXACTLY
  `{e2b-api-key}` and on no other secret — the load-bearing isolation invariant
  (it must NEVER read a per-tenant secret). The JWT public key is a plain env
  value (H1, 2026-06-12), not a mounted secret, so it is not in this set.
- (d) ingress is `all` by design, so invocation is gated by IAM only;
  `--no-allow-unauthenticated` must remain in effect (policy stays private).

## Assumed value

`<django-dispatch-sa>` — the Django dispatch service account does not exist yet,
so it is parameterized (`EXPECTED_INVOKER` in the audit, a placeholder in the
binding command). No other value was invented; everything else matches the live
service.
