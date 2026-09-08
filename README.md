# QuantumCX — Instagram autoposter

Publishes three posts a day to `@quantumcx.pseo` for a year, from a schedule in
this repository. Runs on GitHub Actions. **Costs nothing**: the Instagram Graph
API is free to publish with, and Actions on a public repository is free.

- `cards/` — 277 images, 1080×1350
- `schedule/instagram.csv` — 1,095 rows: caption, datetime (IST), image
- `state/posted.json` — what has already gone out; committed after each run
- `scripts/post_instagram.py` — the poster
- `.github/workflows/instagram.yml` — cron, 3×/day
- `.github/workflows/refresh-token.yml` — keeps the access token alive

---

## The repository must be public

Not a preference. The Graph API does not accept file uploads — it takes an
`image_url` and fetches it, so the cards have to be publicly reachable at the
moment of posting. They're served from `raw.githubusercontent.com`, which only
works on a public repo.

**What that means in practice:** your whole year of captions and images is
visible to anyone who finds the repo, competitors included. If that matters
more than the cost, the alternative is hosting the cards somewhere else public
and pointing `IMAGE_BASE_URL` at it — the workflow supports that with no code
change.

---

## Setup

### 1. Instagram side

1. Your Instagram must be a **Business** (or Creator) account. Personal
   accounts cannot publish via the API at all.
   A linked Facebook Page is *not* required on the Instagram Login path, only
   on the Facebook Login one — link a Page anyway if Meta asks you to complete
   Page Publishing Authorization.
2. At [developers.facebook.com](https://developers.facebook.com), create an
   app → add the **Instagram** product.
3. Choose **Instagram API with Instagram Login** (not the Facebook Login
   variant). The two use different hosts and different permission names, and
   this repo is written for the Instagram Login path.
4. Generate a **long-lived access token** with `instagram_business_basic` and
   `instagram_business_content_publish`.
5. Note your **Instagram user ID** (the numeric ID, not the handle).

### 2. Repository side

1. Create a **public** repo — `quantumcx-social` is a reasonable name — and
   push these files to it.
2. **Settings → Secrets and variables → Actions**, add:

   | Secret | Value |
   |---|---|
   | `IG_ACCESS_TOKEN` | the long-lived token from step 3 above |
   | `IG_USER_ID` | your numeric Instagram user ID |
   | `GH_PAT` | *(optional)* a fine-grained PAT with **Secrets: write** on this repo |

   Optional repository **variable** (Settings → Variables), not a secret:

   | Variable | Use |
   |---|---|
   | `IG_API_VERSION` | defaults to `v21.0`. If a run fails with `NONEXISTENT_VERSION`, bump this — Meta retires versions, and no code change is needed. |

   `GH_PAT` exists only so the token-refresh workflow can write the new token
   back. Without it, refresh still runs and tells you the new value could not
   be stored, and you update `IG_ACCESS_TOKEN` by hand every 50 days.

### 3. Prove it works before trusting it

**Actions → Post to Instagram → Run workflow**, leave *dry run* ticked. It logs
exactly what it would publish and posts nothing.

Then run it again with dry run **unticked** to publish one real post. Check it
on the profile. Only then leave it to the schedule.

---

## How it behaves

**Times.** The schedule is in IST. Cron is UTC, so the workflow fires at 06:35,
14:05 and 17:05 UTC for the 12:00, 19:30 and 22:30 IST slots.

**Cron is not punctual.** GitHub routinely fires scheduled runs 5–15 minutes
late, sometimes much later, and occasionally skips one. The script therefore
looks back **8 hours** for anything unposted rather than only checking the
current minute — a late or missed run catches up by itself. A slot older than
8 hours is deliberately left alone rather than posted at the wrong time of day.

**It will not double-post.** Every published slot is recorded in
`state/posted.json` and committed. Re-running finds nothing due.

**Failures are loud.** If a post fails, the run goes red. A green run that
published nothing is the one failure mode this cannot afford, so the script
exits non-zero on any failure.

**The 60-day trap.** GitHub disables scheduled workflows on a public repo after
60 days with no commit on the default branch, silently. Committing the state
file resets that timer most days, and a monthly heartbeat commit covers any
long quiet stretch.

---

## The one thing that needs you

**The access token expires every 60 days.** `refresh-token.yml` runs on the 1st
and 15th and renews it — twice a month so one missed run cannot let it lapse.

If the token ever *does* expire past 60 days, it cannot be refreshed. Generate
a new long-lived token in the Meta app and update the `IG_ACCESS_TOKEN` secret.
That is the only recurring manual task, and it should never come up if the
refresh workflow is green.

Watch for a red **Refresh Instagram token** run. That is your early warning
that publishing is about to stop.

---

## Limits and costs

| | |
|---|---|
| Instagram publishing | free |
| Actions minutes used | ~90/month (2,000 free on private, unlimited on public) |
| Instagram API cap | 25 posts / rolling 24h — this uses 3 |

## Changing the schedule

Edit `schedule/instagram.csv`. Rows are matched on `scheduled_at`, so changing
a time creates a new slot. Delete an entry from `state/posted.json` to allow a
slot to publish again.

---

## Triggering: why an outside cron service

GitHub's own scheduler has **never fired in this repository**. Every workflow is
`state=active`, the repo is public, `main` is the default branch, Actions is
enabled — and the `event=schedule` run count is zero. A probe workflow that
publishes nothing and calls no API did not fire either, which rules out our cron
lines and our credentials as the cause.

Manual dispatch works perfectly. So the trigger comes from outside.

`publish.yml` is the entry point. It takes **no inputs**, deliberately: the
per-platform workflows default `dry_run` to true, so a dispatch that forgot to
send inputs would produce a green run that published nothing — the one failure
mode that looks like success. With no inputs there is nothing to get wrong.

It runs all three platforms on every trigger. Each script reads its own schedule
and exits without calling any API when nothing is due, so an idle run costs
nothing — including on X, where calls are billed.

### Setting up the trigger

Create a **fine-grained personal access token** (github.com → Settings →
Developer settings → Personal access tokens → Fine-grained):

| Setting | Value |
|---|---|
| Repository access | Only select repositories → this one |
| Permissions | **Contents: Read and write** |
| Expiration | 1 year (calendar a renewal) |

That scope lets the token trigger this repository and nothing else. It cannot
read your other repositories and it cannot reach the Meta, LinkedIn or X
credentials, which stay in GitHub Actions secrets and are never sent anywhere.

Then at [cron-job.org](https://cron-job.org) (free), create jobs with:

- **URL** `https://api.github.com/repos/OWNER/REPO/dispatches`
- **Method** POST
- **Headers**
  - `Authorization: Bearer <the PAT>`
  - `Accept: application/vnd.github+json`
  - `X-GitHub-Api-Version: 2022-11-28`
  - `Content-Type: application/json`
- **Body** `{"event_type":"publish"}`
- **Timezone** Asia/Kolkata

Four jobs, at **12:05, 18:35, 19:35 and 22:35 IST** — a few minutes after each
slot, so every platform is covered:

| Trigger (IST) | Catches |
|---|---|
| 12:05 | Instagram 12:00 |
| 18:35 | LinkedIn 18:30 (Tue/Wed/Thu) |
| 19:35 | X 19:15 and Instagram 19:30 |
| 22:35 | Instagram 22:30 |

A successful call returns **HTTP 204** with an empty body. Anything else means
the token or the URL is wrong.

### If GitHub's scheduler ever wakes up

Leave the external trigger in place. Both paths can fire safely: the state files
record every published slot, all four publishing workflows share one concurrency
group, and whichever runs second finds nothing due.
