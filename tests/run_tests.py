#!/usr/bin/env python3
"""End-to-end tests for the content pipeline.

They run against the live local stack (start it with scripts/setup.sh) and use
it the way people and services would: requests come in through the form and the
API, reviewers answer through the review form, and outages are simulated with
the mock services' fault injection. Standard library only.

    python3 tests/run_tests.py             # every scenario (~3 min, includes a real 1-minute review timeout)
    python3 tests/run_tests.py --fast      # skip the review-timeout scenario
    python3 tests/run_tests.py revision    # only scenarios whose name contains "revision"

Environment: N8N_URL (default http://localhost:5678), MOCK_URL (default http://localhost:8080),
TIMEZONE (default America/Toronto, the workflows' timezone: publish dates are relative to today there).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from urllib import error, request
from zoneinfo import ZoneInfo

N8N = os.environ.get("N8N_URL", "http://localhost:5678").rstrip("/")
MOCK = os.environ.get("MOCK_URL", "http://localhost:8080").rstrip("/")
REVIEW_CHANNEL, ALERT_CHANNEL, DIGEST_CHANNEL = "content-review", "ops-alerts", "content-team"
TRACKER_DB = "demo-content-tracker"
TODAY = datetime.now(ZoneInfo(os.environ.get("TIMEZONE", "America/Toronto"))).date()


def day(offset: int) -> str:
    """A publish date relative to today, so the suite never goes stale."""
    return (TODAY + timedelta(days=offset)).isoformat()


PUBLISH = day(5)


# --------------------------------------------------------------------------- HTTP


def call(method: str, url: str, body: bytes | None = None, headers: dict | None = None) -> tuple[int, str]:
    req = request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except error.HTTPError as exc:
        return exc.code, exc.read().decode()


def post_json(url: str, payload: dict) -> tuple[int, dict]:
    status, text = call("POST", url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    return status, json.loads(text) if text else {}


def post_form(url: str, fields: list[str]) -> int:
    """Submit an n8n form the way the browser does: multipart, fields named field-0, field-1, ..."""
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="field-{i}"\r\n\r\n{value}\r\n'
        for i, value in enumerate(fields)
    ]
    body = ("".join(parts) + f"--{boundary}--\r\n").encode()
    status, _ = call("POST", url, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return status


# ------------------------------------------------------------------- mock helpers


def state() -> dict:
    return json.loads(call("GET", f"{MOCK}/__state")[1])


def reset() -> None:
    call("POST", f"{MOCK}/__reset")


def inject(**faults) -> None:
    post_json(f"{MOCK}/__faults", faults)


def wait_until(check, what: str, timeout: float = 40):
    """Poll until check() returns something truthy, or fail saying what never happened."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.5)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for: {what}")


def messages(channel: str, containing: str = "") -> list[str]:
    return [m["text"] for m in state()["slack_messages"] if m["channel"] == channel and containing in m["text"]]


def review_requests(request_id: str) -> list[str]:
    return [t for t in messages(REVIEW_CHANNEL, request_id) if t.startswith("*Review needed*")]


def page(request_id: str) -> dict | None:
    for p in state()["notion_pages"].values():
        rich = p["properties"].get("Request ID", {}).get("rich_text", [])
        if rich and rich[0]["text"]["content"] == request_id:
            return p
    return None


def prop(p: dict, name: str):
    value = p["properties"].get(name, {})
    if "select" in value:
        return value["select"]["name"]
    if "rich_text" in value:
        return "".join(part["text"]["content"] for part in value["rich_text"])
    if "number" in value:
        return value["number"]
    return None


def status_is(request_id: str, wanted: str):
    return lambda: (p := page(request_id)) and prop(p, "Status") == wanted and p


def llm_calls() -> list[dict]:
    return [c for c in state()["calls"] if c["service"] == "llm"]


def calls_to(service: str) -> list[dict]:
    return [c for c in state()["calls"] if c["service"] == service]


# ---------------------------------------------------------------- pipeline helpers


