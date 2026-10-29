# orca-wait-label

Keeps the `wait for response` label on [OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer) pull requests in step with who owes the next reply. The label means the team asked the PR author for something, so open PRs without it are the review queue. The repository, the label and the other choices are [settings](#settings), so it can run on another repository too.

## How it decides

Every 10 minutes, `wait_label.py` looks at open PRs with new activity from the PR author or a team member (anyone with write access). Bots and `/bot` commands are ignored. A PR is judged once its conversation has been quiet for 30 minutes (by default), so a quick back-and-forth is judged once, at the end.

- A commit by the author after the label was added removes it.
- Otherwise Claude reads the last few messages and answers one question: is the review waiting on the PR author? The label follows the answer.
- A "waiting" answer has to hold on three calls in a row (by default). When the answer is unclear, the label stays off, because a wrong label hides a PR that is waiting on us.
- A label change made after the latest activity, by a person or by this bot, is left alone until there is new activity. The bot never adds the label to a draft.

## Setup

Repository secrets:

- `CLAUDE_CODE_OAUTH_TOKEN`: from `claude setup-token`.
- `WAIT_LABEL_GITHUB_TOKEN`: a GitHub token that can add labels on the target repository.

Scheduled runs only log what they would change until the repository variable `WAIT_LABEL_APPLY` is `true`. A manual run from the Actions tab has an `apply` switch and a `since` time, for catching up after a gap.

## Settings

Each setting is a repository variable that the workflow passes to `wait_label.py`, which reads it from the environment. An unset variable uses the default. The schedule is the one exception: it is in `.github/workflows/wait-label.yml`, because GitHub does not allow a variable there.

| Variable | Default | What it controls |
|---|---|---|
| `WAIT_LABEL_REPO` | `OrcaSlicer/OrcaSlicer` | The repository whose PRs get the label |
| `WAIT_LABEL_LABEL` | `wait for response` | The label's name |
| `WAIT_LABEL_TEAM_ASSOCIATIONS` | `OWNER,MEMBER,COLLABORATOR` | Who counts as the team, by GitHub's author association |
| `WAIT_LABEL_IGNORED_USERS` | `github-actions,copilot-pull-request-reviewer,Copilot,coderabbitai,dependabot` | Accounts whose comments are ignored, besides any `[bot]` account |
| `WAIT_LABEL_COMMAND_PREFIX` | `/bot` | Comments starting with this are commands, not conversation |
| `WAIT_LABEL_MODEL` | `claude-sonnet-5-5` | The Claude model that judges |
| `WAIT_LABEL_CONFIRMATIONS` | `2` | Extra calls that must agree before a "waiting" answer stands |
| `WAIT_LABEL_QUIET_MINUTES` | `30` | How long a conversation must be quiet before it is judged |
| `WAIT_LABEL_CONTEXT_MESSAGES` | `6` | How many recent messages Claude reads |
| `WAIT_LABEL_MAX_TEXT` | `2000` | Characters kept from each message |
| `WAIT_LABEL_LABEL_DRAFTS` | `false` | Whether the label can be added to draft PRs |
| `WAIT_LABEL_PROMPT_FILE` | (built in) | A file with your own instructions for Claude; `{project}` becomes the repository name |
| `WAIT_LABEL_CLAUDE_CODE_VERSION` | `2.1.308` | The Claude Code CLI version the workflow installs |
| `WAIT_LABEL_APPLY` | (unset) | `true` lets scheduled runs change labels |

## Running locally

```bash
python3 wait_label.py --since 2026-10-27T00:00:00Z --verbose
```

Without `--apply` it changes nothing. `--verbose` prints each verdict with Claude's reason; the workflow leaves it off, since its log is public.

## Eval

`eval/` builds cases from the history of open PRs, runs a model on them with the same call the bot makes, and scores the result against reference judgments. The cases and judgments stay local because they judge named contributors' PRs.
