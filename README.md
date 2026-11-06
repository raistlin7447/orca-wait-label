# Waiting on Author

A GitHub Action that labels pull requests waiting on their author. Open PRs without the label are your review queue.

People forget to add and remove a label like this, so a model reads the conversation and does it for them. The label stays on after a reply like "thanks, I'll fix that this week" and comes off when the fix is pushed.

The Action runs on your own runner with your own tokens. It never runs PR code, and the model has no tools. PR titles and recent messages are sent to the model service you choose.

## Quick start

Pick the setup that fits, add the workflow as `.github/workflows/waiting-on-author.yml`, then follow [After setup](#after-setup).

### Copilot in a personal repository

Create a [fine-grained token](https://github.com/settings/personal-access-tokens/new) owned by your personal account, with the **Copilot Requests** account permission. Add it as a repository secret named `COPILOT_TOKEN`. The Action uses that person's Copilot allowance, and the Free plan can't choose a model.

```yaml
name: Waiting on author

on:
  schedule:
    - cron: '7 * * * *'
  workflow_dispatch:

permissions:
  contents: read
  pull-requests: write
  # To create the label if it is missing
  issues: write

concurrency:
  group: waiting-on-author

jobs:
  sweep:
    runs-on: ubuntu-latest
    steps:
      - uses: raistlin7447/waiting-on-author@v1
        with:
          copilot-token: ${{ secrets.COPILOT_TOKEN }}
          # Change to false when the log looks right
          dry-run: true
```

### Copilot in an organization repository

No secret is needed. The organization needs a Copilot plan, and its "Allow use of Copilot CLI billed to the organization" policy must be on. The organization is billed. This setup hasn't been tested yet.

```yaml
name: Waiting on author

on:
  schedule:
    - cron: '7 * * * *'
  workflow_dispatch:

permissions:
  contents: read
  pull-requests: write
  # To create the label if it is missing
  issues: write
  copilot-requests: write

concurrency:
  group: waiting-on-author

jobs:
  sweep:
    runs-on: ubuntu-latest
    steps:
      - uses: raistlin7447/waiting-on-author@v1
        with:
          # Uses the workflow token, so no token input is needed
          # Change to false when the log looks right
          dry-run: true
```

### Claude

Add a repository secret named `CLAUDE_CODE_OAUTH_TOKEN`, with a token from `claude setup-token`. To use an API key, set `anthropic-api-key` instead.

```yaml
name: Waiting on author

on:
  schedule:
    - cron: '7 * * * *'
  workflow_dispatch:

permissions:
  contents: read
  pull-requests: write
  # To create the label if it is missing
  issues: write

concurrency:
  group: waiting-on-author

jobs:
  sweep:
    runs-on: ubuntu-latest
    steps:
      - uses: raistlin7447/waiting-on-author@v1
        with:
          provider: claude
          claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          # Change to false when the log looks right
          dry-run: true
```

### After setup

1. Run the workflow from the Actions tab and read the log. With `dry-run: true`, it only logs what it would change.
2. Change `dry-run` to `false` when the log looks right.

The first runs work through every open PR, oldest first, 20 PRs at a time, and later runs only look at new activity. The `held` count in the log shows how many are left. An hourly schedule gets through about 480 PRs a day, and you can raise `max-judgments` or run the workflow by hand to go faster. Dry runs and real runs keep separate progress, so the first real run starts again from the oldest PR.

Progress is saved in the Actions cache, and GitHub removes a cache that goes unused for 7 days. A run with no saved progress works through every open PR again, which spends credits again. Set `lookback-hours` to limit how far back it goes.

The Action needs a Linux or macOS runner with `gh`, plus `python3` and `pip` for Copilot (Python 3.11 or newer) or `npm` for Claude. `ubuntu-latest` has them all.

To stop the code that handles your token from changing, pin the Action to a commit SHA instead of `@v1`.

On a repository with little activity, GitHub can run a schedule far less often than you asked. Labels update later in that case, and each run still starts where the last one stopped. A timer elsewhere can also start the workflow, with `gh workflow run`.

Model names differ between providers, such as `claude-sonnet-5.5` on Copilot and `claude-sonnet-5-5` on Claude.

## How it decides

Each run looks at open PRs with new activity from the PR author or a team member. Bots are ignored. A PR is judged once its conversation has been quiet for 30 minutes (by default), so a quick back-and-forth is judged once, at the end.

- A PR without the label is skipped until a team member has spoken, which costs no model call.
- A commit by the author after the label was added removes it.
- Otherwise the model reads the last few messages and decides whether the review is waiting on the PR author, and the label is set to match.
- A "waiting" answer has to come back three times in a row (by default). If the calls disagree, the PR counts as not waiting, because a wrong label takes a PR out of your review queue.
- A label change made after the latest activity, by a person or by this Action, is left alone until there is new activity.
- The Action never adds the label to a draft (by default).
- A PR that can't be judged is left as it is until its next activity. If every judgment in a run fails, the run fails and the next run tries them again.

## Inputs

| Input | Default | What it controls |
|---|---|---|
| `provider` | `copilot` | `copilot` or `claude` |
| `copilot-token` | (`github-token`) | Token for Copilot |
| `claude-code-oauth-token` | | Token from `claude setup-token` |
| `anthropic-api-key` | | Anthropic API key, instead of the OAuth token |
| `github-token` | `github.token` | Token that can read PRs and change labels |
| `repository` | this repository | The repository whose PRs get the label |
| `dry-run` | `true` | Only log what would change |
| `since` | | Judge activity after this ISO time, instead of where the last run stopped |
| `lookback-hours` | `0` | How far back to look when no earlier run says where it stopped. `0` means every open PR, oldest first |
| `label` | `waiting on author` | The label's name. It is created if it is missing |
| `label-color` | `fbca04` | The color for a label this Action creates |
| `label-drafts` | `false` | Whether the label can be added to draft PRs |
| `team-associations` | `OWNER,MEMBER,COLLABORATOR` | Who counts as the team, by GitHub's author association |
| `team-users` | | Comma-separated logins that also count as the team |
| `ignored-users` | `github-actions,copilot-pull-request-reviewer,Copilot,coderabbitai,dependabot` | Accounts whose comments are ignored, besides any `[bot]` account |
| `command-prefix` | | Comments starting with this are commands to another bot, not conversation |
| `model` | `claude-sonnet-5-5` for `claude`, Copilot's pick for `copilot` | The model that judges |
| `confirmations` | `2` | Extra calls that must agree before a "waiting" answer stands |
| `quiet-minutes` | `30` | How long a conversation must be quiet before it is judged |
| `context-messages` | `6` | How many recent messages the model reads |
| `max-text` | `2000` | Characters kept from each message |
| `max-judgments` | `20` | PRs judged per run, with the rest held for the next run. `0` means no limit |
| `model-timeout` | `300` | Seconds to wait for each model call |
| `effort` | | Reasoning effort, from `low`, `medium`, `high`, `xhigh` and `max`. Empty leaves the provider's default |
| `github-tries` | `3` | How many times to try a GitHub search before giving up |
| `github-retry-delay` | `10` | Seconds to wait between tries |
| `max-comments` | `30` | Comments fetched per PR, newest first. `context-messages` can only read what these limits fetch |
| `max-reviews` | `20` | Reviews fetched per PR, newest first |
| `max-review-comments` | `20` | Inline comments fetched per review |
| `max-commits` | `10` | Commits fetched per PR, newest first |
| `max-label-events` | `30` | Label changes fetched per PR, newest first |
| `prompt-file` | (built in) | A file with your own instructions for the model; check out your repository first. `{project}` becomes the repository name |
| `log-reasons` | `false` | Log the model's reason for each PR. Actions logs are public on a public repository |
| `claude-code-version` | `2.1.318` | The Claude Code CLI version to install |
| `copilot-sdk-version` | `1.0.28` | The Copilot SDK version to install |

The schedule is in your own workflow file, so you set that there too.

## Outputs

Every run writes its totals to the log and the job summary, and sets these outputs:

| Output | Contains |
|---|---|
| `added`, `removed` | Comma-separated PR numbers |
| `judged`, `held`, `failures` | PR counts |
| `model-calls` | Calls made to the model |
| `input-tokens`, `output-tokens` | Tokens used |
| `cost-usd` | Claude's reported cost at list price. On a subscription token this is an estimate, not a bill |
| `ai-credits` | Copilot AI credits used |

The totals contain no PR text. In our test, a call used about 0.3 AI credits with Sonnet 5.5, and a PR takes up to 1 + `confirmations` calls.

## Closing abandoned PRs

To close PRs that never get a reply, point [`actions/stale`](https://github.com/actions/stale) at the label:

```yaml
- uses: actions/stale@v11
  with:
    only-pr-labels: waiting on author
    days-before-pr-stale: 90
    days-before-pr-close: 14
    # Without these two, actions/stale also closes old issues
    days-before-issue-stale: -1
    days-before-issue-close: -1
```

## Reliability

`eval/` builds cases from the history of open PRs, runs a model on them with the same call the Action makes, and scores the result against reference judgments. The cases and judgments aren't in this repository, because they're about named contributors' PRs.

We compared Sonnet 5.5 with reference judgments on 200 cases from one repository, with three calls required to agree. It said "waiting" on 3 cases where the reference did not, and missed 1 of 76 clear waits. The Claude CLI and the Copilot SDK scored the same. No Free plan account has been tested, so start with a dry run on your repository.

## Running locally

```bash
python3 -m unittest discover -s tests
pip install github-copilot-sdk
WAIT_LABEL_REPO=owner/name WAIT_LABEL_LOG_REASONS=true python3 wait_label.py --since 2026-10-27T00:00:00Z
```

Without `--apply` it changes nothing. It uses your `gh` login, and the provider's token from `WAIT_LABEL_COPILOT_TOKEN`, or from `CLAUDE_CODE_OAUTH_TOKEN` with `WAIT_LABEL_PROVIDER=claude`.
