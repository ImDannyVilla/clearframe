#!/usr/bin/env python3
"""The evidence page (#8). Standard library only.

Runs inside the sandbox on 127.0.0.1:8090, forwarded to the GB10's own loopback
with `openshell forward`. Two pages, as the contract says:

- `/shot/<shot>`: every take of the shot with its status.
- `/take/<take_id>`: measured anomalies with their frames and the brightness
  plot, candidate notes with the model's words and Confirm / Dismiss, the prompt
  diff from version 1 to the latest with its hash, Approve and Accept as is,
  and the take's events in time order.

The page reads SQLite read-only and serves evidence frames from `results/`. It
never writes either: every write is one of the contract's four actions, made
through `evidence.actions` with exactly the contract's fields.

Only the GB10's own browser can reach it. POSTs carry a per-run form token, and
the Host header must be the loopback address, so another web page open in the
same browser can't press the buttons.
"""
from __future__ import annotations

import difflib
import html
import json
import os
import secrets
import sqlite3
import sys
import urllib.parse
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import db  # noqa: E402
from evidence import actions as actions_module  # noqa: E402
from rubric import rubric as rubric_loader  # noqa: E402

DEFAULT_RESULTS = Path("/sandbox/clearframe/host/results")
MAX_FORM_BYTES = 16 * 1024


DEFAULT_QUEUE = Path("/sandbox/clearframe/approved_generation_queue")


@dataclass
class Config:
    db_path: Path = db.DEFAULT_DB
    results_dir: Path = DEFAULT_RESULTS
    rubric_path: Path = ROOT / "rubric" / "rubric.yaml"
    queue_dir: Path = DEFAULT_QUEUE
    host: str = "127.0.0.1"
    port: int = 8090
    actions: actions_module.Actions | None = None  # None: #10's agent.approval on db_path
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))

    def __post_init__(self) -> None:
        if self.actions is None:
            self.actions = actions_module.approval_actions(self.db_path, self.rubric_path, self.queue_dir)

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ.get
        return cls(
            db_path=Path(env("CLEARFRAME_DB", str(db.DEFAULT_DB))),
            results_dir=Path(env("CLEARFRAME_RESULTS", str(DEFAULT_RESULTS))),
            rubric_path=Path(env("CLEARFRAME_RUBRIC", str(ROOT / "rubric" / "rubric.yaml"))),
            queue_dir=Path(env("CLEARFRAME_QUEUE", str(DEFAULT_QUEUE))),
            host=env("EVIDENCE_HOST", "127.0.0.1"),
            port=int(env("EVIDENCE_PORT", "8090")),
        )


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# Reading -----------------------------------------------------------------------------------

class NoDatabase(Exception):
    pass


def open_db(config: Config) -> sqlite3.Connection:
    if not config.db_path.is_file():
        raise NoDatabase()
    return db.connect_readonly(config.db_path)


def take_sort_key(take_id: str) -> tuple[int, int, int]:
    shot, rest = take_id.split("-", 1)
    take, _, retake = rest.partition("r")
    return int(shot), int(take), int(retake or 0)


def read_json(results_dir: Path, relative: str | None):
    path = safe_result_path(results_dir, relative) if relative else None
    if path is None or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def safe_result_path(results_dir: Path, relative: str) -> Path | None:
    """The file under results/ that `relative` names, or None if it would leave results/."""
    if not relative or "\x00" in relative or relative.startswith(("/", "\\")):
        return None
    root = results_dir.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


# Pieces of the page ---------------------------------------------------------------------------

STATUS_HELP = {
    "review_pending": "checks still running, or a check ended in error",
    "needs_confirmation": "candidate notes are waiting for a person",
    "flagged": "a measured anomaly or a confirmed note",
    "clean": "every enabled check finished with nothing to fix",
    "accepted_as_is": "kept as it is by a person",
    "approved_for_regeneration": "a fixed prompt was approved and queued",
}