def new_request(**overrides) -> str:
    payload = {
        "client": "Casa Lupita",
        "platform": "instagram",
        "content_type": "Post caption",
        "brief": "New weekend brunch: chilaquiles verdes and churro French toast, launches Saturday",
        "tone": "Warm & friendly",
        "publish_at": PUBLISH,
        "avoid": "cheap, best in town",
        "requested_by": "Maya (account manager)",
    }
    payload.update(overrides)
    status, body = post_json(f"{N8N}/webhook/content-request", payload)
    assert status == 202, f"expected 202 from the API, got {status}: {body}"
    assert body["request_id"].startswith("CR-"), body
    return body["request_id"]


NOTION_HEADERS = {"Content-Type": "application/json", "Authorization": "Bearer test", "Notion-Version": "2022-06-28"}


def rich(text: str) -> dict:
    return {"rich_text": [{"text": {"content": text}}]}


def seed_request(client: str, status: str, publish_in: int, review_hours_ago: float | None = None, notes: str = "",
                 platform: str = "instagram", requested_by: str = "Maya") -> str:
    """Put a request straight into the tracker (through the Notion API), the way the pipeline would have left it."""
    rid = f"CR-SEED-{uuid.uuid4().hex[:6]}"
    props = {
        "Name": {"title": [{"text": {"content": f"{client} · {platform}"}}]}, "Request ID": rich(rid),
        "Client": rich(client), "Platform": {"select": {"name": platform}}, "Status": {"select": {"name": status}},
        "Publish date": {"date": {"start": day(publish_in)}}, "Requested by": rich(requested_by), "Notes": rich(notes),
    }
    if review_hours_ago is not None:
        asked = datetime.now(timezone.utc) - timedelta(hours=review_hours_ago)
        props["Review requested"] = {"date": {"start": asked.isoformat(timespec="seconds")}}
    body = json.dumps({"parent": {"database_id": TRACKER_DB}, "properties": props}).encode()
    status_code, _ = call("POST", f"{MOCK}/notion/v1/pages", body, NOTION_HEADERS)
    assert status_code == 200
    return rid


def run_digest() -> str:
    status, _ = call("POST", f"{N8N}/webhook/ops-digest", b"{}", {"Content-Type": "application/json"})
    assert status == 200, status
    return wait_until(lambda: messages(DIGEST_CHANNEL, "*Content digest*"), f"digest in #{DIGEST_CHANNEL}")[0]


def section(text: str, emoji: str) -> str:
    return next((part for part in text.split("\n\n") if part.startswith(emoji)), "")


def first_prompt() -> dict:
    return json.loads(llm_calls()[0]["body"]["messages"][-1]["content"])


def review_link(text: str) -> str:
    match = re.search(r"<(http[^|>]+/form-waiting/[^|>]+)\|", text)
    assert match, f"no review link in Slack message:\n{text}"
    # Links point at the public URL people use; the tests talk to the same n8n through N8N_URL.
    return N8N + match.group(1)[match.group(1).index("/form-waiting/"):]


def answer_review(request_id: str, nth: int, decision: str, feedback: str = "", reviewer: str = "Ana") -> None:
    """Wait for the nth review request of a content request, then answer it through the review form."""
    text = wait_until(lambda: len(review_requests(request_id)) >= nth and review_requests(request_id)[nth - 1],
                      f"review request #{nth} for {request_id} in #{REVIEW_CHANNEL}")
    assert post_form(review_link(text), [decision, feedback, reviewer]) == 200


# ---------------------------------------------------------------------- scenarios


def test_happy_path_api_request_is_drafted_reviewed_and_scheduled():
    rid = new_request()
    text = wait_until(lambda: review_requests(rid), "review request")[0]
    assert f"publish {PUBLISH} (in 5 days)" in text and "Urgent" not in text
    answer_review(rid, 1, "Approve")
    done = wait_until(status_is(rid, "Scheduled"), "tracker page marked Scheduled")
    post = state()["posts"][0]
    assert post["external_id"] == rid and post["platform"] == "instagram" and post["scheduled_at"] == PUBLISH
    assert "#CasaLupita" in post["text"]
    assert prop(done, "Post ID") == post["id"] and prop(done, "Approved by") == "Ana"
    assert prop(done, "Priority") == "Normal"
    wait_until(lambda: messages(REVIEW_CHANNEL, "✅ *Scheduled*"), "Slack confirmation")
    assert len(llm_calls()) == 1 and not messages(ALERT_CHANNEL)


