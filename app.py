#!/usr/bin/env python3
"""
Job discovery agent — runs on your machine, not in a browser.

    python3 app.py resolve      # one-time: find each company's hiring-software board
    python3 app.py discover     # fetch open roles, score them, save to jobs.db
    python3 app.py serve        # web UI at http://localhost:8765

No dependencies. Python 3.9+.

Optional: set ANTHROPIC_API_KEY to add a second, LLM-based scoring pass.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "jobs.db")
COMPANIES_PATH = os.path.join(HERE, "companies.json")
PROFILE_PATH = os.path.join(HERE, "profile.json")
# Boards see this string. Set CONTACT_EMAIL so they can reach you if your
# polling ever bothers them; it is good manners and keeps you unblocked.
UA = "job-discovery-agent/1.0 (+https://github.com/pocketmarble/job-discovery-agent)"
if os.environ.get("CONTACT_EMAIL"):
    UA += " contact: %s" % os.environ["CONTACT_EMAIL"]

# --------------------------------------------------------------------------
# HTTP helper
# --------------------------------------------------------------------------


def get_json(url, timeout=20, method="GET", data=None, headers=None):
    """Fetch JSON. Returns (data, error_string). Never raises."""
    req_headers = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        return json.loads(raw), None
    except urllib.error.HTTPError as e:
        return None, "http %s" % e.code
    except urllib.error.URLError as e:
        return None, "network: %s" % e.reason
    except json.JSONDecodeError:
        return None, "not json"
    except Exception as e:  # noqa: BLE001 - fail soft by design
        return None, "error: %s" % e


class _Strip(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out = []

    def handle_data(self, d):
        self.out.append(d)


def strip_html(s):
    if not s:
        return ""
    s = s.replace("&nbsp;", " ")
    p = _Strip()
    try:
        p.feed(s)
    except Exception:  # noqa: BLE001
        return re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", " ".join(p.out)).strip()


# --------------------------------------------------------------------------
# Hiring-software (ATS) clients
#
# Each returns a list of dicts:
#   {external_id, title, location, url, description, posted_at}
# Every one of these endpoints is public and needs no login — they are the
# same endpoints the company's own careers page calls to draw its listings.
# --------------------------------------------------------------------------


def ats_greenhouse(token):
    url = "https://boards-api.greenhouse.io/v1/boards/%s/jobs?content=true" % urllib.parse.quote(token)
    data, err = get_json(url)
    if err:
        return None, err
    out = []
    for j in (data or {}).get("jobs", []) or []:
        out.append(
            {
                "external_id": "gh-%s" % j.get("id"),
                "title": j.get("title") or "",
                "location": ((j.get("location") or {}) or {}).get("name") or "",
                "url": j.get("absolute_url") or "",
                "description": strip_html(j.get("content") or ""),
                "posted_at": (j.get("updated_at") or "")[:10],
            }
        )
    return out, None


def ats_lever(token):
    url = "https://api.lever.co/v0/postings/%s?mode=json" % urllib.parse.quote(token)
    data, err = get_json(url)
    if err:
        return None, err
    out = []
    for j in data or []:
        cats = j.get("categories") or {}
        ts = j.get("createdAt")
        out.append(
            {
                "external_id": "lv-%s" % j.get("id"),
                "title": j.get("text") or "",
                "location": cats.get("location") or "",
                "url": j.get("hostedUrl") or "",
                "description": j.get("descriptionPlain") or strip_html(j.get("description") or ""),
                "posted_at": time.strftime("%Y-%m-%d", time.gmtime(ts / 1000)) if ts else "",
            }
        )
    return out, None


def ats_ashby(token):
    url = "https://api.ashbyhq.com/posting-api/job-board/%s" % urllib.parse.quote(token)
    data, err = get_json(url)
    if err:
        return None, err
    out = []
    for j in (data or {}).get("jobs", []) or []:
        out.append(
            {
                "external_id": "ab-%s" % j.get("id"),
                "title": j.get("title") or "",
                "location": j.get("location") or "",
                "url": j.get("jobUrl") or j.get("applyUrl") or "",
                "description": j.get("descriptionPlain") or strip_html(j.get("descriptionHtml") or ""),
                "posted_at": (j.get("publishedAt") or "")[:10],
            }
        )
    return out, None


def ats_smartrecruiters(token):
    url = "https://api.smartrecruiters.com/v1/companies/%s/postings?limit=100" % urllib.parse.quote(token)
    data, err = get_json(url)
    if err:
        return None, err
    out = []
    for j in (data or {}).get("content", []) or []:
        loc = j.get("location") or {}
        out.append(
            {
                "external_id": "sr-%s" % j.get("id"),
                "title": j.get("name") or "",
                "location": ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x),
                "url": "https://jobs.smartrecruiters.com/%s/%s" % (token, j.get("id")),
                "description": "",  # detail endpoint per posting; fetched lazily if needed
                "posted_at": (j.get("releasedDate") or "")[:10],
            }
        )
    return out, None


def ats_workable(token):
    url = "https://apply.workable.com/api/v1/widget/accounts/%s?details=true" % urllib.parse.quote(token)
    data, err = get_json(url)
    if err:
        return None, err
    out = []
    for j in (data or {}).get("jobs", []) or []:
        out.append(
            {
                "external_id": "wk-%s" % (j.get("shortcode") or j.get("id")),
                "title": j.get("title") or "",
                "location": ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x),
                "url": j.get("url") or j.get("application_url") or "",
                "description": strip_html(j.get("description") or ""),
                "posted_at": (j.get("published_on") or "")[:10],
            }
        )
    return out, None


def ats_recruitee(token):
    url = "https://%s.recruitee.com/api/offers/" % urllib.parse.quote(token)
    data, err = get_json(url)
    if err:
        return None, err
    out = []
    for j in (data or {}).get("offers", []) or []:
        out.append(
            {
                "external_id": "rc-%s" % j.get("id"),
                "title": j.get("title") or "",
                "location": j.get("location") or "",
                "url": j.get("careers_url") or j.get("careers_apply_url") or "",
                "description": strip_html(j.get("description") or ""),
                "posted_at": (j.get("published_at") or "")[:10],
            }
        )
    return out, None


ATS = {
    "greenhouse": ats_greenhouse,
    "lever": ats_lever,
    "ashby": ats_ashby,
    "smartrecruiters": ats_smartrecruiters,
    "workable": ats_workable,
    "recruitee": ats_recruitee,
}


# --------------------------------------------------------------------------
# Resolver: guess which hiring software a company uses, and its board name.
# --------------------------------------------------------------------------


def slug_candidates(name):
    base = name.lower()
    base = re.sub(r"\(.*?\)", " ", base)
    base = re.sub(r"[^a-z0-9 ]+", " ", base)
    words = [w for w in base.split() if w not in {"inc", "the", "labs", "lab", "company", "co"}]
    joined = "".join(words)
    hyphen = "-".join(words)
    first = words[0] if words else joined
    seen, out = set(), []
    for c in [joined, hyphen, first, joined.replace("ai", ""), first + "bio"]:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def resolve_company(company, verbose=True):
    """Try each ATS with a few plausible board names. First real hit wins."""
    if company.get("ats") and company.get("ats_token"):
        return company, "already set"
    cands = company.get("ats_candidates") or slug_candidates(company["name"])
    for ats_name, fn in ATS.items():
        for slug in cands:
            jobs, err = fn(slug)
            if err is None and isinstance(jobs, list) and len(jobs) > 0:
                company["ats"] = ats_name
                company["ats_token"] = slug
                if verbose:
                    print("  ✓ %-28s %s/%s (%d roles)" % (company["name"], ats_name, slug, len(jobs)))
                return company, "resolved"
            time.sleep(0.2)
    if verbose:
        print("  · %-28s no public board found — add ats/ats_token by hand" % company["name"])
    return company, "unresolved"


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

SENIORITY_PATTERNS = [
    ("exec", r"\b(vice president|vp|head of|chief|director)\b"),
    ("principal", r"\b(principal|staff|distinguished|fellow \(staff\))\b"),
    ("senior", r"\bsenior\b|\bsr\.?\b|\bii+\b|\blead\b"),
    ("entry", r"\b(associate|assistant|junior|jr\.?|\bi\b|intern|trainee|co-op|coordinator|specialist)\b"),
]

# Fallback when a profile does not define ladders. Keys are the seniority
# labels above; values are score adjustments.
DEFAULT_LADDER = {"mid": 16, "entry": 10, "senior": 3, "principal": -28, "exec": -45}

YEARS_RE = re.compile(r"(\d{1,2})\s*(?:\+|-|–|\s+to\s+)?\s*(\d{1,2})?\s*\+?\s*years", re.I)
PHD_RE = re.compile(r"\bph\.?\s?d\.?\b|\bdoctorate\b|\bdoctoral\b", re.I)
# "BS or MS in a quantitative field" with no PhD anywhere = the post is pitched
# below your level. Applying reads as overqualified and a flight risk.
SUB_DOCTORAL_RE = re.compile(
    r"\b(b\.?s\.?|b\.?a\.?|m\.?s\.?|m\.?sc\.?|bachelor'?s?|master'?s?)\b[^.]{0,80}?"
    r"\b(degree|in a|required|preferred|or equivalent)\b", re.I)


def detect_seniority(title):
    t = " " + title.lower() + " "
    for label, pat in SENIORITY_PATTERNS:
        if re.search(pat, t):
            return label
    return "mid"


def min_years_required(text):
    """Smallest 'N years' figure mentioned. None if unstated."""
    vals = []
    for m in YEARS_RE.finditer(text or ""):
        try:
            vals.append(int(m.group(1)))
        except (TypeError, ValueError):
            continue
    return min(vals) if vals else None


def score_posting(job, company, profile):
    """Deterministic score 0-100 plus human-readable reasons and flags."""
    title = job.get("title") or ""
    desc = job.get("description") or ""
    blob = (title + "\n" + desc).lower()
    reasons, flags = [], []
    score = 0

    # --- 1. Title match against this company's watch list + lane vocabulary ---
    watch = [w.strip().lower() for w in (company.get("watch_titles") or "").split(",") if w.strip()]
    title_l = title.lower()
    if any(w and w in title_l for w in watch):
        score += 25
        reasons.append("Title matches your watch list for this company.")
    else:
        lane_hits = []
        for lane, cfg in profile["lanes"].items():
            if any(k in title_l for k in cfg["title_keywords"]):
                lane_hits.append(lane)
        if lane_hits:
            score += 18
            reasons.append("Title is a known %s role type." % "/".join(lane_hits))
        else:
            reasons.append("Title is outside your usual role types — judgement call.")

    # --- 2. Lane fit from the body text ---
    lane_scores = {}
    for lane, cfg in profile["lanes"].items():
        hits = [k for k in cfg["body_keywords"] if k in blob]
        if hits:
            lane_scores[lane] = hits
    if lane_scores:
        best = max(lane_scores, key=lambda k: len(lane_scores[k]))
        n = min(len(lane_scores[best]), 6)
        score += 3 * n
        job["lane"] = best
        reasons.append(
            "Reads as %s (%s)." % (profile["lanes"][best]["label"], ", ".join(lane_scores[best][:4]))
        )
    else:
        job["lane"] = company.get("lane") or ""

    # --- 3. The decoder ring: is this the right rung of the ladder? ---
    sen = detect_seniority(title)
    ladder = company.get("ladder", "startup")  # 'big_pharma' | 'startup' | 'academic' | 'agency'
    yrs = min_years_required(desc)
    top_degree = (profile.get("top_degree") or "phd").lower()
    if top_degree == "phd":
        wants_phd = bool(PHD_RE.search(desc))
        sub_doctoral = bool(SUB_DOCTORAL_RE.search(desc)) and not wants_phd
    else:
        # For non-doctoral profiles a PhD requirement is the disqualifier,
        # and there is no such thing as an under-levelled degree ask.
        wants_phd = False
        sub_doctoral = False
        if PHD_RE.search(desc):
            score -= 25
            flags.append("Posting asks for a PhD.")

    ladders = profile.get("ladders") or {}
    ok = ladders.get(ladder) or ladders.get("default") or DEFAULT_LADDER
    score += ok.get(sen, 0)
    if sen in ("principal", "exec"):
        flags.append("Title is above your target rung (%s) — usually a wasted application." % sen)
    elif ok.get(sen, 0) >= 12 and sen == "senior":
        reasons.append("'Senior' is the entry rung on this employer's ladder, not a step up.")

    # Years are judged relative to what this candidate can credibly claim,
    # not against a fixed threshold — 5 years is disqualifying for a new PhD
    # and exactly right for an eight-year manager.
    target_years = profile.get("target_years", 2)
    if yrs is None:
        score += 5
        reasons.append("No years-of-experience requirement stated.")
    elif yrs <= target_years:
        score += 18
        reasons.append("Asks for %d+ years, within your %d — a level match." % (yrs, target_years))
    elif yrs <= target_years + 2:
        score += 7
        reasons.append("Asks for %d+ years against your %d — reachable; lead with your strongest evidence."
                       % (yrs, target_years))
    else:
        score -= 22
        flags.append("Asks for %d+ years of experience; you can claim about %d." % (yrs, target_years))

    if wants_phd:
        score += 10
        reasons.append("PhD named in the requirements.")
    elif sub_doctoral:
        score -= 40
        flags.append("Pitched at BS/MS level with no PhD track — you would read as overqualified.")

    # --- 4. Skill overlap with the resume ---
    hits = [s for s in profile["skills"] if s.lower() in blob]
    if hits:
        score += int(min(len(hits), 8) * 1.5)
        reasons.append("Overlaps your stack: %s." % ", ".join(hits[:6]))

    # --- 5. Location ---
    loc = (job.get("location") or "").lower()
    if any(p in loc for p in profile["locations_good"]):
        score += 6
        reasons.append("Location works (%s)." % job.get("location"))
    elif loc and not any(p in loc for p in profile["locations_ok"]):
        score -= 10
        flags.append("Location may not work: %s." % job.get("location"))

    # --- 6. Hard exclusions ---
    for bad in profile.get("exclude_title_keywords", []):
        if bad in title_l:
            score -= 45
            flags.append("Title contains '%s' — likely not your track." % bad)
            break

    score = max(0, min(100, int(round(score))))
    job["score"] = score
    job["why"] = " ".join(reasons)
    job["flags"] = " ".join(flags)
    job["seniority"] = sen
    job["min_years"] = yrs if yrs is not None else ""
    return job


# --------------------------------------------------------------------------
# Optional second pass: ask Claude to judge the shortlist
# --------------------------------------------------------------------------


def llm_rescore(jobs, profile, limit=15):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return jobs, "no ANTHROPIC_API_KEY set — rule-based scores only"
    top = sorted(jobs, key=lambda j: -j["score"])[:limit]
    payload_jobs = [
        {"i": i, "title": j["title"], "company": j["company"], "excerpt": (j.get("description") or "")[:2500]}
        for i, j in enumerate(top)
    ]
    prompt = (
        "You are screening job postings for one candidate. Here is the candidate profile:\n\n"
        + profile["resume_summary"]
        + "\n\nSeniority rule: at large pharma a fresh PhD enters at 'Senior Scientist'; at "
        "startups a fresh PhD enters at 'Scientist' or 'Scientist I'. 'Principal', 'Staff', "
        "'Director' and anything demanding 5+ years of industry experience are too senior.\n\n"
        "For each posting below, return a fit score 0-100 and one sentence of reasoning. Be "
        "skeptical; do not inflate. Judge only on the text given; never invent requirements.\n\n"
        "Respond with ONLY a JSON array: [{\"i\": 0, \"llm_score\": 72, \"llm_note\": \"...\"}]\n\n"
        + json.dumps(payload_jobs)
    )
    data, err = get_json(
        "https://api.anthropic.com/v1/messages",
        method="POST",
        timeout=120,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        data={
            "model": "claude-sonnet-4-6",
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    if err:
        return jobs, "LLM pass failed (%s) — rule-based scores kept" % err
    try:
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        verdicts = json.loads(text)
    except Exception as e:  # noqa: BLE001
        return jobs, "LLM returned unparseable output (%s)" % e
    for v in verdicts:
        try:
            j = top[int(v["i"])]
        except (KeyError, ValueError, IndexError):
            continue
        j["llm_score"] = int(v.get("llm_score", 0))
        j["llm_note"] = str(v.get("llm_note", ""))[:400]
        j["score"] = int(round(0.5 * j["score"] + 0.5 * j["llm_score"]))
    return jobs, "LLM pass applied to top %d" % len(top)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  external_id TEXT PRIMARY KEY,
  company TEXT, title TEXT, location TEXT, url TEXT,
  description TEXT, posted_at TEXT, lane TEXT,
  score INTEGER, why TEXT, flags TEXT, seniority TEXT, min_years TEXT,
  llm_score INTEGER, llm_note TEXT,
  first_seen TEXT, last_seen TEXT, state TEXT DEFAULT 'new'
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT, finished_at TEXT, companies INTEGER, found INTEGER, note TEXT
);
"""


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert(conn, j):
    today = time.strftime("%Y-%m-%d")
    cur = conn.execute("SELECT state FROM jobs WHERE external_id=?", (j["external_id"],))
    row = cur.fetchone()
    if row:
        conn.execute(
            "UPDATE jobs SET score=?, why=?, flags=?, last_seen=?, llm_score=?, llm_note=? WHERE external_id=?",
            (j["score"], j["why"], j["flags"], today, j.get("llm_score"), j.get("llm_note"), j["external_id"]),
        )
        return False
    conn.execute(
        "INSERT INTO jobs (external_id, company, title, location, url, description, posted_at, lane,"
        " score, why, flags, seniority, min_years, llm_score, llm_note, first_seen, last_seen, state)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'new')",
        (
            j["external_id"], j["company"], j["title"], j.get("location", ""), j.get("url", ""),
            (j.get("description") or "")[:20000], j.get("posted_at", ""), j.get("lane", ""),
            j["score"], j["why"], j["flags"], j.get("seniority", ""), str(j.get("min_years", "")),
            j.get("llm_score"), j.get("llm_note"), today, today,
        ),
    )
    return True


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def load(path):
    with open(path) as f:
        return json.load(f)


