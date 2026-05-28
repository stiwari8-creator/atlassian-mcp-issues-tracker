#!/usr/bin/env python3
"""
Daily fetcher: pulls latest issues + comments from atlassian/atlassian-mcp-server
and VOC issues from Socrates, then rebuilds the dashboard HTML with fresh data.
"""
import urllib.request, urllib.parse, json, re, time, os, base64
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

def socrates_fetch(sql):
    """Submit SQL to Socrates and poll for results."""
    SOCRATES_TOKEN = os.environ.get("SOCRATES_TOKEN", GITHUB_TOKEN)
    headers = {
        "Authorization": f"Bearer {SOCRATES_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": "mcp-tracker-bot/1.0"
    }
    # Submit query
    body = json.dumps({"statement": sql}).encode()
    req = urllib.request.Request(
        "https://dbc-dp-production.cloud.databricks.com/api/2.0/sql/statements",
        data=body, headers=headers
    )
    with urllib.request.urlopen(req) as r:
        result = json.loads(r.read())
    statement_id = result["statement_id"]
    # Poll until done
    for _ in range(30):
        time.sleep(5)
        req2 = urllib.request.Request(
            f"https://dbc-dp-production.cloud.databricks.com/api/2.0/sql/statements/{statement_id}",
            headers=headers
        )
        with urllib.request.urlopen(req2) as r:
            status = json.loads(r.read())
        if status["status"]["state"] in ("SUCCEEDED", "FAILED", "CANCELED"):
            return status
    return None

# ─── GITHUB ISSUES ───────────────────────────────────────
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

# ─── VOC DATA ────────────────────────────────────────────
print("\nFetching VOC data from Socrates...")
voc_items = []
try:
    import subprocess, sys
    # Use databricks-sdk or fall back to saved data if no token
    SOCRATES_TOKEN = os.environ.get("SOCRATES_TOKEN", "")
    if not SOCRATES_TOKEN:
        print("  No SOCRATES_TOKEN — skipping VOC refresh, using baked-in data")
    else:
        result = socrates_fetch("""
            SELECT customer_domain, issue_id, summary, description, priority,
                   created, updated, resolution_date, status, status_display,
                   interested_teams, product, url
            FROM production.customer_360.enterprise_voc
            WHERE day = (SELECT MAX(day) FROM production.customer_360.enterprise_voc)
            AND (LOWER(summary) LIKE '%mcp%' OR LOWER(description) LIKE '%mcp%'
                 OR LOWER(summary) LIKE '%rovo mcp%' OR LOWER(description) LIKE '%rovo mcp%')
            ORDER BY updated DESC
            LIMIT 200
        """)
        if result and result["status"]["state"] == "SUCCEEDED":
            rows = result.get("result", {}).get("data_array", [])
            issues_map = {}
            for row in rows:
                customer, issue_id, summary, desc, priority, created, updated, res_date, status, status_display, teams, product, url = row
                if issue_id not in issues_map:
                    issues_map[issue_id] = {
                        "issue_id": issue_id, "summary": summary,
                        "description": (desc or "")[:300], "priority": priority,
                        "created": created, "updated": updated,
                        "resolution_date": res_date, "status": status,
                        "status_display": status_display, "interested_teams": teams,
                        "product": product, "url": url,
                        "customers": [], "customer_count": 0,
                    }
                if customer not in issues_map[issue_id]["customers"]:
                    issues_map[issue_id]["customers"].append(customer)
                    issues_map[issue_id]["customer_count"] += 1
            voc_items = sorted(issues_map.values(), key=lambda x: -x["customer_count"])
            print(f"  Got {len(voc_items)} unique VOC issues")
        else:
            print(f"  Socrates query failed: {result}")
except Exception as e:
    print(f"  VOC fetch error: {e} — using baked-in data")

# ─── REBUILD HTML ─────────────────────────────────────────
with open("template.html") as f:
    template = f.read()

safe_json = json.dumps(all_items, ensure_ascii=True)
fetched_at = datetime.now(timezone.utc).isoformat()

# Replace GitHub data
template = re.sub(
    r'<script type="application/json" id="__data__">.*?</script>',
    f'<script type="application/json" id="__data__">{safe_json}</script>',
    template, flags=re.DOTALL
)

# Replace VOC data only if we got fresh data
if voc_items:
    voc_json = json.dumps(voc_items, ensure_ascii=True)
    template = re.sub(r'const vocItems = \[.*?\];', f'const vocItems = {voc_json};', template, flags=re.DOTALL)

# Replace timestamp
template = re.sub(r"formatDate\('[^']+'\)", f"formatDate('{fetched_at}')", template)

with open("index.html", "w") as f:
    f.write(template)

open_issues = len([i for i in all_items if not i['is_pr'] and i['state'] == 'open'])
jira_open = len([i for i in all_items if is_jira(i) and not i['is_pr'] and i['state'] == 'open'])
print(f"\n✅ Done! {open_issues} open issues ({jira_open} Jira-specific), {len(voc_items)} VOC issues")
print(f"Fetched at: {fetched_at}")