def test_form_intake_reaches_review():
    status = post_form(f"{N8N}/form/new-content-request", [
        "Nonna Rosa", "Facebook", "Post caption", "Fresh pasta Tuesdays are back, two plates for 30 dollars",
        "Premium", day(6), "", "Leo"])
    assert status == 200, status
    text = wait_until(lambda: messages(REVIEW_CHANNEL, "Nonna Rosa"), "review request for the form submission")[0]
    rid = re.search(r"Request (CR-\S+)", text).group(1)
    p = page(rid)
    assert prop(p, "Platform") == "facebook" and prop(p, "Requested by") == "Leo"
    answer_review(rid, 1, "Reject", "Duplicate request")
    wait_until(status_is(rid, "Rejected"), "clean-up: rejected")


def test_invalid_api_request_gets_400_and_creates_nothing():
    status, body = post_json(f"{N8N}/webhook/content-request", {"client": "Casa Lupita", "platform": "instagram"})
    assert status == 400 and "brief" in body["error"], (status, body)
    status, _ = post_json(f"{N8N}/webhook/content-request", {"client": "Casa Lupita", "brief": "x", "platform": "myspace"})
    assert status == 400
    time.sleep(2)
    assert not state()["calls"], "an invalid request must not reach any service"


def test_revision_loop_sends_feedback_back_to_the_ai():
    rid = new_request()
    answer_review(rid, 1, "Request changes", "Make it shorter")
    second = wait_until(lambda: len(review_requests(rid)) >= 2 and review_requests(rid)[1], "second review request")
    assert "revision 1" in second and "Last feedback: Make it shorter" in second
    prompt = json.loads(llm_calls()[1]["body"]["messages"][-1]["content"])
    assert prompt["feedback"] == "Make it shorter" and prompt["revision"] == 1
    answer_review(rid, 2, "Approve")
    done = wait_until(status_is(rid, "Scheduled"), "Scheduled after one revision")
    assert prop(done, "Revision") == 1 and len(llm_calls()) == 2


def test_revision_limit_hands_over_to_the_account_manager():
    rid = new_request()
    for n in (1, 2, 3):  # MAX_REVISIONS=2: the third "Request changes" is one too many
        answer_review(rid, n, "Request changes", f"Try again ({n})")
    done = wait_until(status_is(rid, "Needs attention"), "tracker page marked Needs attention")
    assert "after 2 revisions" in prop(done, "Notes")
    handover = wait_until(lambda: messages(REVIEW_CHANNEL, "⏳ *Needs attention*"), "Slack hand-over message")[0]
    assert "Daniela, can you take it from here?" in handover, handover  # the client's account manager, from the profile
    assert len(llm_calls()) == 3 and not state()["posts"]


def test_reject_closes_the_request_without_publishing():
    rid = new_request()
    answer_review(rid, 1, "Reject", "Client paused this campaign")
    done = wait_until(status_is(rid, "Rejected"), "tracker page marked Rejected")
    assert prop(done, "Notes") == "Client paused this campaign"
    wait_until(lambda: messages(REVIEW_CHANNEL, "❌ *Rejected*"), "Slack rejection note")
    assert not state()["posts"]


def test_brand_checks_flag_words_the_client_wants_to_avoid():
    rid = new_request(brief="TEST_BANNED Taco Tuesday is back with new fillings")
    text = wait_until(lambda: review_requests(rid), "review request")[0]
    assert "⚠️ Words to avoid found: cheap" in text, text
    assert "✅ Length" in text
    answer_review(rid, 1, "Reject", "Uses a forbidden word")
    wait_until(status_is(rid, "Rejected"), "clean-up: rejected")


