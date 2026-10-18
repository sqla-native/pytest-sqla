"""Run pytest, and fail if any test skipped.

A skipped real-database suite that reports green looks like coverage that is
not there. The integration conftest already fails instead of skipping when
``SQLA_TEST_REQUIRE_ALL=1``; this catches skips from anywhere else too, such
as an ``importorskip`` or a ``skipif`` added later.

    python scripts/check_no_skips.py tests/integration -v
    coverage run scripts/check_no_skips.py -q    # the whole suite, measured

Every argument is passed to pytest, which runs in this process so that
coverage can measure it. The exit code is pytest's, or 1 when pytest passed
but something skipped.
"""

from __future__ import annotations

import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest


def skipped_tests(report: Path) -> list[str]:
    skipped = []
    for case in ElementTree.parse(report).iter("testcase"):
        skip = case.find("skipped")
        if skip is not None:
            name = f"{case.get('classname')}::{case.get('name')}"
            skipped.append(f"{name}: {skip.get('message', '')}")
    return skipped


def main(arguments: list[str]) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "junit.xml"
        code = int(pytest.main([f"--junitxml={report}", *arguments]))
        skipped = skipped_tests(report) if report.exists() else []
    if skipped:
        print(f"\n{len(skipped)} test(s) skipped; this run proves nothing about them:")
        for line in skipped:
            print(f"  {line}")
        if code == 0:
            return 1
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