CSS = """
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0 auto;max-width:1100px;padding:1rem 1.5rem;
color:#111;background:#fff;line-height:1.45}
a{color:#0645ad}a:focus,button:focus,select:focus{outline:3px solid #1a73e8;outline-offset:2px}
h1{font-size:1.6rem;margin:.2rem 0 .8rem}h2{font-size:1.25rem;margin-top:2rem;border-bottom:1px solid #ccc}
h3{font-size:1.05rem}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.35rem .5rem;border-bottom:1px solid #ddd;
vertical-align:top}
.status{display:inline-block;padding:.1rem .5rem;border-radius:.3rem;font-weight:600;border:1px solid #555}
.status-clean{background:#e6f4ea}.status-flagged{background:#fde7e7}.status-needs_confirmation{background:#fff4d6}
.status-review_pending{background:#eef}.status-accepted_as_is{background:#eee}
.status-approved_for_regeneration{background:#e3f0ff}
.frames{display:flex;flex-wrap:wrap;gap:.5rem;margin:.5rem 0}.frames figure{margin:0}
.frames img{width:240px;height:auto;border:1px solid #999}.frames figcaption{font-size:.85rem}
.card{border:1px solid #bbb;border-radius:.4rem;padding:.75rem 1rem;margin:.75rem 0}
.note{background:#f6f6f6;border-left:4px solid #888;padding:.4rem .7rem;white-space:pre-wrap}
.banner{padding:.6rem .9rem;border-radius:.3rem;margin:.6rem 0;font-weight:600}
.banner-ok{background:#e6f4ea;border:1px solid #2e7d32}.banner-refused{background:#fde7e7;border:1px solid #b71c1c}
pre.diff{background:#fafafa;border:1px solid #ccc;padding:.6rem;overflow-x:auto;white-space:pre-wrap}
.add{background:#e6f4ea;display:block}.del{background:#fde7e7;display:block;text-decoration:line-through}
code{font-size:.9em;word-break:break-all}
button{font-size:1rem;padding:.35rem .9rem;margin-right:.4rem;cursor:pointer}
form.inline{display:inline}
.muted{color:#555}
"""


def page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{esc(title)} · ClearFrame</title><style>{CSS}</style></head><body>"
        "<header><p><a href=\"/\">ClearFrame</a> · evidence page · on the GB10 only</p></header>"
        f"<main>{body}</main></body></html>"
    )


def status_badge(status: str) -> str:
    return (f"<span class=\"status status-{esc(status)}\" title=\"{esc(STATUS_HELP.get(status, ''))}\">"
            f"{esc(status.replace('_', ' '))}</span>")


def banner(query: dict) -> str:
    if "refused" in query:
        return f"<div class=\"banner banner-refused\" role=\"alert\">Refused: {esc(query['refused'])}</div>"
    if "done" in query:
        return f"<div class=\"banner banner-ok\" role=\"status\">{esc(query['done'])}</div>"
    return ""


def frame_figures(take_id: str, evidence: list[str], fps) -> str:
    if not evidence:
        return "<p class=\"muted\">No evidence frames.</p>"
    figures = []
    for path in evidence:
        name = path.rsplit("/", 1)[-1]
        number = name.split(".")[0]
        try:
            frame = int(number)
            seconds = f", {frame / fps:.3f} s" if fps else ""
            caption = f"Frame {frame}{seconds}"
        except ValueError:
            caption = name
        figures.append(
            f"<figure><a href=\"/results/{esc(path)}\"><img src=\"/results/{esc(path)}\" "
            f"alt=\"Take {esc(take_id)}, {esc(caption)}\" loading=\"lazy\"></a>"
            f"<figcaption>{esc(caption)}</figcaption></figure>"
        )
    return "<div class=\"frames\">" + "".join(figures) + "</div>"


