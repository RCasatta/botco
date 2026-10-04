import json
import os
import time

import pytest

from botco.lab import Lab
from botco.text import check_post


@pytest.fixture
def lab(tmp_path):
    (tmp_path / "exl3-setup").mkdir()
    (tmp_path / "llama.cpp" / "docs").mkdir(parents=True)
    (tmp_path / "SUMMARY.md").write_text(
        "# Results on 2x 5070 Ti\n\n## Result (2026-09-25)\n| Decode @ 160K | 63.9 t/s |\n\n"
        "## Rejected\n- SGLang: KV too small\n\n## Old\nbody " + "x" * 30000 + "\n")
    (tmp_path / "exl3-setup" / "REPORT.md").write_text("Draft acceptance 67%\n")
    (tmp_path / "CLAUDE.md").write_text("# internal\n")
    (tmp_path / "llama.cpp" / "docs" / "build.md").write_text("# vendored\n")
    return Lab(tmp_path, ["CLAUDE.md", "llama.cpp"])


def test_lists_reports_but_not_excluded_ones(lab):
    notes = {n.path: n.title for n in lab.notes()}
    assert notes == {"SUMMARY.md": "Results on 2x 5070 Ti", "exl3-setup/REPORT.md": "REPORT"}


def test_reads_sections_parts_and_refuses_other_files(lab):
    assert "63.9 t/s" in lab.read("SUMMARY.md", section="result")
    assert "SGLang" not in lab.read("SUMMARY.md", section="result")
    whole = lab.read("SUMMARY.md")
    assert whole.startswith("[part 1 of 3") and "- Rejected" in whole
    assert "x" * 100 in lab.read("SUMMARY.md", part=3)
    assert "Sections" in lab.read("SUMMARY.md", section="nope")
    assert "acceptance" in lab.read("REPORT.md")  # by file name alone
    for bad in ("CLAUDE.md", "../etc/passwd", "llama.cpp/docs/build.md"):
        with pytest.raises(FileNotFoundError):
            lab.read(bad)


def test_search(lab):
    assert lab.search("decode 160k") == "SUMMARY.md:4: | Decode @ 160K | 63.9 t/s |"
    assert lab.search("nothing like this") == "no matches"


def test_posts_keep_internal_details_out():
    for leak in ("served on 100.110.202.124 port 8080", "see ~/best.sh", "logs in /var/lib/hermes", "on localhost"):
        assert any("internal" in p for p in check_post(leak, 280, [])), leak
    assert check_post("ExLlamaV3 1.5.1 decodes 63.9 t/s at 160K context", 280, []) == []
