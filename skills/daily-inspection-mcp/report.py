#!/usr/bin/env python3
"""Issue assembly and rendering for the merged goods inspection card.

Layout (top to bottom): what needs action today → health snapshot → capacity
trends → data quality and blind spots. Healthy items stay silent; a check that
did not run is never silent (it downgrades the header and shows in 数据质量).
"""

import collections
import datetime
import json
import os
import re
import sys

from collect import CLOUD_SKIPPED, CLOUD_UNREACHABLE, CRED_BROKEN, WARN, _pc, cst, now_cst, arrow, pct
from prom import SHARED

if SHARED not in sys.path:
    sys.path.insert(0, SHARED)
import feishu_card as fc  # noqa: E402

STATE_DIR = os.path.expanduser("~/.xlb/state")


# ----------------------------------------------------------------- build
_ANSI = re.compile(r"\x1b\[[0-9;]*m|\[\d+;\d+m")


def _clean(text):
    return _ANSI.sub("", str(text)).strip()


def build(ns, k, mongo, redis, mq, kafka, events, backlog):
    """The actionable list *is* the report; everything else is one-line context."""
    events_ok = events is not None
    events = events or []
    cron, no_ip = k.get("cron") or [], k.get("no_ip") or []
    issues = []
    for name, sev, _n, tgt in [v for v in k["new"] if v[1] in ("critical", "error")]:
        issues.append(("告警", "%s `%s`" % (name, tgt)))

    # Aggregate by node: a CNI failure wedges every new pod on that node at once,
    # and 13 near-identical rows would bury the one fact that matters (which node).
    by_node = collections.OrderedDict()
    for x in no_ip or []:
        by_node.setdefault(x["node"], []).append(x)
    for node, pods in by_node.items():
        worst = max(p["mins"] for p in pods)
        issues.append(("网络", "%d 个 Pod 在节点 `%s` 上已 %d 分钟拿不到 IP — "
                       "CNI 未分配地址，新 Pod 全部无法启动（节点仍报 Ready）"
                       % (len(pods), node, worst)))

    for j in cron or []:
        if not j["alive"]:
            if j["resident"]:
                hint = "常驻任务停了控制器不会自愈，需人工删 Pod 触发重建"
            elif "卡在" in j["detail"]:
                # Pod was created but never started: a node/network problem,
                # not a scheduler one. Saying "check the scheduler" sends the
                # reader to the wrong place.
                hint = "Pod 已创建但未启动，查所在节点的 CNI/IP 分配（见「网络」项）"
            else:
                hint = "检查调度器与 suspend 状态"
            issues.append(("CronJob", "`%s` %s — %s" % (j["name"], j["detail"], hint)))

    for e in events:
        if not e["bad"]:
            continue
        where = "%d 个 %s" % (e["objs"], e["kind"] or "对象")
        if e["objs"] == 1 and e.get("sample"):
            where = "`%s`" % e["sample"]
        issues.append(("事件", "%s ×%d（%s）— %s" % (e["reason"], e["cnt"], where, e["why"])))

    # NotReady is worth naming but not a red-level event on its own: a rolling
    # update legitimately leaves a pod unready for a while. Crash/OOM, schedule
    # failures and CNI faults stay red.
    for pod in k.get("notready") or []:
        issues.append(("稳定性", "`%s` NotReady — 查 events 与 readinessProbe" % pod))

    if k["stab"]["oom"] > 0:
        issues.append(("OOM", "%.0f 次 / 24h" % k["stab"]["oom"]))
    if k["stab"]["restart"] > 100:
        issues.append(("重启", "%.0f 次 / 24h" % k["stab"]["restart"]))

    for sp in k.get("spikes") or []:
        issues.append(("突刺", "`%s` %s 峰值 **%.2f%s**（中位 %.2f）@ %s"
                       % (sp["svc"], sp["kind"], sp["peak"],
                          sp["unit"], sp["median"], cst(sp["at"]))))

    for name, qps, flags in k["svc"]:
        signal = [f for f in flags if f.startswith(("5xx", "P99"))]
        if signal or qps >= 50:
            issues.append(("服务", "`%s` %.0f qps — %s" % (name, qps, "；".join(flags))))

    for m in mongo:
        issues += [("MongoDB", n) for n in m["note"]]
    for r in redis:
        issues += [("Redis", n) for n in r["note"]]

    for x in backlog:
        if x["median"] > 50:      # sustained: consumers cannot keep up
            issues.append(("积压", "`%s` 持续积压，中位 %.0f / 当前 %.0f（topic `%s`）"
                           % (x["group"], x["median"], x["last"], x["topic"])))
    for m in mq:
        if m["hot"]:
            issues.append(("积压", "`%s` 待消费 %s（RocketMQ，归属未校验）"
                           % (m["group"], m["ready"])))

    hard = any(c in ("告警", "OOM", "CronJob", "网络") for c, _ in issues) or \
        any(e["bad"] for e in events)
    level = "red" if hard else ("orange" if issues else "green")
    return {"ns": ns, "k": k, "issues": issues, "level": level, "events": events,
            "events_ok": events_ok, "backlog": backlog, "mongo": mongo,
            "redis": redis, "kafka_n": len(kafka), "cron": cron, "no_ip": no_ip}


