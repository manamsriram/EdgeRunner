# EdgeRunner live deployment — GCP free-tier VM

Paper stays on Render. This runs the **real-money** account on a GCP `e2-micro`
Always Free instance. Target cost: **$0/month**.

## Files

| File | Purpose |
|---|---|
| `gcp.env` | Live environment. **Gitignored, contains secrets.** Generated from the Render paper service with live-specific values replaced. |
| `setup.sh` | One-time VM provisioning: swap, venv, Caddy, DuckDNS, systemd. |
| `deploy.sh` | Deploy `origin/live` (or a pinned commit) + restart. |
| `edgerunner.service` | systemd unit. Single uvicorn worker. |
| `Caddyfile` | HTTPS reverse proxy, auto Let's Encrypt cert. |

## Staying at $0 — every one of these defaults to a billable option

Create the VM with **exactly** these settings. Getting any single one wrong means
you are paying.

```bash
gcloud compute instances create edgerunner-live \
  --zone=us-central1-a \
  --machine-type=e2-micro \
  --image-family=debian-12 --image-project=debian-cloud \
  --boot-disk-type=pd-standard \
  --boot-disk-size=30GB \
  --network-interface=network-tier=STANDARD,subnet=default \
  --tags=http-server,https-server
```

| Setting | Console default | Required |
|---|---|---|
| Machine type | `e2-medium` | **`e2-micro`** |
| Region | varies | **`us-west1`, `us-east1`, or `us-central1` only** |
| Boot disk type | **Balanced** | **`pd-standard`** |
| Boot disk size | varies | **≤ 30 GB** |
| Network tier | **Premium** | **Standard** |

Also:
- **One VM only.** The free tier covers ~730 hrs/month of *one* in-use external
  IPv4. A second VM's IP bills at ~$0.005/hr.
- **Ephemeral IP, not reserved static.** A reserved IP bills while the VM is
  stopped. DuckDNS + the 5-minute cron in `setup.sh` handles IP changes.
- **No snapshots, no Cloud SQL, no load balancer.** None are free.
- Set a **$1 budget alert**. It will *not* stop charges — GCP has no hard spend
  cap — but it tells you within a day instead of at month end.

Open the firewall once per project:

```bash
gcloud compute firewall-rules create allow-http-https \
  --allow=tcp:80,tcp:443 --target-tags=http-server,https-server
```

## Prerequisites

1. **DuckDNS subdomain** — sign in at https://duckdns.org with GitHub, copy the
   token from the top of the page, add a domain — registered: **`edgerunner-live`**.
   Pass the **name only** as `DUCKDNS_DOMAIN` — `edgerunner-live`, not
   `edgerunner-live.duckdns.org`; the script appends the suffix itself.
   Needed because Vercel is HTTPS: browsers hard-block HTTPS→HTTP requests, and
   a public CA only signs names, not bare IPs.
2. **Live Alpaca API keys** — from the *live* account, not paper. Different
   key pair entirely.
3. **Second Supabase project** — free tier allows 2. This is the live database.
   Do not point at the paper DB: the schema has no account column, so live and
   paper fills would land in the same `trade_outcomes` and the bandit/learning
   layer would train on mixed P&L.

## Install

```bash
# from your laptop
gcloud compute scp deploy/gcp/gcp.env edgerunner-live:~/gcp.env --zone=us-central1-a
gcloud compute ssh edgerunner-live --zone=us-central1-a

# on the VM
sudo mkdir -p /opt/edgerunner && sudo mv ~/gcp.env /opt/edgerunner/gcp.env
sudo nano /opt/edgerunner/gcp.env      # fill the three REPLACE_WITH_ placeholders

# The stock debian-12 image has no git, and setup.sh is what installs it —
# so bootstrap git before cloning the repo that contains setup.sh.
sudo apt-get update -qq && sudo apt-get install -y -qq git
git clone https://github.com/manamsriram/EdgeRunner.git /tmp/er
sudo DUCKDNS_TOKEN=<your-duckdns-token> bash /tmp/er/deploy/gcp/setup.sh
```

