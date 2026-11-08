import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update({"WAIT_LABEL_REPO": "owner/repo", "WAIT_LABEL_COMMAND_PREFIX": "/bot", "WAIT_LABEL_TEAM_USERS": "triager",
                   "WAIT_LABEL_IGNORED_USERS": "coderabbitai"})
import models  # noqa: E402
import wait_label as w  # noqa: E402


def user(login, kind="User"):
    return {"__typename": kind, "login": login}


def comment(login, time, text="text", association="CONTRIBUTOR"):
    return {"author": user(login), "authorAssociation": association, "createdAt": time, "bodyText": text}


def commit(time, committer="author", author="author", pushed=None):
    return {"commit": {"committedDate": time, "parents": {"totalCount": 1}, "checkSuites": {"nodes": [{"createdAt": pushed}] if pushed else []},
                       "author": {"user": {"login": author}}, "committer": {"name": "A Person", "user": {"login": committer}}}}


def github_commit(time, author="author", parents=1, name="GitHub"):
    c = commit(time, author=author)
    c["commit"]["committer"] = {"name": name, "user": None}
    c["commit"]["parents"]["totalCount"] = parents
    return c


def pr(comments=(), reviews=(), commits=(), labeled=(), number=1, draft=False):
    return {"number": number, "title": "A title", "isDraft": draft, "author": user("author"), "updatedAt": "2026-01-10T00:00:00Z",
            "comments": {"nodes": list(comments)}, "reviews": {"nodes": list(reviews)}, "commits": {"nodes": list(commits)},
            "labeled": {"nodes": [{"__typename": kind, "createdAt": time, "label": {"name": w.LABEL}} for kind, time in labeled]}}


def decide(p, labeled, start, end):
    return w.decide(p, w.events(p), labeled, start, end)


ASK = comment("reviewer", "2026-01-02T00:00:00Z", "Please rebase", "MEMBER")
ASK_EVENT = (ASK["createdAt"], "reviewer", "MEMBER", "comment", ASK["bodyText"])
START, END = "2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z"


class Events(unittest.TestCase):
    def logins(self, p):
        return [e[1] for e in w.events(p)]

    def test_keeps_author_and_team_only(self):
        p = pr([ASK, comment("stranger", "2026-01-02T01:00:00Z"), comment("author", "2026-01-02T02:00:00Z"),
                comment("triager", "2026-01-02T03:00:00Z")])
        self.assertEqual(self.logins(p), ["reviewer", "author", "triager"])

    def test_skips_bots_and_commands(self):
        bot = comment("renovate", "2026-01-02T00:00:00Z", association="MEMBER") | {"author": user("renovate", "Bot")}
        p = pr([bot, comment("coderabbitai", "2026-01-02T00:00:00Z", association="MEMBER"),
                comment("author", "2026-01-02T00:00:00Z", "/bot add-label x")])
        self.assertEqual(self.logins(p), [])

    def test_web_editor_commit_counts_as_the_author(self):
        p = pr(commits=[github_commit("2026-01-02T00:00:00Z")])
        self.assertEqual(self.logins(p), ["author"])

    def test_update_branch_merge_is_not_the_author_working(self):
        for name in ("GitHub", "GitHub Enterprise"):
            p = pr(commits=[github_commit("2026-01-02T00:00:00Z", parents=2, name=name)])
            self.assertEqual(self.logins(p), [])

    def test_a_team_members_commit_is_not_the_author(self):
        p = pr(commits=[github_commit("2026-01-02T00:00:00Z", author="reviewer")])
        self.assertEqual(self.logins(p), [])

    def test_a_commit_linked_to_no_account_counts_as_the_author(self):
        for empty in (None, {"name": "A Person", "user": None}):
            c = commit("2026-01-02T00:00:00Z")
            c["commit"]["committer"] = c["commit"]["author"] = empty
            self.assertEqual(self.logins(pr(commits=[c])), ["author"])

    def test_an_unlinked_committer_of_a_team_members_commit_is_not_the_author(self):
        c = commit("2026-01-02T00:00:00Z", author="reviewer")
        c["commit"]["committer"] = None
        self.assertEqual(self.logins(pr(commits=[c])), [])

    def test_a_deleted_account_is_left_out(self):
        review = {"author": None, "authorAssociation": "MEMBER", "state": "COMMENTED", "submittedAt": "2026-01-02T00:00:00Z",
                  "bodyText": "Why?", "comments": {"nodes": []}}
        p = pr([comment("x", "2026-01-02T00:00:00Z", association="MEMBER") | {"author": None}], reviews=[review])
        self.assertEqual(self.logins(p), [])

    def test_a_bot_in_team_users_speaks_for_the_team(self):
        bot = comment("triager", "2026-01-02T00:00:00Z") | {"author": user("triager", "Bot")}
        self.assertEqual(self.logins(pr([bot])), ["triager"])

    def test_a_commit_without_checks_dated_after_the_last_update_is_left_out(self):
        p = pr(commits=[commit("2027-01-01T00:00:00Z")]) | {"updatedAt": "2026-01-02T00:00:00Z"}
        self.assertEqual(w.events(p), [])

    def test_a_push_counts_from_its_checks(self):
        p = pr(commits=[commit("2027-01-01T00:00:00Z", pushed="2026-01-02T00:00:00Z")])
        self.assertEqual(w.events(p)[0][0], "2026-01-02T00:00:00Z")

    def test_a_commit_without_checks_does_not_take_a_merges_checks(self):
        merge = github_commit("2026-01-03T00:00:00Z", parents=2)
        merge["commit"]["checkSuites"]["nodes"] = [{"createdAt": "2026-01-03T00:00:00Z"}]
        p = pr(commits=[commit("2026-01-02T00:00:00Z"), merge])
        self.assertEqual(w.events(p)[0][0], "2026-01-02T00:00:00Z")

    def test_commits_pushed_together_count_once_from_the_push(self):
        p = pr(commits=[commit("2026-01-01T00:00:00Z", pushed="2026-01-01T01:00:00Z"), commit("2026-01-02T00:00:00Z"),
                        commit("2026-01-02T01:00:00Z", pushed="2026-01-02T05:00:00Z")])
        self.assertEqual([e[0] for e in w.events(p)], ["2026-01-01T01:00:00Z", "2026-01-02T05:00:00Z"])

    def test_team_users_ignore_case(self):
        self.assertEqual(self.logins(pr([comment("TRIAGER", "2026-01-02T00:00:00Z")])), ["TRIAGER"])

    def test_ignored_users_ignore_case(self):
        self.assertEqual(self.logins(pr([comment("CODERABBITAI", "2026-01-02T00:00:00Z", association="MEMBER")])), [])

    def test_review_comments_join_the_review_text(self):
        review = {"author": user("reviewer"), "authorAssociation": "MEMBER", "state": "COMMENTED",
                  "submittedAt": "2026-01-02T00:00:00Z", "bodyText": "", "comments": {"nodes": [{"path": "a.c", "bodyText": "Why?"}]}}
        self.assertEqual(w.events(pr(reviews=[review]))[0][4], "[a.c] Why?")


