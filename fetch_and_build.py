#!/usr/bin/env python3
"""
Daily fetcher: pulls latest issues + comments from atlassian/atlassian-mcp-server
and VOC issues from Socrates, then rebuilds the dashboard HTML with fresh data.
Also auto-refreshes JTBD signal strength and cross-links based on new issues.
"""
import urllib.request, json, re, time, os
from datetime import datetime, timezone

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
SOURCE_REPO = "atlassian/atlassian-mcp-server"
BASE = f"https://api.github.com/repos/{SOURCE_REPO}"

JIRA_KEYWORDS = [
    'jira', 'getjira', 'searchjira', 'createjira', 'updatejira', 'jql',
    'worklog', 'sprint', 'jsm', 'jira software', 'jira service',
    'jira asset', 'jira project', 'jira board', 'jira issue', 'jira comment',
    'jira ticket', 'jira field', 'jira api', 'jira scope', 'jira mention',
    'addjiraissue', 'editjiraissue', 'addjira', 'jira workflow', 'jira automation'
]

# JTBD keyword matchers — used to auto-detect new issues that belong to each JTBD
JTBD_MATCHERS = {
    "jtbd-1": ["auth", "oauth", "token", "login", "re-auth", "reauth", "pkce", "refresh token", "credential", "session", "expir"],
    "jtbd-2": ["permission", "restrict", "access control", "granular", "external user", "admin", "policy", "block", "domain", "per site"],
    "jtbd-3": ["attachment", "upload", "download", "file", "image", "screenshot", "pdf"],
    "jtbd-4": ["custom field", "acceptance criteria", "field support", "customfield", "actual result", "expected result"],
    "jtbd-5": ["duplicate", "anonymous project", "create issue", "createjiraissue", "double", "two tickets"],
    "jtbd-6": ["payload", "verbose", "token usage", "context window", "adf", "bloat", "token limit", "large result", "crash", "size limit"],
    "jtbd-7": ["sprint", "board", "epic", "agile", "subtask", "child issue", "hierarchy", "backlog"],
    "jtbd-8": ["data center", "server", "on-premise", "self-hosted", "dc", "local", "cluster"],
    "jtbd-9": ["worklog", "time track", "time log", "logged time", "tempo"],
    "jtbd-10": ["multi-site", "multiple site", "multi site", "multiple atlassian", "two sites", "cross-site"],
    "jtbd-11": ["headless", "ci/cd", "pipeline", "kubernetes", "lambda", "n8n", "unattended", "server-to-server", "localhost"],
    "jtbd-12": ["jpd", "product discovery", "insight", "ideas"],
}

def is_jira(item):
    t = item['title'].lower()
    return any(kw in t for kw in JIRA_KEYWORDS)

def gh_fetch(url):
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {GITHUB_TOKEN}",
        "User-Agent": "mcp-tracker-bot/1.0"
    })
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

# ─── GITHUB ISSUES ────────────────────────────────────────────────────────────
print("Fetching GitHub issues...")
all_raw = []
page = 1
while True:
    data = gh_fetch(f"{BASE}/issues?state=all&per_page=100&page={page}")
    if not data:
        break
    all_raw.extend(data)
    print(f"  Page {page}: {len(data)} items")
    if len(data) < 100:
        break
    page += 1
print(f"Total: {len(all_raw)} items")

all_items = []
for raw in all_raw:
    all_items.append({
        "number": raw["number"],
        "title": raw["title"],
        "state": raw["state"],
        "is_pr": "pull_request" in raw,
        "user": raw["user"]["login"],
        "user_avatar": raw["user"]["avatar_url"],
        "labels": [{"name": l["name"], "color": l["color"]} for l in raw.get("labels", [])],
        "comments": raw["comments"],
        "created_at": raw["created_at"],
        "updated_at": raw["updated_at"],
        "closed_at": raw.get("closed_at"),
        "url": raw["html_url"],
        "body": (raw.get("body") or "")[:300],
        "assignees": [a["login"] for a in raw.get("assignees", [])],
        "milestone": raw["milestone"]["title"] if raw.get("milestone") else None,
        "reactions": raw.get("reactions", {}).get("total_count", 0),
        "recent_comments": [],
    })

# Fetch comments for Jira issues
jira_with_comments = [i for i in all_items if is_jira(i) and not i["is_pr"] and i["comments"] > 0]
print(f"Fetching comments for {len(jira_with_comments)} Jira issues...")
for idx, issue in enumerate(jira_with_comments):
    num = issue["number"]
    try:
        comments = gh_fetch(f"{BASE}/issues/{num}/comments?per_page=10")
        issue["recent_comments"] = [
            {
                "user": c["user"]["login"],
                "user_avatar": c["user"]["avatar_url"],
                "body": (c["body"] or "")[:400],
                "created_at": c["created_at"],
                "updated_at": c["updated_at"],
            }
            for c in comments[:5]
        ]
        print(f"  [{idx+1}/{len(jira_with_comments)}] #{num}: {len(comments)} comments")
    except Exception as e:
        print(f"  Error #{num}: {e}")
    time.sleep(0.15)

