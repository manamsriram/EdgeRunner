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
| Dashboard tabs | Portfolio, Performance, Calendar, Analysis | Calendar, Approvals, Controls, Logs |

## Auth lives in the live Supabase project

`SUPABASE_URL` in `gcp.env` and `VITE_SUPABASE_URL` on Vercel must name the
**same** project — the frontend mints the JWT there, the VM verifies it against
that project's JWKS (`api/deps.py:72`). Point them at different projects and
every live route returns 401 with no useful message.

Both name the live project, `ykovhiheebckbdrpmmpz`, so the auth boundary matches
the money boundary. Paper's reads on Render are public (`PROTECT_READS` unset),
so nothing there depends on the token.

The one thing to remember: **the dashboard user must exist in the live project.**
A user created in the paper project fails as "Invalid login credentials", which
reads like a typo.

`SUPABASE_JWT_SECRET` is intentionally absent. `verify_supabase_jwt` prefers
`SUPABASE_URL` (ES256/JWKS) and only falls back to the HS256 shared secret when
the URL is unset — so a stale secret sitting in the env is a silent alternate
verification path, not a backup.

## Logs without SSH

The **Logs** tab (`/logs`, real-money group) reads `GET /api/controls/logs`,
which tails this host's journal behind the same auth as the rest of Controls.
Units: `edgerunner` (app), `edgerunner-deploy` (the poller), `caddy` (TLS).

It exists because `/controls/runs` shows what the bot *did* and nothing about
tracebacks, OOM kills, systemd restarts, or deploy output. Polls every 10 s while
"Follow" is on — polling rather than streaming, because `journalctl -f` would
hold a request open per viewer and this box runs a single uvicorn worker.

The unit name is allowlisted server-side before it reaches `journalctl`, which
matters: `-u` accepts a glob, so an unvalidated value would read any unit on the
box through an authenticated endpoint.

`edgerunner.service` sets `SupplementaryGroups=systemd-journal`. Without it
journald returns an *empty read* rather than an error for units the service user
does not own, so the tab would show "no log lines" instead of failing.

GCP Cloud Logging would be the conventional answer and is deliberately not used:
the Ops Agent costs 100–250 MB RSS on a 1 GB box already running the trader.

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

### Auto-deploy — `edgerunner-deploy.timer`

`setup.sh` installs a systemd timer that polls `origin/live` **every 5 minutes**
and runs `deploy.sh` with `ONLY_IF_CHANGED=1`. Push `live`, and the box picks it
up within ~5 minutes with no SSH.

`ONLY_IF_CHANGED=1` makes the run a no-op when the box already holds the target
commit, so an idle week costs zero restarts — the poll is a `git fetch` and
nothing else.

This is still not auto-deploy on push: `main` moves without touching the box.
Only fast-forwarding `live` ships anything. The timer decides *when* your
decision lands, not *whether*.

```bash
systemctl list-timers edgerunner-deploy.timer   # when it next fires
journalctl -u edgerunner-deploy --since '2 days ago'
```

**Restarts are no longer confined to a safe hour.** The previous nightly cron
fired at 02:00 ET specifically so a restart could not land mid-session. A
5-minute poll gives that up: if you push `live` at 10:30 ET, the trader bounces
at 10:30 ET, mid-session, with positions open. `warm_up()` re-syncs open orders
on start and `AUTONOMY=manual` means nothing enters without you, so the blast
radius is small — but it is not zero. **Push `live` outside market hours unless
you specifically want it live now.**

If a deploy fails to start the service, `deploy.sh` exits non-zero and the box is
left on the **new, broken** commit — there is no auto-rollback. The journal line
prints the exact rollback command, and the Logs tab surfaces it without SSH.

### Unit files ship through `live` too

`deploy.sh` diffs `deploy/gcp/*.service` and `*.timer` against
`/etc/systemd/system/` on every run, copies what changed, and reloads systemd
only then. Before this, only `setup.sh` ever installed units, so a unit edit
merged to `live` silently never took effect — the code updated and the unit on
disk stayed whatever it was at install time.

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
should be a decision, not a side effect of merging. The nightly timer above
applies that decision on a schedule — it never makes it for you.
