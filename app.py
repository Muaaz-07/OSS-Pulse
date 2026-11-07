"""OSS Pulse web dashboard (Flask).

Run from the project root:
    python -m oss_pulse.etl
    python app.py
Then open http://127.0.0.1:5000/

Pages:
  /            — dashboard: repo cards, charts, last-synced status
  /repositories — all tracked repos as cards, searchable
  /releases    — latest releases with Dev/Stable pills, searchable
  /issues      — individual recent issues, searchable by repo or title
  /repo/<owner>/<name> — one repo: computed insights, releases, issues
  /api/summary — the same summary as JSON

Autosync: on open, if the last ETL run is over 24 hours old (or there
is none), a background refresh is started; the page never waits for
it and existing data is never wiped on failure. "Sync now" runs the
same refresh synchronously. Add/delete repos work server-side, so
they work in a deployed Flask app (unlike a static preview, where a
browser fetch to GitHub can be blocked).

Charts are drawn from SQLite with matplotlib and are capped at the
Top 10 repos plus an "Others" aggregate, labelled "Top 10 of N", so
hundreds of tracked repos stay readable.
"""
import hashlib
import io
import os
import math
import sqlite3
import threading
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, Response, url_for)

from oss_pulse.analysis import (compute_insights, load_frames, release_kind,
                                releases_per_year, summary_by_repo,
                                top10_plus_others)
from oss_pulse.etl import (fetch_and_save_repo, github_error_kind,
                           parse_repo_input, read_repos_file, run)

DB_PATH = "data/oss_pulse.db"
REPOS_FILE = "repos.txt"
SYNC_INTERVAL_HOURS = 24
PAGE_SIZE = 9  # repo cards: 3 per row on desktop
LIST_PAGE_SIZE = 30
# Latest releases on /releases is beautification only: no scroll, no pager.
# User asked for 8 at most: the card has max-height:none / overflow
# visible in CSS, so it grows to fit all 8 rows (~450px) instead of
# scrolling or paginating. Full lists live on repo detail pages via
# View all (in-place expand).
LATEST_RELEASES_CAP = 8

app = Flask(__name__)
app.secret_key = "oss-pulse-local"  # flash messages only; not auth
_sync_lock = threading.Lock()
_sync_state = {"running": False, "last_attempt": None, "message": None}
# Preview palette, verbatim from the exported preview (no blue repo
# colour; the primary accent is emerald everywhere). The three starting
# repos keep the exact colours the preview gave them.
PALETTE = ["#059669", "#0891b2", "#d97706", "#7c3aed", "#e11d48",
           "#65a30d", "#db2777", "#0d9488"]
PREVIEW_COLORS = {"python/cpython": "#d97706", "react/react": "#0891b2",
                  "microsoft/vscode": "#059669"}


def repo_color(full_name):
    """Deterministic per-repo colour, so newly added repos get one too.

    hashlib, not hash(): Python's hash() is randomised per process, so
    the old version changed a repo's colour on every restart.
    """
    if full_name in PREVIEW_COLORS:
        return PREVIEW_COLORS[full_name]
    digest = hashlib.md5(str(full_name).encode("utf-8")).hexdigest()
    return PALETTE[int(digest[:8], 16) % len(PALETTE)]


# ---- Preview-faithful view data and charts (SVG/CSS bars, not PNGs) ----
# These mirror the exported preview's JS: same bar-row / stack-track /
# vBarChart geometry, same Top 10 + Others rule, same Dev/Stable rule
# (via analysis.release_kind). Templates render the HTML returned here.
import html as _html


def _safe_str(value):
    """Coerce a DB value to str, turning None/NaN/float into ''.

    pandas returns float NaN for missing language/description/etc.;
    NaN is truthy, so ``(value or '').lower()`` crashes with
    AttributeError: 'float' object has no attribute 'lower'.
    """
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)


def _safe_lower(value):
    return _safe_str(value).lower()


def _pct(part, total):
    return round(part / total * 100) if total else 0


def _fmt_int(n):
    return f"{int(n):,}" if n is not None else "—"


def _top10_items(items, metric):
    """items: list of dicts. Returns (shown, others_dict_or_None, total)."""
    ordered = sorted(items, key=metric, reverse=True)
    if len(ordered) <= 10:
        return ordered, None, len(ordered)
    shown, rest = ordered[:10], ordered[10:]
    return shown, {"count": len(rest), "value": sum(metric(r) for r in rest)}, len(ordered)


def _top_note(total):
    if total > 10:
        return (f'<p class="small" style="margin:0 0 6px">Top 10 of {total} '
                f"— the rest are grouped as Others.</p>")
    return ""