class Decide(unittest.TestCase):
    def test_no_model_call_until_the_team_has_spoken(self):
        self.assertIsNone(decide(pr([comment("author", "2026-01-02T00:00:00Z")]), False, START, END))

    def test_a_labeled_pr_is_judged_even_with_only_the_author_speaking(self):
        p = pr([comment("author", "2026-01-02T00:00:00Z")], labeled=[("LabeledEvent", "2026-01-01T12:00:00Z")])
        self.assertEqual(decide(p, True, START, END), "judge")

    def test_every_pr_is_in_the_window_on_a_first_run(self):
        self.assertEqual(decide(pr([ASK]), False, "", END), "judge")

    def test_new_team_message_is_judged(self):
        self.assertEqual(decide(pr([ASK]), False, START, END), "judge")

    def test_activity_outside_the_window_is_left_alone(self):
        self.assertIsNone(decide(pr([ASK]), False, END, "2026-01-04T00:00:00Z"))
        self.assertIsNone(decide(pr([ASK]), False, START, "2026-01-01T12:00:00Z"))

    def test_label_change_after_the_activity_settles_it(self):
        p = pr([ASK], labeled=[("UnlabeledEvent", "2026-01-02T01:00:00Z")])
        self.assertIsNone(decide(p, False, START, END))

    def test_label_events_ignore_case(self):
        p = pr([ASK], labeled=[("UnlabeledEvent", "2026-01-02T01:00:00Z")])
        p["labeled"]["nodes"][0]["label"]["name"] = w.LABEL.upper()
        self.assertIsNone(decide(p, False, START, END))

    def test_author_commit_after_the_label_removes_it(self):
        p = pr([ASK], commits=[commit("2026-01-02T02:00:00Z")], labeled=[("LabeledEvent", "2026-01-02T01:00:00Z")])
        self.assertEqual(decide(p, True, START, END), "remove")

    def test_a_team_message_after_the_commit_is_judged(self):
        again = comment("reviewer", "2026-01-02T03:00:00Z", "Still broken", "MEMBER")
        p = pr([again], commits=[commit("2026-01-02T02:00:00Z")], labeled=[("LabeledEvent", "2026-01-02T01:00:00Z")])
        self.assertEqual(decide(p, True, START, END), "judge")

    def test_a_commit_pushed_after_a_team_message_removes_the_label(self):
        ping = comment("reviewer", "2026-01-02T03:00:00Z", "Any update?", "MEMBER")
        p = pr([ping], commits=[commit("2026-01-02T02:00:00Z", pushed="2026-01-02T04:00:00Z")],
               labeled=[("LabeledEvent", "2026-01-02T01:00:00Z")])
        self.assertEqual(decide(p, True, "2026-01-02T12:00:00Z", END), "remove")

    def test_update_branch_after_the_label_keeps_it(self):
        p = pr([ASK], commits=[github_commit("2026-01-02T02:00:00Z", parents=2)], labeled=[("LabeledEvent", "2026-01-02T01:00:00Z")])
        self.assertIsNone(decide(p, True, START, END))

    def test_a_commit_dated_before_the_window_still_removes_the_label(self):
        p = pr([ASK], commits=[commit("2026-01-02T02:00:00Z")], labeled=[("LabeledEvent", "2026-01-02T01:00:00Z")])
        self.assertEqual(decide(p, True, "2026-01-02T12:00:00Z", END), "remove")

    def test_a_label_added_after_the_commit_stays(self):
        p = pr([ASK], commits=[commit("2026-01-02T02:00:00Z")], labeled=[("LabeledEvent", "2026-01-02T03:00:00Z")])
        self.assertIsNone(decide(p, True, "2026-01-02T12:00:00Z", END))

    def test_a_label_event_lost_from_the_history_is_judged_not_removed(self):
        p = pr([ASK], commits=[commit("2026-01-02T02:00:00Z")])
        self.assertEqual(decide(p, True, START, END), "judge")

    def test_author_comment_after_the_label_is_judged(self):
        p = pr([ASK, comment("author", "2026-01-02T02:00:00Z")], labeled=[("LabeledEvent", "2026-01-02T01:00:00Z")])
        self.assertEqual(decide(p, True, START, END), "judge")

    def test_drafts_never_get_the_label_by_default(self):
        self.assertIsNone(decide(pr([ASK], draft=True), False, START, END))
        with mock.patch.object(w, "LABEL_DRAFTS", True):
            self.assertEqual(decide(pr([ASK], draft=True), False, START, END), "judge")

    def test_a_labeled_draft_is_still_judged(self):
        self.assertEqual(decide(pr([ASK], draft=True), True, START, END), "judge")


class Prompt(unittest.TestCase):
    def test_keeps_the_last_messages_and_marks_who_spoke(self):
        comments = [comment("author", f"2026-01-02T0{i}:00:00Z", f"message {i}") for i in range(8)] + [
            comment("reviewer", "2026-01-02T09:00:00Z", "x" * 3000, "MEMBER")]
        p = pr(comments)
        prompt = w.prompt_for(p, w.events(p))
        self.assertNotIn("message 2", prompt)
        self.assertIn("message 3", prompt)
        self.assertIn("@reviewer, team member", prompt)
        self.assertIn("x" * w.MAX_TEXT, prompt)
        self.assertNotIn("x" * (w.MAX_TEXT + 1), prompt)

    def test_zero_context_messages_sends_none(self):
        with mock.patch.object(w, "CONTEXT_MESSAGES", 0):
            self.assertNotIn("Please rebase", w.prompt_for(pr([ASK]), [ASK_EVENT]))


