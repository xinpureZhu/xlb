#!/usr/bin/env python3
"""Release verification: deterministic deploy check + runtime windows + card."""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "_shared"))
import feishu_card as fc  # noqa: E402

OUT_DIR = os.path.expanduser("~/.xlb/reports/release")
USER_ID = "ou_5d3dc8766fa70f73a8c0ace68cbbb565"
MARK = {"PASS": "🟢", "WARN": "🟠", "FAIL": "🔴", "UNKNOWN": "⚪"}


def run(script, args):
    cmd = [sys.executable, os.path.join(HERE, script)] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def run_json(script, args):
    rc, out = run(script, args + ["--json"])
    try:
        return rc, json.loads(out)
    except Exception:
        return rc, None


def build_card(deploy, runtime):
    dv = deploy.get("verdict") or "UNKNOWN"
    rv = runtime.get("verdict") or "UNKNOWN"
    services = deploy.get("services") or []
    t0 = (runtime.get("deploy_time") or "")[:16].replace("T", " ")
    elements = [fc.fields([
        ("部署", MARK.get(dv, "⚪") + " " + dv),
        ("运行态", MARK.get(rv, "⚪") + " " + rv),
        ("服务", str(len(services))),
        ("t0", t0 or "-"),
    ]), fc.hr()]

    rm = {s.get("service"): s for s in runtime.get("services") or []}
    for s in services:
        name = s.get("service")
        r = rm.get(name) or {}
        lines = ["**%s** · %s" % (name, s.get("env"))]
        lines.append("部署 %s · 镜像%s · %s/%s" % (
            s.get("deploy_status"), "一致" if s.get("digest_match") else "不一致",
            (s.get("workflow") or {}).get("node"), (s.get("workflow") or {}).get("status")))
        w = r.get("windows") or {}
        if w:
            def trio(key):
                return " / ".join(
                    "n/a" if w.get(x, {}).get(key) is None else str(w[x][key])
                    for x in ("baseline", "plus15", "plus60"))
            lines.append("QPS %s · 5xx%% %s · P99 %s ms · 重启 %s" % (
                trio("qps"), trio("ratio"), trio("p99"), trio("restarts")))
        else:
            lines.append("运行态 UNKNOWN（目标 svc 无指标）")
        elements.append(fc.md("\n".join(lines)))
        elements.append(fc.hr())

    unknown = [s.get("service") for s in runtime.get("services") or [] if s.get("verdict") == "UNKNOWN"]
    if unknown:
        elements.append(fc.note("未覆盖：%s；不得当作 PASS。" % ", ".join(unknown)))
    elements.append(fc.note("t0 取 workflow 完成时间；只读 Efficiency + Grafana。"))
    template = {"FAIL": "red", "WARN": "orange", "UNKNOWN": "grey", "PASS": "green"}.get(rv, "blue")
    if unknown and rv == "PASS":
        template = "orange"
    return fc.card("发布后验证 · %s" % (deploy.get("order") or {}).get("name", "-"), template, elements)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--release-order")
    g.add_argument("--branch")
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args()

    base = ["--release-order", a.release_order] if a.release_order else ["--branch", a.branch]
    rc_d, deploy = run_json("deploy_check.py", base)
    rc_r, runtime = run_json("runtime_check.py", base)
    if not deploy or not runtime:
        raise SystemExit("deploy/runtime check failed")

    _, dtext = run("deploy_check.py", base)
    _, rtext = run("runtime_check.py", base)
    text = dtext.strip() + "\n\n" + rtext.strip()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    oid = a.release_order or "branch"
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "%s-%s.md" % (oid, stamp))
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(text)
    print("\n[saved] %s" % path)

    if a.send:
        try:
            info = fc.send(build_card(deploy, runtime), USER_ID, "xlb-rel-%s-%s" % (str(oid)[-8:], stamp))
        except RuntimeError as e:
            raise SystemExit("send failed: %s" % e)
        print("[sent] message_id=%s" % info.get("message_id"))
    return 0 if rc_d != 2 else 2


if __name__ == "__main__":
    sys.exit(main())