def _v_bar_svg(items):
    """Preview vBarChart geometry, with rotated x-labels so long names
    (PaperWM, morphe-patches, PixelPlayerOSS, …) cannot overlap.

    Bars keep the preview geometry (520 wide, base 168, top 16); the
    SVG is taller (260) to give rotated labels room. Labels are
    rotated -50° around their baseline point: parallel rotated labels
    are separated by slot*sin(50°) ≈ 32px at 11 bars, well over the
    11px font height, so neighbours cannot collide. The full short
    name is kept (no truncation to 11 chars) and also in a <title>
    tooltip for screen readers / hover.
    """
    if not items:
        return '<div class="empty">No repositories match.</div>'
    # Dashboard releases chart uses the TRUE total (Link-header count),
    # not the latest-30 sample size: otherwise every active repo flatlines
    # at 30. Detail lists and insights still use the sample rows.
    shown, others, total = _top10_items(items, lambda r: r.get("total_releases", r["releases"]))
    bars = [{"name": _short_label(r["full_name"]), "value": r.get("total_releases", r["releases"]),
             "color": repo_color(r["full_name"])} for r in shown]
    if others:
        bars.append({"name": f"Others ({others['count']})",
                     "value": others["value"], "color": "#94a39c"})
    mx = max([b["value"] for b in bars] + [1])
    W, H, base, top = 520, 260, 168, 16
    plot_h = base - top
    slot = (W - 52) / len(bars)
    bw = min(52, slot * 0.45)
    out = [_top_note(total),
           f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="Releases by repository">',
           f'<line x1="40" y1="{base}" x2="{W - 6}" y2="{base}" stroke="#cbd5d1"/>']
    for i, b in enumerate(bars):
        cx = 40 + slot * i + slot / 2
        h = max(b["value"] / mx * plot_h, 3 if b["value"] > 0 else 0)
        y = base - h
        if b["value"] == 0:
            out.append(f'<rect x="{cx - bw / 2}" y="{base - 3}" width="{bw}" '
                       f'height="3" fill="#94a39c"/>'
                       f'<text class="val" x="{cx}" y="{base - 10}" '
                       f'text-anchor="middle">0</text>')
        else:
            out.append(f'<rect x="{cx - bw / 2}" y="{y}" width="{bw}" '
                       f'height="{h}" rx="4" fill="{b["color"]}"/>'
                       f'<text class="val" x="{cx}" y="{y - 7}" '
                       f'text-anchor="middle">{b["value"]}</text>')
        nm = _html.escape(b["name"])
        out.append(f'<text class="axis" x="{cx}" y="{base + 16}" '
                   f'text-anchor="end" transform="rotate(-50 {cx} {base + 16})">'
                   f'<title>{nm}</title>{nm}</text>')
    out.append("</svg>")
    return "".join(out)


def _bar_row(name, value, max_val, label, color=None):
    w = round(value / max_val * 100) if max_val else 0
    style = f' style="background:{color}"' if color else ""
    return (f'<div class="bar-row"><span>{_html.escape(str(name))}</span>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{w}%'
            f'{(";" + "background:" + color) if color else ""}"></div></div>'
            f'<b class="mono" style="font-weight:600">{_html.escape(str(label))}</b></div>')


def _stack_row(name, open_n, closed_n, color, max_total):
    tot = open_n + closed_n
    w = tot / max_total * 100 if max_total else 0
    ow = open_n / tot * 100 if tot else 0
    return (f'<div style="margin:14px 0"><div style="display:flex;justify-content:'
            f'space-between;font-size:13.5px;margin-bottom:5px">'
            f'<span>{_html.escape(str(name))}</span>'
            f'<span style="color:var(--muted)">{open_n} open · {closed_n} closed</span></div>'
            f'<div class="stack-track" style="width:{w}%">'
            f'<div style="width:{ow}%;background:{color}"></div>'
            f'<div style="width:{100 - ow}%;background:var(--grey)"></div></div></div>')


