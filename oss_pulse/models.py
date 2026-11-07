"""Pydantic models: the validation step of the ETL pipeline.

Every record coming from the GitHub API is validated through one of these
models before it reaches pandas or SQLite. A record that fails validation
is dropped and counted, never silently stored with a wrong type.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator


class Repository(BaseModel):
    full_name: str
    name: str
    owner: str
    description: Optional[str] = None
    html_url: HttpUrl
    stargazers_count: int = Field(ge=0)
    forks_count: int = Field(ge=0)
    open_issues_count: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime
    language: Optional[str] = None


class Release(BaseModel):
    repo_full_name: str
    tag_name: str
    name: Optional[str] = None
    published_at: Optional[datetime] = None
    html_url: HttpUrl
    draft: bool = False
    prerelease: bool = False

    @field_validator("tag_name", mode="before")
    @classmethod
    def tag_must_be_text(cls, v):
        return str(v) if v is not None else ""


class Issue(BaseModel):
    repo_full_name: str
    number: int = Field(ge=0)
    title: str
    state: str  # "open" or "closed"
    created_at: datetime
    closed_at: Optional[datetime] = None
    html_url: HttpUrl
    comments: int = Field(ge=0)

    @field_validator("state")
    @classmethod
    def state_must_be_known(cls, v):
        if v not in ("open", "closed"):
            raise ValueError(f"unknown issue state: {v}")
        return v