def brightness_plot(series: list[float], threshold: float, spans: list[tuple[int, int]], fps) -> str:
    """Two SVG panels: mean brightness per frame, and the change between frames with the threshold line."""
    if not series:
        return "<p class=\"muted\">No brightness series.</p>"
    width, height, pad = 960, 150, 36
    count = len(series)
    changes = [0.0] + [abs(series[i] - series[i - 1]) for i in range(1, count)]

    def x(frame: float) -> float:
        return pad + (width - 2 * pad) * (frame / max(1, count - 1))

    def panel(values: list[float], top: float, label: str, line: float | None) -> str:
        y_max = max(max(values), line or 0, 1.0) * 1.1

        def y(value: float) -> float:
            return top + height - (height - 10) * (value / y_max)

        parts = [f"<text x=\"{pad}\" y=\"{top - 6}\" font-size=\"13\">{esc(label)}</text>",
                 f"<rect x=\"{pad}\" y=\"{top}\" width=\"{width - 2 * pad}\" height=\"{height}\" "
                 "fill=\"none\" stroke=\"#999\"/>"]
        for start, end in spans:
            parts.append(f"<rect x=\"{x(start - 0.5):.1f}\" y=\"{top}\" width=\"{max(2.0, x(end + 0.5) - x(start - 0.5)):.1f}\" "
                         f"height=\"{height}\" fill=\"#f4b4b4\" opacity=\"0.6\"/>")
        points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
        parts.append(f"<polyline points=\"{points}\" fill=\"none\" stroke=\"#1a4f9c\" stroke-width=\"1.5\"/>")
        if line is not None:
            parts.append(f"<line x1=\"{pad}\" x2=\"{width - pad}\" y1=\"{y(line):.1f}\" y2=\"{y(line):.1f}\" "
                         "stroke=\"#b71c1c\" stroke-width=\"1.5\" stroke-dasharray=\"6 4\"/>")
            parts.append(f"<text x=\"{width - pad}\" y=\"{y(line) - 4:.1f}\" font-size=\"12\" text-anchor=\"end\" "
                         f"fill=\"#b71c1c\">threshold {line:g}</text>")
        parts.append(f"<text x=\"{pad}\" y=\"{top + height + 14}\" font-size=\"11\">frame 0</text>")
        last = f"frame {count - 1}" + (f" ({(count - 1) / fps:.2f} s)" if fps else "")
        parts.append(f"<text x=\"{width - pad}\" y=\"{top + height + 14}\" font-size=\"11\" "
                     f"text-anchor=\"end\">{esc(last)}</text>")
        return "".join(parts)

    peak = max(changes)
    above = sum(1 for value in changes if value > threshold)
    summary = (f"Mean brightness of all {count} frames, measured by code, and the change between consecutive "
               f"frames. The largest change is {peak:.1f} on a 0-255 scale; the threshold is {threshold:g}; "
               f"{above} frame{'s' if above != 1 else ''} cross it. Shaded: the measured anomalies.")
    svg = (f"<svg role=\"img\" aria-labelledby=\"plot-title plot-desc\" viewBox=\"0 0 {width} {2 * height + 80}\" "
           f"width=\"100%\" xmlns=\"http://www.w3.org/2000/svg\"><title id=\"plot-title\">Brightness plot</title>"
           f"<desc id=\"plot-desc\">{esc(summary)}</desc>"
           + panel(series, 20, "Mean brightness per frame (0-255)", None)
           + panel(changes, height + 60, "Change from the previous frame, with the flicker threshold", threshold)
           + "</svg>")
    return svg + f"<p class=\"muted\">{esc(summary)}</p>"


def prompt_diff(old: str, new: str) -> str:
    lines = []
    for line in difflib.ndiff(old.splitlines(), new.splitlines()):
        tag, text = line[:2], line[2:]
        if tag == "+ ":
            lines.append(f"<ins class=\"add\">+ {esc(text) or '&nbsp;'}</ins>")
        elif tag == "- ":
            lines.append(f"<del class=\"del\">- {esc(text) or '&nbsp;'}</del>")
        elif tag == "  ":
            lines.append(f"<span>  {esc(text)}</span>")
    return "<pre class=\"diff\" aria-label=\"Prompt diff\">" + "\n".join(lines) + "</pre>"


