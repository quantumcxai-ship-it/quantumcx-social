#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Publish the Instagram post that is due, using the Instagram Graph API.

Run by .github/workflows/instagram.yml three times a day. Standard library
only, so the workflow needs no dependency install step.

HOW IT DECIDES WHAT TO POST
    Every row in the schedule has a datetime in IST. On each run this finds
    rows whose time has passed, within a lookback window, that are not already
    recorded as posted. The window matters: GitHub's cron is not punctual and
    routinely fires five to fifteen minutes late, occasionally much later, and
    very occasionally skips a run entirely. A window means a late or missed run
    catches up instead of silently dropping a post.

WHY IMAGES COME FROM A URL
    The Graph API will not accept a file upload. It takes image_url and fetches
    it, so the card has to be publicly reachable at that moment. The cards live
    in this repository and are served from raw.githubusercontent.com, which is
    why the repository has to be public.

WHY STATE IS COMMITTED BACK
    posted.json is the record of what has already gone out; without it a
    re-run double-posts. Committing it also resets GitHub's 60-day inactivity
    timer, which would otherwise disable the schedule silently - so the thing
    that prevents duplicates is the same thing that keeps the cron alive.

PUBLISHING IS TWO CALLS
    POST /{ig-user-id}/media       creates a container and validates the media
    POST /{ig-user-id}/media_publish publishes it
    A container can take a moment to become ready, so this polls its status
    rather than assuming.
