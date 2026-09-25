"""Format run log files for display in the dashboard."""
import html
import json
import re

import providers


def _split_meta_body(raw: str) -> tuple[list[str], str]:
    meta = []
    body_parts = []
    for line in raw.splitlines():
        if line.startswith("[tank]"):
            meta.append(line)
        else:
            body_parts.append(line)
    return meta, "\n".join(body_parts).strip()


def _repair_tokenized_stream(body: str) -> str:
    """Rejoin advisory streams where each token was logged on its own line."""
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if len(lines) < 4:
        return body

    short = sum(1 for ln in lines if len(ln) <= 12)
    if short / len(lines) < 0.6:
        return body

    parts: list[str] = []
    for line in lines:
        if not parts:
            parts.append(line)
            continue
        if line[0] in ".,!?;:)]}%'\"":
            parts.append(line)
        else:
            parts[-1] = parts[-1] + " " + line
    return "".join(parts)


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _inline_markdown(text: str) -> str:
    text = _esc(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return text


def _markdown_to_html(body: str) -> str:
    lines = body.splitlines()
    html_parts: list[str] = []
    in_list = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            html_parts.append("<br>")
            continue

        if stripped.startswith("### "):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            html_parts.append(f"<h4>{_inline_markdown(stripped[4:])}</h4>")
            continue
        if stripped.startswith("## "):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            html_parts.append(f"<h3>{_inline_markdown(stripped[3:])}</h3>")
            continue
        if stripped.startswith("# "):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            html_parts.append(f"<h2>{_inline_markdown(stripped[2:])}</h2>")
            continue
        if stripped.startswith("- "):
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            html_parts.append(f"<li>{_inline_markdown(stripped[2:])}</li>")
            continue

        if in_list:
            html_parts.append("</ul>")
            in_list = False
        html_parts.append(f"<p>{_inline_markdown(stripped)}</p>")

    if in_list:
        html_parts.append("</ul>")
    return "\n".join(html_parts)


def _format_meta(meta: list[str]) -> str:
    if not meta:
        return ""
    items = "".join(f"<li><code>{_esc(line)}</code></li>" for line in meta)
    return f'<ul class="run-meta">{items}</ul>'


def _format_advisory(meta: list[str], body: str) -> str:
    body = _repair_tokenized_stream(body)
    return _format_meta(meta) + f'<div class="run-prose">{_markdown_to_html(body)}</div>'


def _format_local_agent(meta: list[str], body: str) -> str:
    sections = []
    current_title = None
    current_lines: list[str] = []

    for line in body.splitlines():
        if line.startswith("=== ") and line.endswith(" ==="):
            if current_title and current_lines:
                sections.append((current_title, "\n".join(current_lines).strip()))
            current_title = line.strip("= ").strip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_title and current_lines:
        sections.append((current_title, "\n".join(current_lines).strip()))

    if sections:
        parts = [_format_meta(meta)]
        for title, content in sections:
            if title.lower() in ("summary", "plan", "report"):
                parts.append(
                    f'<div class="run-section">'
                    f"<h4>{_esc(title)}</h4>"
                    f'<div class="run-prose">{_markdown_to_html(content)}</div>'
                    f"</div>"
                )
            else:
                parts.append(
                    f'<div class="run-section">'
                    f"<h4>{_esc(title)}</h4>"
                    f'<pre class="run-code">{_esc(content)}</pre>'
                    f"</div>"
                )
        return "".join(parts)

    return _format_advisory(meta, body)


def _format_claude_stream_json(meta: list[str], body: str) -> str:
    text_parts = []
    other_lines = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            other_lines.append(line)
            continue
        if isinstance(event, dict):
            if event.get("type") == "text" and event.get("text"):
                text_parts.append(event["text"])
            elif event.get("type") == "assistant" and event.get("message"):
                text_parts.append(str(event["message"]))
            elif "content" in event and isinstance(event["content"], str):
                text_parts.append(event["content"])

    if text_parts:
        content = "".join(text_parts)
        return _format_meta(meta) + f'<div class="run-prose">{_markdown_to_html(content)}</div>'

    if other_lines:
        return _format_meta(meta) + f'<pre class="run-code">{_esc(chr(10).join(other_lines))}</pre>'
    return _format_meta(meta) + f'<pre class="run-code">{_esc(body)}</pre>'


def _logged_failure_note(raw: str) -> str:
    """Put the model-call (or other) failure above the file list in the log panel."""
    detail = ""
    for line in raw.splitlines():
        if "[tank] model call failed:" in line:
            detail = line.split("[tank] model call failed:", 1)[1].strip()
            break
    if not detail:
        for line in raw.splitlines():
            if line.startswith("[tank] error:"):
                detail = line.split("[tank] error:", 1)[1].strip()
                break
    if not detail:
        return ""
    return f'<p class="run-outcome-note">{_esc(detail)}</p>'


def _outcome_note(run, raw: str) -> str:
    """Clarify failed runs where the agent workflow completed but checks did not pass."""
    if run["status"] != "failed":
        return ""
    if re.search(r"\[tank\] test exit code: (?!0\b)\d+", raw):
        return (
            '<p class="run-outcome-note">'
            "The agent finished (patch applied and checks ran), but post-apply tests "
            "failed. Run status is <strong>failed</strong>."
            "</p>"
        )
    if "[tank] run finished with failures" in raw:
        return (
            '<p class="run-outcome-note">'
            "The agent workflow completed, but a follow-up step failed. "
            "Run status is <strong>failed</strong>."
            "</p>"
        )
    return ""


def format_run_output(run, raw: str) -> str:
    if not raw:
        return ""

    meta, body = _split_meta_body(raw)
    provider_id = run["provider"]
    ptype = providers.get_provider_type(provider_id) if provider_id else None
    note = _logged_failure_note(raw) + _outcome_note(run, raw)

    if ptype == "openai_compatible":
        return note + _format_advisory(meta, body)
    if ptype == "local_agent":
        return note + _format_local_agent(meta, body)
    if ptype == "claude_code":
        return note + _format_claude_stream_json(meta, body)
    return note + _format_meta(meta) + f'<pre class="run-code">{_esc(body)}</pre>'
