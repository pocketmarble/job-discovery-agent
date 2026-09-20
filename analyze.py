#!/usr/bin/env python3
"""
analyze.py — turn a resume into a job-search configuration.

    python3 app.py analyze path/to/resume.pdf --location "Brooklyn, NY"

Reads a resume (.pdf, .docx, .txt, .md), asks Claude to work out the candidate's
role lanes, seniority ladder rules, and target employers, and writes:

    profile.json    scoring configuration (lanes, skills, ladders, exclusions)
    companies.json  candidate target employers with watch titles and ladder type
    ANALYSIS.md     the reasoning in prose: lanes, moat, what to target and why

Nothing here is authoritative. The generated company list in particular is the
model's best guess and WILL contain employers that don't hire for this, or don't
exist under that name. `app.py resolve` is the filter: a company whose public
job board cannot be found is either mis-named or not worth polling. Read
ANALYSIS.md and edit companies.json before trusting either.
"""

import base64
import json
import os
import re
import sys
import zipfile

# app.py exposes get_json(); imported lazily to avoid a circular import at load.

SUPPORTED = (".pdf", ".docx", ".txt", ".md")

SCHEMA_INSTRUCTIONS = """
Return ONLY a JSON object, no markdown fences, with exactly these keys:

"headline": one sentence naming what this candidate is, as an employer would
  categorise them.

"moat": 2-3 sentences on what makes them unusual — the combination most of
  their competition lacks. Be specific and evidence-based; no flattery.

"top_degree": one of "phd", "masters", "bachelors" — their highest completed or
  imminent qualification.

"target_years": integer — the years of relevant experience this candidate can
  credibly claim for the lanes below. For someone leaving academia with no
  industry role this is usually 0-2 regardless of how long they trained; for an
  experienced professional it is their actual years in the field.

"seniority_note": 3-5 sentences explaining, for THIS field, what job titles map
  to this candidate's level, which titles are too senior, and which are too
  junior. Name the ladder conventions explicitly (e.g. in pharma a fresh PhD
  enters at "Senior Scientist"; in marketing an 8-year manager targets "Manager"
  and "Senior Manager", never "Coordinator" or "Director").

"lanes": an object of 3-5 lanes keyed "L1".."L5". Each lane is:
  {"label": short name,
   "buys": one sentence — what an employer in this lane is actually buying,
   "why": 2-3 sentences of evidence from the resume,
   "titles": 4-8 exact job titles to search for,
   "title_keywords": 4-8 lowercase substrings that appear in such titles,
   "body_keywords": 8-14 lowercase substrings that appear in such job
     descriptions,
   "strength": "strongest" | "strong" | "reach" | "adjacent"}
  Order lanes by how likely this candidate is to get hired in them.

"skills": 20-35 lowercase terms from the resume that would appear verbatim in a
  matching job description (tools, methods, domains).

"locations_good": lowercase location substrings that fully work.
"locations_ok": lowercase location substrings that are acceptable.
"exclude_title_keywords": 6-12 lowercase title substrings that mean the posting
  is the wrong function for this person.

"ladders": scoring adjustments per employer type, keyed by ladder name. Include
  a "default". Each is {"entry": n, "mid": n, "senior": n, "principal": n,
  "exec": n} where n is roughly -45..+18 and positive means "this rung fits".
  Set these from the field's real conventions, not a template.

"companies": 30-45 real employers to target, each:
  {"name": exact company name,
   "lane": "L1".."L5" or "Adjacent",
   "ladder": one of the keys in "ladders",
   "loc": city or "Remote",
   "watch_titles": comma-separated titles to watch at THIS employer,
   "ats_candidates": 1-3 lowercase guesses at their job-board slug (usually the
     company name lowercased with spaces and punctuation removed),
   "note": one sentence on why this employer fits}
  Weight toward the candidate's stated location. Prefer employers you are
  confident exist and hire for these roles; do not pad the list.

"recruiters": 6-10 staffing or search firms that place this kind of role in this
  market, each {"name": ..., "focus": ..., "note": ...}.

"resources": 5-10 items — certifications, communities, job boards, programs —
  that would materially help, each {"name": ..., "type": ..., "note": ...}.
  Include anything that is a clear skills gap for the lanes above.

"gaps": 2-4 honest weaknesses in this profile relative to the lanes, each a
  sentence. Do not soften them.
"""


