# -*- coding: utf-8 -*-
"""GitHub 关系边探针 v2：只读，绝不写。

铁律：gh api 的 -f/-F 会把请求变成 POST（POST /repos/{o}/{r}/forks = 真的建 fork）。
     本文件只允许 gh_get()，参数一律拼在 URL 查询串里。
"""
import json
import subprocess
import headless
import time
import urllib.parse

OUT = []
SEED = "danielmiessler/fabric"
SEED_OWNER, SEED_REPO = SEED.split("/")


def gh_get(path, tmo=45):
    """只读 GET。path 里自带查询串。绝不接受 -f。"""
    assert " -f " not in path and " -F " not in path
    t0 = time.time()
    try:
        p = headless.run(
            ["gh", "api", "-X", "GET", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=tmo, shell=False,
        )
    except subprocess.TimeoutExpired:
        return False, "timeout", time.time() - t0
    dt = time.time() - t0
    if p.returncode != 0:
        return False, (p.stderr or p.stdout or "")[:300], dt
    try:
        return True, json.loads(p.stdout), dt
    except Exception as e:
        # 纯文本（raw readme）走这里
        return True, p.stdout, dt


def gh_graphql(q, tmo=45):
    t0 = time.time()
    p = headless.run(
        ["gh", "api", "graphql", "-f", "query=" + q],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=tmo, shell=False,
    )
    dt = time.time() - t0
    if p.returncode != 0:
        return False, (p.stderr or "")[:300], dt
    try:
        return True, json.loads(p.stdout), dt
    except Exception as e:
        return False, str(e)[:200], dt


def note(tag, ok, info, dt, extra=None):
    rec = {"tag": tag, "ok": ok, "sec": round(dt, 2)}
    if isinstance(info, list):
        rec["n"] = len(info)
    elif isinstance(info, dict):
        rec["n"] = 1
        rec["keys"] = list(info)[:8]
    else:
        rec["n"] = 0
    if extra:
        rec.update(extra)
    if not ok:
        rec["err"] = str(info)[:220]
    OUT.append(rec)
    print("[%-16s] ok=%-5s %5.2fs  n=%-4s %s" % (tag, ok, dt, rec.get("n"), rec.get("err", "")))
    return rec


# ============ A. GraphQL 单次拿 repo 全套 ============
print("=" * 62)
q_a = """
query($o:String!,$r:String!){
  repository(owner:$o,name:$r){
    nameWithOwner stargazerCount forkCount
    primaryLanguage{name}
    repositoryTopics(first:20){nodes{topic{name}}}
    parent{nameWithOwner}
    owner{login __typename}
    createdAt pushedAt
    diskUsage
  }
}"""
ok, d, dt = gh_graphql(q_a.replace("$o", '"%s"' % SEED_OWNER).replace("$r", '"%s"' % SEED_REPO).replace("query(", "query{").replace(":String!,$r:String!", "").replace(':String!', ""))
if not ok:
    # 简化为无变量写法
    q_a2 = """query{repository(owner:"%s",name:"%s"){nameWithOwner stargazerCount forkCount primaryLanguage{name} repositoryTopics(first:20){nodes{topic{name}}} parent{nameWithOwner} owner{login __typename} createdAt pushedAt}}""" % (SEED_OWNER, SEED_REPO)
    ok, d, dt = gh_graphql(q_a2)
if ok:
    rp = d.get("data", {}).get("repository", {}) or {}
    note("graphql:repo", True, rp, dt, {
        "topics": [x["topic"]["name"] for x in rp.get("repositoryTopics", {}).get("nodes", [])],
        "stars": rp.get("stargazerCount"),
    })
else:
    note("graphql:repo", False, d, dt)

# ============ B. topic 搜同族 ============
print("=" * 62)
q = urllib.parse.quote("topic:llm stars:>500")
ok, d, dt = gh_get("search/repositories?q=%s&per_page=100&sort=stars" % q)
if ok:
    items = d.get("items", []) if isinstance(d, dict) else []
    note("search:topic", True, items, dt, {
        "total_count": d.get("total_count") if isinstance(d, dict) else None,
        "sample": [i["full_name"] for i in items[:5]],
    })
else:
    note("search:topic", False, d, dt)

# ============ C. forks 亲缘 ============
print("=" * 62)
ok, d, dt = gh_get("repos/%s/forks?per_page=30&sort=stargazers" % SEED)
if ok:
    note("forks", True, d, dt, {"sample": [i["full_name"] for i in d[:5]]})
else:
    note("forks", False, d, dt)

# ============ D. 同作者 ============
print("=" * 62)
ok, d, dt = gh_get("users/%s/repos?per_page=100&sort=updated" % SEED_OWNER)
if ok:
    note("owner:repos", True, d, dt, {"sample": [i["full_name"] for i in d[:5]]})
else:
    note("owner:repos", False, d, dt)

# ============ E. stargazers（协同过滤成本）============
print("=" * 62)
ok, d, dt = gh_get("repos/%s/stargazers?per_page=100" % SEED)
if ok:
    logins = [i.get("login") for i in d]
    note("stargazers", True, d, dt, {"sample": logins[:5]})
else:
    note("stargazers", False, d, dt)

# ============ F. README 外链 ============
print("=" * 62)
ok, d, dt = gh_get("repos/%s/readme" % SEED)
if ok:
    import base64 as b64
    txt = ""
    if isinstance(d, dict):
        try:
            txt = b64.b64decode(d.get("content", "")).decode("utf-8", "replace")
        except Exception:
            txt = ""
    else:
        txt = str(d)
    import re
    links = re.findall(r"https?://[^\s)\]\"'>]+", txt)
    note("readme:links", True, {"len": len(txt)}, dt, {"links": len(links), "sample": links[:6]})
else:
    note("readme:links", False, d, dt)

# ============ G. GraphQL 别名批量（省钱关键）============
print("=" * 62)
names = ["danielmiessler/fabric", "run-llama/llama_index", "langchain-ai/langchain",
         "microsoft/autogen", "crewAIInc/crewAI"]
parts = []
for i, n in enumerate(names):
    o, r = n.split("/")
    parts.append('r%d: repository(owner:"%s",name:"%s"){nameWithOwner stargazerCount forkCount '
                 'repositoryTopics(first:10){nodes{topic{name}}}}' % (i, o, r))
ok, d, dt = gh_graphql("query{" + " ".join(parts) + "}")
if ok:
    got = d.get("data", {}) or {}
    note("graphql:batch5", True, got, dt, {"labels": list(got)})
else:
    note("graphql:batch5", False, d, dt)

# ============ H. 依赖关系（GraphQL，最硬的技术栈边）============
print("=" * 62)
q_h = """query{repository(owner:"%s",name:"%s"){
  dependencyGraphManifests(first:5){totalCount nodes{filename
    dependencies(first:10){nodes{packageName requirements}}}}}}""" % (SEED_OWNER, SEED_REPO)
ok, d, dt = gh_graphql(q_h)
if ok:
    m = (d.get("data", {}) or {}).get("repository", {}) or {}
    note("graphql:deps", True, m, dt, {"raw": json.dumps(m)[:200]})
else:
    note("graphql:deps", False, d, dt)

# ============ 配额结算 ============
print("=" * 62)
ok, d, dt = gh_get("rate_limit")
if ok:
    R = d["resources"]
    for k in ("core", "search", "graphql"):
        print("QUOTA %-8s used=%4s/%s" % (k, R[k]["used"], R[k]["limit"]))

with open("_kb/_gh_probe.json", "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=1)
print("\nWROTE _kb/_gh_probe.json  (%d edges)" % len(OUT))
