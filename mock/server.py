"""Local stand-ins for the external services the pipeline talks to.

One small HTTP server (Python standard library only) that implements just the
subset of each API the workflow uses, so the whole pipeline can run and be
tested on a laptop without real accounts:

  /llm/v1/chat/completions        OpenAI-compatible chat completions
  /slack/api/chat.postMessage     Slack Web API (answers HTTP 200 + ok:false on errors, like Slack)
  /notion/v1/pages[/<id>]         Notion API: create and update database pages
  /notion/v1/databases/<id>/query Notion API: query a database (filters, pagination)
  /publisher/v1/posts             a scheduling tool (Buffer-like)

Two read-only views help when demoing: /slack (the review channel) and /board
(the content tracker). Tests drive failures through /__faults, tune behaviour
through /__config and inspect everything through /__state.

The Notion "Clients" database comes pre-filled with three restaurants, so the
pipeline has client profiles to read.
"""

from __future__ import annotations

import html
import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = 8080
LOCK = threading.Lock()


CLIENTS_DB = "demo-clients"
CLIENTS = [
    # name, brand voice, default tone, words to avoid, hashtags, sign-off, account manager
    ("Casa Lupita", "Family-run Mexican kitchen in Kensington Market. Talks like a friendly host, a few Spanish words, "
     "never salesy.", "Warm & friendly", "cheap, authentic, fusion", "#CasaLupita, #KensingtonEats", "¡Buen provecho! 🌮",
     "Daniela"),
    ("Nonna Rosa", "Old-school Italian trattoria. Short and elegant, food first, no emojis in the first line.", "Premium",
     "cheap, deal, fast", "#NonnaRosaTO", "A presto.", "Leo"),
    ("Harbour Oyster Bar", "Bright seafood bar on the waterfront. Playful, a little cheeky, always about the sea.",
     "Playful", "fishy, frozen", "#HarbourOysters, #TOWaterfront", "See you at the bar. 🦪", "Priya"),
]


def _rich(text: str) -> dict:
    return {"rich_text": [{"text": {"content": text}}]}


def _client_pages() -> dict:
    pages = {}
    for name, voice, tone, avoid, hashtags, sign_off, manager in CLIENTS:
        pid = str(uuid.uuid5(uuid.NAMESPACE_URL, "client/" + name))
        pages[pid] = {
            "object": "page", "id": pid, "created_time": "2026-01-05T15:00:00+00:00",
            "last_edited_time": "2026-01-05T15:00:00+00:00", "parent": {"database_id": CLIENTS_DB},
            "url": f"https://www.notion.so/{pid.replace('-', '')}",
            "properties": {
                "Name": {"title": [{"text": {"content": name}}]}, "Brand voice": _rich(voice),
                "Default tone": {"select": {"name": tone}}, "Words to avoid": _rich(avoid), "Hashtags": _rich(hashtags),
                "Sign-off": _rich(sign_off), "Account manager": _rich(manager),
            },
        }
    return pages


def _empty_state() -> dict:
    return {
        "faults": {"llm": [], "slack": [], "notion": [], "publisher": []},
        "config": {"notion_page_size": 100},  # largest page a Notion query returns (the real API's maximum is 100)
        "calls": [],  # every API call, in order: {"service", "method", "path", "status", "body"}
        "slack_messages": [],
        "notion_pages": _client_pages(),
        "posts": [],
    }


STATE = _empty_state()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def next_fault(service: str) -> dict | None:
    """Pop the next queued fault for a service, if any."""
    with LOCK:
        queue = STATE["faults"].get(service, [])
        return queue.pop(0) if queue else None


def record(service: str, method: str, path: str, status: int, body) -> None:
    with LOCK:
        STATE["calls"].append(
            {"at": now_iso(), "service": service, "method": method, "path": path, "status": status, "body": body}
        )


# --------------------------------------------------------------------------- LLM


