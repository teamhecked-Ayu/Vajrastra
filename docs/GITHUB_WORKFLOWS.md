# Approval-gated GitHub workflows

Vajrastra can inspect a selected GitHub branch, prepare a bounded project index,
show a proposed plan and diff, push one atomic commit to a dedicated work branch,
wait for the push-triggered watchdog, collect its test-log artifacts, and open
a draft pull request.

## Read-only inspection

Set a GitHub token in the environment as `GITHUB_TOKEN`. The token needs
repository read access.

    python vajrastra.py github inspect \
      --repo owner/repository \
      --base main \
      --task "fix timeout parsing in the request handler"

Inspection reads the selected branch's recursive Git tree, skips symlinks and
generated/vendor directories, fetches a bounded set of source, test, and
configuration files, and builds a read-only Python AST index. It displays the
safe file map, selected project context, and a proposed plan. It does not
create a branch or write a commit.

## Apply a prepared change

Prepare a JSON file mapping repository paths to their complete replacement
text. Example:

    {
      "src/handler.py": "def parse_timeout(value):\n    ...\n",
      "tests/test_handler.py": "def test_timeout():\n    ...\n"
    }

Then run:

    python vajrastra.py github apply \
      --repo owner/repository \
      --base feat/phase-4-github-native-workflows \
      --branch vajrastra/work/timeout-parser \
      --task "fix timeout parsing in the request handler" \
      --changes changes.json \
      --title "Fix timeout parsing"

The CLI prints the proposed diff before any remote write. It then asks for a
separate exact approval phrase for each operation:

1. Create the work branch.
2. Commit and push the change to that work branch.
3. Open a draft pull request after remote verification passes.

The library default is deny. Without an approval callback, no branch or commit
is created. A denied commit approval can leave the already-approved empty work
branch in place. The workflow never merges or deletes branches.

## Verification and artifacts

The commit push triggers the repository's existing `watchdog.yml` workflow.
The manager waits for the run matching the exact work-branch commit SHA. The
watchdog checks Python 3.10 and 3.12, runs compile and integrity checks, runs
the unit suite, smoke-runs the CLI, and uploads a verification log artifact
for each Python version.

A failed, missing, or timed-out verification blocks pull-request creation.
The CLI prints the Actions run URL and artifact download URLs when available.

The selected base branch must contain the artifact-enabled watchdog workflow.
Until this Phase 4 branch is merged into the intended base, use
`feat/phase-4-github-native-workflows` as the base for test runs. The workflow
does not dispatch arbitrary user-selected workflows.

## Safety limits

- Work branches must use `vajrastra/work/<lowercase-slug>`.
- The engine refuses to write to the base branch, `main`, `core/`,
  `core/security_lock.json`, or `AGENTS.md`.
- Symlinks, generated/vendor paths, unsafe paths, non-UTF-8 patches, and
  oversized files are rejected.
- Repository tree and content reads are bounded. A truncated Git tree aborts
  planning rather than producing an incomplete plan.
- The engine checks the base commit before planning, before writing, and
  before opening a pull request.
- Commit and tree objects are created first; the work-branch ref is advanced
  only after explicit commit/push approval.
- Pull requests are drafts. There is no merge or branch-delete method.
- The end-to-end unit test uses a fake GitHub API for a temporary test
  repository and asserts that only the work-branch ref changes. It does not
  claim a live PR was created during CI.

## Scope and limitations

This integration is currently designed around Vajrastra's GitHub client and
the repository's watchdog workflow. A different repository must provide a
compatible push-triggered workflow that uploads verification artifacts.
The project does not execute arbitrary shell commands supplied in a task
request. Changes are provided as bounded text replacements in a JSON file.