class FakeModel:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def judge(self, prompt):
        self.calls += 1
        return {"waiting_on_author": self.answers.pop(0), "reason": "r"}, models.Usage(1, 10, 2, 0.5, 0.25)


def fresh_report():
    return {"added": [], "removed": [], "judged": 0, "held": 0, "failures": 0, "unusable": 0, "failed_at": [], "usage": models.Usage()}


class Judging(unittest.TestCase):
    def test_waiting_needs_every_confirmation(self):
        model = FakeModel(True, True, True)
        self.assertTrue(w.judge(model, "p", models.Usage())["waiting_on_author"])
        self.assertEqual(model.calls, 3)

    def test_one_disagreement_lands_on_not_waiting(self):
        model = FakeModel(True, False)
        self.assertFalse(w.judge(model, "p", models.Usage())["waiting_on_author"])
        self.assertEqual(model.calls, 2)

    def test_an_answer_without_a_verdict_is_unusable(self):
        class Rambling(FakeModel):
            def judge(self, prompt):
                return {"reason": "r"}, models.Usage()
        with self.assertRaises(models.UnusableAnswer):
            w.judge(Rambling(), "p", models.Usage())

    def test_not_waiting_needs_one_call(self):
        model = FakeModel(False)
        w.judge(model, "p", models.Usage())
        self.assertEqual(model.calls, 1)

    def test_labels_follow_the_verdict_and_usage_adds_up(self):
        report = fresh_report()
        todo = [("t", 1, False, "p"), ("t", 2, True, "p"), ("t", 3, True, "p")]
        with mock.patch.object(w, "set_label") as set_label:
            w.judge_all(todo, FakeModel(True, True, True, False, True, True, True), True, report)
        self.assertEqual([c.args for c in set_label.call_args_list], [(1, True, True), (2, False, True)])
        self.assertEqual((report["added"], report["removed"], report["judged"]), ([1], [2], 3))
        self.assertEqual((report["usage"].calls, report["usage"].input_tokens, report["usage"].ai_credits), (7, 70, 1.75))

    def test_a_failure_does_not_stop_the_run(self):
        class Flaky(FakeModel):
            def judge(self, prompt):
                if prompt == "bad":
                    raise RuntimeError("boom")
                return super().judge(prompt)
        report = fresh_report()
        with mock.patch.object(w, "set_label"):
            w.judge_all([("t", 1, False, "bad"), ("t", 2, False, "p")], Flaky(False), True, report)
        self.assertEqual((report["failures"], report["judged"]), (1, 1))

    def test_a_failed_label_change_keeps_the_judgment(self):
        report, out = fresh_report(), io.StringIO()
        with mock.patch.object(w, "set_label", side_effect=RuntimeError("HTTP 403")), contextlib.redirect_stdout(out):
            w.judge_all([("t", 1, True, "p")], FakeModel(False), True, report)
        self.assertEqual((report["judged"], report["failures"], report["failed_at"], report["removed"]), (1, 1, ["t"], []))
        self.assertIn("Cannot change the label on #1", out.getvalue())

    def test_logged_model_text_stays_on_one_line(self):
        class Injecting(FakeModel):
            def judge(self, prompt):
                if prompt == "bad":
                    raise RuntimeError("boom\n::error::fake")
                return {"waiting_on_author": False, "reason": "fine\n::error::fake"}, models.Usage()
        out = io.StringIO()
        with mock.patch.object(w, "LOG_REASONS", True), contextlib.redirect_stdout(out):
            w.judge_all([("t", 1, False, "p"), ("t", 2, False, "bad")], Injecting(), False, fresh_report())
        self.assertEqual(len(out.getvalue().splitlines()), 2)
        self.assertFalse(any(line.startswith("::error") for line in out.getvalue().splitlines()))


class Search(unittest.TestCase):
    def queries(self, start):
        with mock.patch.object(w, "search", return_value=[]) as search:
            w.collect(start, END, False, fresh_report())
        return [c.args[0] for c in search.call_args_list]

    def test_a_first_run_has_no_date_limit(self):
        self.assertTrue(all("updated:" not in q.replace("sort:updated-asc", "") for q in self.queries("")))

    def test_later_runs_start_after_the_last_one(self):
        self.assertTrue(all(f"updated:>={START}" in q for q in self.queries(START)))

    def test_a_filter_narrows_both_searches(self):
        with mock.patch.object(w, "search", return_value=[]) as search:
            w.collect(START, END, False, fresh_report(), 'base:main -label:"on hold"')
        self.assertTrue(all('is:open sort:updated-asc base:main -label:"on hold" ' in c.args[0] for c in search.call_args_list))

    def found(self, *numbers, updated="2026-01-02T12:00:00Z"):
        return [pr([ASK], number=n) | {"updatedAt": updated} for n in numbers]

    def test_a_cut_off_search_continues_from_its_last_result(self):
        later = "2026-01-03T00:00:00Z"
        with mock.patch.object(w, "search", side_effect=[self.found(1, 2), self.found(2, 3, updated=later), []]) as search, \
                mock.patch.object(w, "SEARCH_LIMIT", 2):
            prs = w.search_all("q", "")
        self.assertEqual([p["number"] for p in prs], [1, 2, 3])
        self.assertEqual([c.args[0] for c in search.call_args_list], ["q", "q updated:>=2026-01-02T12:00:00Z", f"q updated:>={later}"])

    def test_more_prs_in_one_second_than_a_search_returns_still_ends(self):
        with mock.patch.object(w, "search", return_value=self.found(1, 2)) as search, mock.patch.object(w, "SEARCH_LIMIT", 2):
            w.search_all("q", "2026-01-02T12:00:00Z")
        self.assertEqual(search.call_count, 1)

    def test_a_failed_search_changes_no_labels(self):
        p = pr([ASK], commits=[commit("2026-01-02T02:00:00Z")], labeled=[("LabeledEvent", "2026-01-02T01:00:00Z")])
        with mock.patch.object(w, "search", side_effect=[[p], RuntimeError("HTTP 502")]), \
                mock.patch.object(w, "set_label") as set_label, self.assertRaises(RuntimeError):
            w.collect(START, END, True, fresh_report())
        set_label.assert_not_called()

    def test_a_pr_by_a_bot_is_left_out(self):
        with mock.patch.object(w, "search", side_effect=[[], [pr([ASK]) | {"author": user("renovate", "Bot")}]]):
            self.assertEqual(w.collect(START, END, False, fresh_report()), [])


    def test_a_failed_removal_is_recorded_for_a_retry(self):
        p = pr([ASK], commits=[commit("2026-01-02T02:00:00Z")], labeled=[("LabeledEvent", "2026-01-02T01:00:00Z")])
        report = fresh_report()
        with mock.patch.object(w, "search", side_effect=[[p], []]), mock.patch.object(w, "set_label", side_effect=RuntimeError("no")), \
                contextlib.redirect_stdout(io.StringIO()):
            w.collect(START, END, True, report)
        self.assertEqual((report["removed"], report["failed_at"]), ([], ["2026-01-02T02:00:00Z"]))


