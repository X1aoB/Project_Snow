from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest import TestCase, skipUnless
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_release_announcement.py"
SPEC = importlib.util.spec_from_file_location("release_announcement_check", SCRIPT)
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)
NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def notice(identifier="previous", *, published="2026-09-26T00:00:00+08:00", **changes):
    return {"id": identifier, "published_at": published, "title": "原有公告", "summary": "已发布的说明。", "items": ["保留历史内容。"], **changes}


def feed(*updates):
    return json.dumps({"schema_version": 1, "timezone": "Asia/Shanghai", "updates": list(updates), "birthdays": []}, ensure_ascii=False).encode()


class AnnouncementChangeTests(TestCase):
    def assert_rejected(self, candidate, code, *, base=None):
        with self.assertRaises(check.AnnouncementCheckError) as raised:
            check.check_feeds(base or feed(notice()), candidate, now=NOW)
        self.assertEqual(raised.exception.code, code)

    def test_new_effective_notice_and_preserved_history_pass(self):
        candidate = feed(notice("new", published="2026-10-14T10:43:11+08:00", title="聊天反馈优化"), notice())
        self.assertEqual(check.check_feeds(feed(notice()), candidate, now=NOW), {"effective_new_ids": ["new"], "effective_revision_ids": []})

    def test_no_update_and_only_future_notice_reject(self):
        self.assert_rejected(feed(notice()), "update_missing")
        self.assert_rejected(feed(notice("future", published="2026-10-15T00:00:00+00:00"), notice()), "update_missing")

    def test_duplicate_ids_and_invalid_plaintext_or_dates_reject(self):
        self.assert_rejected(feed(notice(), notice()), "duplicate_id")
        for field, value, code in (("title", "  \t", "text_invalid"), ("summary", "<b>text</b>", "text_invalid"),
                                   ("items", [], "text_invalid"), ("items", ["  "], "text_invalid"), ("items", "text", "text_invalid"),
                                   ("published_at", "2026-10-14T10:00:00", "date_invalid"), ("updated_at", "not-a-date", "date_invalid")):
            with self.subTest(field=field, kind=code):
                self.assert_rejected(feed(notice(), notice("new", **{field: value})), code)

    def test_content_revision_needs_new_effective_revision_time(self):
        for changes, code in (({"summary": "正文已经修改。"}, "revision_missing"),
                              ({"summary": "正文已经修改。", "updated_at": "2026-09-26T00:00:00+08:00"}, "revision_not_effective"),
                              ({"summary": "正文已经修改。", "updated_at": "2026-10-15T00:00:00+00:00"}, "revision_not_effective")):
            with self.subTest(code=code):
                self.assert_rejected(feed(notice(**changes)), code)
        # A new notice cannot conceal an unmarked rewrite of a historical one.
        self.assert_rejected(feed(notice("new"), notice(summary="正文已经修改。")), "revision_missing")

    def test_revision_advances_previous_updated_at_and_keeps_publication_fixed(self):
        previous = notice(updated_at="2026-10-13T00:00:00+08:00")
        changed = {**previous, "items": ["补充真实更新内容。"], "updated_at": "2026-10-14T10:00:00+08:00"}
        self.assertEqual(check.check_feeds(feed(previous), feed(changed), now=NOW), {"effective_new_ids": [], "effective_revision_ids": ["previous"]})
        self.assert_rejected(feed({**changed, "updated_at": previous["updated_at"]}), "revision_not_effective", base=feed(previous))
        self.assert_rejected(feed({**changed, "published_at": "2026-09-26T01:00:00+08:00"}), "published_changed", base=feed(previous))

    def test_timestamp_only_and_unicode_whitespace_touch_are_not_updates(self):
        self.assert_rejected(feed(notice(updated_at="2026-10-14T10:00:00+08:00")), "update_missing")
        old = notice(title="聊天 更自然", summary="新版 聊天。", items=["保留 历史。"])
        touched = {**old, "title": " 聊天\t更自然 ", "summary": "新版\u3000聊天。", "items": ["保留\n历史。"], "updated_at": "2026-10-14T10:00:00+08:00"}
        self.assert_rejected(feed(touched), "update_missing", base=feed(old))

    def test_revision_before_publication_is_invalid_for_a_new_notice(self):
        self.assert_rejected(feed(notice("new", updated_at="2026-09-25T00:00:00+08:00"), notice()), "date_invalid")

    def test_published_revision_key_cannot_be_removed_or_rolled_back(self):
        previous = notice(updated_at="2026-10-13T00:00:00+08:00")
        removed = deepcopy(previous)
        removed.pop("updated_at")
        rolled_back = {**previous, "updated_at": "2026-10-12T00:00:00+08:00"}
        for historical in (removed, rolled_back):
            with self.subTest(revision_present="updated_at" in historical):
                self.assert_rejected(feed(notice("new"), historical), "revision_not_effective", base=feed(previous))

    def test_items_reordering_is_formatting_but_duplicate_counts_are_preserved(self):
        previous = notice(items=["第一条内容。", "第二条内容。"])
        reordered = {**previous, "items": list(reversed(previous["items"])), "updated_at": "2026-10-14T10:00:00+08:00"}
        self.assert_rejected(feed(reordered), "update_missing", base=feed(previous))
        repeated = {**reordered, "items": [*reordered["items"], "第一条内容。"]}
        self.assertEqual(check.check_feeds(feed(previous), feed(repeated), now=NOW), {"effective_new_ids": [], "effective_revision_ids": ["previous"]})

    def test_chinese_whitespace_insertion_alone_cannot_satisfy_release_update(self):
        previous = notice(title="聊天反馈优化")
        spaced = {**previous, "title": "聊 天\t反馈\u3000优化", "updated_at": "2026-10-14T10:00:00+08:00"}
        self.assert_rejected(feed(spaced), "update_missing", base=feed(previous))

    def test_deleting_history_is_rejected_even_with_valid_new_notice(self):
        self.assert_rejected(feed(notice("new")), "history_deleted")

    def test_ambiguous_json_and_naive_check_clock_reject(self):
        with self.assertRaises(check.AnnouncementCheckError):
            check.parse_feed(b'{"schema_version":1,"schema_version":1,"timezone":"Asia/Shanghai","updates":[]}')
        with self.assertRaises(check.AnnouncementCheckError):
            check.check_feeds(feed(notice()), feed(notice("new"), notice()), now=NOW.replace(tzinfo=None))
        with self.assertRaises(check.AnnouncementCheckError):
            check.parse_feed(feed(notice()).decode().encode("utf-16"))


