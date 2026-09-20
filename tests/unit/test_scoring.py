from pathlib import Path

from adlc_workflow.scoring import _score_document


def test_score_document_accepts_verdict_summary_score(tmp_path: Path) -> None:
    review = tmp_path / "scorer-review.md"
    review.write_text(
        "# Review\n\n**Verdict:** **REVISE** — score: 6/8\n\n"
        "| Feasibility | 1/2 |\n| Testability | 1/2 |\n"
        "| Scope | 2/2 |\n| Architecture | 2/2 |\n",
        encoding="utf-8",
    )

    assert _score_document(review) == (6, 8, 0)


def test_score_document_accepts_bare_total_table_with_rubric_maximum(tmp_path: Path) -> None:
    review = tmp_path / "scorer-review.md"
    review.write_text(
        "### 1. Feasibility — Score: 1\n### 2. Testability — Score: 1\n"
        "### 3. Scope — Score: 2\n### 4. Architecture — Score: 1\n\n"
        "| Dimension | Score |\n| **Total** | **5** |\n",
        encoding="utf-8",
    )

    assert _score_document(review, expected_maximum=8) == (5, 8, 0)