class Filter(unittest.TestCase):
    def test_keeps_terms_and_quoted_values(self):
        self.assertEqual(w.pr_filter('base:main -label:"on hold",stale -is:draft -no:label fix'),
                         ('base:main -label:"on hold",stale -is:draft -no:label fix', []))

    def test_leaves_out_what_the_action_sets_itself(self):
        terms = "sort:created-desc updated:>2026-01-01 is:pr IS:OPEN"
        self.assertEqual(w.pr_filter(f"{terms} base:main"), ("base:main", terms.split()))

    def test_refuses_what_contradicts_the_action(self):
        for term in ("repo:a/b", "org:a", "is:closed", "is:merged", "-is:open", "type:issue", "no:label", f'label:x,"{w.LABEL.upper()}"'):
            with self.subTest(term), self.assertRaises(ValueError):
                w.pr_filter(f"base:main {term}")

    def test_a_refused_filter_stops_the_run_before_any_search(self):
        with mock.patch.object(w, "PR_FILTER", "is:closed"), mock.patch.object(w, "collect") as collect, \
                mock.patch.object(sys, "argv", ["wait_label.py"]), self.assertRaisesRegex(SystemExit, "open pull requests"):
            w.main()
        collect.assert_not_called()


class Settings(unittest.TestCase):
    def test_an_unset_list_uses_its_default_and_an_empty_one_stays_empty(self):
        self.assertEqual(w.setting_list("NOT_SET", "A,B"), {"A", "B"})
        with mock.patch.dict(os.environ, {"WAIT_LABEL_EMPTY_LIST": ""}):
            self.assertEqual(w.setting_list("EMPTY_LIST", "A,B"), set())

    def test_logins_can_be_written_as_mentions(self):
        self.assertEqual(w.login_list("NOT_SET", "@Alice, bob"), {"alice", "bob"})

    def stopped(self, call, name, value):
        with mock.patch.dict(os.environ, {f"WAIT_LABEL_{name}": value}), self.assertRaises(SystemExit) as raised:
            call()
        return str(raised.exception)

    def test_bad_values_name_the_input_and_what_it_takes(self):
        self.assertEqual(self.stopped(lambda: w.choice("PROVIDER", "copilot", ("copilot", "claude")), "PROVIDER", "Claude"),
                         "provider must be one of copilot, claude, not 'Claude'")
        self.assertIn("effort must be one of low, medium", self.stopped(lambda: w.choice("EFFORT", "", ("", "low", "medium")), "EFFORT", "huge"))
        self.assertIn("max-judgments must be a whole number", self.stopped(lambda: w.number("MAX_JUDGMENTS", "20"), "MAX_JUDGMENTS", "ten"))
        self.assertIn("must be a whole number", self.stopped(lambda: w.number("MAX_JUDGMENTS", "20"), "MAX_JUDGMENTS", "-1"))
        self.assertIn("label-drafts must be true or false", self.stopped(lambda: w.flag("LABEL_DRAFTS", "false"), "LABEL_DRAFTS", "yes"))

    def test_flags_ignore_case(self):
        with mock.patch.dict(os.environ, {"WAIT_LABEL_LABEL_DRAFTS": "True"}):
            self.assertTrue(w.flag("LABEL_DRAFTS", "false"))

    def test_bad_settings_stop_the_run_on_import(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name, value, message in (("TEAM_ASSOCIATIONS", "OWNER,MEMBERS", "team-associations must be made of OWNER"),
                                     ("LABEL_COLOR", "yellow", "label-color must be six hex digits")):
            out = subprocess.run([sys.executable, "-c", "import wait_label"], cwd=root, capture_output=True, text=True,
                                 env=os.environ | {f"WAIT_LABEL_{name}": value})
            self.assertEqual(out.returncode, 1)
            self.assertIn(message, out.stderr)


class GitHubCalls(unittest.TestCase):
    def failing(self, tries, stderr="HTTP 502"):
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1, stderr=stderr)) as run, \
                mock.patch("time.sleep") as sleep, self.assertRaisesRegex(RuntimeError, stderr):
            w.gh("api", "x", tries=tries)
        return run.call_count, sleep.call_count

    def test_retries_wait_between_tries_only(self):
        self.assertEqual(self.failing(3), (3, 2))

    def test_no_tries_still_tries_once(self):
        self.assertEqual(self.failing(0), (1, 0))

    def test_a_refused_search_names_the_permissions(self):
        with mock.patch.object(w, "gh", side_effect=RuntimeError("Resource not accessible by integration")), \
                self.assertRaisesRegex(RuntimeError, "checks: read"):
            w.search("q")

    def test_a_404_or_a_refusal_is_not_tried_again(self):
        for stderr in ("HTTP 404", "Resource not accessible by integration"):
            self.assertEqual(self.failing(3, stderr), (1, 0))


class Labels(unittest.TestCase):
    def test_slashes_in_a_label_are_escaped(self):
        with mock.patch.object(w, "LABEL", "status/waiting on author"), mock.patch.object(w, "gh") as gh:
            w.set_label(1, False, True)
        self.assertTrue(gh.call_args.args[1].endswith("/labels/status%2Fwaiting%20on%20author"))

    def test_a_failed_change_is_not_logged(self):
        out = io.StringIO()
        with mock.patch.object(w, "gh", side_effect=RuntimeError("HTTP 500")), contextlib.redirect_stdout(out), \
                self.assertRaises(RuntimeError):
            w.set_label(1, True, True)
        self.assertEqual(out.getvalue(), "")


