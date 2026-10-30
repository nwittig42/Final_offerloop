"""Write a synthetic one-page test resume PDF (not a real person).

Usage: .venv/bin/python scripts/e2e-emulator/make_test_resume.py [out.pdf]
Default output: scripts/e2e-emulator/data/e2e-test-resume.pdf
"""
import sys
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

LINES = [
    "Avery E2E-Student",
    "avery.e2e@example.test",
    "University of Southern California, Los Angeles, CA",
    "B.S. in Economics, May 2027",
    "Objective: Interested in investment banking and strategy roles.",
    "Experience",
    "Summer Analyst at Regional Investment Bank.",
    "Finance Intern at Campus Startup.",
    "Activities",
    "Club: Trojan Investment Society.",
    "Club: Consulting Club.",
]


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "data" / "e2e-test-resume.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(out), pagesize=letter)
    y = 740
    for line in LINES:
        c.drawString(72, y, line)
        y -= 22
    c.save()
    print(out)


if __name__ == "__main__":
    main()
