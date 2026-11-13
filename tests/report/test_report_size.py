"""A weight budget for the report page (#42), so it does not quietly grow back.

The 2026 season is built for real (a one-second search is enough: the page's size depends
on the fixtures and their badges, not on how well they were placed) and the page measured.
The limits are the issue's own targets, set above today's numbers by about a tenth so a
normal change fits and an accidental per-fixture duplication does not:

    page          under 1 MB          (about 0.89 MB today)
    gzipped       under 180 kB        (about 0.14 MB)
    live nodes    under 10,000        (about 6,800 desktop and 3,700 phone in the default view,
                                       8,700 in the busiest desktop view, the combined list)

If one fails, look for markup repeated per fixture or per option before raising a limit.
"""

from __future__ import annotations

import gzip
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAX_BYTES = 1_000_000
MAX_GZIP_BYTES = 180_000
MAX_NODES = 10_000


@pytest.fixture(scope="module")
def season_page(tmp_path_factory):
    out = tmp_path_factory.mktemp("season")
    subprocess.run(
        [sys.executable, "cli.py", "generate", "--season", "2026", "--solver", "local", "--seed", "42",
         "--time-budget", "1", "--out", str(out)],
        cwd=ROOT, check=True, capture_output=True,
    )
    return out / "2026.html"


def test_the_page_stays_under_a_megabyte(season_page):
    assert season_page.stat().st_size < MAX_BYTES


def test_the_page_compresses_to_under_the_limit(season_page):
    assert len(gzip.compress(season_page.read_bytes())) < MAX_GZIP_BYTES


@pytest.mark.parametrize("viewport", [{"width": 1280, "height": 900}, {"width": 390, "height": 844}])
def test_the_default_view_draws_fewer_than_ten_thousand_nodes(browser, season_page, viewport):
    page = browser.new_page(viewport=viewport)
    page.goto(season_page.as_uri())
    nodes = page.evaluate("document.querySelectorAll('*').length")
    page.close()
    assert nodes < MAX_NODES


def test_the_busiest_view_stays_under_the_node_limit(browser, season_page):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(season_page.as_uri())
    page.click('#panel-1 .view-btn[data-view="combined-list"]')
    nodes = page.evaluate("document.querySelectorAll('*').length")
    page.close()
    assert nodes < MAX_NODES
