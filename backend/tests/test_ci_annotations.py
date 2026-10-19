"""The `::error` lines conftest prints for a failing test on GitHub Actions.

They are how a failure is read from the check run when the job's log cannot
be fetched, so a malformed one does not fail loudly: it simply never shows up
as an annotation.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

from tests.conftest import github_annotations


def _report(nodeid, when, location, crash=None, longrepr=""):
    return SimpleNamespace(
        nodeid=nodeid,
        when=when,
        location=location,
        longrepr=SimpleNamespace(reprcrash=crash) if crash else longrepr,
    )


def test_the_line_is_where_it_failed_and_names_the_test(tmp_path):
    root = tmp_path / "backend"
    crash = SimpleNamespace(
        path=str(root / "tests" / "test_x.py"), lineno=12, message="assert 1 == 2\n +  where ..."
    )
    [line] = github_annotations(
        [_report("tests/test_x.py::test_y", "call", ("tests/test_x.py", 9, "test_y"), crash)],
        str(root),
        str(tmp_path),
    )
    assert line == "::error file=backend/tests/test_x.py,line=12::tests/test_x.py::test_y: assert 1 == 2"


def test_a_crash_outside_the_workspace_points_at_the_test(tmp_path):
    root = tmp_path / "backend"
    crash = SimpleNamespace(path="/usr/lib/python3/somewhere.py", lineno=5, message="KeyError: 'x'")
    [line] = github_annotations(
        [_report("tests/test_x.py::test_y", "setup", ("tests/test_x.py", 9, "test_y"), crash)],
        str(root),
        str(tmp_path),
    )
    assert line.startswith("::error file=backend/tests/test_x.py,line=10::")
    assert line.endswith("tests/test_x.py::test_y (setup): KeyError: 'x'")


def test_newlines_colons_and_commas_cannot_break_the_command(tmp_path):
    root = tmp_path / "back,end:x"
    os.makedirs(root)
    crash = SimpleNamespace(path=str(root / "t.py"), lineno=1, message="50% done, a: b\nsecond line")
    [line] = github_annotations(
        [_report("t.py::test[a\r\nb]", "call", ("t.py", 0, "test"), crash)], str(root), str(tmp_path)
    )
    assert "\n" not in line and "\r" not in line
    assert line.startswith("::error file=back%2Cend%3Ax/t.py,line=1::")
    # Only the property needs `:` and `,` escaped; the message keeps them.
    assert line.endswith("t.py::test[a%0D%0Ab]: 50%25 done, a: b")


def test_a_collection_error_names_its_last_line(tmp_path):
    root = tmp_path / "backend"
    report = SimpleNamespace(
        nodeid="tests/test_bad.py",
        longrepr="tests/test_bad.py:1: in <module>\n    def x(:\nE   SyntaxError: invalid syntax\n",
    )
    [line] = github_annotations([report], str(root), str(tmp_path))
    assert line == (
        "::error file=backend/tests/test_bad.py,line=1::"
        "tests/test_bad.py (collect): SyntaxError: invalid syntax"
    )
