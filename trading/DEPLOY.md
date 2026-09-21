# Deploying to Google Cloud

Two deployables, one repo:

- **Cloud Run Job** (`Dockerfile.job`) — runs `trading.pipeline` or
  `trading.daily_summary` once and exits. Triggered by Cloud Scheduler.
- **Cloud Run Service** (`Dockerfile`) — the always-on Flask Q&A web app
  (`trading.web`), reachable from any browser including your phone.

Both need a GCS bucket for shared state: `trading.journal` (trade history)
and `trading.daily_summary` (cached summaries) write JSON/text there instead
of local disk when `GCS_BUCKET_NAME` is set, because Cloud Run Job
executions and the Cloud Run Service each run in their own ephemeral
container and don't share a filesystem. Locally (Task Scheduler `.bat`
files), leave `GCS_BUCKET_NAME` unset and it keeps using the local
`trading/journal.json` / `trading/summaries/` files as before.

Run these from the repo root (`C:\Users\gusbu\claus`). Replace
`YOUR_PROJECT_ID` and pick a `REGION` (e.g. `us-east4`, closest to ET).

## 0. One-time setup

```sh
gcloud config set project YOUR_PROJECT_ID
REGION=us-east4

gcloud services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  storage.googleapis.com

gcloud artifacts repositories create trading \
  --repository-format=docker --location=$REGION

# Bucket for journal.json + daily summaries. Name must be globally unique.
BUCKET=YOUR_PROJECT_ID-trading-state
gcloud storage buckets create gs://$BUCKET --location=$REGION
```

### Secrets (recommended over plain env vars)

