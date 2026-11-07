"""Extract: fetch data from the GitHub REST API.

No token is hard-coded. If GITHUB_TOKEN is set in the environment it is
used (higher rate limit); otherwise the public unauthenticated limit
applies and the ETL keeps its requests small on purpose: one repository
call, a latest-30 releases sample and up to 30 issues per repository,
plus at most one extra request per repo to count total releases via
the pagination Link header (see fetch_releases_total).

Persistent token, without re-typing it every session: put it in a
`.env` file in the project root (next to app.py) as one line,
`GITHUB_TOKEN=paste-token-here`, and this module loads it on first
use. A real environment variable always wins over `.env`, so a
terminal-set token still overrides the file. `.env` is in
.gitignore and must never be committed, zipped up, or shared; it is
plaintext on your own machine, like any other local credential store.
No extra dependency is needed: the parser below is deliberately
tiny (KEY=VALUE lines, # comments, optional quotes).
"""
import os
import time

import requests

BASE = "https://api.github.com"
_DOTENV_LOADED = False


def _load_dotenv():
    """Load GITHUB_TOKEN from a project-root .env file, once.

    Looks in the current working directory first, then in the project
    root (the parent of this package). Never overwrites a token that
    is already set in the real environment.
    """
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    if os.environ.get("GITHUB_TOKEN"):
        return
    candidates = [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
    ]
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "GITHUB_TOKEN":
                value = value.strip().strip('"').strip("'")
                if value and not value.startswith("paste-"):
                    os.environ["GITHUB_TOKEN"] = value
                return


def _headers():
    _load_dotenv()
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "oss-pulse"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get_response(url, params=None):
    for attempt in range(3):
        resp = requests.get(url, headers=_headers(), params=params, timeout=30)
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            wait = int(resp.headers.get("Retry-After", 60))
            print(f"Rate limited, waiting {wait}s (attempt {attempt + 1}/3)")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError(f"GitHub API request failed after retries: {url}")


def _get(url, params=None):
    return _get_response(url, params=params).json()


def fetch_repo(full_name):
    return _get(f"{BASE}/repos/{full_name}")


def fetch_releases(full_name, limit=20):
    data = _get(f"{BASE}/repos/{full_name}/releases",
                params={"per_page": min(limit, 100)})
    return data[:limit]


def fetch_releases_total(full_name):
    """True total release count, cheaply, via the pagination Link header.

    GitHub gives no total-releases field, so counting naively would
    mean fetching every page (569 releases = 6 requests). Instead:
    fetch page 1 at 100 per page; if there is no rel="last" link, the
    total is just the page size. If there is, fetch only that last
    page and compute (last_page - 1) * 100 + items on the last page.
    Cost: 1 request for repos up to 100 releases, 2 requests for any
    larger repo, no matter how large. The detail lists still use only
    the latest sample (see fetch_releases); this total is for charts
    and counts only, so it never downloads every release body.
    """
    import re
    resp = _get_response(f"{BASE}/repos/{full_name}/releases",
                         params={"per_page": 100})
    data = resp.json()
    if not data:
        return 0
    link = resp.headers.get("Link", "")
    match = re.search(r'<[^>]*[?&]page=(\d+)[^>]*>;\s*rel="last"', link)
    if not match:
        return len(data)
    last_page = int(match.group(1))
    if last_page <= 1:
        return len(data)
    last_data = _get(f"{BASE}/repos/{full_name}/releases",
                     params={"per_page": 100, "page": last_page})
    return (last_page - 1) * 100 + len(last_data)


def fetch_issues(full_name, limit=30):
    # state=all so closed issues (needed for resolution time) are included.
    # Pull requests also appear in the issues endpoint; they are filtered
    # out in transform.py by the presence of a "pull_request" key.
    data = _get(f"{BASE}/repos/{full_name}/issues",
                params={"state": "all", "per_page": min(limit, 100),
                        "sort": "created", "direction": "desc"})
    return [item for item in data if "pull_request" not in item][:limit]