def fake_draft(request: dict, drop_required_hashtags: bool = False) -> dict:
    """Deterministic 'copywriter' so tests can assert on the output.

    It reacts to the fields a real model would: client, platform, brief, tone,
    words to avoid, the client's hashtags and sign-off, and reviewer feedback.
    """
    client = request.get("client", "the restaurant")
    platform = request.get("platform", "instagram")
    brief = request.get("brief", "").strip().rstrip(".")
    feedback = (request.get("feedback") or "").strip()
    revision = int(request.get("revision") or 0)

    # "New weekend brunch: chilaquiles verdes and churro French toast" -> headline + details
    headline, _, details = brief.partition(":")
    details = details.strip() or headline
    hook = f"{headline.strip()} at {client} ✨" if details != headline else f"Something new at {client} ✨"
    body = (
        f"{details[0].upper() + details[1:]}. Made in-house every day and served the way our regulars love it.\n\n"
        f"Tag the friend you're bringing 👇 and book your table through the link in bio."
    )
    if "TEST_BANNED" in brief:
        # Lets the test suite check that brand guardrails catch a forbidden word.
        body += " Best cheap eats in town!"
    if feedback:
        # A revision answers the feedback: shorter copy that keeps the key details.
        body = f"{details[0].upper() + details[1:]}. Book through the link in bio 👆"
    if request.get("sign_off"):
        body += f"\n\n{request['sign_off']}"
    tag = re.sub(r"[^A-Za-z0-9]", "", client) or "Restaurant"
    required = [] if drop_required_hashtags else list(request.get("required_hashtags") or [])
    hashtags = []
    for h in required + [f"#{tag}", "#TorontoEats", "#FoodieTO", "#RestaurantLife", f"#{platform.capitalize()}Food"]:
        if h.lower() not in (x.lower() for x in hashtags):
            hashtags.append(h)
    if platform == "x":
        hashtags = hashtags[:2]
    return {
        "hook": hook,
        "caption": f"{hook}\n\n{body}",
        "hashtags": hashtags,
        "call_to_action": "Book your table - link in bio",
        "alt_text": f"Close-up photo for {client}: {details}",
        "visual_direction": "Warm natural light, close-up of the dish, hands in frame for a human touch.",
        "revision_note": f"Revision {revision}: {feedback}" if feedback else "",
    }


def llm_completion(payload: dict) -> tuple[int, dict | str]:
    fault = next_fault("llm")
    if fault and fault.get("delay"):
        time.sleep(float(fault["delay"]))
    if fault and fault.get("status"):
        return fault["status"], {"error": {"message": fault.get("message", "upstream error"), "type": "server_error"}}

    messages = payload.get("messages") or []
    user = next((m for m in reversed(messages) if m.get("role") == "user"), {"content": "{}"})
    try:
        request = json.loads(user.get("content") or "{}")
    except json.JSONDecodeError:
        request = {"brief": str(user.get("content"))}

    if fault and fault.get("malformed"):
        content = "Sure! Here is your caption: " + json.dumps(fake_draft(request))[:120]  # not valid JSON
    else:
        content = json.dumps(fake_draft(request, drop_required_hashtags=bool(fault and fault.get("drop_required_hashtags"))))

    return 200, {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.get("model", "mock-model"),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 250, "completion_tokens": 180, "total_tokens": 430},
    }


# ------------------------------------------------------------------- Slack/Notion


def slack_post(headers, payload: dict) -> tuple[int, dict]:
    if not (headers.get("Authorization") or "").startswith("Bearer "):
        return 200, {"ok": False, "error": "not_authed"}
    fault = next_fault("slack")
    if fault and "body" in fault:
        return fault.get("status", 200), fault["body"]
    if fault and fault.get("status"):
        return fault["status"], {"ok": False, "error": "service_unavailable"}
    if fault and fault.get("error"):
        return 200, {"ok": False, "error": fault["error"]}  # Slack-style failure: HTTP 200 + ok:false
    if not payload.get("channel") or not payload.get("text"):
        return 200, {"ok": False, "error": "no_text"}
    ts = f"{time.time():.6f}"
    with LOCK:
        STATE["slack_messages"].append({"ts": ts, "at": now_iso(), "channel": payload["channel"], "text": payload["text"]})
    return 200, {"ok": True, "channel": payload["channel"], "ts": ts}