def cmd_resolve(args):
    companies = load(COMPANIES_PATH)
    print("Resolving %d companies — this makes a few requests each, so it takes a minute.\n" % len(companies))
    counts = {"resolved": 0, "already set": 0, "unresolved": 0}
    for c in companies:
        _, status = resolve_company(c)
        counts[status] = counts.get(status, 0) + 1
    with open(COMPANIES_PATH, "w") as f:
        json.dump(companies, f, indent=2)
    print("\n%d resolved, %d already set, %d unresolved." % (counts["resolved"], counts["already set"], counts["unresolved"]))
    print("Unresolved companies are skipped by discover. Fill in 'ats' and 'ats_token' by hand")
    print("if you can find their board (look at where their Careers link sends you).")


def cmd_discover(args):
    companies = load(COMPANIES_PATH)
    profile = load(PROFILE_PATH)
    conn = db()
    started = time.strftime("%Y-%m-%d %H:%M")
    scored, new_count, live, skipped = [], 0, 0, []

    for c in companies:
        ats_name, token = c.get("ats"), c.get("ats_token")
        if not ats_name or not token:
            skipped.append(c["name"])
            continue
        fn = ATS.get(ats_name)
        if not fn:
            skipped.append(c["name"])
            continue
        jobs, err = fn(token)
        if err:
            print("  ! %-28s %s" % (c["name"], err))
            continue
        live += 1
        keep = 0
        for j in jobs:
            j["company"] = c["name"]
            score_posting(j, c, profile)
            if j["score"] >= profile.get("min_score", 40):
                scored.append(j)
                keep += 1
        print("  · %-28s %3d open, %2d above threshold" % (c["name"], len(jobs), keep))
        time.sleep(0.3)

    note = ""
    if scored and not args.no_llm:
        scored, note = llm_rescore(scored, profile)
        print("\n%s" % note)

    for j in scored:
        if upsert(conn, j):
            new_count += 1
    conn.execute(
        "INSERT INTO runs (started_at, finished_at, companies, found, note) VALUES (?,?,?,?,?)",
        (started, time.strftime("%Y-%m-%d %H:%M"), live, len(scored), note),
    )
    conn.commit()
    print("\n%d companies checked, %d postings above threshold, %d of them new." % (live, len(scored), new_count))
    if skipped:
        print("Skipped (no board resolved): %s" % ", ".join(skipped[:8]) + (" …" if len(skipped) > 8 else ""))
    print("Run `python3 app.py serve` to browse them.")