def approver_select(approvers: list[str], name_id: str) -> str:
    options = "".join(f"<option>{esc(name)}</option>" for name in approvers)
    return (f"<label for=\"{name_id}\">Approver</label> "
            f"<select id=\"{name_id}\" name=\"approver\" required>{options}</select> ")


def form(config: Config, action: str, hidden: dict, inner: str, label: str, css: str = "inline",
         extra: str = "") -> str:
    fields = "".join(f"<input type=\"hidden\" name=\"{esc(k)}\" value=\"{esc(v)}\">" for k, v in hidden.items())
    return (f"<form class=\"{css}\" method=\"post\" action=\"/action/{esc(action)}\">"
            f"<input type=\"hidden\" name=\"token\" value=\"{esc(config.token)}\">{fields}{inner}"
            f"<button type=\"submit\">{esc(label)}</button>{extra}</form>")


# Pages -------------------------------------------------------------------------------------

def render_index(conn: sqlite3.Connection) -> str:
    shots = conn.execute("SELECT shot, COUNT(*) AS n FROM takes GROUP BY shot ORDER BY shot").fetchall()
    if not shots:
        return page("Shots", "<h1>Shots</h1><p>No takes have been ingested yet.</p>")
    items = "".join(f"<li><a href=\"/shot/{row['shot']}\">Shot {row['shot']}</a> ({row['n']} takes)</li>"
                    for row in shots)
    return page("Shots", f"<h1>Shots</h1><ul>{items}</ul>")


def render_shot(conn: sqlite3.Connection, shot: int, query: dict) -> str | None:
    rows = conn.execute("SELECT * FROM takes WHERE shot = ?", (shot,)).fetchall()
    if not rows:
        return None
    rows = sorted(rows, key=lambda row: take_sort_key(row["take_id"]))
    originals = [row for row in rows if not row["parent_take_id"]]  # NULL or '' both mean "an original"
    expected = max((row["expected_takes"] or 0) for row in originals) if originals else None
    body = [f"<h1>Shot {shot}</h1>", banner(query)]
    originals = len(originals)
    if expected:
        body.append(f"<p>{originals} of {expected} expected takes have arrived.</p>")
    body.append("<table><caption class=\"muted\">Every take of the shot, with its status</caption>"
                "<thead><tr><th scope=\"col\">Take</th><th scope=\"col\">Status</th><th scope=\"col\">Retake of</th>"
                "<th scope=\"col\">Open issues</th><th scope=\"col\">Updated</th></tr></thead><tbody>")
    for row in rows:
        issues = conn.execute(
            "SELECT state, issue_type, COUNT(*) AS n FROM issues WHERE take_id = ? AND state != 'dismissed' "
            "GROUP BY state, issue_type ORDER BY state, issue_type", (row["take_id"],)).fetchall()
        summary = ", ".join(f"{i['n']} {i['state']} ({i['issue_type'].replace('_', ' ')})" for i in issues) or "none"
        body.append(
            f"<tr><th scope=\"row\"><a href=\"/take/{esc(row['take_id'])}\">{esc(row['take_id'])}</a></th>"
            f"<td>{status_badge(row['status'])}</td><td>{esc(row['parent_take_id'] or '')}</td>"
            f"<td>{esc(summary)}</td><td>{esc(row['updated_at'])}</td></tr>")
    body.append("</tbody></table>")
    return page(f"Shot {shot}", "".join(body))


