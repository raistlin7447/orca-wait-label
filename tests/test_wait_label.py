import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update({"WAIT_LABEL_REPO": "owner/repo", "WAIT_LABEL_COMMAND_PREFIX": "/bot", "WAIT_LABEL_TEAM_USERS": "triager"})
import models  # noqa: E402
import wait_label as w  # noqa: E402


def comment(login, time, text="text", association="CONTRIBUTOR"):
    return {"author": {"login": login}, "authorAssociation": association, "createdAt": time, "bodyText": text}


def commit(time, committer="author", author="author", authored=None):
    return {"commit": {"authoredDate": authored or time, "committedDate": time,
                       "author": {"user": {"login": author}}, "committer": {"user": {"login": committer}}}}


def pr(comments=(), reviews=(), commits=(), labeled=(), number=1, draft=False):
    return {"number": number, "title": "A title", "isDraft": draft, "author": {"login": "author"},
            "comments": {"nodes": list(comments)}, "reviews": {"nodes": list(reviews)}, "commits": {"nodes": list(commits)},
            "labeled": {"nodes": [{"__typename": kind, "createdAt": time, "label": {"name": w.LABEL}} for kind, time in labeled]}}


ASK = comment("reviewer", "2026-01-02T00:00:00Z", "Please rebase", "MEMBER")
START, END = "2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z"


class Events(unittest.TestCase):
    def logins(self, p):
        return [e[1] for e in w.events(p)]

    def test_keeps_author_and_team_only(self):
        p = pr([ASK, comment("stranger", "2026-01-02T01:00:00Z"), comment("author", "2026-01-02T02:00:00Z"),
                comment("triager", "2026-01-02T03:00:00Z")])
        self.assertEqual(self.logins(p), ["reviewer", "author", "triager"])

    def test_skips_bots_and_commands(self):
        p = pr([comment("renovate[bot]", "2026-01-02T00:00:00Z", association="MEMBER"),
                comment("coderabbitai", "2026-01-02T00:00:00Z", association="MEMBER"),
                comment("author", "2026-01-02T00:00:00Z", "/bot add-label x")])
        self.assertEqual(self.logins(p), [])

    def test_web_editor_commit_counts_as_the_author(self):
        p = pr(commits=[commit("2026-01-02T00:00:00Z", committer="web-flow")])
        self.assertEqual(self.logins(p), ["author"])

    def test_update_branch_is_not_the_author_working(self):
        p = pr(commits=[commit("2026-01-02T00:00:00Z", committer="web-flow", authored="2026-01-01T00:00:00Z")])
        self.assertEqual(self.logins(p), [])

    def test_a_team_members_commit_is_not_the_author(self):
        p = pr(commits=[commit("2026-01-02T00:00:00Z", committer="web-flow", author="reviewer")])
        self.assertEqual(self.logins(p), [])

    def test_commits_with_no_author_or_committer_are_skipped(self):
        c = commit("2026-01-02T00:00:00Z")
        c["commit"]["committer"] = c["commit"]["author"] = None
        self.assertEqual(self.logins(pr(commits=[c])), [])

    def test_team_users_ignore_case(self):
        self.assertEqual(self.logins(pr([comment("TRIAGER", "2026-01-02T00:00:00Z")])), ["TRIAGER"])

    def test_ignored_users_ignore_case(self):
        self.assertEqual(self.logins(pr([comment("CODERABBITAI", "2026-01-02T00:00:00Z", association="MEMBER")])), [])

    def test_review_comments_join_the_review_text(self):
        review = {"author": {"login": "reviewer"}, "authorAssociation": "MEMBER", "state": "COMMENTED",
                  "submittedAt": "2026-01-02T00:00:00Z", "bodyText": "", "comments": {"nodes": [{"path": "a.c", "bodyText": "Why?"}]}}
        self.assertEqual(w.events(pr(reviews=[review]))[0][4], "[a.c] Why?")


