"""Tests for scripts/update_notes.py — the pure text/bytes side of notes.json
(pfBlockerNG/pfBlockerNG#3816). The publisher wiring is covered in
test_publish_nightly.py / test_publish_release.py."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import update_notes as un

_PREV = "a" * 40
_NEW = "b" * 40

# The real v3.3.14 Release body (pfBlockerNG/pfBlockerNG), verbatim: a heading,
# one long bullet, and the Full changelog line carrying an arrow and backticks.
_V3_3_14_BODY = (
    "## Bug Fixes\n"
    "\n"
    "- With RAM disks enabled, the boot-restore archive (`aliastables.tar.bz2`) is now "
    "written whenever it is missing: on every update pass, even when no lists changed, "
    "and when the firewall or DNSBL service stops. Previously it was written only when "
    "lists changed, so after a reboot blocking stayed down until a Force Update. An "
    "existing archive is not rewritten, and the archive is replaced atomically so a "
    "failed write never leaves a partial file. Note: after enabling RAM disks for the "
    "first time, run an update before rebooting so the restore hooks are registered. "
    "([#3713](https://github.com/pfBlockerNG/pfBlockerNG/issues/3713))\n"
    "\n"
    "**Full changelog:** [`v3.3.13` \u2192 `v3.3.14`]"
    "(https://github.com/pfBlockerNG/pfBlockerNG/compare/v3.3.13...v3.3.14)\n"
)


class CleanItemTests(unittest.TestCase):
    def test_links_backticks_and_arrow_become_plain_ascii(self) -> None:
        self.assertEqual(
            un.clean_item(
                "Fix [#3713](https://x/3713) in `aliastables.tar.bz2` \u2192 **now** atomic"
            ),
            "Fix #3713 in aliastables.tar.bz2 -> now atomic",
        )

    def test_non_ascii_whitespace_and_control_characters(self) -> None:
        self.assertEqual(
            un.clean_item(
                "Caf\u00e9 \u2022 na\u00efve\u2026 \U0001f680 rocket\tline\ntwo  end"
            ),
            "Cafe - naive... rocket line two end",
        )
        self.assertEqual(un.clean_item("a\x07b\x7fc"), "a b c")
        self.assertEqual(
            un.clean_item("\u2018q\u2019 \u201cdq\u201d a\u2014b a\u2013b \u2190 x"),
            "'q' \"dq\" a-b a-b <- x",
        )
        self.assertEqual(un.clean_item("\u65e5\u672c"), "")
        self.assertEqual(un.clean_item("__under__ and __x__"), "under and x")

    def test_long_text_is_cut_to_100_chars_ending_in_ellipsis(self) -> None:
        cut = un.clean_item("x" * 500)
        self.assertEqual(cut, "x" * 97 + "...")
        self.assertEqual(len(cut), 100)
        self.assertEqual(un.clean_item("y" * 100), "y" * 100)
        self.assertEqual(un.clean_item("y" * 101), "y" * 97 + "...")

    def test_images_are_dropped_and_links_keep_their_text(self) -> None:
        self.assertEqual(un.clean_item("![shot](http://x/a.png)"), "")
        self.assertEqual(un.clean_item("a ![shot](http://x/a.png) b"), "a b")
        self.assertEqual(un.clean_item("[#3713](https://x/3713)"), "#3713")
        self.assertEqual(
            un.clean_item("[wiki](https://x/Foo_(bar)) rest"), "wiki rest"
        )

    def test_html_is_kept_as_literal_text(self) -> None:
        self.assertEqual(
            un.clean_item("<script>alert(1)</script>"), "<script>alert(1)</script>"
        )


class ReleaseItemsTests(unittest.TestCase):
    def test_real_v3_3_14_body_yields_exactly_one_item(self) -> None:
        self.assertEqual(
            un.release_items(_V3_3_14_BODY),
            [
                "With RAM disks enabled, the boot-restore archive "
                "(aliastables.tar.bz2) is now written whenever it..."
            ],
        )

    def test_top_level_bullets_only_in_order_with_blanks_dropped(self) -> None:
        body = "\r\n".join(
            [
                "## Bug Fixes",
                "- First",
                "* Second",
                "  - nested one",
                "    * nested two",
                "\t- tab nested",
                "- ",
                "-   ",
                "-glued is not a bullet",
                "* Third **bold** [x](http://y)",
                "plain paragraph - with a dash",
                "## Features",
                "- Fourth",
                "- Fifth",
                "- Sixth",
                "- Seventh",
                "**Full changelog:** [`a` \u2192 `b`](http://c)",
            ]
        )
        self.assertEqual(
            un.release_items(body),
            [
                "First",
                "Second",
                "Third bold x",
                "Fourth",
                "Fifth",
                "Sixth",
                "Seventh",
            ],
        )

    def test_body_without_bullets_yields_nothing(self) -> None:
        self.assertEqual(un.release_items(""), [])
        self.assertEqual(un.release_items("## Notes\n\nJust a paragraph.\n"), [])

    def test_thematic_breaks_are_not_bullets(self) -> None:
        body = "\n".join(
            [
                "* * *",
                "- - -",
                "-  -  -  -",
                "* _ -",
                "- Real bullet",
                "* * *",
                "- - -",
                "- -- is a flag",
            ]
        )
        self.assertEqual(un.release_items(body), ["Real bullet", "-- is a flag"])

    def test_hostile_bullets(self) -> None:
        body = "\n".join(
            [
                "- [#3713](https://github.com/x/3713)",
                "- ![only](http://x/an-image.png)",
                "- <script>alert(1)</script>",
            ]
        )
        self.assertEqual(
            un.release_items(body), ["#3713", "<script>alert(1)</script>"]
        )

    def test_ten_thousand_bullets_are_all_returned(self) -> None:
        body = "\n".join(f"- item {n}" for n in range(10_000))
        items = un.release_items(body)
        self.assertEqual(len(items), 10_000)
        self.assertEqual(items[0], "item 0")
        self.assertEqual(items[-1], "item 9999")


def _compare(*messages: str, total: int | None = None) -> dict:
    # The GitHub compare API lists commits oldest first.
    return {
        "total_commits": len(messages) if total is None else total,
        "commits": [{"commit": {"message": message}} for message in messages],
    }


class CommitSubjectsTests(unittest.TestCase):
    def test_subjects_are_newest_first_first_line_only_and_cleaned(self) -> None:
        self.assertEqual(
            un.commit_subjects(
                _compare(
                    "oldest subject\n\nbody line",
                    "middle [#1](http://x) `code`\nmore",
                    "newest",
                )
            ),
            (["newest", "middle #1 code", "oldest subject"], 3),
        )

    def test_empty_subjects_are_dropped_but_total_stays_the_commit_count(self) -> None:
        self.assertEqual(
            un.commit_subjects(_compare("kept", "\n\nbody only", "![i](http://x/y.png)")),
            (["kept"], 3),
        )

    def test_malformed_responses_raise_value_error(self) -> None:
        bad = {
            "not a dict": [],
            "missing commits": {"total_commits": 1},
            "missing total": {"commits": []},
            "commits not a list": {"commits": "x", "total_commits": 1},
            "total is a string": {"commits": [], "total_commits": "0"},
            "total is a bool": {"commits": [], "total_commits": False},
            "commit is not an object": {"commits": ["x"], "total_commits": 1},
            "commit has no commit object": {"commits": [{}], "total_commits": 1},
            "message missing": {"commits": [{"commit": {}}], "total_commits": 1},
            "message not a string": {
                "commits": [{"commit": {"message": 5}}],
                "total_commits": 1,
            },
            # More commits exist than the response carried: the listed ones are not
            # the newest, so claiming "newest first" would be wrong.
            "truncated list": _compare("a", "b", total=300),
        }
        for label, response in bad.items():
            with self.subTest(label), self.assertRaises(ValueError):
                un.commit_subjects(response)  # type: ignore[arg-type]


class RenderTests(unittest.TestCase):
    def test_seven_items_render_five_with_the_full_total(self) -> None:
        data = un.render(
            "3.3.14",
            ["a", "b", "c", "d", "e", "f", "g"],
            7,
            {"kind": "release", "tag": "v3.3.14"},
        )
        self.assertEqual(
            data,
            b'{"items":["a","b","c","d","e"],"link":{"kind":"release","tag":"v3.3.14"},'
            b'"schema":1,"total":7,"version":"3.3.14"}\n',
        )

    def test_compare_link_keys_are_sorted(self) -> None:
        data = un.render(
            "20260805120000.ccccccc",
            ["only"],
            12,
            {"kind": "compare", "from": _PREV, "to": _NEW},
        )
        self.assertEqual(
            data,
            (
                '{"items":["only"],"link":{"from":"%s","kind":"compare","to":"%s"},'
                '"schema":1,"total":12,"version":"20260805120000.ccccccc"}\n'
                % (_PREV, _NEW)
            ).encode(),
        )

    def test_no_items_renders_nothing(self) -> None:
        self.assertIsNone(un.render("3.3.14", [], 0, {"kind": "release", "tag": "v1"}))

    def test_output_is_pure_ascii_json(self) -> None:
        data = un.render("1", ["caf\u00e9"], 1, {"kind": "release", "tag": "v1"})
        assert data is not None
        self.assertEqual(data, data.decode("ascii").encode("ascii"))
        self.assertEqual(json.loads(data)["items"], ["caf\u00e9"])

    def test_ten_thousand_items_still_render_five(self) -> None:
        items = un.release_items("\n".join(f"- item {n}" for n in range(10_000)))
        data = un.render("1", items, len(items), {"kind": "release", "tag": "v1"})
        assert data is not None
        doc = json.loads(data)
        self.assertEqual(doc["items"], [f"item {n}" for n in range(5)])
        self.assertEqual(doc["total"], 10_000)


class FetchCompareTests(unittest.TestCase):
    """``fetch_compare`` runs a fake ``gh`` found through PATH."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="update-notes-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "gh.log"
        patcher = mock.patch.dict(
            os.environ,
            {
                "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                "FAKE_GH_LOG": str(self.log),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_gh(self, body: str) -> None:
        gh = self.bin / "gh"
        gh.write_text(
            '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$FAKE_GH_LOG"\n' + body,
            encoding="utf-8",
        )
        gh.chmod(0o755)

    def _call(self, prev: str = _PREV, new: str = _NEW) -> tuple[object, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = un.fetch_compare(prev, new)
        return result, err.getvalue()

    def test_success_returns_the_parsed_response_for_the_exact_endpoint(self) -> None:
        self._fake_gh("""printf '{"total_commits":1,"commits":[]}'\n""")
        result, err = self._call()
        self.assertEqual(result, {"total_commits": 1, "commits": []})
        self.assertEqual(err, "")
        self.assertEqual(
            self.log.read_text(encoding="utf-8").splitlines(),
            [f"api repos/pfBlockerNG/pfBlockerNG/compare/{_PREV}...{_NEW}"],
        )

    def test_non_40_hex_shas_return_none_without_running_gh(self) -> None:
        self._fake_gh("""printf '{"total_commits":0,"commits":[]}'\n""")
        for label, prev, new in (
            ("short", "a" * 39, _NEW),
            ("long", "a" * 41, _NEW),
            ("upper", "A" * 40, _NEW),
            ("not hex", "g" * 40, _NEW),
            ("empty", "", _NEW),
            ("option-like", "--hostile" + "a" * 31, _NEW),
            ("path injection", "../" + "a" * 37, _NEW),
            ("bad new", _PREV, "x" * 40),
            ("trailing newline", _PREV, _NEW + "\n"),
        ):
            with self.subTest(label):
                result, _err = self._call(prev, new)
                self.assertIsNone(result)
        self.assertFalse(self.log.exists(), "gh must never run for an invalid sha")

    def test_failing_gh_returns_none_with_one_warning_line(self) -> None:
        self._fake_gh("echo 'gh: Not Found (HTTP 404)' >&2\nexit 1\n")
        result, err = self._call()
        self.assertIsNone(result)
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertNotIn("Traceback", err)

    def test_non_json_output_returns_none_with_one_warning_line(self) -> None:
        self._fake_gh("echo 'definitely not json'\n")
        result, err = self._call()
        self.assertIsNone(result)
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertNotIn("Traceback", err)

    def test_json_that_is_not_an_object_returns_none(self) -> None:
        self._fake_gh("echo '[1,2]'\n")
        result, err = self._call()
        self.assertIsNone(result)
        self.assertEqual(len(err.splitlines()), 1, err)

    def test_deeply_nested_json_returns_none_with_one_warning_line(self) -> None:
        # json.loads raises RecursionError, not ValueError, on this; a Nightly
        # publication must not abort over a hostile or broken gh response.
        self._fake_gh("head -c 200000 /dev/zero | tr '\\0' '['\n")
        result, err = self._call()
        self.assertIsNone(result)
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertTrue(err.startswith("::warning::"), err)
        self.assertNotIn("Traceback", err)

    def test_missing_gh_returns_none_with_one_warning_line(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}):
            result, err = self._call()
        self.assertIsNone(result)
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertNotIn("Traceback", err)

    def test_timeout_returns_none_with_one_warning_line(self) -> None:
        with mock.patch.object(
            un.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=60),
        ) as run:
            result, err = self._call()
        self.assertIsNone(result)
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertEqual(run.call_args.kwargs["timeout"], 60)


if __name__ == "__main__":
    unittest.main()
