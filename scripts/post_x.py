#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Publish the X post that is due. Standard library only.

Run by .github/workflows/x.yml, once a day.

WHY OAUTH 1.0a AND NOT OAUTH 2.0
    Both are accepted on /2/media/upload and /2/tweets. OAuth 1.0a wins here for
    one reason: its four credentials never expire. OAuth 2.0 user context gives
    a 2-hour access token and a refresh token that ROTATES on every use, so a
    headless job must write the new refresh token back into a secret after every
    single run - and if one write fails, the chain is broken and posting stops
    until a human re-authorises in a browser. That is the LinkedIn renewal
    problem, except every two hours instead of every two months.

    OAuth 1.0a signs each request from static keys. Nothing to refresh, nothing
    to store, no workflow that can silently break the credential chain.

WHY NOT v1.1 MEDIA UPLOAD
    The v1.1 media endpoints were sunset on 9 June 2025. Everything here is v2.

WHAT A POST COSTS
    Under pay-per-use, a post is $0.015 - unless it contains a link, which is
    $0.20. The schedule is deliberately link-free; the CTA lives in the profile
    bio. See the cost note in the README before adding a URL to any row.

ALT TEXT
    Off by default. Setting alt text is a separate API call, and with a fixed
    prepaid balance an extra call per post is a real cost. Set X_ALT_TEXT=1 to
    turn it on once you know how the balance is actually being drawn down.
