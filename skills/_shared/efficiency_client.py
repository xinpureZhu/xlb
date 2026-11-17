#!/usr/bin/env python3
"""Minimal read-only MCP client for the Efficiency platform.

Credentials are resolved in this order:
  1. EFFICIENCY_MCP_URL + EFFICIENCY_MCP_PAT
  2. ~/Library/Application Support/efficiency-mcp/credentials.json (+ URL from ~/.omp/agent/mcp.json)
  3. The Authorization expression stored in ~/.omp/agent/mcp.json

No secret is ever printed. Only read-only tools should be called by callers.
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

CRED_FILE = os.path.expanduser("~/Library/Application Support/efficiency-mcp/credentials.json")
OMP_MCP = os.path.expanduser("~/.omp/agent/mcp.json")
PROTOCOL = "2025-03-26"


class McpError(RuntimeError):
    pass


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_endpoint():
    """Return (url, token). Raises McpError with an actionable message."""
    url = os.environ.get("EFFICIENCY_MCP_URL", "").strip()
    token = os.environ.get("EFFICIENCY_MCP_PAT", "").strip()

    cfg = {}
    if os.path.exists(OMP_MCP):
        try:
            cfg = _read_json(OMP_MCP).get("mcpServers", {}).get("efficiency", {})
        except Exception:
            cfg = {}

    if not url:
        url = (cfg.get("url") or "").strip()
    if not token:
        if os.path.exists(CRED_FILE):
            try:
                cred = _read_json(CRED_FILE)
                token = (cred.get("token") or "").strip()
                if not url:
                    url = (cred.get("server_url") or "").strip()
            except Exception:
                token = ""
    if not token and cfg.get("headers", {}).get("Authorization"):
        expr = cfg["headers"]["Authorization"].strip()
        if expr.startswith("!"):
            try:
                out = subprocess.run(expr[1:], shell=True, capture_output=True,
                                     text=True, timeout=20).stdout.strip()
                token = re.sub(r"^Bearer\s+", "", out, flags=re.I)
            except Exception as e:
                raise McpError("cannot evaluate Authorization expression: %s" % e)
    token = re.sub(r"^Bearer\s+", "", token, flags=re.I)
    if not url or not token:
        raise McpError("no Efficiency MCP credential: set EFFICIENCY_MCP_URL and "
                       "EFFICIENCY_MCP_PAT, or log in with the efficiency bridge")
    return url, token


def _parse_body(raw, content_type, req_id):
    text = raw.decode("utf-8", "replace")
    if "text/event-stream" in (content_type or ""):
        payloads = []
        for line in text.splitlines():
            if line.startswith("data:"):
                payloads.append(line[5:].strip())
        for payload in reversed(payloads):
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if obj.get("id") == req_id:
                return obj
        raise McpError("no JSON-RPC response in SSE stream")
    try:
        return json.loads(text)
    except Exception:
        raise McpError("non-JSON response: %s" % text[:300])


class Client:
    def __init__(self, url=None, token=None, timeout=60):
        self.url, self.token = (url, token) if url and token else resolve_endpoint()
        self.timeout = timeout
        self.session_id = None
        self._req_id = 0
        self._initialize()

    def _post(self, payload, extra_headers=None):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer %s" % self.token,
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        headers.update(extra_headers or {})
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                sid = resp.headers.get("mcp-session-id")
                if sid:
                    self.session_id = sid
                return resp.status, raw, ctype
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            raise McpError("HTTP %s from MCP: %s" % (e.code, body))
        except urllib.error.URLError as e:
            raise McpError("MCP unreachable: %s" % e.reason)

    def _rpc(self, method, params):
        self._req_id += 1
        rid = self._req_id
        status, raw, ctype = self._post({
            "jsonrpc": "2.0", "id": rid, "method": method, "params": params,
        })
        obj = _parse_body(raw, ctype, rid)
        if "error" in obj:
            raise McpError("%s failed: %s" % (method, obj["error"]))
        return obj.get("result", {})

    def _initialize(self):
        self._rpc("initialize", {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "xlb-ops", "version": "1.0"},
        })
        # Initialized notification: failures are non-fatal on this server.
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            pass

    def call(self, name, arguments=None):
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        parts = []
        for item in result.get("content", []):
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
        if result.get("isError"):
            raise McpError("%s returned an error: %s" % (name, "\n".join(parts)[:500]))
        return "\n".join(parts)

    def call_json(self, name, arguments=None):
        text = self.call(name, arguments)
        try:
            return json.loads(text)
        except Exception:
            return text


def _main(argv):
    if len(argv) < 2:
        print("usage: efficiency_client.py <tool> [json-args]", file=sys.stderr)
        return 2
    args = json.loads(argv[2]) if len(argv) > 2 else {}
    client = Client()
    out = client.call(argv[1], args)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))