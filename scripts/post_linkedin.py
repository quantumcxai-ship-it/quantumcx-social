#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Publish the LinkedIn post that is due, to a personal profile.

Run by .github/workflows/linkedin.yml on Tue/Wed/Thu. Standard library only.

WHY A PERSONAL PROFILE AND NOT THE COMPANY PAGE
    Posting to a company page requires LinkedIn's Community Management API,
    gated behind Marketing Developer Platform partner approval. Posting to your
    own feed needs only w_member_social from the self-serve "Share on LinkedIn"
    product. That is the whole reason this targets a person URN.

HOW IT DIFFERS FROM THE INSTAGRAM POSTER
    Instagram fetches an image from a public URL. LinkedIn takes an upload, in
    three calls:
        1. POST /rest/images?action=initializeUpload  -> uploadUrl + image urn
        2. PUT the bytes to that uploadUrl
        3. POST /rest/posts referencing the image urn
    So the card is read from disk here, and the repository does not need to be
    public for LinkedIn's sake.

VERSION HEADER
    LinkedIn requires a LinkedIn-Version header in YYYYMM form and retires old
    versions on a schedule - the August 2025 version sunsets in August 2026.
    It is an env var so it can be bumped without touching code. That is the
    same failure that has Postiz's LinkedIn provider returning 426 to everyone
    right now.

The state file, lookback window and loud failures work exactly as they do for
Instagram, and for the same reasons.
"""

import argparse
import datetime
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request

API = "https://api.linkedin.com"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

SCHEDULE = "schedule/linkedin.csv"
STATE = "state/posted_linkedin.json"
CARDS = "cards"

# Tue/Wed/Thu only, one a day - a missed run should still catch up, but must
# not post yesterday's card a day late.
LOOKBACK_HOURS = 8
MAX_PER_RUN = 1


def http(url, data=None, headers=None, method=None, raw=False):
    body = data if raw else (json.dumps(data).encode() if data is not None else None)
    req = urllib.request.Request(url, data=body, method=method or ("POST" if data is not None else "GET"))
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            txt = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(txt) if txt else {}, dict(r.headers)
            except ValueError:
                return r.status, {"raw": txt}, dict(r.headers)
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(txt), dict(e.headers)
        except ValueError:
            return e.code, {"raw": txt[:600]}, dict(e.headers)
    except urllib.error.URLError as e:
        raise SystemExit("network error reaching LinkedIn: %s" % e.reason)


def headers(token, version):
    return {
        "Authorization": "Bearer %s" % token,
        "LinkedIn-Version": version,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }


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
        if r["scheduled_at"] in state["posted"]:
            continue
        y, mo, d = (int(x) for x in r["date"].split("-"))
        hh, mm = (int(x) for x in r["time"].split(":"))
        when = datetime.datetime(y, mo, d, hh, mm, tzinfo=IST).astimezone(datetime.timezone.utc)
        age = (now_utc - when).total_seconds() / 3600.0
        if 0 <= age <= LOOKBACK_HOURS:
            out.append((when, r))
    out.sort(key=lambda x: x[0])
    return [r for _, r in out]


def upload_image(token, version, person_urn, path):
    """Three-step upload. Returns the image URN to attach to the post."""
    status, body, _ = http(
        API + "/rest/images?action=initializeUpload",
        {"initializeUploadRequest": {"owner": person_urn}},
        headers(token, version))
    if status not in (200, 201) or "value" not in body:
        raise RuntimeError("initializeUpload failed (HTTP %s): %s" % (status, json.dumps(body)[:400]))
    upload_url = body["value"]["uploadUrl"]
    image_urn = body["value"]["image"]

    with open(path, "rb") as f:
        blob = f.read()
    ctype = mimetypes.guess_type(path)[0] or "image/png"
    # The upload URL is pre-signed; it takes the bytes and the bearer token,
    # not the Rest.li headers.
    status, body, _ = http(upload_url, blob,
                           {"Authorization": "Bearer %s" % token, "Content-Type": ctype},
                           method="PUT", raw=True)
    if status not in (200, 201, 202):
        raise RuntimeError("image upload failed (HTTP %s): %s" % (status, json.dumps(body)[:400]))
    return image_urn


def publish(token, version, person_urn, image_urn, commentary, alt_text):
    payload = {
        "author": person_urn,
        "commentary": commentary,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "content": {"media": {"altText": alt_text[:300], "id": image_urn}},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    status, body, hdrs = http(API + "/rest/posts", payload, headers(token, version))
    if status not in (200, 201):
        raise RuntimeError("post creation failed (HTTP %s): %s" % (status, json.dumps(body)[:500]))
    # A successful create returns 201 with the id in the x-restli-id header.
    return hdrs.get("x-restli-id") or hdrs.get("X-RestLi-Id") or body.get("id", "created")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--schedule", default=SCHEDULE)
    ap.add_argument("--state", default=STATE)
    ap.add_argument("--cards", default=CARDS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", help="override current time, ISO 8601 UTC, for testing")
    args = ap.parse_args()

    token = os.environ.get("LI_ACCESS_TOKEN")
    person = os.environ.get("LI_PERSON_URN", "")
    version = os.environ.get("LINKEDIN_VERSION", "202608")
    if person and not person.startswith("urn:li:person:"):
        person = "urn:li:person:%s" % person
    if not args.dry_run and (not token or not person):
        raise SystemExit("LI_ACCESS_TOKEN and LI_PERSON_URN must be set")

    now = (datetime.datetime.fromisoformat(args.now).replace(tzinfo=datetime.timezone.utc)
           if args.now else datetime.datetime.now(datetime.timezone.utc))

    import csv
    with open(args.schedule, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    state = load_state(args.state)
    due = due_rows(rows, state, now)

    print("now (UTC): %s" % now.isoformat(timespec="seconds"))
    print("LinkedIn-Version: %s" % version)
    print("schedule rows: %d | already posted: %d | due now: %d"
          % (len(rows), len(state["posted"]), len(due)))

    if not due:
        print("nothing due. exiting cleanly.")
        return 0

    failures = 0
    for r in due[:MAX_PER_RUN]:
        card = os.path.join(args.cards, r["image"])
        print("\n>>> %s  [%s]  %s" % (r["scheduled_at"], r["pillar"], r["image"]))
        print("    %s" % r["caption"].split("\n")[0][:100])
        if not os.path.exists(card):
            print("    FAILED: card not found at %s" % card)
            failures += 1
            continue
        if args.dry_run:
            print("      DRY RUN - would upload %s and publish" % r["image"])
            continue
        try:
            image_urn = upload_image(token, version, person, card)
            print("    image uploaded: %s" % image_urn)
            post_id = publish(token, version, person, image_urn, r["caption"], r["alt_text"])
            state["posted"][r["scheduled_at"]] = {
                "post_id": post_id, "library_id": r["post_id"],
                "published_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            }
            save_state(args.state, state)
            print("    published: %s" % post_id)
        except Exception as e:
            failures += 1
            print("    FAILED: %s" % e)

    if failures:
        print("\n%d failure(s)." % failures)
        return 1
    print("\ndone. %d/%d slots complete." % (len(state["posted"]), len(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
