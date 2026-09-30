"""Source files must not contain control characters. A literal backspace in
a regex (written where \\b was meant) silently makes the pattern unmatchable;
this happened twice in sec.py and rhp_financials.py."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = {chr(c) for c in range(0, 32)} - {"\n", "\r", "\t"}


def test_no_control_characters_in_python_sources():
    bad = []
    for folder in ("app", "scripts", "tests"):
        for p in (ROOT / folder).rglob("*.py"):
            text = p.read_text(encoding="utf-8")
            hits = sorted({repr(ch) for ch in text if ch in FORBIDDEN})
            if hits:
                bad.append(f"{p.relative_to(ROOT)}: {', '.join(hits)}")
    assert not bad, bad