# ----------------------------------------------------------------- recurrence
def _issue_key(cat, text):
    """Stable identity for an issue across days: category + first backticked
    subject. Peak values drift slightly, so the full text is not usable."""
    m = re.search(r"`([^`]+)`", text)
    return "%s|%s" % (cat, m.group(1) if m else text[:24])


def mark_recurrence(d, persist=False, unverified=(), state_dir=None):
    """The same spike/backlog appearing every day for weeks reads as noise, but
    it is a standing defect. Tag streaks so recurrence escalates instead of
    fading into the background.

    `persist` must only be true for the authoritative daily run. A preview sees
    a different data window (e.g. a backlog that has since drained), so letting
    previews write would clobber the production streaks.

    `unverified` lists issue categories whose check did not run at all (e.g.
    积压 when the cloud credentials are broken). Absence there means "unknown",
    not "resolved", so those keys are carried over instead of dropped — the same
    rule as never rendering a failed query as a recovery.
    """
    today = now_cst().date()
    state_dir = state_dir or STATE_DIR
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, "%s.recurrence.json" % d["ns"])
    try:
        with open(path, encoding="utf-8") as fh:
            prev = json.load(fh)
    except Exception:
        prev = {}
    prev_keys, prev_date = prev.get("keys") or {}, prev.get("date")
    yesterday = (today - datetime.timedelta(days=1)).isoformat()
    new_keys = {}
    for i, (cat, text) in enumerate(d["issues"]):
        k = _issue_key(cat, text)
        if prev_date == yesterday:
            streak = prev_keys.get(k, 0) + 1
        elif prev_date == today.isoformat():
            # Same-day re-run: keep the recorded streak rather than inflating it.
            streak = max(prev_keys.get(k, 1), 1)
        else:
            streak = 1
        new_keys[k] = streak
        if streak >= 2:
            d["issues"][i] = (cat, "%s（已连续 %d 天）" % (text, streak))
    if not persist:
        return d
    # Carry over categories that were never checked, so a broken credential or a
    # skipped cloud section does not read as "the backlog cleared overnight".
    carried = {k: v for k, v in prev_keys.items()
               if k.split("|", 1)[0] in set(unverified) and k not in new_keys}
    if prev_date == today.isoformat():
        # Same-day merge: never erase what an earlier authoritative run recorded.
        merged = dict(prev_keys)
        merged.update(new_keys)
    else:
        merged = dict(carried)
        merged.update(new_keys)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"date": today.isoformat(), "keys": merged},
                      fh, ensure_ascii=False)
    except Exception:
        pass
    return d


