# CLAUDE.md

Context for Claude Code working in this repo.

## What this is

A job discovery agent. It asks companies' hiring software what roles are open,
scores each posting against one candidate's resume and seniority rules, and
serves the results as a local web page.

It has two audiences and both matter:

1. **A working tool** for a real job search — it has to actually find real roles.
2. **A portfolio project** for ML/AI roles. The differentiator is not the
   fetching; it is the scoring and its **eval harness**. Changes that improve
   measured scoring quality are worth more than changes that add features.

## Layout

```
app.py                      everything: ATS clients, resolver, scorer, store, server
companies.json              targets: watch titles, ladder type, resolved board
profile.json                resume keywords, lanes, locations, exclusions, threshold
tests/test_scoring.py       unittest suite + `--report` eval mode
tests/fixtures/postings.json  labelled postings (human_label = the target score)
jobs.db                     SQLite, gitignored
```

Deliberately dependency-free (stdlib only, Python 3.9+) so it runs anywhere with
no setup. **Do not add third-party dependencies without asking.** If a change
seems to need one, propose it first and say what it buys.

## Commands

```bash
python3 app.py resolve              # find each company's public job board
python3 app.py discover [--no-llm]  # fetch + score open roles
python3 app.py serve --port 8765    # local UI

python3 -m unittest discover tests  # regression suite — must stay green
python3 tests/test_scoring.py --report   # scorer vs. human labels
```

## The domain rules the scorer encodes

These come from how scientific hiring actually works and are the heart of the
project. Don't "simplify" them away.

- **Ladder type decides what a title means.** At big pharma (`ladder:
  "big_pharma"`) a fresh PhD enters at *Senior Scientist*. At a startup
  (`ladder: "startup"`) the same fresh PhD enters at *Scientist* / *Scientist I*,
  and *Senior* is a stretch. `academic` favours staff-scientist and fellow roles.
- **Years-of-experience is the real seniority signal**, more reliable than the
  title. 0–2 gains a lot; 5+ is disqualifying.
- **Under-levelling is as bad as over-levelling.** A BS/MS-pitched role with no
  PhD track is a bad application, not a safe one.
- **Four lanes** (see `profile.json`): L1 computational biology, L2 ML/techbio,
  L3 real-world evidence/quantitative, L4 AI-lab evaluation work. Lane detection
  should stay permissive — adjacent roles are wanted, not filtered out.
- Every score must carry its reasoning. A score with no `why` is a bug.

## Working agreements

- **Every scoring change runs the eval report before and after**, and the result
  goes in the commit message (e.g. `MAE 7.8 → 3.8, rank corr 0.92 → 0.95`).
  That history is the story the repo tells a hiring manager.
- Network calls fail soft: log the company, continue the run. A single dead
  board must never abort `discover`.
- Only read boards that companies publish publicly. Identify via User-Agent
  (`CONTACT_EMAIL` env var). Never add LinkedIn scraping — ToS violation and an
  account-ban risk for the user.
- Don't commit `jobs.db` or anything with personal contact details.

## Roadmap

Roughly in value order:

1. **Workday support.** Much of the NJ pharma belt (Merck, BMS, Novartis,
   Regeneron) is on Workday, currently unresolvable. Needs a per-tenant
   `/wday/cxs/{tenant}/{site}/jobs` POST client plus tenant discovery.
2. **Grow the labelled fixture set** to 100+ real postings, so the eval numbers
   mean something. This is the highest-value work in the repo.
3. **Failure taxonomy** in the report: group errors by cause (over-scored
   senior roles, missed lanes, location misreads) rather than one MAE number.
4. **Ablations**: rules-only vs. LLM-only vs. blended, with cost and latency per
   posting. A table of these is the centrepiece of the writeup.
5. Change feed: `discover --since` to show only postings first seen after a date.
6. Optional: split `app.py` into a package once it passes ~800 lines.

## Writeup

The project is the final chapter of an "AI proving ground" arc on
pocketmarble.github.io that currently ends at *transformer from scratch*. The
writeup should lead with the eval table and a failure analysis, not with the
architecture diagram.