class EnsureLabel(unittest.TestCase):
    def test_an_existing_label_needs_nothing_more(self):
        for apply in (True, False):
            with mock.patch.object(w, "gh") as gh:
                w.ensure_label(apply)
            self.assertEqual(gh.call_count, 1)

    def test_a_dry_run_reports_a_missing_label_without_creating_it(self):
        out = io.StringIO()
        with mock.patch.object(w, "gh", side_effect=RuntimeError("HTTP 404")) as gh, contextlib.redirect_stdout(out):
            w.ensure_label(False)
        self.assertIn("doesn't exist", out.getvalue())
        self.assertEqual(gh.call_count, 1)

    def test_a_real_run_creates_a_missing_label(self):
        with mock.patch.object(w, "gh", side_effect=[RuntimeError("HTTP 404"), "{}"]) as gh:
            w.ensure_label(True)
        self.assertIn("POST", gh.call_args.args)

    def test_other_errors_still_raise(self):
        with mock.patch.object(w, "gh", side_effect=RuntimeError("HTTP 500")), self.assertRaises(RuntimeError):
            w.ensure_label(False)


class CheckRepository(unittest.TestCase):
    def test_a_readable_repository_passes(self):
        with mock.patch.object(w, "gh", return_value="owner/repo") as gh:
            w.check_repository()
        self.assertEqual(gh.call_args.args[:2], ("api", "repos/owner/repo"))

    def test_a_repository_the_token_cannot_see_stops_the_run(self):
        with mock.patch.object(w, "gh", side_effect=RuntimeError("HTTP 404")), \
                self.assertRaisesRegex(SystemExit, "repository must be one the token can read, not 'owner/repo'"):
            w.check_repository()

    def test_other_errors_still_raise(self):
        with mock.patch.object(w, "gh", side_effect=RuntimeError("HTTP 500")), self.assertRaises(RuntimeError):
            w.check_repository()

    def test_a_repository_that_is_not_owner_and_name_stops_the_run(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for value in ("OrcaSlicer", "a/b/c", "https://github.com/a/b"):
            out = subprocess.run([sys.executable, "-c", "import wait_label"], cwd=root, capture_output=True, text=True,
                                 env=os.environ | {"WAIT_LABEL_REPO": value})
            self.assertIn(f"repository must be owner/name, not {value!r}", out.stderr)


class Action(unittest.TestCase):
    def test_the_install_step_has_no_tokens(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "action.yml")
        with open(path, encoding="utf-8") as action:
            step = action.read().split("- name: Install")[1].split("- name: Sweep")[0]
        self.assertIn("npm install", step)
        self.assertNotRegex(step.lower(), "token|api-key|secret")


class Run(unittest.TestCase):
    def run_main(self, todo, argv=(), state=None, model=None, retries=0):
        """Runs main() with GitHub and the model faked, and returns the saved state and the window passed to collect."""
        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "state", "state.json")
        if state:
            os.makedirs(os.path.dirname(path))
            with open(path, "w", encoding="utf-8") as out:
                json.dump({"judged_through": state, "retries": retries}, out)
        fake = mock.MagicMock()
        fake.__enter__.return_value = model or FakeModel(*[False] * 10)
        with mock.patch.object(w, "STATE_FILE", path), mock.patch.object(w, "MAX_JUDGMENTS", 2), \
                mock.patch.object(w, "collect", side_effect=lambda s, e, a, r, x: todo) as collect, \
                mock.patch.object(w, "check_repository"), mock.patch.object(w, "ensure_label"), \
                mock.patch.object(w, "open_model", return_value=fake) as open_model, \
                mock.patch.object(w, "set_label"), mock.patch.object(sys, "argv", ["wait_label.py", *argv]):
            w.main()
            self.opened = open_model.called
        saved = None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as saved_file:
                state_saved = json.load(saved_file)
            saved, self.retries = state_saved["judged_through"], state_saved["retries"]
        return saved, collect.call_args.args

    def test_starts_where_the_last_run_stopped_and_saves_the_new_end(self):
        saved, window = self.run_main([], ["--apply"], state=START)
        self.assertEqual(window[0], START)
        self.assertEqual(saved, window[1])

    def test_since_wins_over_the_state_file(self):
        _, window = self.run_main([], ["--apply", "--since", "2026-01-02T00:00:00Z"], state=START)
        self.assertEqual(window[0], "2026-01-02T00:00:00Z")

    def test_a_dry_run_moves_on_too(self):
        saved, window = self.run_main([("2026-01-02T00:00:00Z", 1, False, "p")], state=START)
        self.assertEqual(saved, window[1])

    def test_a_first_run_takes_every_open_pr(self):
        _, window = self.run_main([], ["--apply"])
        self.assertEqual(window[0], "")

    def test_lookback_limits_a_first_run(self):
        with mock.patch.object(w, "LOOKBACK_HOURS", 2):
            _, window = self.run_main([], ["--apply"])
        self.assertTrue(window[0] and window[0] < window[1])

    def test_the_cap_holds_prs_for_the_next_run(self):
        times = [f"2026-01-0{day}T00:00:00Z" for day in (1, 2, 3)]
        saved, _ = self.run_main([(t, i, False, "p") for i, t in enumerate(times)], ["--apply"])
        self.assertEqual(saved, "2026-01-02T23:59:59Z")

    def test_the_cap_does_not_split_a_second(self):
        times = ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"]
        model = FakeModel(*[False] * 3)
        saved, _ = self.run_main([(t, i, False, "p") for i, t in enumerate(times)], ["--apply"], model=model)
        self.assertEqual((saved, model.calls), ("2026-01-02T23:59:59Z", 3))

    def test_more_prs_in_one_second_than_the_cap_are_all_judged(self):
        time, model = "2026-01-02T00:00:00Z", FakeModel(*[False] * 3)
        saved, window = self.run_main([(time, i, False, "p") for i in range(3)], ["--apply"], state="2026-01-01T23:59:59Z", model=model)
        self.assertEqual((saved, model.calls), (window[1], 3))

    def test_a_bad_since_names_the_format(self):
        with self.assertRaisesRegex(SystemExit, "since must be an ISO time"):
            self.run_main([], ["--since", "last tuesday"])

    def test_a_model_copilot_lacks_stops_the_run_where_it_was(self):
        fake = mock.MagicMock()
        fake.__enter__.side_effect = models.BadSetting("model must be one of a, b, not 'c'")
        with mock.patch.object(w, "open_model", return_value=fake), mock.patch.object(w, "write_state") as write, \
                mock.patch.object(w, "collect", return_value=[("2026-01-02T00:00:00Z", 1, False, "p")]), \
                mock.patch.object(w, "check_repository"), mock.patch.object(w, "ensure_label"), \
                mock.patch.object(sys, "argv", ["wait_label.py"]), \
                self.assertRaisesRegex(SystemExit, "model must be one of a, b"):
            w.main()
        write.assert_not_called()

    def test_a_startup_error_stays_on_one_line(self):
        fake = mock.MagicMock()
        fake.__enter__.side_effect = RuntimeError("answered 401: denied\n::error::fake")
        with mock.patch.object(w, "open_model", return_value=fake), \
                mock.patch.object(w, "collect", return_value=[("2026-01-02T00:00:00Z", 1, False, "p")]), \
                mock.patch.object(w, "check_repository"), mock.patch.object(w, "ensure_label"), \
                mock.patch.object(sys, "argv", ["wait_label.py"]), \
                self.assertRaises(SystemExit) as stopped:
            w.main()
        self.assertEqual(str(stopped.exception), "answered 401: denied ::error::fake")

    def test_a_failed_search_stops_the_run_on_one_line(self):
        with mock.patch.object(w, "collect", side_effect=RuntimeError("gh api graphql failed: HTTP 502\nBad gateway")), \
                mock.patch.object(w, "check_repository"), mock.patch.object(w, "ensure_label"), \
                mock.patch.object(w, "write_state") as write, \
                mock.patch.object(sys, "argv", ["wait_label.py"]), self.assertRaises(SystemExit) as stopped:
            w.main()
        self.assertEqual(str(stopped.exception), "gh api graphql failed: HTTP 502 Bad gateway")
        write.assert_not_called()

    def test_a_copilot_client_that_fails_to_start_is_cleaned_up(self):
        class Client:
            def __init__(self, **_):
                pass

            async def start(self):
                raise FileNotFoundError("copilot binary")

            async def stop(self):
                pass
        model = models.CopilotModel("auto", "s", {}, "t")
        with mock.patch.dict(sys.modules, {"copilot": mock.Mock(CopilotClient=Client)}), \
                self.assertRaisesRegex(RuntimeError, "Copilot couldn't start: copilot binary"):
            model.__enter__()
        self.assertTrue(model.loop.is_closed())

    def test_no_model_is_opened_with_nothing_to_judge(self):
        self.run_main([], ["--apply"])
        self.assertFalse(self.opened)

    def test_all_failing_exits_and_keeps_the_state(self):
        class Broken(FakeModel):
            def judge(self, prompt):
                raise RuntimeError("bad token")
        todo = [("2026-01-02T00:00:00Z", 1, False, "p"), ("2026-01-02T12:00:00Z", 2, False, "p")]
        with mock.patch.object(w, "write_state") as write, self.assertRaises(SystemExit):
            self.run_main(todo, ["--apply"], model=Broken())
        write.assert_not_called()

    def test_a_model_service_error_on_the_only_pr_keeps_the_place(self):
        out = io.StringIO()
        with mock.patch.object(w, "write_state") as write, self.assertRaises(SystemExit), contextlib.redirect_stdout(out):
            self.run_main([("2026-01-02T00:00:00Z", 2, False, "bad")], ["--apply"], state=START, model=self.FailsOnBad())
        write.assert_not_called()
        self.assertNotIn("judged-through", out.getvalue())

    def test_an_answer_that_does_not_fit_uses_up_a_retry(self):
        class Unusable(FakeModel):
            def judge(self, prompt):
                raise models.UnusableAnswer("the answer isn't JSON")
        with mock.patch.object(w, "write_state") as write, self.assertRaises(SystemExit):
            self.run_main([("2026-01-02T00:00:00Z", 2, False, "p")], ["--apply"], state=START, model=Unusable())
        write.assert_called_once_with("2026-01-01T23:59:59Z", 1)

    FAILED_SECOND = [("2026-01-02T00:00:00Z", 1, False, "p"), ("2026-01-02T12:00:00Z", 2, False, "bad")]

    class FailsOnBad(FakeModel):
        def judge(self, prompt):
            if prompt == "bad":
                raise RuntimeError("boom")
            return {"waiting_on_author": False, "reason": "r"}, models.Usage()

    def test_the_next_run_starts_again_from_a_failed_pr(self):
        saved, _ = self.run_main(self.FAILED_SECOND, ["--apply"], state=START, model=self.FailsOnBad())
        self.assertEqual((saved, self.retries), ("2026-01-02T11:59:59Z", 1))

    def test_retries_run_out(self):
        saved, window = self.run_main(self.FAILED_SECOND, ["--apply"], state=START, model=self.FailsOnBad(), retries=w.FAILURE_RETRIES)
        self.assertEqual((saved, self.retries), (window[1], 0))

    def test_a_retry_never_moves_back_past_the_start(self):
        report = fresh_report() | {"failed_at": ["2025-01-01T00:00:00Z"]}
        self.assertEqual(w.progress([], 0, START, END, report, 0), (START, 1))

    def test_a_clean_run_clears_the_retries(self):
        _, _ = self.run_main([], ["--apply"], state=START, retries=2)
        self.assertEqual(self.retries, 0)

    def test_progress_is_saved_after_each_pr(self):
        todo = [("2026-01-02T00:00:00Z", 1, False, "p"), ("2026-01-02T12:00:00Z", 2, False, "p")]
        with mock.patch.object(w, "write_state") as write:
            self.run_main(todo, ["--apply"], state=START)
        self.assertEqual(write.call_args_list[0].args, ("2026-01-02T11:59:59Z", 0))

    def test_a_state_file_can_be_a_bare_name(self):
        cwd = os.getcwd()
        os.chdir(tempfile.mkdtemp())
        try:
            with mock.patch.object(w, "STATE_FILE", "state.json"):
                w.write_state(START, 0)
                self.assertEqual(w.read_state(), (START, 0))
        finally:
            os.chdir(cwd)

    def test_a_state_file_without_retries_still_reads(self):
        path = os.path.join(tempfile.mkdtemp(), "state.json")
        with open(path, "w", encoding="utf-8") as out:
            json.dump({"judged_through": START}, out)
        with mock.patch.object(w, "STATE_FILE", path):
            self.assertEqual(w.read_state(), (START, 0))


