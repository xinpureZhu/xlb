"""Extract a namespace's prod middleware dependencies from gitops app manifests.

The manifests are the source of truth: instance names in the cloud console do not
follow a usable convention (goods' Kafka is called `goods-prod`, not
`prod--goods--kafka`), so instance discovery by name misses them.

Two kinds of evidence are collected:
  * literal endpoints (Kafka bootstrap hosts, Redis/ES hosts)
  * `IAC__<OWNER>__<RESOURCE>__*` placeholders, which name the managed resource
    directly, e.g. IAC__RETAIL_BFF__GOODS_REDIS__HOST -> retail-bff--goods-redis

Secret-looking keys are never read.
"""
import glob
import os
import re

SECRET_KEY = re.compile(r"PASSWORD|SECRET|TOKEN|ACCESS_KEY|APPKEY|PRIVATE", re.I)
KAFKA_HOST = re.compile(r"alikafka-serverless-cn-([a-z0-9]+)-\d+-vpc")
REDIS_HOST = re.compile(r"\b(r-[a-z0-9]+)\.redis\.rds\.aliyuncs\.com")
MONGO_HOST = re.compile(r"\b(s-[a-z0-9]+)\.mongodb\.rds\.aliyuncs\.com")
ES_HOST = re.compile(r"\b(es-cn-[a-z0-9]+)\.")
IAC = re.compile(r"IAC__([A-Z0-9]+(?:_[A-Z0-9]+)*?)__([A-Z0-9_]+?)__[A-Z0-9_]+")


def _prod_values(path):
    """Yield (env_var, value) pairs that apply to prod (excluding prod-gray-only)."""
    key = None
    for raw in open(path, encoding="utf-8", errors="replace"):
        line = raw.rstrip("\n")
        m = re.match(r"^  ([A-Z][A-Z0-9_]*):\s*(.*)$", line)
        if m:
            key, inline = m.group(1), m.group(2).strip()
            if inline:
                yield key, inline, False
            continue
        m = re.match(r"^    ([a-z0-9,\-]+):\s*(.+)$", line)
        if m and key:
            envs = {e.strip() for e in m.group(1).split(",")}
            # endpoints must come from an explicit prod label; `default` is only
            # trustworthy for logical names (group/topic), not for hosts
            if "prod" in envs:
                yield key, m.group(2).strip().strip('"'), True
            elif "default" in envs:
                yield key, m.group(2).strip().strip('"'), False


def collect(ns, root="repos/gitops/apps"):
    d = os.path.join(root, ns)
    out = {"kafka": set(), "redis": set(), "mongos": set(), "es": set(),
           "groups": set(), "topics": set(), "iac": set(), "apps": []}
    for f in sorted(glob.glob(os.path.join(d, "*.app.yaml"))):
        out["apps"].append(os.path.basename(f).replace(".app.yaml", ""))
        for key, val, is_prod in _prod_values(f):
            for m in IAC.finditer(val):
                owner, res = m.group(1).lower().replace("_", "-"), m.group(2).lower().replace("_", "-")
                out["iac"].add("%s--%s" % (owner, res))
            if SECRET_KEY.search(key):
                continue          # never read credential values
            if not is_prod:
                if re.search(r"GROUP", key) and re.fullmatch(r"[a-z0-9_\-]+", val or ""):
                    out["groups"].add(val)
                continue
            for m in KAFKA_HOST.finditer(val):
                out["kafka"].add("alikafka_serverless-cn-" + m.group(1))
            for m in REDIS_HOST.finditer(val):
                out["redis"].add(m.group(1))
            for m in MONGO_HOST.finditer(val):
                out["mongos"].add(m.group(1))
            for m in ES_HOST.finditer(val):
                out["es"].add(m.group(1))
            if re.search(r"GROUP", key) and re.fullmatch(r"[a-z0-9_\-]+", val or ""):
                out["groups"].add(val)
            if re.search(r"TOPIC", key) and re.fullmatch(r"[a-z0-9_\-]+", val or ""):
                out["topics"].add(val)
    return out


if __name__ == "__main__":
    import sys
    ns = sys.argv[1] if len(sys.argv) > 1 else "goods"
    r = collect(ns)
    print("namespace: %s（%d 个 app）" % (ns, len(r["apps"])))
    for k in ("kafka", "redis", "mongos", "es"):
        print("  %-8s %s" % (k, ", ".join(sorted(r[k])) or "-"))
    print("  iac      %s" % ", ".join(sorted(r["iac"])))
    print("  groups   %s" % ", ".join(sorted(r["groups"])))
    print("  topics   %s" % ", ".join(sorted(r["topics"])))