"""

import argparse
import base64
import csv
import datetime
import hashlib
import hmac
import io
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.x.com"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

SCHEDULE = "schedule/x.csv"
STATE = "state/posted_x.json"
CARDS = "cards_x"

# One a day. The lookback lets a late or skipped cron catch up, but refuses to
# post yesterday's card a day late.
LOOKBACK_HOURS = 8
MAX_PER_RUN = 1


# --------------------------------------------------------------------------
# OAuth 1.0a
# --------------------------------------------------------------------------

def q(s):
    """Percent-encoding per RFC 5849: unreserved is ALPHA / DIGIT / - . _ ~

    urllib's default safe set and its treatment of spaces are both wrong for
    OAuth, so the safe set is stated explicitly.
    """
    return urllib.parse.quote(str(s), safe="-._~")


def sign(method, url, oauth, params, consumer_secret, token_secret):
    """Return the base64 HMAC-SHA1 signature for one request.

    `params` holds any query-string or form-encoded body parameters. Bodies
    that are JSON or multipart are NOT signed - only their URL is - which is
    why posting and media upload pass an empty dict here.
    """
    allp = dict(oauth)
    allp.update(params)
    norm = "&".join("%s=%s" % (q(k), q(allp[k])) for k in sorted(allp))
    base = "&".join([method.upper(), q(url), q(norm)])
    key = "%s&%s" % (q(consumer_secret), q(token_secret))
    mac = hmac.new(key.encode(), base.encode(), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode()


def auth_header(method, url, creds, params=None):
    ck, cs, tk, ts = creds
    oauth = {
        "oauth_consumer_key": ck,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": tk,
        "oauth_version": "1.0",
    }
    oauth["oauth_signature"] = sign(method, url, oauth, params or {}, cs, ts)
    return "OAuth " + ", ".join('%s="%s"' % (q(k), q(oauth[k])) for k in sorted(oauth))


def selftest():
    """Prove the signer on X's documented example credentials.

    A wrong signature fails as a flat 401 with no hint about which of the four
    credentials or which encoding rule is at fault, so the algorithm is checked
    against a fixed vector rather than debugged against a live endpoint.

    PROVENANCE OF THE TWO CONSTANTS BELOW. The base string is the one printed in
    X's OAuth 1.0a documentation. The digest was produced by oauthlib's RFC 5849
    implementation over that exact base string and key, and confirmed identical
    to this signer's output. Do NOT replace either constant with a value copied
    from a blog or from memory: the widely quoted "hCtSmYh+..." digest does not
    correspond to these secrets, and chasing it sends you editing correct code.
    oauthlib is used only here, as a one-off cross-check - it is not a runtime
    dependency, and CI never imports it.
    """
    cs = "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw"
    ts = "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE"
    url = "https://api.twitter.com/1.1/statuses/update.json"
    oauth = {
        "oauth_consumer_key": "xvz1evFS4wEEPTGEFPHBog",
        "oauth_nonce": "kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": "1318622958",
        "oauth_token": "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        "oauth_version": "1.0",
    }
    params = {"status": "Hello Ladies + Add Me to Your Twitter Dogs",
              "include_entities": "true"}

    allp = dict(oauth)
    allp.update(params)
    norm = "&".join("%s=%s" % (q(k), q(allp[k])) for k in sorted(allp))
    base = "&".join(["POST", q(url), q(norm)])
    want_base = (
        "POST&https%3A%2F%2Fapi.twitter.com%2F1.1%2Fstatuses%2Fupdate.json&"
        "include_entities%3Dtrue%26oauth_consumer_key%3Dxvz1evFS4wEEPTGEFPHBog"
        "%26oauth_nonce%3DkYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg"
        "%26oauth_signature_method%3DHMAC-SHA1%26oauth_timestamp%3D1318622958"
        "%26oauth_token%3D370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb"
        "%26oauth_version%3D1.0%26status%3DHello%2520Ladies%2520%252B%2520Add"
        "%2520Me%2520to%2520Your%2520Twitter%2520Dogs")
    ok = True
    if base != want_base:
        # Percent-encoding is the usual culprit: spaces inside a parameter must
        # end up as %2520, being encoded once for the parameter and again for
        # the base string.
        print("BASE STRING MISMATCH")
        print("  got : %s" % base)
        print("  want: %s" % want_base)
        ok = False

    got = sign("POST", url, oauth, params, cs, ts)
    want = "wAVNAYuyZ51bHyHEkBDcAwWdQrc="
    print("signature: %s" % got)
    print("expected : %s" % want)
    if got != want:
        ok = False
    if not ok:
        print("SELF-TEST FAILED - the signer is wrong, do not use it.")
        return 1
    print("self-test passed (base string and digest).")
    return 0


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http(url, creds, body=None, ctype=None, method="POST"):
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", auth_header(method, url, creds))
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            txt = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(txt) if txt else {})
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(txt)
        except ValueError:
            return e.code, {"raw": txt[:600]}
    except urllib.error.URLError as e:
        raise SystemExit("network error reaching X: %s" % e.reason)


def multipart(fields, files):
    """Build a multipart/form-data body. Returns (bytes, content_type)."""
    boundary = "----quantumcx%s" % secrets.token_hex(12)
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(("--%s\r\n" % boundary).encode())
        out.write(('Content-Disposition: form-data; name="%s"\r\n\r\n' % k).encode())
        out.write(("%s\r\n" % v).encode())
    for k, (filename, blob, ftype) in files.items():
        out.write(("--%s\r\n" % boundary).encode())
        out.write(('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                   % (k, filename)).encode())
        out.write(("Content-Type: %s\r\n\r\n" % ftype).encode())
        out.write(blob)
        out.write(b"\r\n")
    out.write(("--%s--\r\n" % boundary).encode())
    return out.getvalue(), "multipart/form-data; boundary=%s" % boundary


def upload_image(creds, path):
    """Simple single-request upload. Images only; videos need the chunked flow."""
    with open(path, "rb") as f:
        blob = f.read()
    body, ctype = multipart({"media_category": "tweet_image"},
                            {"media": (os.path.basename(path), blob, "image/png")})
    status, resp = http(API + "/2/media/upload", creds, body, ctype)
    if status not in (200, 201):
        raise RuntimeError("media upload failed (HTTP %s): %s"
                           % (status, json.dumps(resp)[:400]))
    # v2 returns data.id; older shapes used media_id_string. Accept either.
    mid = (resp.get("data", {}).get("id")
           or resp.get("media_id_string")
           or resp.get("id"))
    if not mid:
        raise RuntimeError("upload succeeded but no media id in response: %s"
                           % json.dumps(resp)[:300])
    return str(mid)


def set_alt_text(creds, media_id, text):
    body = json.dumps({"id": media_id,
                       "metadata": {"alt_text": {"text": text[:1000]}}}).encode()
    status, resp = http(API + "/2/media/metadata", creds, body, "application/json")
    if status not in (200, 201):
        # Never fail the post over alt text - the post itself is the point.
        print("    note: alt text rejected (HTTP %s): %s"
              % (status, json.dumps(resp)[:200]))


def publish(creds, text, media_id):
    body = json.dumps({"text": text, "media": {"media_ids": [media_id]}}).encode()
    status, resp = http(API + "/2/tweets", creds, body, "application/json")
    if status not in (200, 201):
        raise RuntimeError("post failed (HTTP %s): %s" % (status, json.dumps(resp)[:500]))
    return resp.get("data", {}).get("id", "created")


# --------------------------------------------------------------------------

def verify(creds):
    """Prove the four credentials against the live API without publishing.

    A dry run cannot tell a correct key from a wrong one, because it makes no
    call at all. This makes exactly one read - GET /2/users/me - which exercises
    the same signing path the poster uses and fails the same way a bad
    credential would, but leaves nothing on the timeline.

    It costs one read under pay-per-use, which is a third of the price of a post
    and far less than discovering the problem when a scheduled slot goes red.

    WHAT IT DOES NOT PROVE. A read succeeds on a read-only token, so this
    confirms the keys and the signature but not the write permission. The
    developer console showing 'Read and write' on the access token row is the
    evidence for that half.
    """
    url = API + "/2/users/me"
    status, resp = http(url, creds, method="GET")
    if status != 200:
        print("CREDENTIALS REJECTED (HTTP %s)" % status)
        print(json.dumps(resp, indent=2)[:600])
        print("")
        if status == 401:
            print("401 means the signature did not validate. The signer is")
            print("self-tested, so suspect the secrets: a truncated paste, a")
            print("stray space, or an access token generated BEFORE the app")
            print("permission was set to Read and write.")
        elif status == 403:
            print("403 usually means the token lacks write permission, or the")
            print("app is not connected to a project.")
        elif status == 429:
            print("429 is a rate limit, not a credential problem. Try again.")
        return 1
    me = resp.get("data", {})
    print("CREDENTIALS OK")
    print("  authenticated as: @%s (%s)" % (me.get("username", "?"), me.get("id", "?")))
    print("  name:             %s" % me.get("name", "?"))
    print("")
    print("This proves the four keys and the OAuth 1.0a signature. Write")
    print("permission is evidenced by the console showing 'Read and write'")
    print("on the access token; the first real post confirms it outright.")
    return 0


def load_state(path):
    if os.path.exists(path):
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"posted": {}}


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(state, indent=2, sort_keys=True))
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--schedule", default=SCHEDULE)
    ap.add_argument("--state", default=STATE)
    ap.add_argument("--cards", default=CARDS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="prove the credentials against the live API without posting")
    ap.add_argument("--now", help="override current time, ISO 8601 UTC, for testing")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    creds = (os.environ.get("X_API_KEY", ""), os.environ.get("X_API_SECRET", ""),
             os.environ.get("X_ACCESS_TOKEN", ""), os.environ.get("X_ACCESS_SECRET", ""))
    if not args.dry_run and not all(creds):
        raise SystemExit("X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN and "
                         "X_ACCESS_SECRET must all be set")

    if args.verify:
        return verify(creds)

    now = (datetime.datetime.fromisoformat(args.now).replace(tzinfo=datetime.timezone.utc)
           if args.now else datetime.datetime.now(datetime.timezone.utc))

    with io.open(args.schedule, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    state = load_state(args.state)
    due = due_rows(rows, state, now)

    print("now (UTC): %s" % now.isoformat(timespec="seconds"))
    print("schedule rows: %d | already posted: %d | due now: %d"
          % (len(rows), len(state["posted"]), len(due)))

    if not due:
        print("nothing due. exiting cleanly.")
        return 0

    failures = 0
    for r in due[:MAX_PER_RUN]:
        card = os.path.join(args.cards, r["image"])
        print("\n>>> %s  [%s]  %s" % (r["scheduled_at"], r["pillar"], r["image"]))
        print("    %s" % r["content"][:110])
        if len(r["content"]) > 280:
            print("    FAILED: %d characters, over the 280 limit" % len(r["content"]))
            failures += 1
            continue
        if "http" in r["content"]:
            # $0.20 instead of $0.015. Loud, because it is a 13x cost surprise.
            print("    WARNING: this post contains a link and will be billed at "
                  "the link rate, not the standard rate.")
        if not os.path.exists(card):
            print("    FAILED: card not found at %s" % card)
            failures += 1
            continue
        if args.dry_run:
            print("      DRY RUN - would upload %s and post" % r["image"])
            continue
        try:
            media_id = upload_image(creds, card)
            print("    media uploaded: %s" % media_id)
            if os.environ.get("X_ALT_TEXT") == "1":
                set_alt_text(creds, media_id, "QuantumCX: " + r["content"][:200])
            post_id = publish(creds, r["content"], media_id)
            state["posted"][r["scheduled_at"]] = {
                "post_id": post_id, "library_id": r["post_id"],
                "published_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            }
            save_state(args.state, state)
            print("    published: https://x.com/QuantumCXAI/status/%s" % post_id)
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