def _enrich(repos, releases, issues):
    """One preview-shaped dict per repo, computed from DB data only."""
    out = []
    for _, repo in repos.iterrows():
        name = repo["full_name"]
        rel = releases[releases["repo_full_name"] == name] if not releases.empty else releases
        iss = issues[issues["repo_full_name"] == name] if not issues.empty else issues
        rel_rows, iss_rows = [], []
        avg_gap = median_gap = rel_from = rel_to = None
        months = None
        if not rel.empty:
            rel = rel.copy()
            rel["published_at"] = pd.to_datetime(rel["published_at"], errors="coerce", utc=True)
            rel = rel.sort_values("published_at")
            diffs = rel["published_at"].diff().dt.total_seconds().div(86400).dropna()
            if len(diffs):
                avg_gap, median_gap = round(float(diffs.mean()), 1), round(float(diffs.median()), 1)
            rel_from = str(rel.iloc[0]["published_at"])[:10]
            rel_to = str(rel.iloc[-1]["published_at"])[:10]
            by_month = {}
            for _, r in rel.iterrows():
                dt = pd.to_datetime(r["published_at"], utc=True)
                if pd.isna(dt):
                    continue
                key = (dt.year, dt.month)
                by_month[key] = by_month.get(key, 0) + 1
            years = {y for y, _ in by_month}
            months = []
            for (y, m) in sorted(by_month):
                label = pd.Timestamp(y, m, 1).strftime("%b")
                if len(years) > 1:
                    label += f" {y}"
                months.append([label, by_month[(y, m)]])
            rel_desc = rel.sort_values("published_at", ascending=False)
            for _, r in rel_desc.iterrows():
                tag = _safe_str(r["tag_name"])
                rel_rows.append({"tag": tag, "name": _safe_str(r.get("name")),
                                 "date": str(r["published_at"])[:10],
                                 "prerelease": bool(r.get("prerelease")),
                                 "draft": bool(r.get("draft")),
                                 "html_url": _safe_str(r.get("html_url")) or
                                     f"https://github.com/{name}/releases/tag/{tag}",
                                 "kind": release_kind(r.get("tag_name"), r.get("name"),
                                                      bool(r.get("prerelease")),
                                                      bool(r.get("draft")))})
        if not iss.empty:
            iss = iss.copy()
            iss["created_at"] = pd.to_datetime(iss["created_at"], errors="coerce", utc=True)
            iss = iss.sort_values("created_at", ascending=False)
            for _, r in iss.iterrows():
                num = r.get("number")
                iss_rows.append({"title": _safe_str(r["title"]), "state": r["state"],
                                 "date": str(r["created_at"])[:10],
                                 "number": num,
                                 "html_url": _safe_str(r.get("html_url")) or
                                     (f"https://github.com/{name}/issues/{int(num)}"
                                      if num is not None and not (isinstance(num, float) and pd.isna(num))
                                      else f"https://github.com/{name}/issues")})
        closed = iss[iss["state"] == "closed"] if not iss.empty else iss
        close_avg = None
        if not closed.empty and closed["resolution_days"].notna().any():
            close_avg = float(closed["resolution_days"].mean())
        if len(closed) < 2 or close_avg is None:
            close_avg_text = "—"
        elif close_avg < 1 / 24:
            close_avg_text = f"{max(1, round(close_avg * 1440))} min"
        elif close_avg < 1:
            close_avg_text = f"{round(close_avg * 24, 1)} hours"
        else:
            close_avg_text = f"{round(close_avg, 1)} days"
        _total_rel = repo.get("total_releases")
        try:
            _total_rel = int(_total_rel) if _total_rel is not None and not pd.isna(_total_rel) else len(rel_rows)
        except (TypeError, ValueError):
            _total_rel = len(rel_rows)
        out.append({
            "full_name": name, "desc": _safe_str(repo.get("description")),
            "html_url": _safe_str(repo.get("html_url")) or f"https://github.com/{name}",
            "language": _safe_str(repo.get("language")) or None, "stars": int(repo["stargazers_count"]),
            "forks": int(repo["forks_count"]), "open_total": int(repo["open_issues_count"]),
            "releases": len(rel_rows), "total_releases": _total_rel,
            "prereleases": sum(1 for r in rel_rows if r["prerelease"]),
            "drafts": sum(1 for r in rel_rows if r["draft"]),
            "latest_tag": rel_rows[0]["tag"] if rel_rows else None,
            "latest_date": rel_rows[0]["date"] if rel_rows else None,
            "latest_pre": rel_rows[0]["prerelease"] if rel_rows else False,
            "avg_gap": avg_gap, "median_gap": median_gap,
            "rel_from": rel_from, "rel_to": rel_to, "months": months,
            "releases_list": rel_rows, "issues_list": iss_rows,
            "issues": len(iss_rows), "open": int((iss["state"] == "open").sum()) if not iss.empty else 0,
            "closed": int((iss["state"] == "closed").sum()) if not iss.empty else 0,
            "close_avg_days": close_avg, "close_avg_text": close_avg_text,
            "color": repo_color(name),
        })
    return out


def _totals(enriched):
    return {"repos": len(enriched),
            "releases": sum(r["releases"] for r in enriched),
            "issues": sum(r["issues"] for r in enriched),
            "open": sum(r["open"] for r in enriched),
            "closed": sum(r["closed"] for r in enriched)}


def _short_label(full_name):
    """Short chart label: the repo name, not the full owner/name."""
    return str(full_name).split("/")[-1] if "/" in str(full_name) else str(full_name)


def _style_axes(ax):
    """One clean chart style: no overlapping labels, room for labels."""
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(labelsize=9)
    ax.set_axisbelow(True)
    ax.grid(axis="y", color="#e5e7eb", linewidth=0.6, linestyle="--")
    ax.set_facecolor("white")
    for label in ax.get_xticklabels():
        label.set_rotation(28)
        label.set_ha("right")


def _png(fig):
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.35,
                facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return Response(buf.read(), mimetype="image/png")


def get_data():
    conn = sqlite3.connect(DB_PATH)
    try:
        # Migrate old DBs in place: add total_releases if missing, so
        # an update never wipes a user's repos. Totals stay NULL until
        # the next sync/add fetches them from GitHub; until then the
        # chart falls back to the sample size (see _enrich).
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(repositories)")]
            if cols and "total_releases" not in cols:
                conn.execute("ALTER TABLE repositories ADD COLUMN total_releases INTEGER")
                conn.commit()
        except Exception:
            pass
        repos, releases, issues, runs = load_frames(conn)
    finally:
        conn.close()
    summary = summary_by_repo(repos, releases, issues) if not repos.empty else pd.DataFrame()
    latest = runs.iloc[0] if not runs.empty else None
    return repos, releases, issues, summary, latest