# ----------------------------------------------------------------- fields
def _peak(k):
    pk = k.get("peak") or {}
    t, pv = pk.get("today"), pk.get("prev")
    if not t:
        return None
    line = "**%.0f** qps @ %s" % (t["peak"], cst(t["peak_at"]))
    sub = "谷 %.0f ｜ 峰谷比 %s" % (t["trough"], ("%.0f×" % t["ratio"]) if t["ratio"] else "n/a")
    if pv:
        dd = pct(t["peak"], pv["peak"])
        if dd != float("inf"):
            sub += " ｜ 环比 %s" % arrow(dd)
    return line, sub


def _no_cloud():
    """Label for cloud sections when the aliyun CLI could not be queried."""
    if CRED_BROKEN:
        return "⚠️ 凭据失效未检查"
    if CLOUD_UNREACHABLE:
        return "⚠️ 云端未检查"
    if CLOUD_SKIPPED:
        return "⚠️ 云检查已跳过"
    return "无数据"


def _backlog_field(d):
    bl = d.get("backlog") or []
    if not bl:
        return _no_cloud()
    top = bl[0]
    cur = max(bl, key=lambda x: x["last"])
    lines = ["24h 峰值 **%.0f** @ `%s` %s" % (top["peak"], top["group"], cst(top["peak_at"]))]
    if cur["last"] > 0:
        lines.append("当前最高 **%.0f** @ `%s` / topic `%s`"
                     % (cur["last"], cur["group"], cur["topic"]))
    return "\n".join(lines)


def _mw_field(d):
    parts = []
    for m in d.get("mongo") or []:
        parts.append("Mongo " + "　".join(
            "分片%d CPU %s 内存 %s" % (i, _pc(x["cpu"]), _pc(x["mem"]))
            for i, x in enumerate(m["shards"], 1)))
    for r in d.get("redis") or []:
        parts.append("Redis CPU %s 内存 %s" % (_pc(r["cpu"], 2), _pc(r["mem"])))
    return "\n".join(parts) or _no_cloud()


def _p99_field(k):
    rows = (k.get("red") or {}).get("p99") or []
    if not rows:
        return "无数据"
    head = "`%s` **%.0f ms**" % rows[0]
    rest = "　".join("`%s` %.0f ms" % (s, v) for s, v in rows[1:])
    return head + (("　" + rest) if rest else "")


def _rule_field(k):
    """Rule-level breakdown: the headline count alone hides which rule is
    chronic (37 × HpaMaxedOut means the HPA ceiling is the real finding)."""
    rules = k.get("rules") or {}
    if not rules:
        return "无 firing"
    items = sorted(rules.items(), key=lambda x: -x[1])[:4]
    return "　".join("`%s` / %s ×%d" % (name, sev, n) for (name, sev), n in items)


def _red_field(k):
    red = k.get("red") or {}
    if not red.get("sampled"):
        return "UNKNOWN（1h 仅 %s 请求，样本不足）" % ("%.0f" % (red.get("all") or 0))
    return "**%.3f%%**（%.0f / %.0f）" % (red["ratio"], red["bad"], red["all"])


def _notready_field(k):
    pods = k.get("notready") or []
    return "无" if not pods else "、".join("`%s`" % p for p in pods[:6])


def _fields(d):
    """(label, value, is_short). Multi-line values take a full row: at half
    width they wrap mid-value and the two columns drift out of alignment."""
    k = d["k"]
    pk = _peak(k)
    return [("告警", "firing **%d**　24h 前 %d　新增 %d / 恢复 %d"
             % (k["alerts"], k["alerts_prev"], len(k["new"]), len(k["gone"])), True),
            ("5xx（1h）", _red_field(k), True),
            ("重启 / OOM（24h）", "**%.0f** / **%.0f** 次"
             % (k["stab"]["restart"], k["stab"]["oom"]), True),
            ("NotReady", _notready_field(k), True),
            ("P99 Top3（1h）", _p99_field(k), False),
            ("告警规则", _rule_field(k), False),
            ("峰值 QPS（24h）", "%s\n%s" % pk if pk else "无 Istio 流量", False),
            ("中间件峰值（24h）", _mw_field(d), False),
            ("MQ 积压", _backlog_field(d), False)]