class Decide(unittest.TestCase):
    def test_no_model_call_until_the_team_has_spoken(self):
        self.assertIsNone(w.decide(pr([comment("author", "2026-01-02T00:00:00Z")]), False, START, END))

    def test_a_labeled_pr_is_judged_even_with_only_the_author_speaking(self):
        p = pr([comment("author", "2026-01-02T00:00:00Z")], labeled=[("LabeledEvent", "2026-01-01T12:00:00Z")])
        self.assertEqual(w.decide(p, True, START, END), "judge")

    def test_every_pr_is_in_the_window_on_a_first_run(self):
        self.assertEqual(w.decide(pr([ASK]), False, "", END), "judge")

    def test_new_team_message_is_judged(self):
        self.assertEqual(w.decide(pr([ASK]), False, START, END), "judge")

    def test_activity_outside_the_window_is_left_alone(self):
        self.assertIsNone(w.decide(pr([ASK]), False, END, "2026-01-04T00:00:00Z"))
        self.assertIsNone(w.decide(pr([ASK]), False, START, "2026-01-01T12:00:00Z"))

    def test_label_change_after_the_activity_settles_it(self):
        p = pr([ASK], labeled=[("UnlabeledEvent", "2026-01-02T01:00:00Z")])
        self.assertIsNone(w.decide(p, False, START, END))

    def test_label_events_ignore_case(self):
        p = pr([ASK], labeled=[("UnlabeledEvent", "2026-01-02T01:00:00Z")])
        p["labeled"]["nodes"][0]["label"]["name"] = w.LABEL.upper()
        self.assertIsNone(w.decide(p, False, START, END))

    def test_author_commit_after_the_label_removes_it(self):
        p = pr([ASK], commits=[commit("2026-01-02T02:00:00Z")], labeled=[("LabeledEvent", "2026-01-02T01:00:00Z")])
        self.assertEqual(w.decide(p, True, START, END), "remove")

    def test_author_comment_after_the_label_is_judged(self):
        p = pr([ASK, comment("author", "2026-01-02T02:00:00Z")], labeled=[("LabeledEvent", "2026-01-02T01:00:00Z")])
        self.assertEqual(w.decide(p, True, START, END), "judge")

    def test_drafts_never_get_the_label_by_default(self):
        self.assertIsNone(w.decide(pr([ASK], draft=True), False, START, END))
        with mock.patch.object(w, "LABEL_DRAFTS", True):
            self.assertEqual(w.decide(pr([ASK], draft=True), False, START, END), "judge")

    def test_a_labeled_draft_is_still_judged(self):
        self.assertEqual(w.decide(pr([ASK], draft=True), True, START, END), "judge")


class Prompt(unittest.TestCase):
    def test_keeps_the_last_messages_and_marks_who_spoke(self):
        comments = [comment("author", f"2026-01-02T0{i}:00:00Z", f"message {i}") for i in range(8)] + [
            comment("reviewer", "2026-01-02T09:00:00Z", "x" * 3000, "MEMBER")]
        prompt = w.prompt_for(pr(comments))
        self.assertNotIn("message 2", prompt)
        self.assertIn("message 3", prompt)
        self.assertIn("@reviewer, team member", prompt)
        self.assertIn("x" * w.MAX_TEXT, prompt)
        self.assertNotIn("x" * (w.MAX_TEXT + 1), prompt)


class FakeModel:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def judge(self, prompt):
        self.calls += 1
        return {"waiting_on_author": self.answers.pop(0), "reason": "r"}, models.Usage(1, 10, 2, 0.5, 0.25)


def fresh_report():
    return {"added": [], "removed": [], "judged": 0, "held": 0, "failures": 0, "usage": models.Usage()}


class Judging(unittest.TestCase):
    def test_waiting_needs_every_confirmation(self):
        model = FakeModel(True, True, True)
        self.assertTrue(w.judge(model, "p", models.Usage())["waiting_on_author"])
        self.assertEqual(model.calls, 3)

    def test_one_disagreement_lands_on_not_waiting(self):
        model = FakeModel(True, False)
        self.assertFalse(w.judge(model, "p", models.Usage())["waiting_on_author"])
        self.assertEqual(model.calls, 2)

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

    def test_a_cut_off_search_stops_at_its_last_result(self):
        cut_off = [pr([ASK], number=i) | {"updatedAt": "2026-01-02T12:00:00Z"} for i in range(3)]
        with mock.patch.object(w, "search", side_effect=[[], cut_off]), mock.patch.object(w, "SEARCH_LIMIT", 3):
            _, end = w.collect(START, END, False, fresh_report())
        self.assertEqual(end, "2026-01-02T12:00:00Z")

    def test_a_complete_search_keeps_the_end(self):
        with mock.patch.object(w, "search", return_value=[pr([ASK]) | {"updatedAt": "2026-01-02T12:00:00Z"}]):
            todo, end = w.collect(START, END, False, fresh_report())
        self.assertEqual((len(todo), end), (2, END))


class Labels(unittest.TestCase):
    def test_slashes_in_a_label_are_escaped(self):
        with mock.patch.object(w, "LABEL", "status/waiting on author"), mock.patch.object(w, "gh") as gh:
            w.set_label(1, False, True)
        self.assertTrue(gh.call_args.args[-1].endswith("/labels/status%2Fwaiting%20on%20author"))