def _last_run_dt(latest):
    if latest is None:
        return None
    try:
        return datetime.fromisoformat(str(latest["run_at"]).replace("Z", "+00:00"))
    except ValueError:
        return None


def _sync_due(latest):
    last = _last_run_dt(latest)
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() > SYNC_INTERVAL_HOURS * 3600


def _reconcile_tracked_repos():
    """Keep added repos tracked across restarts and across updates.

    Adding a repo writes it to both the DB and repos.txt, so a restart
    of the same folder already kept it. The gap was an update: unzipping
    a new version over the folder replaces repos.txt (and the DB) with
    the shipped 3-repo versions, silently dropping added repos. So,
    before any sync, any repo present in the DB but missing from
    repos.txt is appended back to repos.txt -- autosync then keeps
    refreshing it instead of forgetting it. This cannot recover repos
    if the DB itself was overwritten by an update zip; for that, see
    the README update note: when updating, do not overwrite
    data/oss_pulse.db or repos.txt.
    """
    try:
        if not os.path.exists(DB_PATH):
            return
        conn = sqlite3.connect(DB_PATH)
        try:
            db_repos = [r[0] for r in conn.execute(
                "SELECT full_name FROM repositories")]
        finally:
            conn.close()
        existing = read_repos_file(REPOS_FILE)
        # facebook/react in repos.txt is the same repo as react/react
        # in the DB (GitHub redirect); do not re-add it as a duplicate.
        known = set(existing) | ({"react/react"} if "facebook/react" in existing else set())
        missing = [r for r in db_repos if r not in known]
        if missing:
            with open(REPOS_FILE, "a", encoding="utf-8") as f:
                for r in missing:
                    f.write(r + "\n")
    except Exception:
        pass


def _run_sync():
    _reconcile_tracked_repos()
    with _sync_lock:
        if _sync_state["running"]:
            return
        _sync_state["running"] = True
        _sync_state["last_attempt"] = datetime.now(timezone.utc).isoformat()
    try:
        run(read_repos_file(REPOS_FILE), DB_PATH, allow_fallback=False)
        _sync_state["message"] = "Synced."
    except Exception as exc:  # noqa: BLE001 - keep existing data, report only
        _sync_state["message"] = f"Sync failed, existing data kept: {exc}"
    finally:
        with _sync_lock:
            _sync_state["running"] = False


def maybe_autosync(latest, repos=None):
    """Start a background refresh on open if one is due (never blocks).

    Also triggers a refresh when repos.txt lists a repo that is not in
    the DB yet (e.g. repos.txt survived an update but the DB did not),
    instead of waiting up to 24 hours for that repo to reappear.
    """
    due = _sync_due(latest)
    if not due and repos is not None and not repos.empty:
        tracked = read_repos_file(REPOS_FILE)
        canonical = {"react/react" if t == "facebook/react" else t for t in tracked}
        db_names = set(repos["full_name"])
        due = bool(canonical - db_names)
    if due and not _sync_state["running"]:
        threading.Thread(target=_run_sync, daemon=True).start()
        return True
    return False


def _filter_repos(summary, q):
    if not q or summary.empty:
        return summary
    q = q.lower()
    names = summary["full_name"].astype(str).str.lower()
    langs = summary["language"].astype(str).replace({"nan": ""}).str.lower()
    return summary[names.str.contains(q, na=False) | langs.str.contains(q, na=False)]


def _paginate(df, page, size):
    total = len(df)
    pages = max(1, math.ceil(total / size)) if total else 1
    page = max(1, min(page, pages))
    return df.iloc[(page - 1) * size: page * size], page, pages, total


def _page_ctx(q, page, pages, total):
    return {"q": q or "", "page": page, "pages": pages, "total": total}