"""

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# There are two Instagram publishing paths and they are NOT interchangeable:
#
#   Instagram API with Instagram Login  -> graph.instagram.com
#       permissions: instagram_business_basic, instagram_business_content_publish
#       token refreshes at graph.instagram.com/refresh_access_token
#
#   Instagram API with Facebook Login   -> graph.facebook.com
#       permissions: instagram_basic, instagram_content_publish,
#                    pages_read_engagement, and a linked Facebook Page
#
# This uses the Instagram Login path, which is the simpler setup and the one
# refresh-token.yml is written against. Mixing the two silently fails.
#
# The version is an env var on purpose. Meta retires API versions, and a
# hardcoded one is exactly how Postiz's LinkedIn provider started returning
# "426 NONEXISTENT_VERSION" to everyone. If you see that error, bump
# IG_API_VERSION - no code change needed.
API_VERSION = os.environ.get("IG_API_VERSION", "v21.0")
GRAPH = os.environ.get("IG_API_BASE", "https://graph.instagram.com/%s" % API_VERSION).rstrip("/")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

SCHEDULE = "schedule/instagram.csv"
STATE = "state/posted.json"

# How far back to look for posts that should already have gone out. Covers a
# late cron and one entirely missed run.
LOOKBACK_HOURS = 8
# Instagram allows 25 published posts per rolling 24 hours. Three a day sits
# well inside that; this is a guard against a bug, not against the schedule.
MAX_PER_RUN = 3
CONTAINER_POLL_SECONDS = 5
CONTAINER_POLL_ATTEMPTS = 12


def http(url, data=None, method=None):
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw}
    except urllib.error.URLError as e:
        raise SystemExit("network error reaching Instagram: %s" % e.reason)


def load_schedule(path):
    import csv
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_state(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"posted": {}}


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def due_rows(rows, state, now_utc):
    out = []
    for r in rows:
        key = r["scheduled_at"]
        if key in state["posted"]:
            continue
        d, t = r["date"], r["time"]
        y, mo, dy = (int(x) for x in d.split("-"))
        hh, mm = (int(x) for x in t.split(":"))
        when = datetime.datetime(y, mo, dy, hh, mm, tzinfo=IST).astimezone(datetime.timezone.utc)
        age = (now_utc - when).total_seconds() / 3600.0
        if 0 <= age <= LOOKBACK_HOURS:
            out.append((when, r))
    out.sort(key=lambda x: x[0])
    return [r for _, r in out]


def publish(ig_user_id, token, image_url, caption, dry_run):
    if dry_run:
        print("      DRY RUN - would create container and publish")
        return "dry-run-id"

    status, body = http("%s/%s/media" % (GRAPH, ig_user_id),
                        {"image_url": image_url, "caption": caption, "access_token": token})
    if status != 200 or "id" not in body:
        raise RuntimeError("container creation failed (HTTP %s): %s" % (status, json.dumps(body)[:400]))
    container = body["id"]

    # A container is not immediately publishable; Instagram fetches and checks
    # the image first. Publishing too early returns a confusing error.
    for attempt in range(CONTAINER_POLL_ATTEMPTS):
        s, b = http("%s/%s?fields=status_code&access_token=%s"
                    % (GRAPH, container, urllib.parse.quote(token)))
        code = b.get("status_code")
        if code == "FINISHED":
            break
        if code == "ERROR":
            raise RuntimeError("Instagram rejected the media: %s" % json.dumps(b)[:400])
        time.sleep(CONTAINER_POLL_SECONDS)
    else:
        raise RuntimeError("container %s never became ready" % container)

    status, body = http("%s/%s/media_publish" % (GRAPH, ig_user_id),
                        {"creation_id": container, "access_token": token})
    if status != 200 or "id" not in body:
        raise RuntimeError("publish failed (HTTP %s): %s" % (status, json.dumps(body)[:400]))
    return body["id"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--schedule", default=SCHEDULE)
    ap.add_argument("--state", default=STATE)
    ap.add_argument("--image-base", default=os.environ.get("IMAGE_BASE_URL", ""),
                    help="public base URL for the cards directory")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", help="override the current time, ISO 8601 UTC, for testing")
    args = ap.parse_args()

    token = os.environ.get("IG_ACCESS_TOKEN")
    ig_user_id = os.environ.get("IG_USER_ID")
    if not args.dry_run and (not token or not ig_user_id):
        raise SystemExit("IG_ACCESS_TOKEN and IG_USER_ID must be set in the environment")
    if not args.image_base:
        raise SystemExit("IMAGE_BASE_URL must be set (public URL of the cards directory)")

    now = (datetime.datetime.fromisoformat(args.now).replace(tzinfo=datetime.timezone.utc)
           if args.now else datetime.datetime.now(datetime.timezone.utc))

    rows = load_schedule(args.schedule)
    state = load_state(args.state)
    due = due_rows(rows, state, now)

    print("now (UTC): %s" % now.isoformat(timespec="seconds"))
    print("schedule rows: %d | already posted: %d | due now: %d"
          % (len(rows), len(state["posted"]), len(due)))

    if not due:
        print("nothing due. exiting cleanly.")
        return 0

    if len(due) > MAX_PER_RUN:
        print("WARNING: %d posts due but capping this run at %d. The rest will "
              "go out on the next run, which is what the lookback window is for."
              % (len(due), MAX_PER_RUN))
        due = due[:MAX_PER_RUN]

    failures = 0
    for r in due:
        image_url = args.image_base.rstrip("/") + "/" + r["image"]
        print("\n>>> %s  [%s]  %s" % (r["scheduled_at"], r["pillar"], r["image"]))
        print("    %s" % r["caption"].split("\n")[0][:90])
        try:
            media_id = publish(ig_user_id, token, image_url, r["caption"], args.dry_run)
            state["posted"][r["scheduled_at"]] = {
                "media_id": media_id,
                "post_id": r["post_id"],
                "published_at": datetime.datetime.now(datetime.timezone.utc)
                                 .isoformat(timespec="seconds"),
            }
            if not args.dry_run:
                save_state(args.state, state)
            print("    published: %s" % media_id)
        except Exception as e:
            failures += 1
            print("    FAILED: %s" % e)

    # A failure must surface as a red run. A silently green workflow that
    # posted nothing is the failure mode this whole schedule cannot afford.
    if failures:
        print("\n%d of %d posts failed." % (failures, len(due)))
        return 1
    print("\ndone. %d posted, %d/%d slots complete."
          % (len(due), len(state["posted"]), len(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