def notion_error(status: int, code: str, message: str) -> tuple[int, dict]:
    return status, {"object": "error", "status": status, "code": code, "message": message}


def notion_pages(headers, method: str, page_id: str | None, payload: dict) -> tuple[int, dict]:
    if not (headers.get("Authorization") or "").startswith("Bearer "):
        return notion_error(401, "unauthorized", "API token is invalid.")
    if not headers.get("Notion-Version"):
        return notion_error(400, "missing_version", "Notion-Version header failed validation.")
    fault = next_fault("notion")
    if fault and fault.get("status"):
        return notion_error(fault["status"], "service_unavailable", "Notion is unavailable.")

    with LOCK:
        if method == "POST":
            if not payload.get("parent", {}).get("database_id"):
                return notion_error(400, "validation_error", "body.parent.database_id should be defined.")
            pid = str(uuid.uuid4())
            page = {
                "object": "page",
                "id": pid,
                "created_time": now_iso(),
                "last_edited_time": now_iso(),
                "parent": payload["parent"],
                "properties": payload.get("properties", {}),
                "url": f"https://www.notion.so/{pid.replace('-', '')}",
            }
            STATE["notion_pages"][pid] = page
            return 200, notion_read(page)
        page = STATE["notion_pages"].get(page_id or "")
        if not page:
            return notion_error(404, "object_not_found", f"Could not find page with ID: {page_id}.")
        page["properties"].update(payload.get("properties", {}))
        page["last_edited_time"] = now_iso()
        return 200, notion_read(page)


PROPERTY_TYPES = ("title", "rich_text", "select", "status", "date", "number", "checkbox", "url")


def notion_read(page: dict) -> dict:
    """A page the way the Notion API returns it: every property carries its type, every text its plain_text."""
    out = json.loads(json.dumps(page))
    for prop in out["properties"].values():
        kind = next((k for k in PROPERTY_TYPES if k in prop), None)
        if kind:
            prop["type"] = kind
        if kind in ("title", "rich_text"):
            for part in prop[kind]:
                part.setdefault("type", "text")
                part["plain_text"] = part.get("text", {}).get("content", "")
    return out


def matches(page: dict, flt: dict | None) -> bool:
    """The subset of Notion's filter language the workflows use; anything else is a 400, like a real validation error."""
    if not flt:
        return True
    if "and" in flt:
        return all(matches(page, f) for f in flt["and"])
    if "or" in flt:
        return any(matches(page, f) for f in flt["or"])
    value = prop_text(page["properties"].get(flt.get("property"), {}))
    for kind in ("title", "rich_text", "select", "status"):
        if kind in flt:
            condition = flt[kind]
            if "equals" in condition:
                return value == condition["equals"]
            if "does_not_equal" in condition:
                return value != condition["does_not_equal"]
            if "contains" in condition:
                return condition["contains"].lower() in value.lower()
    raise ValueError(f"unsupported filter: {json.dumps(flt)}")


def notion_query(headers, database_id: str, payload: dict) -> tuple[int, dict]:
    if not (headers.get("Authorization") or "").startswith("Bearer "):
        return notion_error(401, "unauthorized", "API token is invalid.")
    if not headers.get("Notion-Version"):
        return notion_error(400, "missing_version", "Notion-Version header failed validation.")
    fault = next_fault("notion")
    if fault and fault.get("status"):
        return notion_error(fault["status"], "service_unavailable", "Notion is unavailable.")
    try:
        start = int(str(payload.get("start_cursor") or "cursor-0").removeprefix("cursor-"))
        with LOCK:
            rows = [p for p in STATE["notion_pages"].values()
                    if p["parent"].get("database_id") == database_id and matches(p, payload.get("filter"))]
            size = max(1, min(int(payload.get("page_size") or 100), 100, STATE["config"]["notion_page_size"]))
    except ValueError as exc:
        return notion_error(400, "validation_error", str(exc))
    rows.sort(key=lambda p: p["created_time"])
    chunk, more = rows[start:start + size], start + size < len(rows)
    return 200, {"object": "list", "results": [notion_read(p) for p in chunk],
                 "next_cursor": f"cursor-{start + size}" if more else None, "has_more": more,
                 "type": "page_or_database", "page_or_database": {}}


