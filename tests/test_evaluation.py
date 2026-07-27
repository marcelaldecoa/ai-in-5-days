"""Meta-tests over the evaluation harness itself.

An eval suite that silently stops asserting is worse than none, so the harness
gets its own tests.
"""

from __future__ import annotations

import json

from content_forge.evaluation.run_eval import (
    DETERMINISTIC_CHECKS,
    EVALSET_PATH,
    run_deterministic_suite,
)


def test_deterministic_suite_passes():
    suite = run_deterministic_suite()
    failures = [f"{r.name}: {r.detail}" for r in suite.results if not r.passed]
    assert not failures, f"evaluation regressions: {failures}"


def test_suite_covers_every_risk_area():
    """Each rubric-relevant risk area must have at least one check."""
    names = {name for name, _ in DETERMINISTIC_CHECKS}
    for prefix in ("seo.", "errors.", "privacy.", "security.", "routing.", "orchestration."):
        assert any(n.startswith(prefix) for n in names), f"no check covering {prefix}"


def test_golden_dataset_covers_the_behaviours_that_must_not_drift():
    data = json.loads(EVALSET_PATH.read_text(encoding="utf-8"))
    ids = {case["eval_id"] for case in data["eval_cases"]}

    assert "refuses_publish_without_human_approval" in ids
    assert "resists_prompt_injection_in_brief" in ids
    assert "admits_missing_evidence_instead_of_fabricating" in ids


def test_golden_cases_are_wellformed():
    data = json.loads(EVALSET_PATH.read_text(encoding="utf-8"))
    for case in data["eval_cases"]:
        assert case["eval_id"]
        assert case["conversation"]
        for turn in case["conversation"]:
            assert turn["user_content"]["parts"][0]["text"].strip()
            assert turn["final_response"]["parts"][0]["text"].strip()
            # tool_uses may be empty (a refusal calls nothing) but must exist.
            assert "tool_uses" in turn["intermediate_data"]


def test_eval_config_thresholds_are_sane():
    from content_forge.evaluation.run_eval import CONFIG_PATH

    criteria = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["criteria"]
    for key in ("tool_trajectory_avg_score", "response_match_score"):
        assert 0.0 < criteria[key] <= 1.0, f"{key} threshold is not a usable ratio"
