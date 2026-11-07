"""Load: store the transformed data in SQLite.

Schema:
- repositories(full_name PK, name, owner, description, html_url,
               stargazers_count, forks_count, open_issues_count,
               created_at, updated_at, language, total_releases)
  total_releases is the TRUE total from the GitHub pagination Link
  header (see extract.fetch_releases_total), not the size of the
  latest-30 sample stored in releases. Charts use it; detail lists
  still use the sample rows. NULL/old DBs fall back to the sample
  size wherever the total is read, never to a crash.
- releases(repo_full_name, tag_name, name, published_at, html_url,
           draft, prerelease)  — PK (repo_full_name, tag_name)
- issues(repo_full_name, number, title, state, created_at, closed_at,
         html_url, comments, resolution_days) — PK (repo_full_name, number)
- etl_runs(id PK, run_at, repos, rows_repos, rows_releases, rows_issues,
           dropped, source) — one row per ETL run, so the dashboard can
  show when the data was last refreshed and from where.
"""
import sqlite3
from datetime import datetime, timezone


def init_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS repositories (
            full_name TEXT PRIMARY KEY, name TEXT, owner TEXT,
            description TEXT, html_url TEXT, stargazers_count INTEGER,
            forks_count INTEGER, open_issues_count INTEGER,
            created_at TEXT, updated_at TEXT, language TEXT,
            total_releases INTEGER);
        CREATE TABLE IF NOT EXISTS releases (
            repo_full_name TEXT, tag_name TEXT, name TEXT,
            published_at TEXT, html_url TEXT, draft INTEGER,
            prerelease INTEGER, published_year INTEGER,
            PRIMARY KEY (repo_full_name, tag_name));
        CREATE TABLE IF NOT EXISTS issues (
            repo_full_name TEXT, number INTEGER, title TEXT, state TEXT,
            created_at TEXT, closed_at TEXT, html_url TEXT,
            comments INTEGER, resolution_days REAL,
            PRIMARY KEY (repo_full_name, number));
        CREATE TABLE IF NOT EXISTS etl_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT,
            repos TEXT, rows_repos INTEGER, rows_releases INTEGER,
            rows_issues INTEGER, dropped INTEGER, source TEXT);
    """)
    conn.commit()
    # Existing DBs created before total_releases existed: add the
    # column in place, so an update never wipes a user's data.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(repositories)")]
    if "total_releases" not in cols:
        conn.execute("ALTER TABLE repositories ADD COLUMN total_releases INTEGER")
        conn.commit()
    return conn


def _total_releases(repo, releases_df):
    """True total if the caller supplied one, else the sample size."""
    total = repo.get("total_releases") if isinstance(repo, dict) else None
    if total is not None:
        try:
            return int(total)
        except (TypeError, ValueError):
            pass
    return len(releases_df) if releases_df is not None and not releases_df.empty else 0


def save_repo(conn, repo, releases_df, issues_df):
    """Save one repo's data without recording a full ETL run.

    Used by the web UI's add-repo flow, so adding one repo does not
    rewrite the 'last synced' time for every other repo.
    """
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO repositories VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (repo["full_name"], repo["name"], repo["owner"],
         repo.get("description"), _to_text(repo["html_url"]),
         repo["stargazers_count"], repo["forks_count"],
         repo["open_issues_count"], _to_text(repo["created_at"]),
         _to_text(repo["updated_at"]), repo.get("language"),
         _total_releases(repo, releases_df)))
    if releases_df is not None and not releases_df.empty:
        for row in releases_df.to_dict("records"):
            cur.execute(
                "INSERT OR REPLACE INTO releases VALUES (?,?,?,?,?,?,?,?)",
                (row["repo_full_name"], row["tag_name"], row.get("name"),
                 _to_text(row.get("published_at")), _to_text(row["html_url"]),
                 int(bool(row.get("draft"))), int(bool(row.get("prerelease"))),
                 int(row["published_year"]) if row.get("published_year") == row.get("published_year") else None))
    if issues_df is not None and not issues_df.empty:
        for row in issues_df.to_dict("records"):
            res = row.get("resolution_days")
            cur.execute(
                "INSERT OR REPLACE INTO issues VALUES (?,?,?,?,?,?,?,?,?)",
                (row["repo_full_name"], int(row["number"]), row["title"],
                 row["state"], _to_text(row.get("created_at")),
                 _to_text(row.get("closed_at")), _to_text(row["html_url"]),
                 int(row.get("comments", 0)),
                 float(res) if res == res and res is not None else None))
    conn.commit()


def delete_repo(conn, full_name):
    cur = conn.cursor()
    cur.execute("DELETE FROM releases WHERE repo_full_name = ?", (full_name,))
    cur.execute("DELETE FROM issues WHERE repo_full_name = ?", (full_name,))
    cur.execute("DELETE FROM repositories WHERE full_name = ?", (full_name,))
    conn.commit()
    return cur.rowcount >= 0


def _to_text(value):
    if value is None:
        return None
    return str(value)


def load_all(conn, repos, releases_df, issues_df, run_meta):
    cur = conn.cursor()
    # A refresh replaces each synced repo's sample, it does not pile
    # new rows on top of old ones. This runs only after every fetch in
    # the run has succeeded (see etl.run), so a failed run never wipes
    # data: load_all is simply never reached.
    for repo in repos:
        cur.execute("DELETE FROM releases WHERE repo_full_name = ?", (repo["full_name"],))
        cur.execute("DELETE FROM issues WHERE repo_full_name = ?", (repo["full_name"],))
    for repo in repos:
        # Fallback total = this repo's sample size, if the ETL run did
        # not supply a Link-header total (e.g. sample-data mode).
        sample_n = 0
        if releases_df is not None and not releases_df.empty:
            sample_n = int((releases_df["repo_full_name"] == repo["full_name"]).sum())
        total = repo.get("total_releases")
        if total is None:
            total = sample_n
        cur.execute(
            "INSERT OR REPLACE INTO repositories VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (repo["full_name"], repo["name"], repo["owner"],
             repo.get("description"), _to_text(repo["html_url"]),
             repo["stargazers_count"], repo["forks_count"],
             repo["open_issues_count"], _to_text(repo["created_at"]),
             _to_text(repo["updated_at"]), repo.get("language"), int(total)))
    if releases_df is not None and not releases_df.empty:
        for row in releases_df.to_dict("records"):
            cur.execute(
                "INSERT OR REPLACE INTO releases VALUES (?,?,?,?,?,?,?,?)",
                (row["repo_full_name"], row["tag_name"], row.get("name"),
                 _to_text(row.get("published_at")), _to_text(row["html_url"]),
                 int(bool(row.get("draft"))), int(bool(row.get("prerelease"))),
                 int(row["published_year"]) if row.get("published_year") == row.get("published_year") else None))
    if issues_df is not None and not issues_df.empty:
        for row in issues_df.to_dict("records"):
            res = row.get("resolution_days")
            cur.execute(
                "INSERT OR REPLACE INTO issues VALUES (?,?,?,?,?,?,?,?,?)",
                (row["repo_full_name"], int(row["number"]), row["title"],
                 row["state"], _to_text(row.get("created_at")),
                 _to_text(row.get("closed_at")), _to_text(row["html_url"]),
                 int(row.get("comments", 0)),
                 float(res) if res == res and res is not None else None))
    cur.execute(
        "INSERT INTO etl_runs (run_at, repos, rows_repos, rows_releases, rows_issues, dropped, source)"
        " VALUES (?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), run_meta["repos"],
         run_meta["rows_repos"], run_meta["rows_releases"],
         run_meta["rows_issues"], run_meta["dropped"], run_meta["source"]))
    conn.commit()