def publisher_post(headers, payload: dict) -> tuple[int, dict]:
    fault = next_fault("publisher")
    if fault and fault.get("status"):
        return fault["status"], {"error": "temporarily unavailable"}
    if not payload.get("text") or not payload.get("platform"):
        return 422, {"error": "text and platform are required"}
    post = {
        "id": f"post_{uuid.uuid4().hex[:10]}",
        "status": "scheduled",
        "platform": payload["platform"],
        "scheduled_at": payload.get("scheduled_at") or now_iso(),
        "text": payload["text"],
        "external_id": payload.get("external_id"),
    }
    with LOCK:
        STATE["posts"].append(post)
    return 201, post


# ------------------------------------------------------------------------- views


def prop_text(prop: dict) -> str:
    """Plain text of a Notion property, whatever its type."""
    if not prop:
        return ""
    kind = prop.get("type") or next((k for k in ("title", "rich_text", "select", "status", "date", "number") if k in prop), "")
    value = prop.get(kind)
    if kind in ("title", "rich_text"):
        return "".join(part.get("text", {}).get("content", "") for part in value or [])
    if kind in ("select", "status"):
        return (value or {}).get("name", "")
    if kind == "date":
        return (value or {}).get("start", "")
    return "" if value is None else str(value)


STYLE = """
:root { --bg:#f6f4ef; --card:#fff; --ink:#1f2328; --muted:#6b6f76; --line:#e4e0d8; --accent:#c2410c; }
* { box-sizing:border-box; } body { margin:0; font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--ink); }
header { padding:18px 28px; border-bottom:1px solid var(--line); background:#fff; display:flex; gap:16px; align-items:baseline; }
header h1 { font-size:18px; margin:0; } header span { color:var(--muted); font-size:13px; }
main { padding:24px 28px; } a { color:var(--accent); }
.feed { max-width:760px; display:flex; flex-direction:column; gap:12px; }
.msg { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; white-space:pre-wrap; }
.msg .meta { color:var(--muted); font-size:12px; margin-bottom:6px; white-space:normal; }
.board { display:grid; grid-template-columns:repeat(6, minmax(170px, 1fr)); gap:14px; align-items:start; overflow-x:auto; }
.col h2 { font-size:13px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); margin:0 0 8px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px; margin-bottom:10px; font-size:13px; }
.card b { display:block; font-size:14px; margin-bottom:4px; } .card small { color:var(--muted); display:block; }
.pill { display:inline-block; font-size:11px; padding:1px 8px; border-radius:99px; background:#f1ede4; margin-top:6px; }
.pill.urgent { background:#fde3d3; color:#9a3412; margin-left:4px; }
.empty { color:var(--muted); font-size:13px; }
"""

# Refresh the content every few seconds without reloading the page (keeps scroll position).
LIVE_RELOAD = """<script>
setInterval(async () => {
  try {
    const html = await (await fetch(location.href, { cache: "no-store" })).text();
    const fresh = new DOMParser().parseFromString(html, "text/html").querySelector("main");
    const main = document.querySelector("main");
    if (fresh && main.innerHTML !== fresh.innerHTML) main.innerHTML = fresh.innerHTML;
  } catch (e) {}
}, 2000);
</script>"""

BOARD_COLUMNS = ["Drafting", "In review", "Needs attention", "Scheduled", "Rejected", "Failed"]


def page_shell(title: str, subtitle: str, body: str) -> str:
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'><title>{title}</title>"
        f"<style>{STYLE}</style></head><body><header><h1>{title}</h1><span>{subtitle}</span></header>"
        f"<main>{body}</main>{LIVE_RELOAD}</body></html>"
    )


def slack_mrkdwn_to_html(text: str) -> str:
    out = html.escape(text)
    out = re.sub(r"&lt;(https?://[^|&]+)\|([^&]+)&gt;", r"<a href='\1' target='_blank'>\2</a>", out)
    out = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", out)
    return out