def test_client_profile_shapes_the_draft():
    rid = new_request(client="Nonna Rosa", tone="", avoid="")
    text = wait_until(lambda: review_requests(rid), "review request")[0]
    prompt = first_prompt()
    assert "Old-school Italian trattoria" in prompt["brand_voice"] and prompt["tone"] == "Premium"
    assert prompt["words_to_avoid"] == ["cheap", "deal", "fast"] and prompt["required_hashtags"] == ["#NonnaRosaTO"]
    assert "A presto." in text and "✅ Client hashtags included" in text and "ℹ️ Client profile applied" in text
    answer_review(rid, 1, "Reject", "clean-up")
    wait_until(status_is(rid, "Rejected"), "clean-up: rejected")


def test_unknown_client_is_drafted_from_the_request_only():
    rid = new_request(client="Brand New Bistro", tone="")
    text = wait_until(lambda: review_requests(rid), "review request")[0]
    prompt = first_prompt()
    assert prompt["brand_voice"] == "" and prompt["required_hashtags"] == [] and prompt["tone"] == "Warm & friendly"
    assert prompt["words_to_avoid"] == ["cheap", "best in town"]
    assert "ℹ️ No client profile for Brand New Bistro: drafted from the request only" in text
    answer_review(rid, 1, "Reject", "clean-up")
    wait_until(status_is(rid, "Rejected"), "clean-up: rejected")


def test_client_profile_outage_does_not_block_the_request():
    inject(notion=[{}] + [{"status": 503}] * 3)  # the tracker page is created, then the profile lookup fails 3 times
    rid = new_request()
    text = wait_until(lambda: review_requests(rid), "review request without the profile", timeout=60)
    assert "⚠️ Client profile unavailable (Notion error)" in text[0]
    assert first_prompt()["words_to_avoid"] == ["cheap", "best in town"] and not messages(ALERT_CHANNEL)
    answer_review(rid, 1, "Reject", "clean-up")
    wait_until(status_is(rid, "Rejected"), "clean-up: rejected")


def test_brand_checks_flag_missing_client_hashtags():
    inject(llm=[{"drop_required_hashtags": True}])
    rid = new_request()
    text = wait_until(lambda: review_requests(rid), "review request")[0]
    assert "⚠️ Missing client hashtags: #KensingtonEats" in text, text
    answer_review(rid, 1, "Reject", "clean-up")
    wait_until(status_is(rid, "Rejected"), "clean-up: rejected")


def test_requests_publishing_soon_are_marked_urgent():
    rid = new_request(publish_at=day(1))
    text = wait_until(lambda: review_requests(rid), "review request")[0]
    assert "(tomorrow) · 🔥 *Urgent*" in text, text
    assert prop(page(rid), "Priority") == "Urgent"
    answer_review(rid, 1, "Reject", "clean-up")
    wait_until(status_is(rid, "Rejected"), "clean-up: rejected")


def test_llm_errors_are_retried():
    inject(llm=[{"status": 500}, {"status": 500}])
    rid = new_request()
    wait_until(lambda: review_requests(rid), "review request after two failed LLM calls")
    assert [c["status"] for c in llm_calls()] == [500, 500, 200]
    answer_review(rid, 1, "Reject", "clean-up")
    wait_until(status_is(rid, "Rejected"), "clean-up: rejected")


def test_llm_outage_marks_failed_and_alerts_once():
    inject(llm=[{"status": 500}] * 3)
    rid = new_request()
    done = wait_until(status_is(rid, "Failed"), "tracker page marked Failed")
    assert prop(done, "Notes").startswith("AI draft:")
    alert = wait_until(lambda: messages(ALERT_CHANNEL), "alert in #ops-alerts")[0]
    assert "at: AI draft" in alert and rid in alert and "/executions/" in alert
    assert "Detail: HTTP 500: upstream error" in alert, alert  # the provider's message, not raw JSON
    time.sleep(5)  # the error workflow runs too; it must not alert a second time
    assert len(messages(ALERT_CHANNEL)) == 1 and not review_requests(rid)


