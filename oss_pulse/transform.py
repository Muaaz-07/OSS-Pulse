"""Transform: validate with Pydantic, then clean with pandas.

Steps, in order:
1. Map each raw GitHub record to the fields our Pydantic models expect.
2. Validate. Invalid records are dropped and counted.
3. Load the valid records into pandas, parse dates, drop exact
   duplicates, and derive the fields the analysis needs:
   - issues: resolution_days = closed_at - created_at (closed only)
   - releases: published year, for the releases-per-year view
"""
import pandas as pd

from .models import Issue, Release, Repository


def _validate(model, records):
    valid, dropped = [], 0
    for rec in records:
        try:
            valid.append(model(**rec).model_dump())
        except Exception:
            dropped += 1
    return valid, dropped


def transform_repo(raw):
    rec = {
        "full_name": raw["full_name"],
        "name": raw["name"],
        "owner": raw["owner"]["login"] if isinstance(raw["owner"], dict) else raw["owner"],
        "description": raw.get("description"),
        "html_url": raw["html_url"],
        "stargazers_count": raw["stargazers_count"],
        "forks_count": raw["forks_count"],
        "open_issues_count": raw["open_issues_count"],
        "created_at": raw["created_at"],
        "updated_at": raw["updated_at"],
        "language": raw.get("language"),
    }
    valid, dropped = _validate(Repository, [rec])
    return valid, dropped


def transform_releases(full_name, raw_list):
    records = [{
        "repo_full_name": full_name,
        "tag_name": item.get("tag_name"),
        "name": item.get("name"),
        "published_at": item.get("published_at"),
        "html_url": item["html_url"],
        "draft": item.get("draft", False),
        "prerelease": item.get("prerelease", False),
    } for item in raw_list]
    valid, dropped = _validate(Release, records)
    df = pd.DataFrame(valid)
    if not df.empty:
        df["published_at"] = pd.to_datetime(df["published_at"], errors="coerce", utc=True)
        df["published_year"] = df["published_at"].dt.year
        df = df.drop_duplicates(subset=["repo_full_name", "tag_name"])
    return df, dropped


def transform_issues(full_name, raw_list):
    records = [{
        "repo_full_name": full_name,
        "number": item["number"],
        "title": item["title"],
        "state": item["state"],
        "created_at": item["created_at"],
        "closed_at": item.get("closed_at"),
        "html_url": item["html_url"],
        "comments": item.get("comments", 0),
    } for item in raw_list]
    valid, dropped = _validate(Issue, records)
    df = pd.DataFrame(valid)
    if not df.empty:
        df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce", utc=True)
        df["closed_at"] = pd.to_datetime(df["closed_at"], errors="coerce", utc=True)
        df["resolution_days"] = (df["closed_at"] - df["created_at"]).dt.total_seconds() / 86400
        df = df.drop_duplicates(subset=["repo_full_name", "number"])
    return df, dropped