def render_slack() -> str:
    with LOCK:
        messages = list(reversed(STATE["slack_messages"]))
    items = "".join(
        f"<div class='msg'><div class='meta'>#{html.escape(m['channel'])} · {m['at']}</div>{slack_mrkdwn_to_html(m['text'])}</div>"
        for m in messages
    ) or "<p class='empty'>No messages yet.</p>"
    return page_shell("Slack (local mock)", "what the workflow posts to the review and alert channels", f"<div class='feed'>{items}</div>")


def render_board() -> str:
    with LOCK:
        pages = list(STATE["notion_pages"].values())
    columns = []
    for col in BOARD_COLUMNS:
        cards = [p for p in pages if prop_text(p["properties"].get("Status")) == col]
        html_cards = "".join(
            "<div class='card'>"
            f"<b>{html.escape(prop_text(p['properties'].get('Name')))}</b>"
            f"<small>{html.escape(prop_text(p['properties'].get('Request ID')))}</small>"
            f"<small>Publish: {html.escape(prop_text(p['properties'].get('Publish date')) or '-')}</small>"
            f"<span class='pill'>rev {html.escape(prop_text(p['properties'].get('Revision')) or '0')}</span>"
            + ("<span class='pill urgent'>🔥 urgent</span>" if prop_text(p["properties"].get("Priority")) == "Urgent" else "")
            +
            "</div>"
            for p in sorted(cards, key=lambda p: p["last_edited_time"], reverse=True)
        ) or "<p class='empty'>-</p>"
        columns.append(f"<div class='col'><h2>{col} · {len(cards)}</h2>{html_cards}</div>")
    return page_shell("Content tracker (Notion mock)", "one card per content request", f"<div class='board'>{''.join(columns)}</div>")


# ----------------------------------------------------------------------- routing


class Handler(BaseHTTPRequestHandler):
    server_version = "content-pipeline-mock/1.0"

    def log_message(self, fmt, *args):  # quieter logs: one line per request
        print(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}", flush=True)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw.decode("utf-8", "replace")}

    def _send(self, status: int, body, content_type: str = "application/json") -> None:
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            links = "".join(f"<p><a href='{p}'>{p}</a></p>" for p in ("/slack", "/board", "/__state"))
            return self._send(200, page_shell("Content pipeline mocks", "local stand-ins for Slack, Notion, LLM and publisher", links), "text/html")
        if path == "/slack":
            return self._send(200, render_slack(), "text/html")
        if path == "/board":
            return self._send(200, render_board(), "text/html")
        if path == "/__state":
            with LOCK:
                return self._send(200, STATE)
        if path == "/health":
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def _dispatch(self, method: str):
        global STATE
        path = urlparse(self.path).path
        payload = self._read_json()

        if path == "/__reset":
            with LOCK:
                STATE = _empty_state()
            return self._send(200, {"ok": True})
        if path == "/__config":
            with LOCK:
                STATE["config"].update(payload)
            return self._send(200, {"ok": True, "config": STATE["config"]})
        if path == "/__faults":
            with LOCK:
                for service, faults in payload.items():
                    STATE["faults"].setdefault(service, []).extend(faults)
            return self._send(200, {"ok": True, "faults": STATE["faults"]})

        if method == "POST" and path == "/llm/v1/chat/completions":
            service, (status, body) = "llm", llm_completion(payload)
        elif method == "POST" and path == "/slack/api/chat.postMessage":
            service, (status, body) = "slack", slack_post(self.headers, payload)
        elif method == "POST" and (m := re.fullmatch(r"/notion/v1/databases/([^/]+)/query", path)):
            service, (status, body) = "notion", notion_query(self.headers, m.group(1), payload)
        elif path.startswith("/notion/v1/pages"):
            page_id = path[len("/notion/v1/pages/"):] or None
            service, (status, body) = "notion", notion_pages(self.headers, method, page_id, payload)
        elif method == "POST" and path == "/publisher/v1/posts":
            service, (status, body) = "publisher", publisher_post(self.headers, payload)
        else:
            return self._send(404, {"error": "not found"})

        record(service, method, path, status, payload)
        self._send(status, body)


if __name__ == "__main__":
    print(f"mock services listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
