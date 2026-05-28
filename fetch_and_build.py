#!/usr/bin/env python3
"""
Daily fetcher: pulls latest issues + comments from atlassian/atlassian-mcp-server
and rebuilds the dashboard HTML with fresh data.
"""
import urllib.request, json, re, time, os, base64
from datetime import datetime, timezone

TOKEN = os.environ["GITHUB_TOKEN"]
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

def fetch(url):
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {TOKEN}",
        "User-Agent": "mcp-tracker-bot/1.0"
    })
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

print("Fetching issues...")
all_raw = []
page = 1
while True:
    data = fetch(f"{BASE}/issues?state=all&per_page=100&page={page}")
    if not data:
        break
    all_raw.extend(data)
    print(f"  Page {page}: {len(data)} items")
    if len(data) < 100:
        break
    page += 1

print(f"Total: {len(all_raw)} items")

# Build structured items
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

# Fetch comments for Jira issues with comments
jira_with_comments = [i for i in all_items if is_jira(i) and not i["is_pr"] and i["comments"] > 0]
print(f"Fetching comments for {len(jira_with_comments)} Jira issues...")
for i, issue in enumerate(jira_with_comments):
    num = issue["number"]
    try:
        comments = fetch(f"{BASE}/issues/{num}/comments?per_page=10")
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
        print(f"  [{i+1}/{len(jira_with_comments)}] #{num}: {len(comments)} comments")
    except Exception as e:
        print(f"  Error #{num}: {e}")
    time.sleep(0.15)

# Load the template HTML
with open("template.html") as f:
    template = f.read()

# Replace data and timestamp
safe_json = json.dumps(all_items, ensure_ascii=True)
fetched_at = datetime.now(timezone.utc).isoformat()

# Replace data script tag
template = re.sub(
    r'<script type="application/json" id="__data__">.*?</script>',
    f'<script type="application/json" id="__data__">{safe_json}</script>',
    template,
    flags=re.DOTALL
)

# Replace fetched_at timestamp
template = re.sub(
    r"formatDate\('[^']+'\)",
    f"formatDate('{fetched_at}')",
    template
)

with open("index.html", "w") as f:
    f.write(template)

open_issues = len([i for i in all_items if not i['is_pr'] and i['state'] == 'open'])
jira_open = len([i for i in all_items if is_jira(i) and not i['is_pr'] and i['state'] == 'open'])
print(f"\nDone! {open_issues} open issues ({jira_open} Jira-specific)")
print(f"Fetched at: {fetched_at}")
