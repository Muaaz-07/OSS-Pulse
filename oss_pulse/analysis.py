"""EDA: read the SQLite data back with pandas and summarise it.

Every number on the dashboard comes from these functions, so the
analysis can be checked against the database directly.
"""
import pandas as pd


def load_frames(conn):
    repos = pd.read_sql_query("SELECT * FROM repositories ORDER BY stargazers_count DESC", conn)
    releases = pd.read_sql_query("SELECT * FROM releases", conn)
    issues = pd.read_sql_query("SELECT * FROM issues", conn)
    runs = pd.read_sql_query("SELECT * FROM etl_runs ORDER BY id DESC LIMIT 1", conn)
    return repos, releases, issues, runs


def summary_by_repo(repos, releases, issues):
    """One row per repository: popularity, release activity, issue health."""
    rows = []
    for _, repo in repos.iterrows():
        name = repo["full_name"]
        rel = releases[releases["repo_full_name"] == name] if not releases.empty else releases
        iss = issues[issues["repo_full_name"] == name] if not issues.empty else issues
        closed = iss[iss["state"] == "closed"] if not iss.empty else iss
        _total = repo.get("total_releases")
        try:
            _total = int(_total) if _total is not None and not pd.isna(_total) else len(rel)
        except (TypeError, ValueError):
            _total = len(rel)
        rows.append({
            "full_name": name,
            "stars": int(repo["stargazers_count"]),
            "forks": int(repo["forks_count"]),
            "open_issues_api": int(repo["open_issues_count"]),
            "language": repo.get("language"),
            "releases_in_sample": len(rel),
            "total_releases": _total,
            "latest_release": str(rel["published_at"].max())[:10] if not rel.empty and rel["published_at"].notna().any() else "—",
            "issues_in_sample": len(iss),
            "open_in_sample": int((iss["state"] == "open").sum()) if not iss.empty else 0,
            "closed_in_sample": int((iss["state"] == "closed").sum()) if not iss.empty else 0,
            "avg_resolution_days": round(float(closed["resolution_days"].mean()), 1)
                if not closed.empty and closed["resolution_days"].notna().any() else None,
            "median_resolution_days": round(float(closed["resolution_days"].median()), 1)
                if not closed.empty and closed["resolution_days"].notna().any() else None,
        })
    df = pd.DataFrame(rows)
    # Keep "no value" as None, not NaN, so templates and JSON show a dash
    # instead of "nan" for repositories with no closed issues in the sample.
    for col in ("avg_resolution_days", "median_resolution_days"):
        df[col] = pd.Series([None if (isinstance(v, float) and v != v) else v for v in df[col]], dtype=object)
    return df


import re

_DEV_RE = re.compile(r"dev|alpha|beta|rc|pre|nightly", re.IGNORECASE)


def release_kind(tag, name, prerelease=False, draft=False):
    """Dev/Stable pill, computed from the release itself, never hand-set.

    Dev: a prerelease, a draft, or a tag/name containing dev, alpha,
    beta, rc, pre or nightly. Everything else is Stable.
    """
    if prerelease or draft:
        return "Dev"
    for text in (tag, name):
        # Tag/name can be NULL in the DB -> pandas NaN float, which is
        # truthy, so ``text or ""`` does NOT save us and _DEV_RE.search
        # raises TypeError. Coerce NaN/None to "" first.
        if text is None:
            continue
        try:
            if pd.isna(text):
                continue
        except (TypeError, ValueError):
            pass
        if _DEV_RE.search(str(text)):
            return "Dev"
    return "Stable"


def compute_insights(repo, releases, issues):
    """Insights computed from one repo's own data, for any repo.

    No per-repo text is written by hand anywhere: a newly added repo
    gets its insights from exactly these rules. Thin data is reported
    as thin data, never invented.
    """
    name = repo["full_name"]
    rel = releases[releases["repo_full_name"] == name] if not releases.empty else releases
    iss = issues[issues["repo_full_name"] == name] if not issues.empty else issues
    lines = [f"{int(repo['stargazers_count']):,} stars · {int(repo['forks_count']):,} forks"]
    if rel.empty:
        lines.append("No releases through the GitHub Releases API.")
    else:
        rel = rel.copy()
        rel["published_at"] = pd.to_datetime(rel["published_at"], errors="coerce", utc=True)
        rel = rel.sort_values("published_at")
        gaps = rel["published_at"].diff().dt.total_seconds().div(86400).dropna()
        latest = rel.iloc[-1]
        lines.append(f"{len(rel)} releases · latest {latest['tag_name']} ({str(latest['published_at'])[:10]})")
        if len(gaps):
            avg, med = float(gaps.mean()), float(gaps.median())
            lines.append(f"Average gap between releases: {avg:.1f} days (median {med:.1f})")
            if med == 0:
                lines.append("Releases often land in same-day batches.")
            elif avg < 7:
                lines.append("Steady release cadence.")
            else:
                lines.append("Release cadence is uneven, with longer gaps.")
        pre = int(rel["prerelease"].sum()) if "prerelease" in rel else 0
        if pre:
            lines.append(f"Prereleases: {pre} of {len(rel)} ({100 * pre // len(rel)}%).")
    if iss.empty:
        lines.append("No issues in the fetched sample.")
    else:
        open_n = int((iss["state"] == "open").sum())
        closed = iss[iss["state"] == "closed"]
        lines.append(f"Issues in sample: {len(iss)} ({open_n} open, {len(closed)} closed)")
        if len(closed) == 0:
            lines.append("No closed issues in sample — no average close time.")
        elif len(closed) == 1:
            lines.append("Only 1 closed issue in sample — average is not meaningful.")
        elif closed["resolution_days"].notna().any():
            avg = float(closed["resolution_days"].mean())
            if avg < 1:
                lines.append(f"Average close time: {avg:.2f} days (~{avg * 24:.1f} hours).")
            else:
                lines.append(f"Average close time: {avg:.1f} days.")
    return lines


def top10_plus_others(rows, label_col, value_col):
    """Cap a chart at Top 10 plus an 'Others' aggregate, labelled as such."""
    if rows.empty:
        return rows
    top = rows.sort_values(value_col, ascending=False).head(10).copy()
    rest = rows[~rows.index.isin(top.index)]
    if not rest.empty:
        other = {label_col: "Others", value_col: rest[value_col].sum()}
        top = pd.concat([top, pd.DataFrame([other])], ignore_index=True)
    return top


def releases_per_year(releases):
    if releases.empty or "published_year" not in releases:
        return pd.DataFrame(columns=["repo_full_name", "published_year", "count"])
    df = releases.dropna(subset=["published_year"]).copy()
    df["published_year"] = df["published_year"].astype(int)
    return df.groupby(["repo_full_name", "published_year"]).size().reset_index(name="count")