def _issue_rows(issues):
    """Group by category so a repeated label becomes one heading instead of
    being restated on every line. Single-item categories stay inline."""
    groups = collections.OrderedDict()
    for cat, text in issues:
        groups.setdefault(cat, []).append(text)
    rows = []
    for cat, texts in groups.items():
        if len(texts) == 1:
            rows.append("**%s**　%s" % (cat, texts[0]))
        else:
            rows.append("**%s**" % cat)
            rows += ["　• %s" % t for t in texts]
    return rows


def _warnings():
    return list(dict.fromkeys(_clean(w) for w in WARN if w))


def _note(d):
    gaps = ["VictoriaLogs 错误模式", "中间件服务端指标（慢查询/连接数）", "证书到期"]
    if not d.get("events_ok"):
        gaps.insert(0, "K8s 事件原因（SLS 不可达）")
    return ("对比最近 24h vs 前 24h ｜ 积压取云监控 24h 峰值 ｜ 空向量=UNKNOWN，不当正常 ｜ "
            "未覆盖：%s ｜ series=%d" % ("、".join(gaps), d["k"].get("series") or 0))


def effective_level(d):
    """Missing data is not a healthy signal: never report green while any check
    failed to run, otherwise a query failure reads as an all-clear
    (observed 2026-08-16/17: green card, spike detection had no data)."""
    if d["level"] == "green" and _warnings():
        return "orange"
    return d["level"]


# ----------------------------------------------------------------- render
def render_text(d):
    mark = {"red": "[红]", "orange": "[橙]", "green": "[绿]"}[effective_level(d)]
    L = ["%s %s 巡检 · %s CST" % (mark, d["ns"], now_cst().strftime("%m-%d %H:%M"))]
    if d["issues"]:
        L.append("待处理 %d 项：" % len(d["issues"]))
        L += ["  - [%s] %s" % (c, t.replace("**", "").replace("`", "")) for c, t in d["issues"]]
    elif _warnings():
        L.append("未发现问题，但部分检查未完成 ⚠️（详见数据质量说明）")
    else:
        L.append("无需处置")
    L.append("")
    L += ["  %s: %s" % (a, b.replace("**", "").replace("\n", " / "))
          for a, b, _ in _fields(d)]
    warns = _warnings()
    if warns:
        L.append("")
        L.append("数据质量 %d 项：" % len(warns))
        L += ["  - %s" % w for w in warns]
    L.append("")
    L.append("  " + _note(d))
    return "\n".join(L)


def render_card(d):
    E = []
    warns = _warnings()
    if d["issues"]:
        shown = d["issues"][:8]
        rows = "\n".join(_issue_rows(shown))
        more = "\n…另 %d 项" % (len(d["issues"]) - 8) if len(d["issues"]) > 8 else ""
        E.append(fc.md("**待处理 %d 项**\n%s%s" % (len(d["issues"]), rows, more)))
    elif warns:
        # No issues found, but some checks did not run. Claiming ✅ here is how a
        # query failure becomes a false green (observed 2026-08-16/17).
        E.append(fc.md("**未发现问题，但部分检查未完成** ⚠️\n详见下方数据质量说明"))
    else:
        E.append(fc.md("**无需处置** ✅"))
    E.append(fc.hr())
    E.append({"tag": "div", "fields": [
        {"is_short": short, "text": {"tag": "lark_md", "content": "**%s**\n%s" % (a, b)}}
        for a, b, short in _fields(d)]})
    if warns:
        E.append(fc.hr())
        E.append(fc.md("**⚠️ 数据质量（%d 项）**\n%s"
                       % (len(warns), "\n".join("• %s" % w for w in warns[:6]))))
    E.append(fc.note(_note(d)))
    return fc.card("%s 巡检 · %s" % (d["ns"], now_cst().strftime("%m-%d %H:%M")),
                   effective_level(d), E)