The code just reads `os.environ`, so either works, but Secret Manager keeps
your keys out of the visible service/job config (`--set-env-vars` values
show up in plaintext to anyone with viewer access to the Cloud Run config;
`--set-secrets` doesn't). Same env var names either way — no code changes.

```sh
for name in ALPACA_API_KEY ALPACA_SECRET_KEY ANTHROPIC_API_KEY GMAIL_ADDRESS GMAIL_APP_PASSWORD; do
  echo -n "PASTE_VALUE_FOR_$name" | gcloud secrets create $name --data-file=-
done
```

The commands below use `--set-secrets` assuming you did this. If you'd
rather use plain env vars, swap `--set-secrets` for `--set-env-vars` with
literal `KEY=value` pairs.

Grant the runtime service account (default compute service account unless
you set one) access to the bucket and secrets **now, before deploying** —
`gcloud run deploy`/`jobs create` validate secret access at revision-creation
time, so granting access afterward (as an earlier version of this doc did)
fails with `Permission denied on secret ...`:

```sh
PROJECT_NUMBER=$(gcloud projects describe YOUR_PROJECT_ID --format='value(projectNumber)')
SA=$PROJECT_NUMBER-compute@developer.gserviceaccount.com

gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member=serviceAccount:$SA --role=roles/storage.objectAdmin
for name in ALPACA_API_KEY ALPACA_SECRET_KEY ANTHROPIC_API_KEY GMAIL_ADDRESS GMAIL_APP_PASSWORD; do
  gcloud secrets add-iam-policy-binding $name \
    --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor
done
```

(Add `APP_PASSWORD` to that loop too once you've created it, in step 2.)

## 1. Build and push the images

`gcloud builds submit --tag` always builds a file literally named
`Dockerfile` in the source root, with no `-f` flag to pick a different one
(current gcloud CLI). That's fine for the web image; the job image needs a
one-line `cloudbuild.job.yaml` (already in the repo root) to point at
`Dockerfile.job`:

```sh
gcloud builds submit --tag $REGION-docker.pkg.dev/YOUR_PROJECT_ID/trading/web .
gcloud builds submit --config=cloudbuild.job.yaml --substitutions=_TAG=$REGION-docker.pkg.dev/YOUR_PROJECT_ID/trading/job .
```

## 2. Cloud Run Service (web Q&A app)

**After the one-time setup below, `trading-web` auto-deploys on every push to
`main`** via a Cloud Build trigger (`cloudbuild.yaml` at the repo root:
builds the web image, pushes it tagged with the commit SHA, then `gcloud run
deploy`s it). The manual commands in this section are for the first deploy,
or for `trading-pipeline`/`trading-daily-summary`, which aren't wired to the
trigger. One-time trigger setup (already done for this project; recorded
here for a future project):

```sh
gcloud builds connections create github klaus-github --region=$REGION
# Follow the printed link to authorize Cloud Build against GitHub, then:
gcloud builds repositories create klaus \
  --remote-uri="https://github.com/YOUR_GH_USER/YOUR_REPO.git" \
  --connection=klaus-github --region=$REGION

for role in roles/run.admin roles/iam.serviceAccountUser roles/artifactregistry.writer; do
  gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
    --member=serviceAccount:$PROJECT_NUMBER@cloudbuild.gserviceaccount.com \
    --role=$role --condition=None
done

gcloud builds triggers create github \
  --name=trading-web-deploy \
  --repository=projects/YOUR_PROJECT_ID/locations/$REGION/connections/klaus-github/repositories/klaus \
  --branch-pattern="^main$" \
  --build-config=cloudbuild.yaml \
  --region=$REGION
```

```sh
gcloud run deploy trading-web \
  --image $REGION-docker.pkg.dev/YOUR_PROJECT_ID/trading/web \
  --region $REGION \
  --allow-unauthenticated \
  --min-instances=0 --max-instances=1 \
  --memory=512Mi \
  --set-env-vars=GCS_BUCKET_NAME=$BUCKET \
  --set-secrets=ALPACA_API_KEY=ALPACA_API_KEY:latest,ALPACA_SECRET_KEY=ALPACA_SECRET_KEY:latest,ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest
```

`--allow-unauthenticated` is what makes "open the link on my phone" work
with no sign-in. That also means anyone who gets the URL can ask it
questions about your (paper) account. To close that off with minimal
friction, set an `APP_PASSWORD` secret/env var — the app then requires HTTP
Basic Auth (your phone's browser will just prompt once and remember it):

```sh
echo -n "pick-a-password" | gcloud secrets create APP_PASSWORD --data-file=-
gcloud secrets add-iam-policy-binding APP_PASSWORD \
  --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor
gcloud run services update trading-web --region $REGION \
  --update-secrets=APP_PASSWORD=APP_PASSWORD:latest
```

## 3. Cloud Run Jobs (pipeline + daily summary)

Same image for both; the daily-summary job just overrides the command.
(Bucket/secret access was already granted to `$SA` in step 0.)

```sh
gcloud run jobs create trading-pipeline \
  --image $REGION-docker.pkg.dev/YOUR_PROJECT_ID/trading/job \
  --region $REGION \
  --memory=512Mi --task-timeout=600s --max-retries=1 \
  --set-env-vars=GCS_BUCKET_NAME=$BUCKET \
  --set-secrets=ALPACA_API_KEY=ALPACA_API_KEY:latest,ALPACA_SECRET_KEY=ALPACA_SECRET_KEY:latest,ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest

gcloud run jobs create trading-daily-summary \
  --image $REGION-docker.pkg.dev/YOUR_PROJECT_ID/trading/job \
  --region $REGION \
  --command=python --args=-m,trading.daily_summary \
  --memory=512Mi --task-timeout=300s --max-retries=1 \
  --set-env-vars=GCS_BUCKET_NAME=$BUCKET \
  --set-secrets=ALPACA_API_KEY=ALPACA_API_KEY:latest,ALPACA_SECRET_KEY=ALPACA_SECRET_KEY:latest,ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,GMAIL_ADDRESS=GMAIL_ADDRESS:latest,GMAIL_APP_PASSWORD=GMAIL_APP_PASSWORD:latest
```

Grant these jobs' service account the same bucket/secret access as step 2
(swap `SA=` to `gcloud run jobs describe ... --format='value(spec.template.spec.template.spec.serviceAccountName)'`).

## 4. Cloud Scheduler (replaces Task Scheduler)

Cloud Scheduler's `--time-zone` handles EST/EDT automatically — no manual
DST adjustment needed. Both run weekdays only.

```sh
PROJECT_NUMBER=$(gcloud projects describe YOUR_PROJECT_ID --format='value(projectNumber)')
SCHEDULER_SA=$PROJECT_NUMBER-compute@developer.gserviceaccount.com

# Let the scheduler's service account invoke Cloud Run Jobs
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member=serviceAccount:$SCHEDULER_SA --role=roles/run.invoker

gcloud scheduler jobs create http trading-pipeline-930et \
  --location=$REGION \
  --schedule="30 9 * * 1-5" \
  --time-zone="America/New_York" \
  --uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/YOUR_PROJECT_ID/jobs/trading-pipeline:run" \
  --http-method=POST \
  --oauth-service-account-email=$SCHEDULER_SA

gcloud scheduler jobs create http trading-summary-400et \
  --location=$REGION \
  --schedule="0 16 * * 1-5" \
  --time-zone="America/New_York" \
  --uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/YOUR_PROJECT_ID/jobs/trading-daily-summary:run" \
  --http-method=POST \
  --oauth-service-account-email=$SCHEDULER_SA
```

## 5. Verify

```sh
gcloud run jobs execute trading-pipeline --region $REGION --wait
gcloud run jobs execute trading-daily-summary --region $REGION --wait
curl -u "user:$APP_PASSWORD" https://<your-trading-web-url>/
```

Note: the exact path `/healthz` (no trailing slash) on the default
`*.run.app` domain gets intercepted upstream of the container and returns
Google's generic 404 page instead of reaching the Flask route — observed on
this project's `trading-web`. `/healthz/` (trailing slash) and `/` do reach
the app normally. Harmless (Cloud Run's own container startup probe is
TCP-based, not HTTP, so this doesn't affect deploys), but don't rely on
`/healthz` for manual or external monitoring checks against the default
domain.

Then open the `trading-web` URL (`gcloud run services describe trading-web
--region $REGION --format='value(status.url)'`) on your phone and ask it
something.

## 6. Code mode (Klaus editing this repo from the web page)

Adds a third deployable: **`trading-codemode`**, a Cloud Run Job built from
`Dockerfile.codemode` that runs the actual Claude Code CLI (Node + git,
`--permission-mode acceptEdits`, same `--allowedTools`/`--disallowedTools`
allowlist as `claus.py`'s local `run_claude_code`). It's kept isolated from
`trading-web` on purpose — `trading-web` itself only runs plain, fixed-
argument `git` commands (merge/revert/push), never the agentic CLI. See
`trading/codemode.py` and `trading/codemode_job.py`.

**New secret** — a GitHub PAT scoped to just this repo (fine-grained token,
Contents: Read and write, no other permissions):

```sh
echo -n "PASTE_YOUR_GITHUB_TOKEN" | gcloud secrets create GITHUB_TOKEN --data-file=-
gcloud secrets add-iam-policy-binding GITHUB_TOKEN \
  --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor
```

**Build and deploy the job:**

```sh
gcloud builds submit --config=cloudbuild.codemode.yaml \
  --substitutions=_TAG=$REGION-docker.pkg.dev/YOUR_PROJECT_ID/trading/codemode .

gcloud run jobs create trading-codemode \
  --image $REGION-docker.pkg.dev/YOUR_PROJECT_ID/trading/codemode \
  --region $REGION \
  --memory=1Gi --task-timeout=600s --max-retries=0 \
  --set-env-vars=GCS_BUCKET_NAME=$BUCKET,GITHUB_REPO=YOUR_GH_USER/YOUR_REPO \
  --set-secrets=ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,GITHUB_TOKEN=GITHUB_TOKEN:latest
```

`--max-retries=0` is load-bearing — a retried execution would re-run the
whole clone→CLI→push flow and could push the branch twice; better to just
surface the failure and let you re-trigger it from the page.

Grant this job's service account the same bucket access as the others
(step 0's `SA`, or the job's own runtime SA if you set a custom one):

```sh
gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member=serviceAccount:$SA --role=roles/storage.objectAdmin
```

**Update `trading-web`** — it needs `GITHUB_TOKEN` (for the merge/revert
push), `GMAIL_ADDRESS`/`GMAIL_APP_PASSWORD` (it sends the confirmation-code
and risk-notification emails directly now, not just the daily-summary job),
and enough config to call the job's `:run` API:

```sh
gcloud run services update trading-web --region $REGION \
  --set-env-vars=GITHUB_REPO=YOUR_GH_USER/YOUR_REPO,GCP_PROJECT=YOUR_PROJECT_ID,GCP_REGION=$REGION,CODEMODE_JOB_NAME=trading-codemode \
  --update-secrets=GITHUB_TOKEN=GITHUB_TOKEN:latest,GMAIL_ADDRESS=GMAIL_ADDRESS:latest,GMAIL_APP_PASSWORD=GMAIL_APP_PASSWORD:latest
```

`trading-web`'s runtime service account also needs permission to start the
job (`run.jobs.run`, part of `roles/run.developer`):

```sh
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member=serviceAccount:$SA --role=roles/run.developer
```

That's project-wide; narrow it to just this job afterward if you'd rather
(`gcloud run jobs add-iam-policy-binding trading-codemode --region=$REGION
--member=serviceAccount:$SA --role=roles/run.developer`).

**Try it:** open `trading-web`, type an instruction in the "Code mode" card,
wait for the diff, check your email for the 4-digit code, enter it with
"run it". The merge push to `main` fires the existing `trading-web-deploy`
trigger from step 2, same as any other push.

## Notes

- `WATCHLIST`, `MAX_POSITION_NOTIONAL_USD`, etc. in `trading/config.py`
  still apply unchanged — nothing about risk limits changed for the cloud
  deploy.
- The paper-trading-only Alpaca endpoint is still hardcoded in
  `config.py`; nothing in this deploy path touches live trading.
- `requirements.txt` gained `yfinance` (already imported by
  `agents/research_fundamentals.py`, previously missing from the file),
  `google-cloud-storage` (for the new GCS-backed storage), and
  `google-auth` (code mode uses it to call the Cloud Run Jobs API).
- Code mode edits are unrestricted by file path beyond the CLI's own
  `--allowedTools`/`--disallowedTools` allowlist — asking it to edit
  `cloudbuild.yaml` or this file is allowed, same trust level as any other
  file in the repo. Known scope, not an oversight.
