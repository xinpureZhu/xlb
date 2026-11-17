#!/usr/bin/env python3
"""Read-only Prometheus client for the prod Grafana datasource.

Grafana is behind Feishu SSO. The authenticated browser session cookie is
exported once to ~/.xlb/credentials/grafana-cookie.json (600). If it expires,
the preflight fails with GrafanaAuthError; re-export from the relay browser.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_HOST = "https://grafana.observability.ali-prod.xlbsoft.com"
COOKIE_FILE = os.path.expanduser("~/.xlb/credentials/grafana-cookie.json")


class GrafanaError(RuntimeError):
    pass


class GrafanaAuthError(GrafanaError):
    pass


class Prom:
    def __init__(self, host=None, cookie=None, timeout=45):
        self.host = (host or os.environ.get("GRAFANA_HOST") or DEFAULT_HOST).rstrip("/")
        self.cookie = cookie or self._load_cookie()
        self.timeout = timeout

    @staticmethod
    def _load_cookie():
        if os.environ.get("GRAFANA_COOKIE"):
            return os.environ["GRAFANA_COOKIE"]
        if not os.path.exists(COOKIE_FILE):
            raise GrafanaAuthError("missing %s; export the Grafana session cookie" % COOKIE_FILE)
        with open(COOKIE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        cookie = (data.get("cookie") or "").strip()
        if not cookie:
            raise GrafanaAuthError("empty cookie in %s" % COOKIE_FILE)
        return cookie

    def _get(self, path, params):
        url = "%s%s?%s" % (self.host, path, urllib.parse.urlencode(params))
        req = urllib.request.Request(url, headers={"Cookie": self.cookie, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 401, 403):
                raise GrafanaAuthError("Grafana session expired (HTTP %s); re-export the cookie" % e.code)
            raise GrafanaError("Grafana HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:200]))
        except urllib.error.URLError as e:
            raise GrafanaError("Grafana unreachable: %s" % e.reason)
        except OSError as e:      # SSL EOF / reset surfaces as a bare OSError
            raise GrafanaError("Grafana unreachable: %s" % e)
        try:
            data = json.loads(body)
        except Exception:
            raise GrafanaAuthError("Grafana returned non-JSON (likely SSO redirect)")
        if data.get("status") != "success":
            raise GrafanaError("Grafana query failed: %s" % str(data)[:200])
        return data.get("data", {}).get("result", [])

    def query(self, expr, when=None):
        params = {"query": expr}
        if when is not None:
            params["time"] = str(int(when))
        return self._get("/api/datasources/proxy/uid/prometheus/api/v1/query", params)

    def query_range(self, expr, start, end, step=60):
        return self._get("/api/datasources/proxy/uid/prometheus/api/v1/query_range", {
            "query": expr, "start": str(int(start)), "end": str(int(end)), "step": str(int(step)),
        })


def values(result):
    """[(labels, [floats])] from an instant or range result."""
    out = []
    for s in result or []:
        pairs = s.get("values") or ([s["value"]] if s.get("value") else [])
        vals = []
        for pair in pairs:
            try:
                vals.append(float(pair[1]))
            except Exception:
                continue
        out.append((s.get("metric", {}), vals))
    return out


def total(result):
    return sum(sum(vals) for _, vals in values(result))


def now():
    return int(time.time())