"""Decision D-007: model outputs are shown as SCORES, not probabilities, until
a walk-forward model beats the base rate. The static frontend must never
present the heuristic as a probability, must render every forward-grading
category, and ships zero user-visible em dashes."""
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
UI_FILES = ["app.js", "pages-ipo-detail.js", "index.html", "app.html", "landing.js", "login.js", "nav.js"]
EM_DASH = "—"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_scores_are_labelled_as_scores_not_probabilities():
    for name in ("app.js", "pages-ipo-detail.js", "index.html"):
        text = _read(name)
        assert "Listing score" in text, name
        assert "Listing probability" not in text, name


def test_score_pills_read_score_fields_not_probability_fields():
    for name in ("app.js", "pages-ipo-detail.js"):
        text = _read(name)
        assert "fmt(s.listing, 0)" in text, name
        assert "fmt(s.listing_probability" not in text, name
        assert "fmt(s.long_term_probability" not in text, name
        assert "not calibrated probabilities" in text, name


def test_track_record_renders_every_grading_category():
    text = _read("app.js")
    assert "ledger.by_category" in text or "t.ledger.by_category" in text
    for cat in ("GRADED", "PENDING", "NOT_YET_ELIGIBLE", "INVALID_FORWARD_RECORD", "BLOCKED"):
        assert cat in text, cat
    # Old JSON without a ledger must still render.
    assert "t.graded_with_outcome" in text


def test_model_performance_flags_no_discrimination():
    assert "No discriminating power measured yet" in _read("app.js")


def test_no_user_visible_em_dashes_in_frontend():
    for name in UI_FILES:
        if (STATIC / name).exists():
            assert EM_DASH not in _read(name), name
