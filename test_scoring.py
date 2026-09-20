#!/usr/bin/env python3
"""
Tests for the scorer, plus the seed of an eval harness.

    python3 -m unittest discover tests     # pass/fail regression suite
    python3 tests/test_scoring.py --report # scorer vs. human labels, per posting

The fixtures in tests/fixtures/postings.json carry a `human_label` — the score
Austin would give that posting himself. The report measures how close the
scorer gets. Grow the fixture file as real postings come in; that labelled set
is what turns this from a script into something with a metric.
"""

import importlib.util
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

spec = importlib.util.spec_from_file_location("app", os.path.join(ROOT, "app.py"))
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

PROFILE = json.load(open(os.path.join(ROOT, "profile.json")))
COMPANIES = {c["name"]: c for c in json.load(open(os.path.join(ROOT, "companies.json")))}
FIXTURES = json.load(open(os.path.join(HERE, "fixtures", "postings.json")))


def score_fixture(fx):
    company = COMPANIES.get(fx["company"], {"name": fx["company"], "ladder": "startup", "watch_titles": ""})
    job = {"title": fx["title"], "location": fx["location"], "description": fx["description"]}
    return app.score_posting(job, company, PROFILE)


class TestSeniorityDecoding(unittest.TestCase):
    def test_titles_classify(self):
        cases = {
            "Senior Scientist, Computational Biology": "senior",
            "Principal Scientist": "principal",
            "Director, Computational Biology": "exec",
            "Bioinformatics Analyst": "mid",
            "Associate Scientist": "entry",
            "Scientist I": "entry",
        }
        for title, expected in cases.items():
            self.assertEqual(app.detect_seniority(title), expected, title)

    def test_years_extraction(self):
        self.assertEqual(app.min_years_required("PhD with 0-2 years of experience"), 0)
        self.assertEqual(app.min_years_required("requires 5+ years industry experience"), 5)
        self.assertEqual(app.min_years_required("MS with 4 years or PhD"), 4)
        self.assertIsNone(app.min_years_required("PhD required. No years mentioned."))

    def test_same_title_scores_by_ladder(self):
        """'Senior Scientist' is the entry rung at big pharma, a stretch at a startup."""
        desc = "PhD in computational biology. RNA-seq, Python, genomics."
        job = {"title": "Senior Scientist, Computational Biology", "location": "New York, NY", "description": desc}
        pharma = app.score_posting(dict(job), {"name": "X", "ladder": "big_pharma", "watch_titles": ""}, PROFILE)
        startup = app.score_posting(dict(job), {"name": "Y", "ladder": "startup", "watch_titles": ""}, PROFILE)
        self.assertGreater(pharma["score"], startup["score"])


class TestScoringBehaviour(unittest.TestCase):
    def test_too_senior_is_suppressed(self):
        for fid in ("immunai-principal-scientist", "merck-director-compbio"):
            fx = next(f for f in FIXTURES if f["id"] == fid)
            j = score_fixture(fx)
            self.assertLess(j["score"], 40, fid)
            self.assertTrue(j["flags"], "expected a flag on %s" % fid)

    def test_wrong_function_is_rejected(self):
        fx = next(f for f in FIXTURES if f["id"] == "illumina-sales-ae")
        self.assertLess(score_fixture(fx)["score"], 30)

    def test_strong_matches_clear_threshold(self):
        for fid in ("regeneron-senior-scientist-compbio", "flatiron-quantitative-scientist",
                    "immunai-computational-biologist"):
            fx = next(f for f in FIXTURES if f["id"] == fid)
            self.assertGreaterEqual(score_fixture(fx)["score"], 75, fid)

    def test_lane_assignment(self):
        expected = {
            "flatiron-quantitative-scientist": "L3",
            "evolutionaryscale-research-scientist-ml": "L2",
            "openai-research-scientist-bio-evals": "L4",
        }
        for fid, lane in expected.items():
            fx = next(f for f in FIXTURES if f["id"] == fid)
            self.assertEqual(score_fixture(fx).get("lane"), lane, fid)

    def test_every_score_carries_reasoning(self):
        for fx in FIXTURES:
            self.assertTrue(score_fixture(fx)["why"].strip(), fx["id"])


def report():
    """Scorer vs. human labels. This is the number to improve."""
    rows, errs = [], []
    for fx in FIXTURES:
        j = score_fixture(fx)
        d = j["score"] - fx["human_label"]
        errs.append(abs(d))
        rows.append((fx["human_label"], j["score"], d, fx["company"], fx["title"]))
    rows.sort(key=lambda r: -r[0])
    print("%5s %6s %6s  %s" % ("human", "agent", "delta", "posting"))
    print("-" * 78)
    for h, a, d, co, t in rows:
        print("%5d %6d %+6d  %s — %s" % (h, a, d, co, t[:42]))
    n = len(errs)
    mae = sum(errs) / n
    within10 = sum(1 for e in errs if e <= 10)
    # rank agreement (Spearman, no scipy)
    def ranks(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0] * len(vals)
        for pos, i in enumerate(order):
            r[i] = pos
        return r
    h_r, a_r = ranks([r[0] for r in rows]), ranks([r[1] for r in rows])
    d2 = sum((h_r[i] - a_r[i]) ** 2 for i in range(n))
    rho = 1 - (6 * d2) / (n * (n * n - 1))
    print("-" * 78)
    print("n=%d   mean abs error %.1f   within 10 pts: %d/%d   rank corr %.2f" % (n, mae, within10, n, rho))
    print("\nAdd real postings with your own labels to tests/fixtures/postings.json,")
    print("then re-run. Watch MAE fall and rank correlation rise as you tune weights.")


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    else:
        unittest.main()