def test_invalid_json_from_the_model_gets_one_more_try():
    inject(llm=[{"malformed": True}])
    rid = new_request()
    wait_until(lambda: review_requests(rid), "review request after a malformed reply")
    retry_prompt = json.loads(llm_calls()[1]["body"]["messages"][-1]["content"])
    assert "not a valid JSON" in retry_prompt["format_note"]
    answer_review(rid, 1, "Reject", "clean-up")
    wait_until(status_is(rid, "Rejected"), "clean-up: rejected")


def test_invalid_json_twice_marks_failed():
    inject(llm=[{"malformed": True}, {"malformed": True}])
    rid = new_request()
    done = wait_until(status_is(rid, "Failed"), "tracker page marked Failed")
    assert "did not return a valid draft" in prop(done, "Notes")
    wait_until(lambda: messages(ALERT_CHANNEL), "alert in #ops-alerts")


def test_slack_ok_false_is_retried_then_fails():
    # Slack answers errors with HTTP 200 + {"ok": false, "error": ...}. n8n's HTTP Request node treats a
    # 2xx JSON body with an "error" field as a failed call, so this goes through retries + error output.
    inject(slack=[{"error": "channel_not_found"}] * 3)
    rid = new_request()
    done = wait_until(status_is(rid, "Failed"), "tracker page marked Failed")
    assert prop(done, "Notes").startswith("Send draft to Slack:") and "channel_not_found" in prop(done, "Notes")
    wait_until(lambda: messages(ALERT_CHANNEL, "at: Send draft to Slack"), "alert in #ops-alerts")
    assert len(calls_to("slack")) == 4  # 3 tries for the review message + the alert


def test_slack_ok_false_without_error_field_is_caught_by_the_guard():
    inject(slack=[{"body": {"ok": False, "warning": "missing_charset"}}])  # no "error" field: n8n sees a success
    rid = new_request()
    done = wait_until(status_is(rid, "Failed"), "tracker page marked Failed")
    assert "Slack answered:" in prop(done, "Notes")
    wait_until(lambda: messages(ALERT_CHANNEL, "at: Send draft to Slack"), "alert in #ops-alerts")


def test_publisher_errors_are_retried():
    inject(publisher=[{"status": 503}, {"status": 503}])
    rid = new_request()
    answer_review(rid, 1, "Approve")
    wait_until(status_is(rid, "Scheduled"), "Scheduled after two failed scheduling calls", timeout=60)
    assert [c["status"] for c in calls_to("publisher")] == [503, 503, 201]


def test_publisher_outage_marks_failed():
    inject(publisher=[{"status": 503}] * 3)
    rid = new_request()
    answer_review(rid, 1, "Approve")
    done = wait_until(status_is(rid, "Failed"), "tracker page marked Failed", timeout=60)
    assert prop(done, "Notes") == "Schedule the post: HTTP 503: temporarily unavailable", prop(done, "Notes")
    wait_until(lambda: messages(ALERT_CHANNEL, "at: Schedule the post"), "alert in #ops-alerts")


def test_notion_outage_still_alerts():
    inject(notion=[{"status": 503}] * 3)
    rid = new_request()
    alert = wait_until(lambda: messages(ALERT_CHANNEL), "alert in #ops-alerts")[0]
    assert "at: Create tracker page in Notion" in alert and rid in alert
    assert "HTTP 503: Notion is unavailable." in alert, alert
    assert not llm_calls(), "no draft should be written for a request that is not tracked"


def test_review_timeout_hands_over_to_the_account_manager():
    rid = new_request(review_timeout_minutes=1)
    wait_until(lambda: review_requests(rid), "review request")
    done = wait_until(status_is(rid, "Needs attention"), "Needs attention after the review timeout", timeout=200)
    assert "within 1 minutes" in prop(done, "Notes")
    handover = wait_until(lambda: messages(REVIEW_CHANNEL, "⏳ *Needs attention*"), "Slack hand-over message")[0]
    assert "Daniela, can you take it from here?" in handover