class Providers(unittest.TestCase):
    SECRETS = {"GH_TOKEN": "a", "GITHUB_TOKEN": "b", "WAIT_LABEL_COPILOT_TOKEN": "c", "CLAUDE_CODE_OAUTH_TOKEN": "d",
               "ANTHROPIC_API_KEY": "e", "MY_JOB_SECRET": "f", "EMPTY": "", "PATH": "p"}

    def test_secrets_never_reach_a_model_process(self):
        with mock.patch.dict(os.environ, self.SECRETS, clear=True):
            self.assertEqual(models.model_env(), {"PATH": "p"})

    def test_claude_gets_only_its_own_tokens(self):
        with mock.patch.dict(os.environ, self.SECRETS, clear=True), mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(stdout=json.dumps({"structured_output": {"waiting_on_author": False}}))
            models.ClaudeModel("m", "s", {}).judge("p")
        self.assertEqual(run.call_args.kwargs["env"], {"CLAUDE_CODE_OAUTH_TOKEN": "d", "ANTHROPIC_API_KEY": "e", "PATH": "p"})

    def test_a_claude_timeout_keeps_the_prompt_out_of_the_error(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["claude", "secret prompt"], 300)):
            with self.assertRaises(RuntimeError) as failed:
                models.ClaudeModel("m", "secret prompt", {}).judge("p")
        self.assertNotIn("secret prompt", str(failed.exception))

    def test_claude_reads_structured_output_and_usage(self):
        result = {"structured_output": {"waiting_on_author": True, "reason": "r"}, "total_cost_usd": 0.5,
                  "usage": {"input_tokens": 2, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4, "output_tokens": 5}}
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout=json.dumps(result))):
            verdict, usage = models.ClaudeModel("m", "s", {}).judge("p")
        self.assertTrue(verdict["waiting_on_author"])
        self.assertEqual((usage.input_tokens, usage.output_tokens, usage.cost_usd), (9, 5, 0.5))

    def test_claude_passes_effort_only_when_set(self):
        self.assertNotIn("--effort", models.ClaudeModel("m", "s", {}).args)
        self.assertEqual(models.ClaudeModel("m", "s", {}, effort="low").args[-2:], ["--effort", "low"])

    def test_query_uses_the_history_limits(self):
        for limit in (f"comments(last: {w.MAX_COMMENTS})", f"commits(last: {w.MAX_COMMITS})",
                      f"reviews(last: {w.MAX_REVIEWS})", f"timelineItems(last: {w.MAX_LABEL_EVENTS},"):
            self.assertIn(limit, w.QUERY)

    def test_claude_failure_reports_its_stderr(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout="", stderr="Invalid token\n", returncode=1)):
            with self.assertRaisesRegex(RuntimeError, "Invalid token"):
                models.ClaudeModel("m", "s", {}).judge("p")

    def test_a_claude_answer_that_is_not_json_is_unusable_and_kept_out_of_the_error(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout=json.dumps({"result": "The author of #5 is slow"}))):
            with self.assertRaises(models.UnusableAnswer) as raised:
                models.ClaudeModel("m", "s", {}).judge("p")
        self.assertNotIn("#5", str(raised.exception))

    def test_copilot_refuses_a_model_it_does_not_offer(self):
        class Client:
            def __init__(self, **_):
                self.stopped = False

            async def start(self):
                pass

            async def stop(self):
                self.stopped = True

            async def list_models(self):
                return [mock.Mock(id="claude-sonnet-5.5"), mock.Mock(id="auto")]
        with mock.patch.dict(sys.modules, {"copilot": mock.Mock(CopilotClient=Client)}):
            with self.assertRaisesRegex(models.BadSetting, "model must be one of claude-sonnet-5.5, auto, not 'claude-sonet-5.5'"):
                models.CopilotModel("claude-sonet-5.5", "s", {}, "t").__enter__()
            with models.CopilotModel("auto", "s", {}, "t") as model:
                self.assertFalse(model.client.stopped)

    def answers(self, *bodies):
        """Fakes the Models API, where None stands for a 404 and a number for another error status."""
        def respond(request, timeout):
            self.requests.append(request)
            self.timeouts.append(timeout)
            body = bodies[min(len(self.requests), len(bodies)) - 1]
            if isinstance(body, Exception):
                raise body
            if isinstance(body, bytes):
                response = mock.MagicMock()
                response.__enter__.return_value = io.BytesIO(body)
                return response
            if body is None or isinstance(body, int):
                raise urllib.error.HTTPError(request.full_url, body or 404, "x", {}, io.BytesIO(b"denied"))
            response = mock.MagicMock()
            response.__enter__.return_value = io.BytesIO(json.dumps(body).encode())
            return response
        self.requests, self.timeouts = [], []
        return mock.patch("urllib.request.urlopen", side_effect=respond)

    EFFORTS = {"supported": True, "low": {"supported": True}, "high": {"supported": True}, "max": {"supported": False}}

    def claude(self, model="claude-sonnet-5-5", effort=""):
        return models.ClaudeModel(model, "s", {}, effort=effort).__enter__()

    def test_claude_refuses_a_model_anthropic_does_not_offer(self):
        with self.answers(None, {"data": [{"id": "claude-sonnet-5-5"}, {"id": "claude-opus-5-5"}]}), \
                self.assertRaisesRegex(models.BadSetting, "model must be one of claude-sonnet-5-5, claude-opus-5-5, or a family such as sonnet, not 'sonet'"):
            self.claude("sonet")
        self.assertTrue(self.requests[0].full_url.endswith("/v1/models/sonet"))

    def test_a_claude_family_name_runs_its_newest_model(self):
        caps = {"effort": self.EFFORTS}
        family = {"data": [{"id": "claude-sonnet-5", "created_at": "2026-06-29T00:00:00Z", "capabilities": caps},
                           {"id": "claude-sonnet-5-5", "created_at": "2026-09-28T00:00:00Z", "capabilities": caps},
                           {"id": "claude-opus-5-5", "created_at": "2026-10-01T00:00:00Z", "capabilities": caps}]}
        with self.answers(None, family), contextlib.redirect_stdout(io.StringIO()):
            model = self.claude("Sonnet", "high")
        self.assertEqual((model.model, model.args[model.args.index("--model") + 1]), ("claude-sonnet-5-5", "claude-sonnet-5-5"))

    def test_claude_checks_effort_against_the_model(self):
        info = {"id": "claude-sonnet-5-5", "capabilities": {"effort": self.EFFORTS}}
        with self.answers(info, info, info):
            self.claude(effort="high")
            with self.assertRaisesRegex(models.BadSetting, "effort must be one of low, high for claude-sonnet-5-5, not 'max'"):
                self.claude(effort="max")
            self.claude()
        no_effort = {"id": "claude-haiku-4-5", "capabilities": {"effort": {"supported": False}}}
        with self.answers(no_effort), self.assertRaisesRegex(models.BadSetting, "takes no effort setting"):
            self.claude("claude-haiku-4-5", "low")

    def test_claude_lookups_use_the_key_or_the_token(self):
        info = {"id": "m", "capabilities": {"effort": {"supported": False}}}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key", "CLAUDE_CODE_OAUTH_TOKEN": "token"}), self.answers(info):
            self.claude()
        self.assertEqual(self.requests[0].get_header("X-api-key"), "key")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "", "CLAUDE_CODE_OAUTH_TOKEN": "token"}), self.answers(info):
            self.claude()
        self.assertEqual(self.requests[0].get_header("Authorization"), "Bearer token")
        self.assertEqual(self.timeouts, [models.LOOKUP_TIMEOUT])

    def test_a_refused_claude_lookup_raises(self):
        with self.answers(401), self.assertRaisesRegex(RuntimeError, "answered 401"):
            self.claude()
        self.assertEqual(len(self.requests), 1)

    def test_a_busy_or_unreachable_model_list_is_tried_again(self):
        info = {"id": "m", "capabilities": {"effort": {"supported": False}}}
        with self.answers(503, info), mock.patch("time.sleep") as sleep:
            self.claude()
        self.assertEqual((len(self.requests), sleep.call_count), (2, 1))
        with self.answers(urllib.error.URLError("no route")), mock.patch("time.sleep"), \
                self.assertRaisesRegex(RuntimeError, "couldn't be reached"):
            self.claude()
        self.assertEqual(len(self.requests), models.LOOKUP_TRIES)

    def test_a_model_list_that_is_not_json_raises(self):
        with self.answers(b"<html>Sign in</html>"), self.assertRaisesRegex(RuntimeError, "didn't answer with JSON"):
            self.claude()

    def test_a_gateway_that_only_lists_models_still_finds_a_full_id(self):
        listing = {"data": [{"id": "claude-sonnet-5-5", "created_at": "2026-09-28T00:00:00Z", "capabilities": {"effort": self.EFFORTS}}]}
        out = io.StringIO()
        with self.answers(None, listing), contextlib.redirect_stdout(out):
            self.assertEqual(self.claude(effort="high").model, "claude-sonnet-5-5")
        self.assertEqual(out.getvalue(), "")

    def test_a_gateway_list_without_capabilities_skips_the_effort_check(self):
        out = io.StringIO()
        with self.answers(None, {"data": [{"id": "claude-sonnet-5-5"}]}), contextlib.redirect_stdout(out):
            self.assertEqual(self.claude(effort="high").model, "claude-sonnet-5-5")
        self.assertIn("effort goes to Claude unchecked", out.getvalue())

    def test_null_capabilities_or_a_bare_effort_level_do_not_crash(self):
        with self.answers(None, {"data": [{"id": "claude-sonnet-5-5", "capabilities": None}]}), contextlib.redirect_stdout(io.StringIO()):
            self.claude(effort="high")
        out = io.StringIO()
        with self.answers({"id": "m", "capabilities": {"effort": {"supported": True}}}), contextlib.redirect_stdout(out):
            self.claude("m", "high")
        self.assertIn("effort goes to Claude unchecked", out.getvalue())
        bare = {"id": "m", "capabilities": {"effort": {"supported": True, "high": {}, "low": {"supported": True}}}}
        with self.answers(bare), self.assertRaisesRegex(models.BadSetting, "effort must be one of low for m"):
            self.claude("m", "high")

    def test_a_base_url_without_a_scheme_names_the_setting(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "api.example.com"}), \
                self.assertRaisesRegex(models.BadSetting, "ANTHROPIC_BASE_URL must be a full URL"):
            self.claude()

    def test_a_gateway_without_a_model_list_skips_the_check(self):
        out = io.StringIO()
        with self.answers(None, None), contextlib.redirect_stdout(out):
            model = self.claude("sonnet", "high")
        self.assertEqual(model.model, "sonnet")
        self.assertIn("unchecked", out.getvalue())

    def test_a_failed_copilot_model_list_stops_the_client(self):
        class Client:
            stopped = False

            def __init__(self, **_):
                pass

            async def start(self):
                pass

            async def stop(self):
                Client.stopped = True
                raise ConnectionError("already gone\n::error::fake")

            async def list_models(self):
                raise ConnectionError("denied")
        model, out = models.CopilotModel("auto", "s", {}, "t"), io.StringIO()
        with mock.patch.dict(sys.modules, {"copilot": mock.Mock(CopilotClient=Client)}), contextlib.redirect_stdout(out), \
                self.assertRaisesRegex(RuntimeError, "Copilot couldn't start: denied"):
            model.__enter__()
        self.assertTrue(Client.stopped and model.loop.is_closed())
        self.assertIn("didn't stop cleanly: already gone ::error::fake", out.getvalue())
        self.assertFalse(any(line.startswith("::error") for line in out.getvalue().splitlines()))

    def test_copilot_checks_effort_against_the_model(self):
        model = models.CopilotModel("kimi-k3", "s", {}, "t", effort="medium")
        with self.assertRaisesRegex(models.BadSetting, "effort must be one of low, high, max for kimi-k3, not 'medium'"):
            model.check([mock.Mock(id="kimi-k3", supported_reasoning_efforts=["low", "high", "max"])])
        with self.assertRaisesRegex(models.BadSetting, "takes no effort setting"):
            model.check([mock.Mock(id="kimi-k3", supported_reasoning_efforts=None)])

    def test_claude_errors_raise(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout=json.dumps({"is_error": True, "result": "no"}))):
            with self.assertRaises(RuntimeError):
                models.ClaudeModel("m", "s", {}).judge("p")


if __name__ == "__main__":
    unittest.main()