class Run(unittest.TestCase):
    def run_main(self, todo, argv=(), state=None, model=None):
        """Runs main() with GitHub and the model faked, and returns the saved state and the window passed to collect."""
        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "state", "state.json")
        if state:
            os.makedirs(os.path.dirname(path))
            with open(path, "w", encoding="utf-8") as out:
                json.dump({"judged_through": state}, out)
        fake = mock.MagicMock()
        fake.__enter__.return_value = model or FakeModel(*[False] * 10)
        with mock.patch.object(w, "STATE_FILE", path), mock.patch.object(w, "MAX_JUDGMENTS", 2), \
                mock.patch.object(w, "collect", side_effect=lambda s, e, a, r: (todo, e)) as collect, mock.patch.object(w, "ensure_label"), \
                mock.patch.object(w, "open_model", return_value=fake) as open_model, \
                mock.patch.object(w, "set_label"), mock.patch.object(sys, "argv", ["wait_label.py", *argv]):
            w.main()
            self.opened = open_model.called
        saved = None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as saved_file:
                saved = json.load(saved_file)["judged_through"]
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

    def test_the_cap_does_not_skip_a_pr_in_the_same_second(self):
        times = ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z"]
        saved, _ = self.run_main([(t, i, False, "p") for i, t in enumerate(times)], ["--apply"])
        self.assertEqual(saved, "2026-01-01T23:59:59Z")

    def test_more_prs_in_one_second_than_the_cap_still_moves_on(self):
        time = "2026-01-02T00:00:00Z"
        saved, _ = self.run_main([(time, i, False, "p") for i in range(3)], ["--apply"], state="2026-01-01T23:59:59Z")
        self.assertEqual(saved, time)

    def test_an_unknown_provider_stops_the_run(self):
        with mock.patch.object(w, "PROVIDER", "Claude"), self.assertRaises(SystemExit) as stopped:
            self.run_main([], ["--apply"])
        self.assertIn("Unknown provider", str(stopped.exception))

    def test_tools_are_installed_only_on_opening_a_model(self):
        with mock.patch.object(w, "INSTALL", True), mock.patch.object(w, "install") as install:
            self.run_main([], ["--apply"])
            install.assert_not_called()
            w.open_model()
            install.assert_called_once()

    def test_no_model_is_opened_with_nothing_to_judge(self):
        self.run_main([], ["--apply"])
        self.assertFalse(self.opened)

    def test_all_failing_exits_and_keeps_the_state(self):
        class Broken(FakeModel):
            def judge(self, prompt):
                raise RuntimeError("bad token")
        with self.assertRaises(SystemExit):
            self.run_main([("2026-01-02T00:00:00Z", 1, False, "p")], ["--apply"], model=Broken())


class Providers(unittest.TestCase):
    def test_github_tokens_never_reach_the_model(self):
        env = {"GH_TOKEN": "a", "GITHUB_TOKEN": "b", "WAIT_LABEL_COPILOT_TOKEN": "c", "EMPTY": "", "PATH": "p"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(models.model_env(), {"PATH": "p"})

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

    def test_install_uses_the_provider_and_version(self):
        with mock.patch("subprocess.run") as run:
            models.install("claude", "1.2.3", "4.5.6")
        self.assertIn("@anthropic-ai/claude-code@1.2.3", run.call_args.args[0])
        with mock.patch("subprocess.run") as run, mock.patch.object(sys, "path", list(sys.path)):
            models.install("copilot", "1.2.3", "4.5.6")
        self.assertIn("github-copilot-sdk==4.5.6", run.call_args.args[0])

    def test_installs_see_no_tokens(self):
        env = {"GH_TOKEN": "a", "GITHUB_TOKEN": "b", "WAIT_LABEL_COPILOT_TOKEN": "c",
               "CLAUDE_CODE_OAUTH_TOKEN": "d", "ANTHROPIC_API_KEY": "e", "PATH": "p"}
        for provider in ("claude", "copilot"):
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("subprocess.run") as run, \
                    mock.patch.object(sys, "path", list(sys.path)), mock.patch("tempfile.mkdtemp", return_value="t"):
                models.install(provider, "1.2.3", "4.5.6")
            self.assertEqual(run.call_args.kwargs["env"], {"PATH": "p"})

    def test_copilot_installs_from_wheels_only(self):
        with mock.patch("subprocess.run") as run, mock.patch.object(sys, "path", list(sys.path)):
            models.install("copilot", "1.2.3", "4.5.6")
        self.assertIn(":all:", run.call_args.args[0])

    def test_claude_errors_raise(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout=json.dumps({"is_error": True, "result": "no"}))):
            with self.assertRaises(RuntimeError):
                models.ClaudeModel("m", "s", {}).judge("p")


if __name__ == "__main__":
    unittest.main()