# ─── AUTO-REFRESH JTBD ────────────────────────────────────────────────────────
print("\nAuto-refreshing JTBD signal strength...")

def matches_jtbd(issue, jtbd_id):
    keywords = JTBD_MATCHERS.get(jtbd_id, [])
    text = (issue["title"] + " " + issue["body"]).lower()
    return any(kw in text for kw in keywords)

# Load current JTBD from template
with open("template.html") as f:
    template = f.read()

jtbd_match = re.search(r'const jtbdItems = (\[.*?\]);', template, re.DOTALL)
jtbd_items = json.loads(jtbd_match.group(1))

# For each JTBD, find all matching GitHub issues (open only)
open_issues = [i for i in all_items if not i["is_pr"] and i["state"] == "open"]

for jtbd in jtbd_items:
    jtbd_id = jtbd["id"]
    # Find matching open issues not already in the list
    existing_numbers = {g["number"] for g in jtbd.get("gh_issues", [])}
    new_matches = [
        {"number": i["number"], "title": i["title"]}
        for i in open_issues
        if matches_jtbd(i, jtbd_id) and i["number"] not in existing_numbers
    ]
    if new_matches:
        print(f"  {jtbd_id}: +{len(new_matches)} new matching issues: {[m['number'] for m in new_matches]}")
        jtbd["gh_issues"] = jtbd.get("gh_issues", []) + new_matches

    # Recalculate signal strength based on total evidence
    gh_count = len(jtbd.get("gh_issues", []))
    voc_count = len(jtbd.get("voc_issues", []))
    total_customers = sum(len(v.get("customers", [])) for v in jtbd.get("voc_issues", []))
    # Signal = base from gh_issues + boost from VOC customer count
    new_signal = min(10, gh_count + min(5, voc_count * 2) + min(2, total_customers // 4))
    if new_signal != jtbd["signal_strength"]:
        print(f"  {jtbd_id}: signal {jtbd['signal_strength']} → {new_signal}")
        jtbd["signal_strength"] = new_signal

# Re-sort by signal strength
jtbd_items.sort(key=lambda x: -x["signal_strength"])

# ─── REBUILD HTML ─────────────────────────────────────────────────────────────
fetched_at = datetime.now(timezone.utc).isoformat()

# Replace GitHub data
safe_json = json.dumps(all_items, ensure_ascii=True)
# Replace __data__ block safely without regex
import re as _re
_data_start = '<script type="application/json" id="__data__">'
_data_end = '</script>'
_s = template.find(_data_start)
_e = template.find(_data_end, _s) + len(_data_end)
if _s >= 0 and _e > _s:
    template = template[:_s] + _data_start + safe_json + _data_end + template[_e:]

# Replace JTBD data
jtbd_json = json.dumps(jtbd_items, ensure_ascii=True)
# Replace jtbdItems safely without regex
repl_jtbd = 'const jtbdItems = ' + jtbd_json + ';'
_ji_start = template.find('const jtbdItems = ')
_ji_end = template.find(';', _ji_start) + 1
if _ji_start >= 0:
    template = template[:_ji_start] + repl_jtbd + template[_ji_end:]

# Replace timestamp
# Replace timestamp safely
old_ts = re.search(r"formatDate\('[^']+'\)", template)
if old_ts:
    template = template.replace(old_ts.group(0), f"formatDate('{fetched_at}')", 1)

with open("index.html", "w") as f:
    f.write(template)

# Also update template.html with latest JTBD (so next run builds on this)
# Replace jtbdItems in template2 safely
_ji2_start = template.find('const jtbdItems = ')
_ji2_end = template.find(';', _ji2_start) + 1
if _ji2_start >= 0:
    template2 = template[:_ji2_start] + repl_jtbd + template[_ji2_end:]
else:
    template2 = template
with open("template.html", "w") as f:
    f.write(template2)

open_issues_count = len([i for i in all_items if not i['is_pr'] and i['state'] == 'open'])
jira_open = len([i for i in all_items if is_jira(i) and not i['is_pr'] and i['state'] == 'open'])
print(f"\n✅ Done! {open_issues_count} open issues ({jira_open} Jira), JTBD updated")
print(f"Fetched at: {fetched_at}")
