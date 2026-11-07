"""Run the OSS Pulse ETL pipeline.

Usage:
    python -m oss_pulse.etl                 # repos from repos.txt, DB data/oss_pulse.db
    python -m oss_pulse.etl --repos python/cpython,facebook/react
    python -m oss_pulse.etl --sample-data   # offline demo data only
    python -m oss_pulse.etl --db data/my.db

Exit code 0 on success. With --sample-data, or if the GitHub API fails
and --allow-sample-fallback is given, sample data is loaded instead and
the run is recorded with source="sample" so the dashboard can say so.
"""
import argparse
import os
import sys

import pandas as pd

from . import extract, load, transform
from .sample_data import SAMPLE_REPOS, sample_issues, sample_releases


import re

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def parse_repo_input(raw):
    """Accept 'owner/repo' or a full GitHub URL; return owner/repo or None.

    Strips the domain, a leading github.com/, a trailing slash, a
    trailing .git, and any path after the repo name (e.g. /releases).
    Also accepts the git@github.com:owner/repo form.
    """
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^git@github\.com:", "", text)
    text = re.sub(r"^https?://(www\.)?github\.com/", "", text)
    text = re.sub(r"^github\.com/", "", text)
    text = text.strip("/").removesuffix(".git")
    parts = text.split("/")
    if len(parts) < 2:
        return None
    candidate = f"{parts[0]}/{parts[1]}"
    return candidate if _REPO_RE.match(candidate) else None


def github_error_kind(exc):
    """Map a GitHub fetch failure to a specific, honest message kind."""
    status = None
    resp = getattr(exc, "response", None)
    if resp is not None:
        status = getattr(resp, "status_code", None)
    if status == 404:
        return "not_found"
    if status in (403, 429):
        return "rate_limit"
    return "unreachable"


def fetch_and_save_repo(full_name, db_path, releases_limit=30, issues_limit=30):
    """Fetch one repo from GitHub and save it; return (canonical, n_rel, n_iss).

    Raises on GitHub failure without touching existing data: the caller
    must catch it and report github_error_kind(exc), never wipe data.
    """
    conn = load.init_db(db_path)
    try:
        valid, _ = transform.transform_repo(extract.fetch_repo(full_name))
        if not valid:
            raise RuntimeError(f"GitHub returned no usable data for {full_name}")
        canonical = valid[0]["full_name"]
        rel_df, _ = transform.transform_releases(
            canonical, extract.fetch_releases(full_name, limit=releases_limit))
        iss_df, _ = transform.transform_issues(
            canonical, extract.fetch_issues(full_name, limit=issues_limit))
        # True total, cheaply via the Link header. If counting fails,
        # do NOT fail the whole add: fall back to the sample size, so
        # a total-count hiccup never blocks adding a repo.
        try:
            valid[0]["total_releases"] = extract.fetch_releases_total(full_name)
        except Exception:
            valid[0]["total_releases"] = len(rel_df)
        load.save_repo(conn, valid[0], rel_df, iss_df)
        return canonical, len(rel_df), len(iss_df)
    finally:
        conn.close()


def read_repos_file(path):
    repos = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#"):
            repos.append(line)
    return repos


def run(repos, db_path, use_sample=False, allow_fallback=False):
    conn = load.init_db(db_path)
    all_repos, all_releases, all_issues = [], [], []
    dropped = 0
    source = "sample" if use_sample else "github"

    for full_name in repos:
        if use_sample:
            raw_repo = next((r for r in SAMPLE_REPOS if r["full_name"] == full_name), None)
            if raw_repo is None:
                print(f"Skipping {full_name}: no sample data for it")
                continue
            valid, d = transform.transform_repo(raw_repo)
            all_repos.extend(valid)
            dropped += d
            rel_df, d = transform.transform_releases(full_name, sample_releases(full_name))
            iss_df, d2 = transform.transform_issues(full_name, sample_issues(full_name))
            dropped += d + d2
            all_releases.append(rel_df)
            all_issues.append(iss_df)
            continue
        try:
            valid, d = transform.transform_repo(extract.fetch_repo(full_name))
            all_repos.extend(valid)
            dropped += d
            # GitHub may redirect a repository to a new owner (facebook/react
            # is now react/react). Use the canonical name the API returns,
            # so repositories, releases and issues join on the same key.
            canonical = valid[0]["full_name"] if valid else full_name
            if canonical != full_name:
                print(f"Note: {full_name} is now {canonical} on GitHub; using the canonical name.")
            # Sample is now 30, same as the add-repo flow, so a repo's
            # detail lists do not shrink from 30 to 20 after an autosync.
            # The chart total is counted separately (Link header, below).
            rel_df, d = transform.transform_releases(canonical, extract.fetch_releases(full_name, limit=30))
            iss_df, d2 = transform.transform_issues(canonical, extract.fetch_issues(full_name))
            dropped += d + d2
            all_releases.append(rel_df)
            all_issues.append(iss_df)
            try:
                valid[0]["total_releases"] = extract.fetch_releases_total(full_name)
            except Exception:
                valid[0]["total_releases"] = len(rel_df)
            print(f"OK  {full_name}: {len(rel_df)} releases in sample, "
                  f"{valid[0]['total_releases']} total, {len(iss_df)} issues")
        except Exception as exc:  # noqa: BLE001 - report and decide on fallback
            print(f"FAILED {full_name}: {exc}")
            if not allow_fallback:
                raise
            print("Falling back to sample data for this run.")
            return run(repos, db_path, use_sample=True, allow_fallback=False)

    releases_df = pd.concat(all_releases, ignore_index=True) if all_releases else pd.DataFrame()
    issues_df = pd.concat(all_issues, ignore_index=True) if all_issues else pd.DataFrame()
    load.load_all(conn, all_repos, releases_df, issues_df, {
        "repos": ",".join(repos), "rows_repos": len(all_repos),
        "rows_releases": len(releases_df), "rows_issues": len(issues_df),
        "dropped": dropped, "source": source})
    conn.close()
    print(f"Done: {len(all_repos)} repos, {len(releases_df)} releases, "
          f"{len(issues_df)} issues, {dropped} invalid records dropped. "
          f"Source={source}. Database: {db_path}")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="OSS Pulse ETL")
    parser.add_argument("--repos", help="Comma-separated owner/repo list (default: repos.txt)")
    parser.add_argument("--db", default="data/oss_pulse.db", help="SQLite path")
    parser.add_argument("--sample-data", action="store_true", help="Use offline sample data")
    parser.add_argument("--allow-sample-fallback", action="store_true",
                        help="If the GitHub API fails, load sample data instead of failing")
    args = parser.parse_args(argv)
    repos = args.repos.split(",") if args.repos else read_repos_file("repos.txt")
    repos = [r.strip() for r in repos if r.strip()]
    os.makedirs(os.path.dirname(args.db) or ".", exist_ok=True)
    run(repos, args.db, use_sample=args.sample_data, allow_fallback=args.allow_sample_fallback)
    return 0


if __name__ == "__main__":
    sys.exit(main())