def render_take(conn: sqlite3.Connection, config: Config, policy: dict, take_id: str, query: dict) -> str | None:
    take = conn.execute("SELECT * FROM takes WHERE take_id = ?", (take_id,)).fetchone()
    if take is None:
        return None
    status, fps = take["status"], take["fps"]
    issues = conn.execute("SELECT * FROM issues WHERE take_id = ? ORDER BY issue_id", (take_id,)).fetchall()
    versions = conn.execute("SELECT * FROM prompt_versions WHERE take_id = ? ORDER BY version",
                            (take_id,)).fetchall()
    events = conn.execute("SELECT * FROM events WHERE take_id = ? ORDER BY at, event_id", (take_id,)).fetchall()
    approvers = list(policy["approvers"])
    waiting = any(issue["state"] == "candidate" for issue in issues)
    can_decide_notes = status in ("needs_confirmation", "flagged")

    body = [f"<h1>Take {esc(take_id)}</h1>", banner(query),
            f"<p>Status: {status_badge(status)} <span class=\"muted\">({esc(STATUS_HELP.get(status, ''))})</span></p>",
            f"<p><a href=\"/shot/{take['shot']}\">All takes of shot {take['shot']}</a>"]
    if take["parent_take_id"]:
        body.append(f" · retake of <a href=\"/take/{esc(take['parent_take_id'])}\">{esc(take['parent_take_id'])}</a>")
    body.append("</p>")
    errored = db.errored_checks(conn, take_id)
    if errored:
        body.append("<p class=\"banner banner-refused\">Checks that ended in error: "
                    f"{esc(', '.join(c.replace('_', ' ') for c in errored))}. The take stays reviewing.</p>")

    # Measured anomalies and the brightness plot.
    measured = [issue for issue in issues if issue["state"] == "measured"]
    body.append("<h2 id=\"measured\">Measured anomalies</h2>")
    if not measured:
        body.append("<p>Code measured every frame and found no anomaly past the threshold.</p>")
    for issue in measured:
        body.append(
            f"<section class=\"card\" aria-labelledby=\"h-{esc(issue['issue_id'])}\">"
            f"<h3 id=\"h-{esc(issue['issue_id'])}\">{esc(issue['issue_type'].replace('_', ' '))}: frames "
            f"{issue['frame_start']} to {issue['frame_end']} ({issue['t_start']:.3f} s to {issue['t_end']:.3f} s)</h3>"
            f"<p>Measured {issue['value']:g} against a threshold of {issue['threshold']:g} "
            "(mean brightness change between consecutive frames, 0-255).</p>"
            + frame_figures(take_id, json.loads(issue["evidence"]), fps) + "</section>")
    series = read_json(config.results_dir, take["brightness_series"])
    threshold = next((issue["threshold"] for issue in measured if issue["threshold"] is not None),
                     policy["flicker_threshold"])
    body.append("<h3>Brightness, every frame</h3>")
    if isinstance(series, list):
        body.append(brightness_plot([float(v) for v in series], float(threshold),
                                    [(i["frame_start"], i["frame_end"]) for i in measured], fps))
    else:
        body.append("<p class=\"muted\">The brightness series isn't in results/ yet.</p>")

    # Candidate notes.
    notes = [issue for issue in issues if issue["state"] != "measured"]
    body.append("<h2 id=\"notes\">Candidate notes</h2>")
    if not notes:
        body.append("<p>The reviewer noted nothing.</p>")
    for issue in notes:
        state = issue["state"]
        decided = (f" by {esc(issue['decided_by'])} at {esc(issue['decided_at'])}"
                   if issue["decided_by"] else "")
        body.append(
            f"<section class=\"card\" aria-labelledby=\"h-{esc(issue['issue_id'])}\">"
            f"<h3 id=\"h-{esc(issue['issue_id'])}\">{esc(issue['issue_type'].replace('_', ' '))}: frames "
            f"{issue['frame_start']} to {issue['frame_end']} ({issue['t_start']:.3f} s to {issue['t_end']:.3f} s)"
            f" · {esc(state)}{decided}</h3>"
            "<p>The reviewer model's words (a candidate, not a finding):</p>"
            f"<blockquote class=\"note\">{esc(issue['model_note'] or '(no words given)')}</blockquote>"
            + frame_figures(take_id, json.loads(issue["evidence"]), fps))
        if state == "candidate" and can_decide_notes:
            # One form, two buttons: the approver goes with whichever is pressed.
            body.append(form(
                config, "confirm_note", {"issue_id": issue["issue_id"]},
                approver_select(approvers, f"approver-{issue['issue_id']}"), "Confirm note",
                extra="<button type=\"submit\" formaction=\"/action/dismiss_note\">Dismiss note</button>"))
        body.append("</section>")

    # The prompt and its fix.
    body.append("<h2 id=\"prompt\">Prompt</h2>")
    if not versions:
        body.append("<p class=\"muted\">No prompt version yet.</p>")
    else:
        first, latest = versions[0], versions[-1]
        if latest["version"] == first["version"]:
            body.append("<p>Version 1, from the generation log. No fix has been added.</p>"
                        f"<pre class=\"diff\">{esc(first['prompt'])}</pre>")
        else:
            fixes = ", ".join(t.replace("_", " ") for t in json.loads(latest["fixes"])) or "none"
            body.append(f"<p>Version 1 to version {latest['version']} (fixes: {esc(fixes)}).</p>"
                        + prompt_diff(first["prompt"], latest["prompt"]))
        body.append(f"<p>Latest: version {latest['version']}, hash <code>{esc(latest['prompt_hash'])}</code></p>")

    # Decisions.
    body.append("<h2 id=\"decide\">Decision</h2>")
    approvals = conn.execute("SELECT * FROM approvals WHERE take_id = ? ORDER BY approval_id", (take_id,)).fetchall()
    for approval in approvals:
        extra = (f", prompt version {approval['prompt_version']}, queued as {esc(approval['queue_file'])}, "
                 f"returns as {esc(approval['next_take_id'])}" if approval["queue_file"] else "")
        body.append(f"<p>{esc(approval['decision'].replace('_', ' '))} by {esc(approval['approver'])} at "
                    f"{esc(approval['decided_at'])}{extra}. Decisions are final.</p>")
    if not approvals:
        latest = versions[-1] if versions else None
        controls = []
        if status == "flagged" and not waiting and latest is not None and latest["version"] > 1:
            controls.append(form(
                config, "approve_regeneration",
                {"take_id": take_id, "prompt_version": latest["version"], "prompt_hash": latest["prompt_hash"]},
                approver_select(approvers, "approver-approve"),
                f"Approve version {latest['version']} for regeneration", css="card"))
        elif status == "flagged":
            reason = ("a note is still waiting" if waiting else "no fixed prompt version yet")
            controls.append(f"<p class=\"muted\">Approve isn't available: {esc(reason)}.</p>")
        if status in ("flagged", "needs_confirmation"):
            controls.append(form(config, "accept_as_is", {"take_id": take_id},
                                 approver_select(approvers, "approver-accept"), "Accept as is", css="card"))
        body.append("".join(controls) or f"<p class=\"muted\">No decision to make while the take is "
                                         f"{esc(status.replace('_', ' '))}.</p>")

    # The audit trail.
    body.append("<h2 id=\"events\">Events (the audit trail)</h2><table><thead><tr><th scope=\"col\">At</th>"
                "<th scope=\"col\">Actor</th><th scope=\"col\">Event</th><th scope=\"col\">Detail</th></tr></thead><tbody>")
    for event in events:
        body.append(f"<tr><td>{esc(event['at'])}</td><td>{esc(event['actor'])}</td><td>{esc(event['kind'])}</td>"
                    f"<td><code>{esc(event['detail'])}</code></td></tr>")
    body.append("</tbody></table>")
    return page(f"Take {take_id}", "".join(body))


