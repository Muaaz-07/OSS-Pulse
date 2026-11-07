"""Sample data, used ONLY when the GitHub API cannot be reached.

This exists so the dashboard and the analysis can still be demonstrated
offline (for example in a viva with no network). Sample rows are marked
with source="sample" in etl_runs and the dashboard shows a clear
"SAMPLE DATA" banner when the latest run used them. Sample data must
never be presented as real GitHub data.
"""
SAMPLE_REPOS = [
    {"full_name": "python/cpython", "name": "cpython", "owner": "python",
     "description": "The Python programming language", "html_url": "https://github.com/python/cpython",
     "stargazers_count": 65000, "forks_count": 32000, "open_issues_count": 9000,
     "created_at": "2017-02-10T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z", "language": "Python"},
    {"full_name": "facebook/react", "name": "react", "owner": "facebook",
     "description": "The library for web and native user interfaces", "html_url": "https://github.com/facebook/react",
     "stargazers_count": 230000, "forks_count": 48000, "open_issues_count": 1200,
     "created_at": "2013-05-24T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z", "language": "JavaScript"},
    {"full_name": "microsoft/vscode", "name": "vscode", "owner": "microsoft",
     "description": "Visual Studio Code", "html_url": "https://github.com/microsoft/vscode",
     "stargazers_count": 170000, "forks_count": 30000, "open_issues_count": 6000,
     "created_at": "2015-09-03T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z", "language": "TypeScript"},
]


def sample_releases(full_name):
    # (tag, published date) — a small, clearly-labelled illustrative set.
    tags = [("v1.0", "2024-03-01"), ("v1.1", "2024-09-01"), ("v2.0", "2025-03-01"),
            ("v2.1", "2025-09-01")]
    return [{"tag_name": t, "name": t, "published_at": f"{d}T00:00:00Z",
             "html_url": f"https://github.com/{full_name}/releases/tag/{t}",
             "draft": False, "prerelease": False} for t, d in tags]


def sample_issues(full_name):
    rows = []
    for i in range(1, 11):
        closed = i % 3 != 0
        rows.append({
            "number": i, "title": f"Sample issue {i} for {full_name}",
            "state": "closed" if closed else "open",
            "created_at": f"2025-0{(i % 9) + 1}-10T00:00:00Z",
            "closed_at": f"2025-0{(i % 9) + 1}-1{5 + (i % 4)}T00:00:00Z" if closed else None,
            "html_url": f"https://github.com/{full_name}/issues/{i}",
            "comments": i % 5})
    return rows