# --------------------------------------------------------------------------
# Local web UI
# --------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job discovery</title>
<style>
:root{--bg:#f6f7f5;--card:#fff;--ink:#1c2321;--soft:#5a6660;--line:#e2e6e1;--accent:#1f6f54;
--accentbg:#e4efe9;--warn:#b0802b;--warnbg:#f6ecd8;--bad:#a4442f;--badbg:#f4e2dc}
@media(prefers-color-scheme:dark){:root{--bg:#151a18;--card:#1d2422;--ink:#e6ebe8;--soft:#9aa8a1;
--line:#2c3531;--accent:#5eb08e;--accentbg:#23352d;--warn:#d3a54f;--warnbg:#3a3020;--bad:#d97a5f;--badbg:#3b2721}}
*{box-sizing:border-box;margin:0}
body{font:14px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink);padding:20px}
.wrap{max-width:1000px;margin:0 auto}
header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:14px}
h1{font-size:19px}
.meta{color:var(--soft);font-size:12.5px}
.bar{display:flex;gap:8px;margin:0 0 14px;flex-wrap:wrap}
input,select{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
.job{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:10px;
display:grid;grid-template-columns:52px 1fr auto;gap:14px;align-items:start}
.sc{width:52px;height:52px;border-radius:10px;display:flex;align-items:center;justify-content:center;
font-weight:700;font-size:17px;background:var(--badbg);color:var(--bad)}
.sc.mid{background:var(--warnbg);color:var(--warn)} .sc.hi{background:var(--accentbg);color:var(--accent)}
h3{font-size:15px} .sub{color:var(--soft);font-size:12.5px;margin-top:2px}
.why{margin-top:7px;font-size:13px} .flags{margin-top:5px;font-size:13px;color:var(--bad)}
.llm{margin-top:5px;font-size:12.5px;color:var(--soft);font-style:italic}
a.btn{display:inline-block;padding:6px 12px;border-radius:8px;background:var(--accent);color:#fff;
text-decoration:none;font-weight:600;font-size:12.5px;white-space:nowrap}
.empty{padding:50px;text-align:center;color:var(--soft)}
</style></head><body><div class="wrap">
<header><h1>Job discovery</h1><span class="meta" id="meta"></span></header>
<div class="bar">
<input id="q" placeholder="Search title, company, text">
<select id="lane"><option value="">All lanes</option></select>
<select id="min"><option value="40">Score 40+</option><option value="60" selected>Score 60+</option>
<option value="75">Score 75+</option><option value="0">Everything</option></select>
</div>
<div id="list"></div></div>
<script>
let all=[];
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function cls(n){return n>=75?'hi':(n>=55?'mid':'')}
function draw(){
  const q=document.getElementById('q').value.toLowerCase();
  const lane=document.getElementById('lane').value;
  const min=+document.getElementById('min').value;
  const rows=all.filter(j=>j.score>=min&&(!lane||j.lane===lane)&&
    (!q||(j.title+' '+j.company+' '+(j.why||'')+' '+(j.description||'')).toLowerCase().includes(q)))
    .sort((a,b)=>b.score-a.score);
  document.getElementById('list').innerHTML = rows.length? rows.map(j=>`
    <div class="job"><div class="sc ${cls(j.score)}">${j.score}</div>
    <div><h3>${esc(j.title)}</h3>
    <div class="sub">${esc(j.company)}${j.location?' · '+esc(j.location):''}${j.lane?' · '+esc(j.lane):''}${j.posted_at?' · posted '+esc(j.posted_at):''}${j.first_seen?' · found '+esc(j.first_seen):''}</div>
    ${j.why?`<div class="why">${esc(j.why)}</div>`:''}
    ${j.flags?`<div class="flags">${esc(j.flags)}</div>`:''}
    ${j.llm_note?`<div class="llm">Claude: ${esc(j.llm_note)}</div>`:''}
    </div><div>${j.url?`<a class="btn" href="${esc(j.url)}" target="_blank" rel="noopener">Open</a>`:''}</div></div>`).join('')
    : '<div class="empty">Nothing at this threshold. Lower the score filter, or run <code>python3 app.py discover</code>.</div>';
  document.getElementById('meta').textContent = rows.length+' shown of '+all.length+' saved';
}
fetch('/api/jobs').then(r=>r.json()).then(d=>{
  all=d.jobs;
  const lanes=[...new Set(all.map(j=>j.lane).filter(Boolean))].sort();
  const sel=document.getElementById('lane');
  lanes.forEach(l=>{const o=document.createElement('option');o.value=l;o.textContent=l;sel.appendChild(o)});
  if(d.run){document.getElementById('meta').textContent='last run '+d.run.finished_at}
  draw();
});
['q','lane','min'].forEach(id=>document.getElementById(id).addEventListener('input',draw));
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/jobs"):
            conn = db()
            jobs = [dict(r) for r in conn.execute(
                "SELECT external_id,company,title,location,url,posted_at,lane,score,why,flags,"
                "llm_note,first_seen,state, substr(description,1,600) AS description "
                "FROM jobs WHERE state != 'dismissed' ORDER BY score DESC")]
            run = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            self._send(200, json.dumps({"jobs": jobs, "run": dict(run) if run else None}).encode(),
                       "application/json")
            return
        self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")


def cmd_serve(args):
    srv = HTTPServer(("127.0.0.1", args.port), Handler)
    print("Serving on http://localhost:%d  (ctrl-C to stop)" % args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="turn a resume into lanes, targets and scoring config")
    a.add_argument("resume", help="path to a .pdf, .docx, .txt or .md resume")
    a.add_argument("--location", default="", help='e.g. "Brooklyn, NY"')
    a.add_argument("--notes", default="", help="anything the resume does not say")
    a.add_argument("--force", action="store_true", help="overwrite without keeping .bak files")
    sub.add_parser("resolve", help="find each company's public job board")
    d = sub.add_parser("discover", help="fetch and score open roles")
    d.add_argument("--no-llm", action="store_true", help="skip the Claude scoring pass")
    s = sub.add_parser("serve", help="browse results in a local web page")
    s.add_argument("--port", type=int, default=8765)
    args = p.parse_args()
    if args.cmd == "analyze":
        import analyze  # noqa: PLC0415 - optional path, keeps startup light
        return analyze.run(args)
    {"resolve": cmd_resolve, "discover": cmd_discover, "serve": cmd_serve}[args.cmd](args)


if __name__ == "__main__":
    main()
