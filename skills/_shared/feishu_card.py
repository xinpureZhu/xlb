#!/usr/bin/env python3
"""Feishu interactive-card helpers for XLB ops notifications."""

import json
import subprocess


def md(content):
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def fields(pairs):
    return {"tag": "div", "fields": [
        {"is_short": True, "text": {"tag": "lark_md", "content": "**%s**\n%s" % (k, v)}}
        for k, v in pairs
    ]}


def hr():
    return {"tag": "hr"}


def note(content):
    return {"tag": "note", "elements": [{"tag": "lark_md", "content": content}]}


def card(title, template, elements):
    return {"config": {"wide_screen_mode": True},
            "header": {"template": template,
                       "title": {"tag": "plain_text", "content": title}},
            "elements": elements}


def send(payload, user_id, key):
    r = subprocess.run(["lark-cli", "im", "+messages-send", "--user-id", user_id,
                        "--msg-type", "interactive", "--content",
                        json.dumps(payload, ensure_ascii=False),
                        "--idempotency-key", key, "--as", "bot"],
                       capture_output=True, text=True, timeout=120)
    try:
        res = json.loads(r.stdout)
    except Exception:
        raise RuntimeError("send failed: %s" % ((r.stdout or r.stderr)[:300]))
    if not res.get("ok"):
        raise RuntimeError("send failed: %s" % json.dumps(res, ensure_ascii=False)[:300])
    return res.get("data") or {}


def from_markdown(text):
    """Turn report Markdown into card elements; tables are not rendered by lark_md."""
    elements = []
    buf = []

    def flush():
        if buf:
            elements.append(md("\n".join(buf).strip()))
            buf.clear()

    for line in text.splitlines():
        if line.startswith("# "):
            continue
        if line.startswith("## ") or line.startswith("### "):
            flush()
            if elements:
                elements.append(hr())
            buf.append("**%s**" % line.lstrip("#").strip())
        elif line.strip().startswith("|") or line.strip().startswith(">"):
            continue
        else:
            buf.append(line)
    flush()
    return elements