@app.route("/")
def index():
    repos, releases, issues, summary, latest = get_data()
    syncing = maybe_autosync(latest, repos)
    q = (request.args.get("q") or "").strip()
    enriched = _enrich(repos, releases, issues)
    ql = q.lower()
    filtered = [r for r in enriched if not ql or ql in _safe_lower(r["full_name"])
                or ql in _safe_lower(r["language"])
                or _safe_lower(r["latest_tag"]).find(ql) != -1]
    page = request.args.get("page", 1, type=int)
    pages = max(1, math.ceil(len(filtered) / PAGE_SIZE)) if filtered else 1
    page = max(1, min(page, pages))
    page_rows = filtered[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    # Dashboard charts, preview style.
    dash_rel_svg = _v_bar_svg(filtered)
    shown, others, tot_n = _top10_items(filtered, lambda r: r["issues"])
    max_iss = max([r["issues"] for r in shown] + ([others["value"]] if others else []) + [1])
    dash_iss_bars = _top_note(tot_n) + "".join(
        _bar_row(_short_label(r["full_name"]), r["issues"], max_iss,
                 str(r["issues"]), r["color"]) for r in shown)
    if others:
        dash_iss_bars += _bar_row(f"Others ({others['count']})", others["value"],
                                  max_iss, str(others["value"]), "#94a39c")
    return render_template("index.html", summary=page_rows,
                           filtered_repos=filtered,
                           all_summary=enriched, totals=_totals(enriched),
                           latest=latest, syncing=syncing, sync_state=_sync_state,
                           repo_color=repo_color, dash_rel_svg=dash_rel_svg,
                           dash_iss_bars=dash_iss_bars,
                           **_page_ctx(q, page, pages, len(filtered)))


@app.route("/repositories")
def repositories():
    repos, releases, issues, summary, latest = get_data()
    syncing = maybe_autosync(latest, repos)
    q = (request.args.get("q") or "").strip()
    enriched = _enrich(repos, releases, issues)
    ql = q.lower()
    filtered = [r for r in enriched if not ql or ql in _safe_lower(r["full_name"])
                or ql in _safe_lower(r["language"])
                or ql in _safe_lower(r["desc"])]
    page = request.args.get("page", 1, type=int)
    pages = max(1, math.ceil(len(filtered) / PAGE_SIZE)) if filtered else 1
    page = max(1, min(page, pages))
    page_rows = filtered[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    return render_template("repositories.html", summary=page_rows,
                           totals=_totals(enriched),
                           latest=latest, syncing=syncing, sync_state=_sync_state,
                           repo_color=repo_color,
                           **_page_ctx(q, page, pages, len(filtered)))


@app.route("/releases")
def releases_view():
    repos, releases, issues, summary, latest = get_data()
    syncing = maybe_autosync(latest, repos)
    q = (request.args.get("q") or "").strip().lower()
    enriched = _enrich(repos, releases, issues)
    filtered_repos = [r for r in enriched if not q or q in _safe_lower(r["full_name"])
                      or any(q in _safe_lower(x["tag"]) for x in r["releases_list"])]
    # Latest releases, newest first, with Dev/Stable pills (preview style).
    entries = []
    for r in filtered_repos:
        for x in r["releases_list"]:
            entries.append({"repo": r, "tag": x["tag"], "date": x["date"],
                            "prerelease": x["prerelease"], "draft": x["draft"],
                            "html_url": x.get("html_url"),
                            "kind": x["kind"]})
    entries.sort(key=lambda e: e["date"] or "", reverse=True)
    # Beautification only: cap Latest releases to what fits the card
    # (see LATEST_RELEASES_CAP), no scroll, no pager for this list.
    # The repo grid below keeps its own rpage pagination.
    page_entries = entries[:LATEST_RELEASES_CAP]
    page, pages = 1, 1
    # Average-gap bars: fastest 10 first, preview bar-row style.
    order = sorted(filtered_repos,
                   key=lambda r: (r["avg_gap"] is None, r["avg_gap"] if r["avg_gap"] is not None else 0))
    max_gap = max([r["avg_gap"] for r in filtered_repos if r["avg_gap"] is not None] + [0])
    gap_bars = ""
    for r in order[:10]:
        if r["avg_gap"] is None:
            gap_bars += (f'<div class="bar-row"><span>{_html.escape(_short_label(r["full_name"]))}</span>'
                         f'<div class="bar-track"></div><b class="mono" style="font-weight:600">n/a</b></div>')
        else:
            gap_bars += _bar_row(_short_label(r["full_name"]), r["avg_gap"], max_gap,
                                 f"{r['avg_gap']}d", r["color"])
    if len(order) > 10:
        gap_bars += f'<p class="small" style="margin:8px 0 0">Fastest 10 of {len(order)} shown.</p>'
    # Repo grid on this page, paginated as cards (own rpage parameter,
    # so paging the grid does not move the latest-releases list).
    gpages = max(1, math.ceil(len(filtered_repos) / PAGE_SIZE)) if filtered_repos else 1
    gpage = max(1, min(request.args.get("rpage", 1, type=int), gpages))
    grid_rows = filtered_repos[(gpage - 1) * PAGE_SIZE: gpage * PAGE_SIZE]
    fastest = min([r for r in filtered_repos if r["avg_gap"] is not None],
                  key=lambda r: r["avg_gap"], default=None)
    return render_template("releases.html", releases=page_entries, gap_bars=gap_bars,
                           summary=grid_rows, totals=_totals(enriched),
                           rpage=gpage, gpages=gpages, grid_total=len(filtered_repos),
                           fastest=fastest, repo_count=len(enriched),
                           latest=latest, syncing=syncing, sync_state=_sync_state,
                           repo_color=repo_color,
                           **_page_ctx(request.args.get("q") or "", page, pages, len(entries)))


@app.route("/issues")
def issues_view():
    repos, releases, issues, summary, latest = get_data()
    syncing = maybe_autosync(latest, repos)
    q = (request.args.get("q") or "").strip().lower()
    enriched = _enrich(repos, releases, issues)
    # Summary cards follow the same search, otherwise a filtered view
    # still leaks every other repo above the results.
    def repo_name_matches(r):
        return (not q or q in _safe_lower(r["full_name"])
                or q in _safe_lower(r["language"])
                or q in _safe_lower(r["desc"]))
    card_repos = [r for r in enriched if repo_name_matches(r)
                  or any(q in _safe_lower(x["title"]) for x in r["issues_list"])]
    # Individual issue rows: repo-name search returns all of that repo's
    # issues; a title word returns matching titles across repos.
    entries = []
    for r in enriched:
        for x in r["issues_list"]:
            if q and not (repo_name_matches(r) or q in _safe_lower(x["title"])):
                continue
            entries.append({"repo": r, "title": x["title"], "state": x["state"],
                            "date": x["date"], "number": x.get("number"),
                            "html_url": x["html_url"]})
    entries.sort(key=lambda e: e["date"] or "", reverse=True)
    page = request.args.get("page", 1, type=int)
    pages = max(1, math.ceil(len(entries) / LIST_PAGE_SIZE)) if entries else 1
    page = max(1, min(page, pages))
    page_entries = entries[(page - 1) * LIST_PAGE_SIZE: page * LIST_PAGE_SIZE]
    # Stacked open/closed bars, Top 10 + Others, preview style.
    shown, others, tot_n = _top10_items(card_repos, lambda r: r["issues"])
    max_tot = max([r["issues"] for r in shown] + ([others["value"]] if others else []) + [1])
    stacked = _top_note(tot_n)
    for r in shown:
        stacked += _stack_row(_short_label(r["full_name"]), r["open"], r["closed"],
                              r["color"], max_tot)
    if others:
        # Others must be the repos left over AFTER the Top 10 by issues
        # in the same (possibly search-filtered) list -- not enriched[10:],
        # which is ordered by stars, so it double-counted top repos and
        # dropped the real bottom repos from the chart.
        rest = sorted(card_repos, key=lambda r: r["issues"], reverse=True)[10:]
        stacked += _stack_row(f"Others ({others['count']})",
                              sum(r["open"] for r in rest),
                              sum(r["closed"] for r in rest),
                              "#94a39c", max_tot)
    tot = _totals(card_repos)
    if tot["issues"]:
        overall = (f'<div class="stack-track" role="img" aria-label="{tot["open"]} open, '
                   f'{tot["closed"]} closed"><div style="width:{tot["open"] / tot["issues"] * 100}%;'
                   f'background:var(--accent)"></div><div style="width:'
                   f'{tot["closed"] / tot["issues"] * 100}%;background:var(--grey)"></div></div>')
        overall_legend = (f'<span><i style="background:var(--accent)"></i>Open · {tot["open"]} '
                          f'({_pct(tot["open"], tot["issues"])}%)</span>'
                          f'<span><i style="background:var(--grey)"></i>Closed · {tot["closed"]} '
                          f'({_pct(tot["closed"], tot["issues"])}%)</span>')
    else:
        overall, overall_legend = ('<div class="empty">No issues captured yet.</div>', "")
    # Repo grid on this page, own rpage parameter.
    gpages = max(1, math.ceil(len(card_repos) / PAGE_SIZE)) if card_repos else 1
    gpage = max(1, min(request.args.get("rpage", 1, type=int), gpages))
    grid_rows = card_repos[(gpage - 1) * PAGE_SIZE: gpage * PAGE_SIZE]
    return render_template("issues.html", issues=page_entries, summary=grid_rows,
                           stacked=stacked, overall=overall, overall_legend=overall_legend,
                           totals=_totals(enriched), iss_totals=tot,
                           rpage=gpage, gpages=gpages, grid_total=len(card_repos),
                           latest=latest, syncing=syncing, sync_state=_sync_state,
                           repo_color=repo_color,
                           **_page_ctx(request.args.get("q") or "", page, pages, len(entries)))


@app.route("/repo/<owner>/<name>")
def repo_detail(owner, name):
    full_name = f"{owner}/{name}"
    repos, releases, issues, summary, latest = get_data()
    syncing = maybe_autosync(latest, repos)
    row = repos[repos["full_name"] == full_name]
    if row.empty:
        abort(404)
    rel = releases[releases["repo_full_name"] == full_name].copy()
    if not rel.empty:
        rel["published_at"] = pd.to_datetime(rel["published_at"], errors="coerce", utc=True)
        rel = rel.sort_values("published_at", ascending=False)
    rel_rows = rel.to_dict("records")
    for r in rel_rows:
        r["kind"] = release_kind(r.get("tag_name"), r.get("name"),
                                 bool(r.get("prerelease")), bool(r.get("draft")))
        r["published_at"] = str(r["published_at"])[:10] if r.get("published_at") is not None else "—"
        if not _safe_str(r.get("html_url")):
            r["html_url"] = f"https://github.com/{full_name}/releases/tag/{_safe_str(r.get('tag_name'))}"
    iss = issues[issues["repo_full_name"] == full_name].copy()
    if not iss.empty:
        iss["created_at"] = pd.to_datetime(iss["created_at"], errors="coerce", utc=True)
        iss = iss.sort_values("created_at", ascending=False)
    srow = summary[summary["full_name"] == full_name]
    enriched = _enrich(repos, releases, issues)
    cur = next((r for r in enriched if r["full_name"] == full_name), None)
    # Project switcher: most recently added first, capped so it fits
    # 2 lines (12 pills at desktop width, as in the user's screenshot),
    # with the current repo always included. Added order = repos.txt
    # order reversed (new appends go at the end of repos.txt), mapping
    # facebook/react -> react/react, then any DB repo not in repos.txt.
    _file_order = []
    try:
        _file_order = read_repos_file(REPOS_FILE)
    except OSError:
        _file_order = []
    _by_name = {r["full_name"]: r for r in enriched}
    _mapped = []
    for _f in _file_order:
        if _f in _by_name:
            _mapped.append(_f)
        elif _f == "facebook/react" and "react/react" in _by_name:
            _mapped.append("react/react")
    _added_order = list(reversed(_mapped)) + [r["full_name"] for r in enriched
                                              if r["full_name"] not in _mapped]
    _switcher_names = [full_name] + [n for n in _added_order if n != full_name]
    switcher_repos = [_by_name[n] for n in _switcher_names[:12] if n in _by_name]
    months_bars = ""
    if cur and cur["months"]:
        mx = max(m[1] for m in cur["months"])
        months_bars = "".join(_bar_row(m[0], m[1], mx, str(m[1]), cur["color"])
                             for m in cur["months"])
    iss_rows = iss.to_dict("records")
    for r in iss_rows:
        r["created_at"] = str(r["created_at"])[:10] if r.get("created_at") is not None else "—"
        r["closed_at"] = str(r["closed_at"])[:10] if r.get("closed_at") not in (None, "NaT") else None
        if not _safe_str(r.get("html_url")):
            _num = r.get("number")
            r["html_url"] = (f"https://github.com/{full_name}/issues/{int(_num)}"
                             if _num is not None else f"https://github.com/{full_name}/issues")
    repo_dict = row.iloc[0].to_dict()
    for _k in ("description", "language", "html_url"):
        if _k in repo_dict and isinstance(repo_dict[_k], float) and pd.isna(repo_dict[_k]):
            repo_dict[_k] = None
    return render_template(
        "repo.html", repo=repo_dict,
        summary=srow.iloc[0].to_dict() if not srow.empty else {},
        detail=cur, all_repos=enriched, switcher_repos=switcher_repos, months_bars=months_bars,
        totals=_totals(enriched),
        insights=compute_insights(row.iloc[0], releases, issues),
        releases=rel_rows, issues=iss_rows,
        latest=latest, syncing=syncing, sync_state=_sync_state,
        repo_color=repo_color)


@app.route("/add", methods=["POST"])
def add_repo():
    raw = request.form.get("repo", "")
    parsed = parse_repo_input(raw)
    if not parsed:
        flash("Enter a repository as owner/repo, or paste its full GitHub URL.")
        return redirect(url_for("repositories"))
    existing_repos, _, _, _, _ = get_data()
    if not existing_repos.empty and parsed in list(existing_repos["full_name"]):
        flash(f"{parsed} is already tracked.")
        return redirect(url_for("repo_detail", owner=parsed.split("/")[0], name=parsed.split("/")[1]))
    try:
        canonical, n_rel, n_iss = fetch_and_save_repo(parsed, DB_PATH)
    except Exception as exc:  # noqa: BLE001 - existing data is untouched here
        kind = github_error_kind(exc)
        if kind == "not_found":
            flash(f"Repository not found or private: {parsed}. Existing repositories are unchanged.")
        elif kind == "rate_limit":
            flash("GitHub rate limit reached. Try again later. Existing repositories are unchanged.")
        else:
            flash("Could not reach GitHub. Check your connection and try again. Existing repositories are unchanged.")
        return redirect(url_for("repositories"))
    # Track the canonical name in repos.txt, unless it (or a redirect
    # source for it, e.g. facebook/react -> react/react) is already tracked.
    try:
        existing = read_repos_file(REPOS_FILE)
        already = canonical in existing or (
            not existing_repos.empty and canonical in list(existing_repos["full_name"]))
        if not already:
            with open(REPOS_FILE, "a", encoding="utf-8") as f:
                f.write(canonical + "\n")
    except OSError:
        pass
    flash(f"Added {canonical}: {n_rel} releases, {n_iss} issues.")
    return redirect(url_for("repo_detail", owner=canonical.split("/")[0], name=canonical.split("/")[1]))


@app.route("/delete/<owner>/<name>", methods=["POST"])
def delete_repo(owner, name):
    full_name = f"{owner}/{name}"
    conn = sqlite3.connect(DB_PATH)
    try:
        from oss_pulse.load import delete_repo as _delete
        _delete(conn, full_name)
    finally:
        conn.close()
    try:
        kept = [r for r in read_repos_file(REPOS_FILE) if r != full_name]
        with open(REPOS_FILE, "w", encoding="utf-8") as f:
            for r in kept:
                f.write(r + "\n")
    except OSError:
        pass
    flash(f"Removed {full_name}.")
    return redirect(url_for("repositories"))


@app.route("/sync", methods=["POST"])
def sync_now():
    _run_sync()
    flash(_sync_state.get("message") or "Sync complete.")
    return redirect(request.form.get("next") or url_for("index"))


@app.route("/api/sync-status")
def api_sync_status():
    _, _, _, _, latest = get_data()
    return jsonify({"running": _sync_state["running"],
                    "message": _sync_state.get("message"),
                    "last_run": latest.to_dict() if latest is not None else None,
                    "due": _sync_due(latest)})


@app.route("/api/summary")
def api_summary():
    repos, releases, issues, summary, latest = get_data()
    out = []
    for _, row in summary.iterrows():
        d = row.to_dict()
        repo_row = repos[repos["full_name"] == d["full_name"]].iloc[0]
        d["insights"] = compute_insights(repo_row, releases, issues)
        out.append(d)
    return jsonify({
        "latest_run": latest.to_dict() if latest is not None else None,
        "repositories": out,
    })


@app.route("/chart/stars.png")
def chart_stars():
    _, _, _, summary, _ = get_data()
    # Titles live in the HTML headings, not inside the PNG, so nothing overlaps.
    fig, ax = plt.subplots(figsize=(10, 4.4), dpi=150)
    if summary.empty:
        ax.text(0.5, 0.5, "No repository data", ha="center")
    else:
        top = top10_plus_others(summary[["full_name", "stars"]].rename(
            columns={"full_name": "label", "stars": "value"}), "label", "value")
        labels = [_short_label(l) if l != "Others" else "Others" for l in top["label"]]
        colours = [repo_color(l) if l != "Others" else "#94a3b8" for l in top["label"]]
        ax.bar(labels, top["value"], color=colours)
        ax.set_ylabel("Stars")
        _style_axes(ax)
    return _png(fig)


@app.route("/chart/releases_by_year.png")
def chart_releases_by_year():
    _, releases, _, _, _ = get_data()
    fig, ax = plt.subplots(figsize=(10, 4.6), dpi=150)
    if rpy_empty := releases.empty:
        ax.text(0.5, 0.5, "No release data in the database", ha="center")
    else:
        rpy = releases_per_year(releases)
        top_repos = (releases.groupby("repo_full_name").size()
                     .sort_values(ascending=False).head(10).index.tolist())
        rpy = rpy[rpy["repo_full_name"].isin(top_repos)]
        pivot = rpy.pivot(index="published_year", columns="repo_full_name", values="count").fillna(0)
        short_to_colour = {_short_label(c): repo_color(c) for c in top_repos}
        pivot.columns = [_short_label(c) for c in pivot.columns]
        colours = [short_to_colour.get(c, "#059669") for c in pivot.columns]
        pivot.plot(kind="bar", ax=ax, color=colours, width=0.75)
        ax.set_ylabel("Releases")
        # Legend below the chart, never on top of the bars.
        ax.legend(frameon=False, fontsize=9, loc="upper center",
                  bbox_to_anchor=(0.5, -0.16), ncol=3)
        _style_axes(ax)
    return _png(fig)


@app.route("/chart/issue_state.png")
def chart_issue_state():
    _, _, issues, _, _ = get_data()
    fig, ax = plt.subplots(figsize=(10, 4.4), dpi=150)
    if issues.empty:
        ax.text(0.5, 0.5, "No issue data in the database", ha="center")
    else:
        counts = issues.groupby(["repo_full_name", "state"]).size().unstack(fill_value=0)
        for col in ("open", "closed"):
            if col not in counts:
                counts[col] = 0
        counts["total"] = counts["open"] + counts["closed"]
        top = counts.sort_values("total", ascending=False).head(10)
        rest = counts[~counts.index.isin(top.index)]
        if not rest.empty:
            top.loc["Others"] = [rest["open"].sum(), rest["closed"].sum(), rest["total"].sum()]
        top.index = [_short_label(i) if i != "Others" else "Others" for i in top.index]
        top[["open", "closed"]].plot(kind="bar", stacked=True, ax=ax,
                                     color=["#059669", "#94a3b8"], width=0.65)
        ax.set_ylabel("Issues")
        ax.legend(frameon=False, fontsize=9, loc="upper center",
                  bbox_to_anchor=(0.5, -0.16), ncol=2)
        _style_axes(ax)
    return _png(fig)


@app.route("/chart/avg_gap.png")
def chart_avg_gap():
    _, releases, _, _, _ = get_data()
    fig, ax = plt.subplots(figsize=(10, 4.4), dpi=150)
    gaps = []
    if not releases.empty:
        for name, g in releases.groupby("repo_full_name"):
            g = g.copy()
            g["published_at"] = pd.to_datetime(g["published_at"], errors="coerce", utc=True)
            g = g.sort_values("published_at")
            diffs = g["published_at"].diff().dt.total_seconds().div(86400).dropna()
            if len(diffs):
                gaps.append((name, round(float(diffs.mean()), 1), len(g)))
    if not gaps:
        ax.text(0.5, 0.5, "No release-gap data", ha="center")
    else:
        gaps = sorted(gaps, key=lambda x: x[2], reverse=True)[:10]
        ax.bar([_short_label(g[0]) for g in gaps], [g[1] for g in gaps], color="#059669")
        ax.set_ylabel("Days")
        _style_axes(ax)
    return _png(fig)


if __name__ == "__main__":
    app.run(debug=True)
