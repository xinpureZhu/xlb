#!/usr/bin/env python3
"""Issue assembly and rendering for the merged goods inspection card.

Layout mirrors the cost-report card the team already reads: 4 KPI tiles with
coloured values, one highlighted derived line, then a table-like list grouped by
category with right-aligned numbers, and a grey footer carrying the 口径/盲区.

Card JSON 2.0 is used by default (needs Feishu >= 7.20). `XLB_CARD_SCHEMA=1.0`
renders the previous layout; `send_card` also falls back automatically if the
server rejects the 2.0 payload.
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
CARD_SCHEMA = os.environ.get("XLB_CARD_SCHEMA", "2.0")

# Feishu rejects raw RGBA in text_color (ErrCode 10002), so the readable-but-
# quiet tone is the `grey` enum at full body size, not 12px.
MUTED = "grey"

_ANSI = re.compile(r"\x1b\[[0-9;]*m|\[\d+;\d+m")
_URL = re.compile(r"https?://([^/\s?\"'<>]+)\S*")

# Category -> colour on the card. Red = paged someone / data loss risk,
# orange = needs a look today, grey = context only.
# One accent colour on purpose: red = needs action today, dark = neutral fact.
# Mixing orange/green/red on every row made the card read as noise.
CAT_COLOR = {
    "告警": "red", "网络": "red", "CronJob": "red", "OOM": "red", "重启": "red",
    "事件": "red", "稳定性": "red", "突刺": "red", "服务": "red",
    "MongoDB": "red", "Redis": "red", "积压": "red",
}


def _clean(text):
    """Strip ANSI and collapse signed URLs to their host: a 200-char signed URL
    used to eat the whole 数据质量 block."""
    out = _URL.sub(lambda m: m.group(1), _ANSI.sub("", str(text)))
    return out.strip()


def item(cat, text, value="", meta="", level=None):
    """One actionable row: subject phrase, right-aligned number, grey context."""
    return {"cat": cat, "text": text, "value": value, "meta": meta,
            "level": level or CAT_COLOR.get(cat, "default")}


def _line(it):
    out = it["text"]
    if it["value"]:
        out += " — " + it["value"]
    if it["meta"]:
        out += "（%s）" % it["meta"]
    return out


# ----------------------------------------------------------------- build
def build(ns, k, mongo, redis, mq, kafka, events, backlog):
    """The actionable list *is* the report; everything else is one-line context."""
    events_ok = events is not None
    events = events or []
    cron, no_ip = k.get("cron") or [], k.get("no_ip") or []
    issues = []
    for name, sev, _n, tgt in [v for v in k["new"] if v[1] in ("critical", "error")]:
        issues.append(item("告警", "`%s` 新告警" % name,
                           meta="%s · 目标 `%s`" % (sev, tgt)))

    # Aggregate by node: a CNI failure wedges every new pod on that node at once,
    # and 13 near-identical rows would bury the one fact that matters (which node).
    by_node = collections.OrderedDict()
    for x in no_ip:
        by_node.setdefault(x["node"], []).append(x)
    for node, pods in by_node.items():
        worst = max(p["mins"] for p in pods)
        issues.append(item("网络", "Pod 拿不到 IP（新 Pod 全部无法启动，节点仍报 Ready）",
                           value="%d 个" % len(pods),
                           meta="节点 `%s` · 已 %d 分钟 · CNI 未分配地址" % (node, worst)))

    for j in cron:
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
            issues.append(item("CronJob", "`%s` 未在运行" % j["name"],
                               value="停止" if "24h" in j["detail"] else "卡住",
                               meta="%s — %s" % (j["detail"], hint)))

    for e in events:
        if not e["bad"]:
            continue
        where = "%d 个 %s" % (e["objs"], e["kind"] or "对象")
        if e["objs"] == 1 and e.get("sample"):
            where = "`%s`" % e["sample"]
        issues.append(item("事件", "%s（%s）" % (e["reason"], where),
                           value="×%d" % e["cnt"], meta=e["why"]))

    # NotReady is worth naming but not a red-level event on its own: a rolling
    # update legitimately leaves a pod unready for a while. Crash/OOM, schedule
    # failures and CNI faults stay red.
    for pod in k.get("notready") or []:
        issues.append(item("稳定性", "`%s` NotReady" % pod,
                           meta="查 readinessProbe 与 events"))

    if k["stab"]["oom"] > 0:
        issues.append(item("OOM", "容器 OOM 事件", value="%.0f 次" % k["stab"]["oom"],
                           meta="24h"))
    if k["stab"]["restart"] > 100:
        issues.append(item("重启", "容器重启", value="%.0f 次" % k["stab"]["restart"],
                           meta="24h"))

    for sp in k.get("spikes") or []:
        bits = ["中位 %s" % _dur(sp["median"])]
        if sp.get("ratio"):
            bits.append("×%.0f" % sp["ratio"])
        bits.append(cst(sp["at"]))
        share = sp.get("share") or 0.0
        if share:
            # 1 breach in 288 buckets is 0.35%: printing "0%" next to a spike
            # contradicts itself.
            bits.append("%s 点超阈" % ("<1%" if share * 100 < 1 else "%.0f%%" % (share * 100)))
        it = item("突刺", "`%s` %s 突刺" % (sp["svc"], sp["kind"]),
                  value=_dur(sp["peak"]), meta=" · ".join(bits))
        it["share"] = share
        issues.append(it)

    for name, qps, flags in k["svc"]:
        signal = [f for f in flags if f.startswith(("5xx", "P99"))]
        if signal or qps >= 50:
            issues.append(item("服务", "`%s`" % name, value="%.0f qps" % qps,
                               meta="；".join(flags)))

    for m in mongo:
        issues += [item("MongoDB", n, meta="24h 峰值") for n in m["note"]]
    for r in redis:
        issues += [item("Redis", n, meta="24h 峰值") for n in r["note"]]

    for x in backlog:
        if x["median"] > 50:      # sustained: consumers cannot keep up
            issues.append(item("积压", "`%s` 持续积压" % x["group"],
                               value="中位 %.0f" % x["median"],
                               meta="当前 %.0f · topic `%s`" % (x["last"], x["topic"])))
    for m in mq:
        if m["hot"]:
            issues.append(item("积压", "`%s` 待消费" % m["group"],
                               value="%s" % m["ready"], meta="RocketMQ · 归属未校验"))

    hard = any(c in ("告警", "OOM", "CronJob", "网络") for c in CAT_COLOR if
               any(i["cat"] == c for i in issues)) or any(e["bad"] for e in events)
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
    for it in d["issues"]:
        k = _issue_key(it["cat"], it["text"])
        if prev_date == yesterday:
            streak = prev_keys.get(k, 0) + 1
        elif prev_date == today.isoformat():
            # Same-day re-run: keep the recorded streak rather than inflating it.
            streak = max(prev_keys.get(k, 1), 1)
        else:
            streak = 1
        new_keys[k] = streak
        if streak >= 2:
            it["text"] = "%s（已连续 %d 天）" % (it["text"], streak)
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
    sub = "谷 %.0f · 峰谷比 %s" % (t["trough"], ("%.0f×" % t["ratio"]) if t["ratio"] else "n/a")
    if pv:
        dd = pct(t["peak"], pv["peak"])
        if dd != float("inf"):
            sub += " · 环比 %s" % arrow(dd)
    return "**%.0f** qps @ %s" % (t["peak"], cst(t["peak_at"])), sub


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
    out = "24h 峰值 **%.0f** @ `%s` %s" % (top["peak"], top["group"], cst(top["peak_at"]))
    if cur["last"] > 0:
        out += "\n当前最高 **%.0f** @ `%s` / topic `%s`" % (cur["last"], cur["group"], cur["topic"])
    return out


def _mw_field(d):
    parts = []
    for m in d.get("mongo") or []:
        parts.append("Mongo " + "　".join(
            "分片%d CPU %s 内存 %s" % (i, _pc(x["cpu"]), _pc(x["mem"]))
            for i, x in enumerate(m["shards"], 1)))
    for r in d.get("redis") or []:
        parts.append("Redis CPU %s 内存 %s" % (_pc(r["cpu"], 2), _pc(r["mem"])))
    return "\n".join(parts) or _no_cloud()


def _p99_rows(k):
    return (k.get("red") or {}).get("p99") or []


def _red_field(k):
    red = k.get("red") or {}
    if not red.get("sampled"):
        return "UNKNOWN（1h 仅 %s 请求，样本不足）" % ("%.0f" % (red.get("all") or 0))
    return "**%.3f%%**（%.0f / %.0f）" % (red["ratio"], red["bad"], red["all"])


def _rule_field(k):
    """Rule-level breakdown: the headline count alone hides which rule is
    chronic (37 × HpaMaxedOut means the HPA ceiling is the real finding)."""
    rules = k.get("rules") or {}
    if not rules:
        return "无 firing"
    items = sorted(rules.items(), key=lambda x: -x[1])[:4]
    return "　".join("`%s` / %s ×%d" % (name, sev, n) for (name, sev), n in items)


def _notready_field(k):
    pods = k.get("notready") or []
    return "无" if not pods else "、".join("`%s`" % p for p in pods[:6])


def _fields_v1(d):
    """(label, value, is_short) for the 1.0 layout."""
    k = d["k"]
    pk = _peak(k)
    return [("告警", "firing **%d**　24h 前 %d　新增 %d / 恢复 %d"
             % (k["alerts"], k["alerts_prev"], len(k["new"]), len(k["gone"])), True),
            ("5xx（1h）", _red_field(k), True),
            ("重启 / OOM（24h）", "**%.0f** / **%.0f** 次"
             % (k["stab"]["restart"], k["stab"]["oom"]), True),
            ("NotReady", _notready_field(k), True),
            ("P99 Top3（1h）", "　".join("`%s` %.0f ms" % (s, v) for s, v in _p99_rows(k))
             or "无数据", False),
            ("告警规则", _rule_field(k), False),
            ("峰值 QPS（24h）", "\n".join(pk) if pk else "无 Istio 流量", False),
            ("中间件峰值（24h）", _mw_field(d), False),
            ("MQ 积压", _backlog_field(d), False)]


def _warnings():
    return list(dict.fromkeys(_clean(w) for w in WARN if w))


def _note(d):
    gaps = ["VictoriaLogs 错误模式", "中间件服务端指标（慢查询/连接数）", "证书到期"]
    if not d.get("events_ok"):
        gaps.insert(0, "K8s 事件原因（SLS 不可达）")
    return ("对比最近 24h vs 前 24h ｜ 积压取云监控 24h 峰值 ｜ 空向量=UNKNOWN，不当正常 ｜ "
            "未覆盖：%s ｜ series=%d" % ("、".join(gaps), d["k"].get("series") or 0))


def _note_short(d):
    gaps = ["VictoriaLogs", "中间件服务端指标", "证书到期"]
    if not d.get("events_ok"):
        gaps.insert(0, "K8s 事件原因")
    return ("空向量=UNKNOWN ｜ 未覆盖 %s ｜ series=%d"
            % ("、".join(gaps), d["k"].get("series") or 0))


def effective_level(d):
    """Missing data is not a healthy signal: never report green while any check
    failed to run, otherwise a query failure reads as an all-clear
    (observed 2026-08-16/17: green card, spike detection had no data)."""
    if d["level"] == "green" and _warnings():
        return "orange"
    return d["level"]


# ----------------------------------------------------------------- render text
def render_text(d):
    mark = {"red": "[红]", "orange": "[橙]", "green": "[绿]"}[effective_level(d)]
    L = ["%s %s 巡检 · %s CST" % (mark, d["ns"], now_cst().strftime("%m-%d %H:%M"))]
    if d["issues"]:
        L.append("待处理 %d 项：" % len(d["issues"]))
        L += ["  - [%s] %s" % (it["cat"], _line(it).replace("**", "").replace("`", ""))
              for it in d["issues"]]
    elif _warnings():
        L.append("未发现问题，但部分检查未完成 ⚠️（详见数据质量说明）")
    else:
        L.append("无需处置")
    L.append("")
    k = d["k"]
    pk = _peak(k)
    L.append("  告警: firing %d（24h 前 %d，新增 %d / 恢复 %d）｜ %s"
             % (k["alerts"], k["alerts_prev"], len(k["new"]), len(k["gone"]), _rule_field(k)))
    L.append("  5xx(1h): %s ｜ 重启/OOM: %.0f/%.0f ｜ NotReady: %s"
             % (_red_field(k).replace("**", ""), k["stab"]["restart"],
                k["stab"]["oom"], _notready_field(k)))
    L.append("  P99 Top3: " + ("　".join("`%s` %.0f ms" % (s, v) for s, v in _p99_rows(k))
                              or "无数据"))
    L.append("  峰值 QPS: " + (" / ".join(x.replace("**", "") for x in pk)
                             if pk else "无 Istio 流量"))
    L.append("  中间件: " + _mw_field(d).replace("\n", " / "))
    L.append("  MQ 积压: " + _backlog_field(d).replace("**", "").replace("\n", " / "))
    warns = _warnings()
    if warns:
        L.append("")
        L.append("数据质量 %d 项：" % len(warns))
        L += ["  - %s" % w for w in warns]
    L.append("")
    L.append("  " + _note(d))
    return "\n".join(L)


# ----------------------------------------------------------------- card: text bits
def _div(text, size="normal", color="default", align="left"):
    return {"tag": "div", "text": {"tag": "plain_text", "content": text,
                                   "text_size": size, "text_align": align,
                                   "text_color": color}}


def _md(content, size="normal", align="left", color=None, margin=None):
    el = {"tag": "markdown", "content": content, "text_size": size,
          "text_align": align}
    if color:
        el["content"] = "<font color='%s'>%s</font>" % (color, content)
    if margin:
        el["margin"] = margin
    return el


def _color_for(k):
    """Tile colour follows direction-is-bad semantics, not the magnitude."""
    red = k.get("red") or {}
    out = {}
    out["alerts"] = "red" if k["alerts"] else "green"
    out["5xx"] = "grey" if not red.get("sampled") else (
        "red" if (red.get("ratio") or 0) > 0.1 else "green")
    out["restart"] = "red" if (k["stab"]["restart"] or k["stab"]["oom"]) else "green"
    return out


def _kpi_v2(d):
    k = d["k"]
    col = _color_for(k)
    n = len(d["issues"])
    red = k.get("red") or {}
    if red.get("sampled"):
        v5 = "%.3f%%" % red["ratio"]
        sub5 = "%.0f / %.0fk 请求" % (red["bad"], red["all"] / 1000.0)
    else:
        v5 = "UNKNOWN"
        sub5 = "1h 仅 %.0f 请求" % (red.get("all") or 0)
    # Uniform three-line tile: 指标名 / 数值 / 时间窗 + 说明. The window used to
    # live in the label of two tiles only, which made the block read unevenly.
    tiles = [
        ("待处置", "%d" % n, "今日 · 新增 %d · 恢复 %d" % (len(k["new"]), len(k["gone"])),
         "red" if n else "green"),
        ("告警", "%d" % k["alerts"], "当前 firing · 24h 前 %d" % k["alerts_prev"],
         col["alerts"]),
        ("5xx", v5, "1h · %s" % sub5, col["5xx"]),
        ("重启 / OOM", "%.0f / %.0f" % (k["stab"]["restart"], k["stab"]["oom"]),
         "24h · NotReady %s" % _notready_field(k).replace("`", ""), col["restart"]),
    ]
    cols = [{"tag": "column", "width": "weighted", "weight": 1,
             "vertical_align": "top", "vertical_spacing": "2px", "padding": "2px 4px",
             "elements": [_md("**%s**" % label, size="normal"),
                          _div(value, "heading-2", color),
                          _div(sub, "notation", MUTED)]}
            for label, value, sub, color in tiles]
    # Two per row: four tiles in one row truncate on a phone.
    return [{"tag": "column_set", "flex_mode": "bisect", "horizontal_spacing": "small",
             "margin": "0px 0px 8px 0px", "columns": pair}
            for pair in (cols[:2], cols[2:])]


def _dur(seconds):
    ms = (seconds or 0) * 1000
    return ("%.1f s" % (ms / 1000)) if ms >= 1000 else ("%.0f ms" % ms)


def _card_text(text):
    """Backticked tokens become bold: inline-code chips make rows tall."""
    return re.sub(r"`([^`]+)`", r"**\1**", text)


def _section(title, suffix="", summary=""):
    """Section title + one synthesis line.

    One shape for every section — bold 16px name, grey suffix after a `·` — so
    突刺 / 延迟与容量 / 积压 never look like different kinds of heading. The
    summary says what the rows add up to; rows alone leave the reader counting.
    """
    body = "**▎%s**" % title
    if suffix:
        body += " <font color='%s'>· %s</font>" % (MUTED, suffix)
    out = [_md(body, size="heading-4", margin="10px 0px 2px 0px")]
    if summary:
        out.append(_md(summary, size="normal", color=MUTED, margin="0px 0px 2px 0px"))
    return out


def _row_v2(it):
    """Two lines: subject on top, grey context underneath.

    Cramming 中位 / ×倍数 / 占比 / 时间 onto one line turned every row into a
    wall of separators; the context gets its own smaller grey line instead.
    """
    left = [_md(_card_text(it["text"]), size="normal")]
    if it["meta"]:
        left.append(_div(it["meta"].replace("`", ""), "notation", MUTED))
    cols = [{"tag": "column", "width": "weighted", "weight": 5,
             "vertical_align": "center", "vertical_spacing": "2px", "elements": left}]
    if it["value"]:
        cols.append({"tag": "column", "width": "weighted", "weight": 2,
                     "horizontal_align": "right", "vertical_align": "center",
                     "elements": [_div(it["value"], "normal", it["level"], "right")]})
    return {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "small",
            "margin": "0px", "columns": cols}


def _summary_for(cat, items):
    """Synthesis line above a group. Only 突刺 currently needs one: its rows
    are magnitudes, and "one blip" vs "still flapping" is the actual question."""
    if cat != "突刺":
        return ""
    ratios = [float(m.group(1)) for it in items
              for m in [re.search(r"×(\d+)", it["meta"])] if m]
    if not ratios:
        return ""
    worst = max(items, key=lambda it: float(
        (re.search(r"×(\d+)", it["meta"]) or [0, 0])[1]))
    name = re.search(r"`([^`]+)`", worst["text"])
    head = "%d 个服务出现尖峰，最大 ×%d（%s）" % (
        len(items), int(max(ratios)), name.group(1) if name else "?")
    shares = [it for it in items if it.get("share")]
    if not shares:
        return head
    it = max(shares, key=lambda x: x["share"])
    pct = it["share"] * 100
    nm = re.search(r"`([^`]+)`", it["text"])
    name = nm.group(1) if nm else "?"
    if pct >= 10:
        return head + "；%s 有 %.0f%% 的 5 分钟点超阈，属持续偏高" % (name, pct)
    return head + "；均为低频瞬时尖峰（最高 %s）" % (
        "<1%" if pct < 1 else "%.0f%%" % pct)


def _capacity_summary(d):
    k = d["k"]
    bits = []
    today, prev = (k.get("peak") or {}).get("today"), (k.get("peak") or {}).get("prev")
    if today and prev:
        dd = pct(today["peak"], prev["peak"])
        if dd != float("inf"):
            bits.append("流量峰值环比 %s" % arrow(dd))
    lvl = []
    for m in d.get("mongo") or []:
        shards = (m["shards"] or [])[:3]
        for i, sh in enumerate(shards, 1):
            name = "Mongo" if len(shards) == 1 else "Mongo 分片%d" % i
            if sh.get("mem") is not None:
                lvl.append(("%s 内存" % name, sh["mem"]))
            if sh.get("cpu") is not None:
                lvl.append(("%s CPU" % name, sh["cpu"]))
    for r in d.get("redis") or []:
        if r.get("mem") is not None:
            lvl.append(("Redis 内存", r["mem"]))
    if lvl:
        name, val = max(lvl, key=lambda x: x[1])
        bits.append("中间件最高水位 %s %.0f%%%s"
                    % (name, val, "（超 80% 预警线）" if val >= 80 else "，未达预警线"))
    return "；".join(bits)


def _backlog_summary(d):
    bl = [x for x in d.get("backlog") or []
          if x["median"] > 50 or x["peak"] >= 10000]
    if not bl:
        return ""
    alive = [x for x in bl if x["last"] > 0]
    tail = ("其中 %d 组当前仍有积压" % len(alive)) if alive else "当前均已清空"
    return "%d 组 24h 峰值超过 1 万；%s" % (len(bl), tail)


def _grouped_rows(issues):
    groups = collections.OrderedDict()
    for it in issues:
        groups.setdefault(it["cat"], []).append(it)
    out = []
    for cat, items in groups.items():
        out += _section(cat, "%d 项" % len(items), _summary_for(cat, items))
        out += [_row_v2(it) for it in items]
    return out


def _capacity_rows(d):
    """QPS / middleware — the numbers you compare over time."""
    k = d["k"]
    rows = []
    today = (k.get("peak") or {}).get("today")
    if today:
        prev = (k.get("peak") or {}).get("prev")
        ratio = ("%.0f×" % today["ratio"]) if today.get("ratio") else "n/a"
        meta = "峰 @%s · 峰谷比 %s" % (cst(today["peak_at"]), ratio)
        if prev:
            dd = pct(today["peak"], prev["peak"])
            if dd != float("inf"):
                meta += " · 环比 %s" % arrow(dd)
        rows.append(item("流量", "峰值 QPS", value="%.0f qps" % today["peak"],
                         meta=meta, level="default"))
    else:
        rows.append(item("流量", "Istio 流量", value="无数据", meta="空向量=UNKNOWN",
                         level="grey"))
    for m in d.get("mongo") or []:
        shards = (m["shards"] or [])[:3]
        for i, sh in enumerate(shards, 1):
            name = "Mongo" if len(shards) == 1 else "Mongo 分片%d" % i
            # Field names on the left: "CPU 1.87% · 内存 34%" wraps in the
            # narrow right column on a phone, "1.87% / 34%" does not.
            rows.append(item("中间件", name, value="%s / %s" % (_pc(sh["cpu"]), _pc(sh["mem"])),
                             meta="CPU / 内存",
                             level="red" if (sh["mem"] or 0) >= 80 or (sh["cpu"] or 0) >= 70
                             else "default"))
    for r in d.get("redis") or []:
        rows.append(item("中间件", "Redis", value="%s / %s" % (_pc(r["cpu"], 2), _pc(r["mem"])),
                         meta="CPU / 内存",
                         level="red" if (r["mem"] or 0) >= 80 else "default"))
    bl = d.get("backlog") or []
    return rows


def _backlog_rows(d, max_rows=3):
    """Sustained backlog deserves its own section: it is a different question
    from "how much CPU are the instances burning"."""
    listed = {m.group(1) for it in d["issues"] if it["cat"] == "积压"
              for m in [re.search(r"`([^`]+)`", it["text"])] if m}
    bl = [x for x in d.get("backlog") or [] if x["group"] not in listed
          and (x["median"] > 50 or x["peak"] >= 10000)]
    rows = []
    for x in bl[:max_rows]:
        meta = cst(x["peak_at"])
        if x["last"]:
            meta += " · 当前 %.0f" % x["last"]
        rows.append(item("积压", "`%s`" % x["group"], value="%.0f" % x["peak"],
                         meta=meta, level="default"))
    if len(bl) > max_rows:
        rows.append(item("积压", "另有 %d 个消费组有积压峰值" % (len(bl) - max_rows),
                         level="grey"))
    return rows


# ----------------------------------------------------------------- card: 2.0
MAX_ISSUE_ROWS = 10


HEADER_TEMPLATE = {"red": "red", "orange": "wathet", "green": "green"}


def _card_v2(d):
    warns = _warnings()
    k = d["k"]
    E = _kpi_v2(d) + [_md("告警规则：%s" % _card_text(_rule_field(k)), size="normal",
                         margin="2px 0px 4px 0px")]
    if d["issues"]:
        shown = d["issues"][:MAX_ISSUE_ROWS]
        E += _grouped_rows(shown)
        if len(d["issues"]) > MAX_ISSUE_ROWS:
            E.append(_md("…另 %d 项（见完整 MD 报告）" % (len(d["issues"]) - MAX_ISSUE_ROWS),
                         size="notation", color="grey"))
    else:
        E.append(_md("**无需处置** ✅" if not warns
                     else "**未发现问题，但部分检查未完成** ⚠️", size="normal"))
    E += _section("延迟与容量", "24h 峰值", _capacity_summary(d))
    E += [_row_v2(it) for it in _capacity_rows(d)]
    bl_rows = _backlog_rows(d)
    if bl_rows:
        E += _section("积压", "24h 峰值", _backlog_summary(d))
        E += [_row_v2(it) for it in bl_rows]
    if warns:
        E.append(_md("**⚠️ 数据质量（%d 项）**" % len(warns), size="notation",
                     color="orange", margin="10px 0px 2px 0px"))
        for w in warns[:6]:
            E.append(_md("· %s" % w, size="notation", color=MUTED))
    E.append(_md(_note_short(d), size="notation", color=MUTED, margin="10px 0px 0px 0px"))
    return {"schema": "2.0",
            "config": {"width_mode": "fill", "update_multi": True},
            "header": {
                "template": HEADER_TEMPLATE[effective_level(d)],
                "title": {"tag": "plain_text", "content": "%s 巡检 · %s"
                          % (d["ns"], now_cst().strftime("%m-%d %H:%M"))},
                "subtitle": {"tag": "plain_text", "content": "只读 · 24h vs 前 24h"},
            },
            "body": {"padding": "8px 12px 10px 12px", "vertical_spacing": "0px",
                     "elements": E}}


# ----------------------------------------------------------------- card: 1.0
def _card_v1(d):
    E = []
    warns = _warnings()
    if d["issues"]:
        shown = d["issues"][:8]
        rows = "\n".join(
            ("**%s**　%s" % (it["cat"], _line(it)))
            for it in shown)
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
        for a, b, short in _fields_v1(d)]})
    if warns:
        E.append(fc.hr())
        E.append(fc.md("**⚠️ 数据质量（%d 项）**\n%s"
                       % (len(warns), "\n".join("• %s" % w for w in warns[:6]))))
    E.append(fc.note(_note(d)))
    return fc.card("%s 巡检 · %s" % (d["ns"], now_cst().strftime("%m-%d %H:%M")),
                   effective_level(d), E)


def render_card(d, schema=None):
    if (schema or CARD_SCHEMA) == "1.0":
        return _card_v1(d)
    return _card_v2(d)