# --------------------------------------------------------------------------
# Resume text extraction (no third-party libraries)
# --------------------------------------------------------------------------


def text_from_docx(path):
    """A .docx is a zip of XML. Pull the paragraph text out of it."""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def read_resume(path):
    """Returns (kind, payload). kind is 'text' or 'pdf_b64'."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED:
        raise SystemExit("Unsupported file type %s. Use one of: %s" % (ext, ", ".join(SUPPORTED)))
    if ext == ".pdf":
        with open(path, "rb") as f:
            return "pdf_b64", base64.b64encode(f.read()).decode("ascii")
    if ext == ".docx":
        return "text", text_from_docx(path)
    with open(path, encoding="utf-8", errors="replace") as f:
        return "text", f.read()


# --------------------------------------------------------------------------
# The analysis call
# --------------------------------------------------------------------------


def build_message(kind, payload, location, notes):
    preamble = (
        "You are advising someone on a job search. Read their resume and work out "
        "which kinds of roles they should target, what those roles are called, and "
        "which employers to approach.\n\n"
        "Be concrete and skeptical. Judge only on evidence in the resume — never "
        "invent experience, credentials, or metrics. Where the resume is weak, say "
        "so in \"gaps\" rather than papering over it.\n\n"
        "Candidate's location: %s\n" % (location or "not stated")
    )
    if notes:
        preamble += "Additional context from the candidate: %s\n" % notes
    preamble += "\n" + SCHEMA_INSTRUCTIONS

    if kind == "pdf_b64":
        content = [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": payload}},
            {"type": "text", "text": preamble},
        ]
    else:
        content = [{"type": "text", "text": preamble + "\n\n=== RESUME ===\n" + payload}]
    return [{"role": "user", "content": content}]


def call_claude(messages, max_tokens=16000):
    from app import get_json  # noqa: PLC0415 - avoids circular import at module load

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit(
            "analyze needs an API key.\n\n"
            "    export ANTHROPIC_API_KEY=sk-ant-...\n\n"
            "Get one at https://console.anthropic.com/. This is the only command "
            "that requires it; resolve, discover and serve all work without."
        )
    data, err = get_json(
        "https://api.anthropic.com/v1/messages",
        method="POST",
        timeout=300,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        data={"model": "claude-sonnet-4-6", "max_tokens": max_tokens, "messages": messages},
    )
    if err:
        raise SystemExit("API call failed: %s" % err)
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    if data.get("stop_reason") == "max_tokens":
        print("  ! response hit the token ceiling; the tail may be truncated", file=sys.stderr)
    return text


def parse_json_response(text):
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass
    raise SystemExit("Could not parse the model's response as JSON. Re-run; if it persists, "
                     "the resume may be too long for one pass.")


# --------------------------------------------------------------------------
# Writing the configuration out
# --------------------------------------------------------------------------


def to_profile(a):
    lanes = {}
    for key, lane in (a.get("lanes") or {}).items():
        lanes[key] = {
            "label": lane.get("label", key),
            "title_keywords": [s.lower() for s in lane.get("title_keywords", [])],
            "body_keywords": [s.lower() for s in lane.get("body_keywords", [])],
        }
    return {
        "min_score": 40,
        "top_degree": a.get("top_degree", "phd"),
        "target_years": int(a.get("target_years", 2)),
        "ladders": a.get("ladders") or {},
        "resume_summary": (a.get("headline", "") + " " + a.get("moat", "") + " " + a.get("seniority_note", "")).strip(),
        "skills": [s.lower() for s in a.get("skills", [])],
        "locations_good": [s.lower() for s in a.get("locations_good", [])],
        "locations_ok": [s.lower() for s in a.get("locations_ok", [])],
        "exclude_title_keywords": [s.lower() for s in a.get("exclude_title_keywords", [])],
        "lanes": lanes,
    }


def to_companies(a):
    out = []
    for c in a.get("companies", []):
        out.append(
            {
                "name": c.get("name", "").strip(),
                "lane": c.get("lane", ""),
                "ladder": c.get("ladder", "default"),
                "watch_titles": c.get("watch_titles", ""),
                "loc": c.get("loc", ""),
                "ats_candidates": [s.lower() for s in (c.get("ats_candidates") or [])],
                "note": c.get("note", ""),
            }
        )
    return [c for c in out if c["name"]]


def to_markdown(a, resume_path):
    L = []
    w = L.append
    w("# Job search analysis\n")
    w("Generated from `%s`. Everything here is a starting point to argue with, "
      "not a verdict.\n" % os.path.basename(resume_path))
    w("## Who you are, to an employer\n")
    w(a.get("headline", "") + "\n")
    w("## Your moat\n")
    w(a.get("moat", "") + "\n")
    w("## Level: what to target and what to skip\n")
    w(a.get("seniority_note", "") + "\n")
    w("## Lanes\n")
    for key, lane in (a.get("lanes") or {}).items():
        w("### %s — %s  *(%s)*\n" % (key, lane.get("label", ""), lane.get("strength", "")))
        w("**They buy:** %s\n" % lane.get("buys", ""))
        w(lane.get("why", "") + "\n")
        titles = lane.get("titles") or []
        if titles:
            w("**Search these titles:** " + ", ".join(titles) + "\n")
    gaps = a.get("gaps") or []
    if gaps:
        w("## Gaps, stated plainly\n")
        for g in gaps:
            w("- %s" % g)
        w("")
    recs = a.get("recruiters") or []
    if recs:
        w("## Recruiters to register with\n")
        for r in recs:
            w("- **%s** — %s. %s" % (r.get("name", ""), r.get("focus", ""), r.get("note", "")))
        w("")
    res = a.get("resources") or []
    if res:
        w("## Resources, certifications, communities\n")
        for r in res:
            w("- **%s** (%s) — %s" % (r.get("name", ""), r.get("type", ""), r.get("note", "")))
        w("")
    comps = a.get("companies") or []
    if comps:
        w("## Target employers (%d)\n" % len(comps))
        w("| Company | Lane | Location | Titles to watch |")
        w("| --- | --- | --- | --- |")
        for c in comps:
            w("| %s | %s | %s | %s |" % (c.get("name", ""), c.get("lane", ""),
                                         c.get("loc", ""), c.get("watch_titles", "")))
        w("")
    w("---\n")
    w("Next: `python3 app.py resolve` to find these employers' job boards. Any "
      "company that fails to resolve is either named wrong or not worth polling — "
      "that step doubles as a check on this list.")
    return "\n".join(L)


def run(args):
    path = args.resume
    if not os.path.exists(path):
        raise SystemExit("No such file: %s" % path)
    here = os.path.dirname(os.path.abspath(__file__))

    print("Reading %s …" % os.path.basename(path))
    kind, payload = read_resume(path)
    if kind == "text" and len(payload.strip()) < 200:
        raise SystemExit("That file yielded almost no text (%d chars). If it is a scanned "
                         "PDF, export a text version first." % len(payload.strip()))

    print("Analysing — this takes a minute or two.")
    raw = call_claude(build_message(kind, payload, args.location, args.notes))
    a = parse_json_response(raw)

    profile, companies = to_profile(a), to_companies(a)
    if not profile["lanes"]:
        raise SystemExit("The analysis came back without lanes; re-run.")

    for name, data in (("profile.json", profile), ("companies.json", companies)):
        target = os.path.join(here, name)
        if os.path.exists(target) and not args.force:
            backup = target + ".bak"
            os.replace(target, backup)
            print("  kept your previous %s as %s" % (name, os.path.basename(backup)))
        with open(target, "w") as f:
            json.dump(data, f, indent=2)

    md_path = os.path.join(here, "ANALYSIS.md")
    with open(md_path, "w") as f:
        f.write(to_markdown(a, path))

    print("\nWrote profile.json, companies.json, ANALYSIS.md")
    print("  %d lanes, %d target companies, %d skills" %
          (len(profile["lanes"]), len(companies), len(profile["skills"])))
    for g in (a.get("gaps") or [])[:3]:
        print("  gap: %s" % g)
    print("\nRead ANALYSIS.md and edit companies.json, then: python3 app.py resolve")