`DUCKDNS_DOMAIN` defaults to `edgerunner-live`; override it only if you
registered a different name.

Not using an interactive shell? `gcloud compute ssh` needs a TTY for
`journalctl -f` and `nano`. For one-shot commands use
`gcloud compute ssh edgerunner-live --zone=us-central1-a --command='...'`.

Verify:

```bash
journalctl -u edgerunner -f
curl https://edgerunner-live.duckdns.org/
free -h          # confirm 4G swap present
```

## Live-trading gate

`ALPACA_PAPER=false` alone will **not** trade live. `trader/config.py`
requires `ALLOW_LIVE_TRADING=true` as a second, independently-named flag, so no
single stale or copy-pasted env var can put real money at risk. Both are set in
`gcp.env`; neither is set on Render.

## Memory

1 GB RAM for a process that OOM'd at 512 MB on Render. Mitigations already
applied:

- 4 GB swapfile, `vm.swappiness=10`
- `UNIVERSE_SIZE=20` (paper runs 40), `CRYPTO_UNIVERSE_SIZE=10`
- `MALLOC_TRIM_THRESHOLD_` carried over from Render
- Single uvicorn worker

Swap is a shock absorber, not more RAM. If `journalctl` shows swap thrash during
ticks, cut `UNIVERSE_SIZE` first. The RSS checkpoints from commit `5e004e1` log
after precompute/bandit/digest — read those before changing anything else.

## Paper vs live split

| | Paper (Render) | Live (this VM) |
|---|---|---|
| `AUTONOMY` | `auto` — trades unattended | **`manual`** — queues a proposal, waits for you |
| `PROTECT_READS` | unset (public reads) | **`true`** — reads need a JWT |
| `ALLOW_LIVE_TRADING` | unset | `true` |
| Dashboard tabs | Portfolio, Performance, Calendar, Analysis | Calendar, Approvals, Controls |

Under `AUTONOMY=manual` the risk gate runs identically, then `pipeline.py:1289`
calls `create_proposal()` instead of submitting — nothing reaches the broker
without an approval. Note `pipeline.py:1661`: options entries have no manual
path and are blocked outright under `manual`. Options are off in `gcp.env`
anyway, so this costs nothing today.

## Frontend

Add to Vercel, then redeploy:

```
VITE_LIVE_API_URL=https://edgerunner-live.duckdns.org
```

Set `FRONTEND_ORIGIN` in `gcp.env` to the Vercel URL or CORS will reject the
browser calls.

The "Real money" nav group (Calendar / Approvals / Controls) reads this variable.
If it's unset the frontend silently falls back to `VITE_API_URL` — meaning those
tabs would show **paper** data under a REAL MONEY badge. Set it before shipping.

## Releases — live tracks the `live` branch, never `main`

```
main  ──●──●──●──●──●──●──▶   Render paper + Vercel, auto-deploy every push
              │
             live                GCP live box, moves only when you say so
```

`live` carries **no commits of its own**. It is a pointer you fast-forward from
`main` once a change has proven itself on paper. Nothing is ever merged back
from `live` into `main` — there is nothing to merge back, which is the point.

```bash
# laptop — promote main to live
git checkout live
git merge --ff-only main
git push origin live

# VM — nothing moves until this runs
sudo bash /opt/edgerunner/app/deploy/gcp/deploy.sh
```

`--ff-only` is what keeps `live` a pure subset of `main`. If it refuses, `live`
has drifted — someone committed directly to it. Fix that rather than forcing.

Rollback pins a commit directly:

```bash
sudo bash /opt/edgerunner/app/deploy/gcp/deploy.sh a1b2c3d
```

`deploy.sh` checks out a detached HEAD (no local branch to drift), prints the
commits being introduced before it switches, and prints the exact rollback
command if the service fails to start.

**Frontend note:** Vercel builds from `main`, and one frontend serves both
accounts. UI changes — including the Real money tabs — must be on `main` to
exist at all. Only the backend is pinned to `live`.

No auto-deploy on push, deliberately. This service holds real money; updates
should be a decision, not a side effect of merging.