# Serving -------------------------------------------------------------------------------------

def make_handler(config: Config):
    allowed_hosts = {f"127.0.0.1:{config.port}", f"localhost:{config.port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "ClearFrameEvidence/1"

        def log_message(self, fmt, *args):  # one short line per request, no query strings
            sys.stderr.write(f"{db.now()} evidence: {self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}\n")

        def host_ok(self) -> bool:
            host = self.headers.get("Host", "")
            port = self.server.server_address[1]
            return host in allowed_hosts | {f"127.0.0.1:{port}", f"localhost:{port}"}

        def send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def html_page(self, status: int, text: str) -> None:
            self.send(status, text.encode("utf-8"))

        def not_found(self, what: str = "Not found") -> None:
            self.html_page(HTTPStatus.NOT_FOUND, page("Not found", f"<h1>{esc(what)}</h1><p><a href=\"/\">Shots</a></p>"))

        def redirect(self, location: str) -> None:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if not self.host_ok():
                return self.send(HTTPStatus.FORBIDDEN, b"Forbidden", "text/plain")
            parsed = urllib.parse.urlsplit(self.path)
            query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            parts = [urllib.parse.unquote(p) for p in parsed.path.split("/") if p]
            if parts[:1] == ["results"]:
                return self.serve_result("/".join(parts[1:]))
            try:
                conn = open_db(config)
            except NoDatabase:
                return self.html_page(HTTPStatus.OK, page("Waiting", "<h1>No database yet</h1><p>The automation "
                                                                     "hasn't ingested any result file.</p>"))
            try:
                if not parts:
                    return self.html_page(HTTPStatus.OK, render_index(conn))
                if len(parts) == 2 and parts[0] == "shot" and parts[1].isdigit():
                    text = render_shot(conn, int(parts[1]), query)
                    return self.html_page(HTTPStatus.OK, text) if text else self.not_found("No such shot")
                if len(parts) == 2 and parts[0] == "take":
                    policy = rubric_loader.load(config.rubric_path)
                    text = render_take(conn, config, policy, parts[1], query)
                    return self.html_page(HTTPStatus.OK, text) if text else self.not_found("No such take")
                return self.not_found()
            finally:
                conn.close()

        def serve_result(self, relative: str) -> None:
            path = safe_result_path(config.results_dir, relative)
            if path is None or path.suffix.lower() not in (".jpg", ".jpeg") or not path.is_file():
                return self.not_found()
            self.send(HTTPStatus.OK, path.read_bytes(), "image/jpeg")

        def do_POST(self):
            if not self.host_ok():
                return self.send(HTTPStatus.FORBIDDEN, b"Forbidden", "text/plain")
            parts = [p for p in urllib.parse.urlsplit(self.path).path.split("/") if p]
            if len(parts) != 2 or parts[0] != "action" or parts[1] not in actions_module.FIELDS:
                return self.not_found()
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_FORM_BYTES:
                return self.send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, b"Too large", "text/plain")
            form_data = {k: v[-1] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8")).items()}
            if not secrets.compare_digest(form_data.get("token", ""), config.token):
                return self.send(HTTPStatus.FORBIDDEN, b"Stale or missing form token: reload the page", "text/plain")
            action = parts[1]
            fields = {name: form_data.get(name, "") for name in actions_module.FIELDS[action]}
            take_id = fields.get("take_id") or self.take_of_issue(fields.get("issue_id", ""))
            if "prompt_version" in fields:
                try:
                    fields["prompt_version"] = int(fields["prompt_version"])
                except ValueError:
                    return self.back(take_id, refused="prompt_version is not a number")
            try:
                assert config.actions is not None
                config.actions(action, **fields)
            except actions_module.Refused as refusal:
                return self.back(take_id, refused=str(refusal))
            return self.back(take_id, done=f"{action.replace('_', ' ')}: done")

        def take_of_issue(self, issue_id: str) -> str:
            try:
                conn = open_db(config)
            except NoDatabase:
                return ""
            try:
                row = conn.execute("SELECT take_id FROM issues WHERE issue_id = ?", (issue_id,)).fetchone()
                return row["take_id"] if row else ""
            finally:
                conn.close()

        def back(self, take_id: str, **message) -> None:
            target = f"/take/{urllib.parse.quote(take_id)}" if take_id else "/"
            self.redirect(target + "?" + urllib.parse.urlencode(message))

    return Handler


def serve(config: Config) -> ThreadingHTTPServer:
    if config.host not in ("127.0.0.1", "localhost"):
        raise SystemExit("evidence: refusing to listen beyond the loopback address")
    return ThreadingHTTPServer((config.host, config.port), make_handler(config))


def main() -> int:
    config = Config.from_env()
    server = serve(config)
    print(f"evidence: http://{config.host}:{server.server_address[1]}/ reading {config.db_path} and "
          f"{config.results_dir}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