@skipUnless(shutil.which("git"), "Git is required for committed-feed fixture checks")
class CommittedAnnouncementTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="snow-announcement-git-")
        self.addCleanup(self.temporary.cleanup)
        self.repository = Path(self.temporary.name)
        self.path = self.repository / check.FEED_PATH
        self.path.parent.mkdir(parents=True)
        self.git("init", "--initial-branch=main")

    def git(self, *arguments):
        result = subprocess.run(["git", "-c", "user.name=AnnouncementFixture", "-c", "user.email=fixture@example.invalid",
                                 "-c", "commit.gpgsign=false", "-c", "core.fsmonitor=false",
                                 "-c", "core.hooksPath=" + str(self.repository / "no-hooks"), *arguments],
                                cwd=self.repository, capture_output=True, check=True, timeout=20)
        return result.stdout.decode().strip()

    def commit(self, payload):
        self.path.write_bytes(payload)
        self.git("add", "--", check.FEED_PATH)
        self.git("commit", "-m", "Synthetic announcement fixture")
        return self.git("rev-parse", "HEAD")

    def test_committed_candidate_passes_despite_invalid_working_tree(self):
        base_raw = feed(notice())
        base = self.commit(base_raw)
        candidate_raw = feed(notice("new", title="Committed-only notice"), notice())
        candidate = self.commit(candidate_raw)
        self.path.write_text("UNCOMMITTED_INVALID_JSON", encoding="utf-8")
        report = check.check_committed(self.repository, base, candidate, now=NOW)
        self.assertTrue(report["passed"])
        self.assertEqual(report["base_feed_sha256"], check.digest(base_raw))
        self.assertEqual(report["candidate_feed_sha256"], check.digest(candidate_raw))
        self.assertEqual(report["effective_new_ids"], ["new"])
        self.assertNotIn("Committed-only notice", json.dumps(report))

    def test_uncommitted_notice_cannot_repair_committed_missing_update(self):
        base = self.commit(feed(notice()))
        (self.repository / "unrelated.txt").write_text("synthetic unrelated change", encoding="utf-8")
        self.git("add", "--", "unrelated.txt")
        self.git("commit", "-m", "Synthetic unrelated fixture")
        candidate = self.git("rev-parse", "HEAD")
        self.path.write_bytes(feed(notice("uncommitted"), notice()))
        with self.assertRaises(check.AnnouncementCheckError) as raised:
            check.check_committed(self.repository, base, candidate, now=NOW)
        self.assertEqual(raised.exception.code, "update_missing")

    def test_replace_refs_cannot_change_exact_base_commit_feed(self):
        base_raw = feed(notice())
        base = self.commit(base_raw)
        candidate_raw = feed(notice("new"), notice())
        candidate = self.commit(candidate_raw)
        self.git("replace", base, candidate)
        report = check.check_committed(self.repository, base, candidate, now=NOW)
        self.assertTrue(report["passed"])
        self.assertEqual(report["base_feed_sha256"], check.digest(base_raw))
        self.assertEqual(report["candidate_feed_sha256"], check.digest(candidate_raw))

    def test_git_reader_disables_lazy_fetch_without_printing_inherited_environment(self):
        with (patch.dict(check.os.environ, {"GIT_NO_LAZY_FETCH": "0", "SNOW_TEST_PRIVATE_ENV": "PRIVATE_VALUE"}),
              patch.object(check.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"commit\n", b"")) as git):
            self.assertEqual(check.git_read(self.repository, ["cat-file", "-t", "a" * 40], "commit_unavailable"), b"commit\n")
        self.assertIn("--no-replace-objects", git.call_args.args[0])
        self.assertEqual(git.call_args.kwargs["env"]["GIT_NO_LAZY_FETCH"], "1")
        self.assertEqual(git.call_args.kwargs["env"]["SNOW_TEST_PRIVATE_ENV"], "PRIVATE_VALUE")

    def test_cli_safe_metadata_and_create_once_report(self):
        base = self.commit(feed(notice()))
        candidate = self.commit(feed(notice("new", title="Private fixture body should not be printed"), notice()))
        output = self.repository / "result.json"
        with (patch.object(Path, "cwd", return_value=self.repository), patch.object(check, "datetime", wraps=datetime) as clock,
              patch("sys.stdout", new_callable=io.StringIO) as printed):
            clock.now.return_value = NOW
            self.assertEqual(check.main(["--base-sha", base, "--candidate-sha", candidate, "--output", str(output)]), 0)
        report = json.loads(printed.getvalue())
        self.assertEqual(set(report), {"passed", "base_sha", "candidate_sha", "checked_at", "base_feed_sha256", "candidate_feed_sha256", "effective_new_ids", "effective_revision_ids"})
        self.assertNotIn("Private fixture", printed.getvalue())
        before = output.read_bytes()
        with (patch.object(Path, "cwd", return_value=self.repository), patch.object(check, "datetime", wraps=datetime) as clock,
              patch("sys.stdout", new_callable=io.StringIO) as printed):
            clock.now.return_value = NOW
            self.assertEqual(check.main(["--base-sha", base, "--candidate-sha", candidate, "--output", str(output)]), 1)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(json.loads(printed.getvalue())["error_code"], "report_write_failed")

    def test_invalid_sha_never_calls_git_and_git_errors_are_sanitized(self):
        with patch.object(check.subprocess, "run", side_effect=AssertionError("must not call Git")), patch("sys.stdout", new_callable=io.StringIO) as printed:
            self.assertEqual(check.main(["--base-sha", "PRIVATE_INVALID_VALUE", "--candidate-sha", "a" * 40]), 1)
        self.assertNotIn("PRIVATE_INVALID_VALUE", printed.getvalue())
        with patch.object(check.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"SECRET_RAW_GIT_STDERR")), patch("sys.stdout", new_callable=io.StringIO) as printed:
            self.assertEqual(check.main(["--base-sha", "a" * 40, "--candidate-sha", "b" * 40]), 1)
        self.assertNotIn("SECRET_RAW_GIT_STDERR", printed.getvalue())
