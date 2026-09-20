# Job discovery agent

Finds open roles at your target companies, scores them against your resume and
your seniority rules, and shows the results in a local web page.

Runs on your machine. No dependencies, no accounts, no scraping of sites that
don't want to be read.

## Run it

```bash
cd jobagent

python3 app.py resolve          # once: find each company's public job board
python3 app.py discover         # fetch open roles and score them
python3 app.py serve            # browse at http://localhost:8765
```

Optional, for a second opinion on the shortlist:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python3 app.py discover         # adds a Claude scoring pass on the top 15
```

Use `python3 app.py discover --no-llm` to skip that pass.

## How it works

When a company opens a role, someone enters it into the hiring software the
company rents — Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee.
The company's careers page is a thin wrapper that asks that software what's open
and displays the answer.

Those systems answer publicly, with no login, because that's how they feed the
careers page. This tool asks the same question directly. That means:

- listings are current — a filled role is simply absent from the answer
- descriptions come through in full, so scoring reads the real requirements
- no stale mirrors, which is what you get from searching the web for jobs

`resolve` figures out which system each company uses and under what name, by
trying a few plausible names against each one and keeping whatever answers with
real roles. It writes the result back into `companies.json`, so it only has to
happen once per company.

## Scoring

`profile.json` holds your resume keywords, your four lanes, and your location
rules. `companies.json` holds each company's watch titles and — importantly —
its `ladder` type, which is how the seniority decoding works:

- `big_pharma`: a fresh PhD enters at **Senior Scientist**, so "Senior" scores well
- `startup`: a fresh PhD enters at **Scientist**, so "Senior" is a step too far
- `academic`: staff scientist and fellow titles score well

On top of that the scorer reads the requirements text for a years-of-experience
figure and for whether a PhD is named. A posting asking for 0–2 years gains a
lot; 5+ years loses more. Principal, Staff and Director titles are penalised
hard, because applying to them is how you spend a year getting no replies.

Every score comes with the reasons behind it and any red flags, so you can
disagree with it. It's a triage tool, not a judge.


## Scoring quality

The scorer is measured, not asserted. `tests/fixtures/postings.json` holds
postings with a `human_label` — the score I would give each one myself — and
the eval report compares the agent against them:

```
$ python3 tests/test_scoring.py --report

human  agent  delta  posting
------------------------------------------------------------------------------
   95     98     +3  Regeneron — Senior Scientist, Computational Biology
   92     94     +2  Flatiron Health — Quantitative Scientist
   88     80     -8  Immunai — Computational Biologist
   84     86     +2  EvolutionaryScale — Research Scientist, Machine Learning
   70     71     +1  Tempus — Bioinformatics Scientist I
   68     65     -3  OpenAI — Research Scientist, Biology Evaluations
   25     29     +4  New York Genome Center — Bioinformatics Analyst
    8      0     -8  Immunai — Principal Scientist, Computational Biology
    0      0     +0  Merck — Director, Computational Biology
    0      0     +0  Illumina — Senior Sales Account Executive
------------------------------------------------------------------------------
n=10   mean abs error 3.8   within 10 pts: 9/10   rank corr 0.95
```

The harness has already paid for itself: the first run scored that NYGC
analyst posting at 76 against a label of 25. It is a BS/MS-level role — a bad
application, not a safe one — which surfaced a missing rule. Detecting
sub-doctoral postings took the mean absolute error from 7.8 to 3.8.

Ten labelled postings is a starting point, not a result. Every scoring change
should move these numbers and say so in the commit message.

## Files

| file | what it holds |
|---|---|
| `app.py` | everything: board clients, resolver, scorer, storage, web UI |
| `companies.json` | your targets, their watch titles, ladder type, board details |
| `profile.json` | your skills, lanes, locations, exclusions, score threshold |
| `jobs.db` | SQLite; every posting ever seen, with first-seen dates |

Because postings are stored with a `first_seen` date and a stable id, running
`discover` on a schedule turns it into a change feed: anything with today's
`first_seen` is new since the last run.

## Running it on a schedule

macOS/Linux, every weekday at 8am, via `crontab -e`:

```
0 8 * * 1-5 cd /path/to/jobagent && /usr/bin/python3 app.py discover >> run.log 2>&1
```

## Notes and limits

- Companies on Workday (much of big pharma) are not covered yet; Workday needs a
  per-tenant endpoint. `resolve` will report them as unresolved.
- If `resolve` can't find a company, open its careers page and look at where the
  "Careers" or "Apply" link sends you — the domain tells you the system, and the
  path segment after it is usually the token. Add `ats` and `ats_token` by hand.
- SmartRecruiters list responses omit descriptions, so those score on title,
  location and seniority only.
- The tool identifies itself by User-Agent and only reads boards companies
  publish for the public. Don't point it at LinkedIn — that's against their terms
  and a good way to lose the account you need.
