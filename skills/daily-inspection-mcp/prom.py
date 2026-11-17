#!/usr/bin/env python3
"""Prometheus access through the cookie-authenticated prod Grafana datasource.

One retry on a transient failure, then a warning plus an empty result: a failed
query must never be rendered as a zero. Auth failures are NOT retried — they
propagate so the preflight can refuse to build a report at all.
"""

import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SHARED = os.path.join(HERE, "..", "_shared")
if SHARED not in sys.path:
    sys.path.insert(0, SHARED)

from grafana_prom import GrafanaAuthError, GrafanaError, Prom as GrafanaProm, total  # noqa: E402,F401

# Data-quality warnings for the current run. Collected here because every
# collector talks to this client; the report prints them and refuses to go green
# while any check failed.
WARN = []

_AGO = re.compile(r"^now(?:-(\d+)([smhd]))?$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def epoch(at):
    """'now' -> None (Prometheus default), 'now-24h' -> unix ts."""
    if at is None or at == "now":
        return None
    if isinstance(at, (int, float)):
        return float(at)
    m = _AGO.match(str(at).strip())
    if not m:
        raise ValueError("unsupported time expression: %r" % (at,))
    if m.group(1) is None:
        return None
    return time.time() - int(m.group(1)) * _UNIT[m.group(2)]


class Prom:
    """Call shape shared by every collector: q / num / by / range."""

    def __init__(self, host=None, cookie=None, timeout=45):
        self.client = GrafanaProm(host=host, cookie=cookie, timeout=timeout)
        self.host = self.client.host

    def _call(self, fn, *args):
        try:
            return fn(*args)
        except GrafanaAuthError:
            raise
        except GrafanaError as e:
            time.sleep(2)          # transient SSL EOF / reset: one retry
            try:
                return fn(*args)
            except GrafanaAuthError:
                raise
            except GrafanaError as e2:
                WARN.append("query failed: %s" % str(e2)[:100])
                return []

    def q(self, expr, at="now"):
        return self._call(self.client.query, expr, epoch(at))

    def range(self, expr, start, end, step=300):
        end_ts = epoch(end)
        return self._call(self.client.query_range, expr, epoch(start),
                          time.time() if end_ts is None else end_ts, step)

    def num(self, expr, at="now", default=0.0):
        try:
            return float(self.q(expr, at)[0]["value"][1])
        except (IndexError, ValueError, KeyError, TypeError):
            return default

    def by(self, expr, keys, at="now"):
        """{tuple(label values): float}"""
        out = {}
        for s in self.q(expr, at):
            metric = s.get("metric") or {}
            try:
                out[tuple(metric.get(k, "") for k in keys)] = float(s["value"][1])
            except (ValueError, KeyError, TypeError):
                pass
        return out

    def sum(self, expr, at="now"):
        return total(self.q(expr, at))