def test_digest_keeps_deadlines_reviews_and_handoffs_visible():
    late = seed_request("Casa Lupita", "In review", 1, review_hours_ago=26)
    waiting = seed_request("Nonna Rosa", "In review", 10, review_hours_ago=5, platform="facebook")
    handoff = seed_request("Harbour Oyster Bar", "Needs attention", 6, notes="Nobody reviewed it within 2880 minutes",
                           requested_by="Priya")
    failed = seed_request("Casa Lupita", "Failed", 8, notes="AI draft: HTTP 500: upstream error", platform="tiktok")
    seed_request("Nonna Rosa", "Scheduled", 3)
    seed_request("Nonna Rosa", "Scheduled", 20)
    seed_request("Old Bistro", "Rejected", 1)
    text = run_digest()
    urgent = section(text, "🔥")
    assert urgent.startswith("🔥 *Publishing soon, not scheduled yet* (1)") and late in urgent, text
    assert "publishes tomorrow · In review for 26 h · Maya" in urgent, urgent
    review = section(text, "👀")
    assert review.startswith("👀 *Waiting for review* (1)") and waiting in review
    assert "waiting 5 h · publishes in 10 days" in review, review
    attention = section(text, "🙋")
    assert handoff in attention and "Nobody reviewed it within 2880 minutes · Priya" in attention, attention
    assert failed in section(text, "🚨") and "AI draft: HTTP 500" in section(text, "🚨")
    assert "✅ Scheduled for the next 7 days: 1" in text and "Old Bistro" not in text


def test_digest_reads_every_page_of_the_tracker():
    post_json(f"{MOCK}/__config", {"notion_page_size": 2})  # Notion returns at most 100 per page; 2 forces pagination
    ids = [seed_request("Nonna Rosa", "In review", 10 + n, review_hours_ago=3) for n in range(5)]
    text = run_digest()
    assert "👀 *Waiting for review* (5)" in text and all(rid in text for rid in ids), text
    queries = [c for c in calls_to("notion") if c["path"] == f"/notion/v1/databases/{TRACKER_DB}/query"]
    assert len(queries) == 3 and queries[1]["body"]["start_cursor"], queries


def test_digest_says_so_when_nothing_is_waiting():
    seed_request("Nonna Rosa", "Scheduled", 2)
    text = run_digest()
    assert "Nothing is waiting on anyone." in text and "✅ Scheduled for the next 7 days: 1" in text, text


def test_digest_outage_alerts_ops_once():
    inject(notion=[{"status": 503}] * 3)
    status, _ = call("POST", f"{N8N}/webhook/ops-digest", b"{}", {"Content-Type": "application/json"})
    assert status == 200
    alert = wait_until(lambda: messages(ALERT_CHANNEL, "Content digest failed"), "digest alert in #ops-alerts")[0]
    assert "at: Read the tracker in Notion" in alert and "HTTP 503: Notion is unavailable." in alert, alert
    assert "/executions/" in alert
    time.sleep(5)  # the error workflow runs too; it must not alert a second time
    assert len(messages(ALERT_CHANNEL)) == 1 and not messages(DIGEST_CHANNEL)


SLOW = {"test_review_timeout_hands_over_to_the_account_manager"}


def main(argv: list[str]) -> int:
    fast = "--fast" in argv
    filters = [a for a in argv if not a.startswith("--")]
    tests = [(name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)]
    tests = [(n, f) for n, f in tests if not (fast and n in SLOW) and (not filters or any(x in n for x in filters))]

    code, _ = call("GET", f"{MOCK}/health")
    assert code == 200, f"mock services not reachable at {MOCK}"
    # Right after a (re)start n8n is up before its webhooks are registered; an empty request
    # gets a 400 from the API intake once it is, without touching any service.
    wait_until(lambda: post_json(f"{N8N}/webhook/content-request", {})[0] == 400, "the API intake to answer", 90)
    failed = 0
    for name, fn in tests:
        reset()
        started = time.time()
        try:
            fn()
            print(f"PASS  {name}  ({time.time() - started:.1f}s)", flush=True)
        except Exception:  # noqa: BLE001 - report every failure and keep going
            failed += 1
            print(f"FAIL  {name}  ({time.time() - started:.1f}s)\n{traceback.format_exc()}", flush=True)
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
