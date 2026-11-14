"""The GitHub Actions workflows and the repository hygiene around them. Standard library only, so the YAML is checked as text.

These pin the things that went wrong in the live bots (see the project's bot-schedule-incident doc): a state file that .gitignore
silently excluded, a schedule that is wrong in one season, secrets that name the live channel."""
import ast
import fnmatch
import glob
import os
import re
import sys
import unittest

from tests import helpers as h

import config as cfg

WF = os.path.join(h.ROOT, ".github", "workflows")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


SHADOW = read(os.path.join(WF, "shadow.yml"))
TESTS = read(os.path.join(WF, "tests.yml"))


def code_lines(text):
    """Workflow text without comment-only lines."""
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))


class ShadowWorkflow(unittest.TestCase):
    def test_cron_covers_both_seasons_of_the_16_20_et_run(self):
        crons = re.findall(r'cron:\s*"([^"]+)"', SHADOW)
        self.assertEqual(crons, ["20 21 * * 1-5"], "one backup line: 21:20 UTC is 16:20 EST / 17:20 EDT; weekdays only")
        self.assertEqual(cfg.TARGET_ET, "16:20")

    def test_dispatch_inputs(self):
        for name in ("scheduled_for_et", "trigger", "dry_run"):
            self.assertRegex(SHADOW, rf"(?m)^      {name}:\s*$")
        self.assertRegex(SHADOW, r"dry_run:\n(?:        .*\n)*?        type: boolean")
        self.assertRegex(SHADOW, r'trigger:\n(?:        .*\n)*?        default: "manual"')

    def test_one_run_at_a_time_and_none_cancelled(self):
        self.assertRegex(SHADOW, r"(?m)^concurrency:\n  group: shadow-bot\n  cancel-in-progress: false")

    def test_permissions_are_the_minimum(self):
        self.assertRegex(SHADOW, r"(?m)^permissions:\n  contents: write\n")
        self.assertEqual(re.findall(r"(?m)^\s*(?:actions|checks|issues|pull-requests|packages|id-token|deployments|statuses): ",
                                    SHADOW), [])

    def test_timeout(self):
        m = re.search(r"timeout-minutes:\s*(\d+)", SHADOW)
        self.assertIsNotNone(m)
        self.assertLessEqual(int(m.group(1)), 30)

    def test_only_the_shadow_webhook_secret_is_used(self):
        used = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", code_lines(SHADOW)))
        self.assertEqual(used, {"SHADOW_DISCORD_WEBHOOK_URL"})
        self.assertNotRegex(code_lines(SHADOW), r"\bDISCORD_WEBHOOK_URL\b(?<!SHADOW_DISCORD_WEBHOOK_URL)")
        self.assertNotIn("GH_PAT", SHADOW)
        self.assertNotIn("GITHUB_TOKEN", code_lines(SHADOW), "the default token is used implicitly by checkout")

    def test_no_secret_is_echoed_or_traced(self):
        body = code_lines(SHADOW)
        self.assertNotRegex(body, r"echo[^\n]*\$\{\{\s*secrets")
        self.assertNotRegex(body, r"echo[^\n]*SHADOW_DISCORD_WEBHOOK_URL")
        self.assertNotRegex(body, r"(?m)^\s*(set -x|set -o xtrace)")
        self.assertNotIn("ACTIONS_STEP_DEBUG", body)

    def test_the_guard_is_called_with_the_config_values(self):
        i = SHADOW.index("python run_guard.py")
        block = SHADOW[i:SHADOW.index("\n          fi", i)]
        args = " ".join(block.replace("\\\n", " ").split())
        self.assertIn(f"--job {cfg.JOB_ID}", args)
        self.assertIn(f"--target {cfg.TARGET_ET}", args)
        self.assertIn(f"--usable-until {cfg.USABLE_UNTIL_ET}", args)
        self.assertIn(f"--on-time-min {cfg.ON_TIME_MIN}", args)
        self.assertIn("--on-too-late run_flagged", args)
        self.assertIn('-- python shadow_job.py --trigger "$RUN_TRIGGER"', args)
        self.assertNotIn("--marker", args, "the marker is the default, .run_guard_shadow.json")

    def test_environment_contract_with_the_worker(self):
        self.assertIn("SCHEDULED_FOR_ET: ${{ inputs.scheduled_for_et }}", SHADOW)
        self.assertIn("RUN_TRIGGER: ${{ inputs.trigger || github.event_name }}", SHADOW)
        self.assertIn("SHADOW_DISCORD_WEBHOOK_URL: ${{ secrets.SHADOW_DISCORD_WEBHOOK_URL }}", SHADOW)

    def test_dry_run_path_writes_nothing(self):
        self.assertIn('if [ "$DRY_RUN" = "true" ]; then\n            python shadow_job.py --dry-run --trigger manual', SHADOW)
        self.assertIn("if: ${{ always() && !inputs.dry_run }}", SHADOW, "the commit step is skipped for a dry run")
        self.assertIn("DRY_RUN: ${{ inputs.dry_run && 'true' || 'false' }}", SHADOW)

    def test_runtime(self):
        self.assertIn("runs-on: ubuntu-latest", SHADOW)
        self.assertRegex(SHADOW, r'python-version: "3\.13"')
        self.assertIn("actions/checkout@v4", SHADOW)
        self.assertIn("actions/setup-python@v5", SHADOW)
        self.assertNotRegex(code_lines(SHADOW), r"pip install", "the bot is standard library only")

    def test_state_is_committed_back_even_when_the_job_failed(self):
        self.assertRegex(SHADOW, r"- name: Commit state\n        if: \$\{\{ always\(\)")

    def test_a_stuck_discord_queue_turns_the_run_red_after_state_is_saved(self):
        names = re.findall(r"(?m)^      - name: (.+)$", SHADOW)
        last = "Fail the run if Discord messages are stuck"
        self.assertEqual(names[-1], last)
        self.assertLess(names.index("Commit state"), names.index(last))                  # save first, announce last
        step = SHADOW[SHADOW.index(f"- name: {last}"):]
        self.assertIn("if: ${{ always() && !inputs.dry_run }}", step)
        self.assertIn(f"[ -e {cfg.DELIVERY_FAILED_FILE} ]", step)
        self.assertIn("exit 1", step)
        self.assertIn("::error", step)

    def test_the_delivery_marker_is_never_committed(self):
        m = re.search(r"for f in ([^;]+); do", SHADOW)
        self.assertNotIn(cfg.DELIVERY_FAILED_FILE, m.group(1).split())

    def test_every_state_file_is_in_the_commit_list(self):
        m = re.search(r"for f in ([^;]+); do", SHADOW)
        self.assertIsNotNone(m)
        listed = m.group(1).split()
        for name in (cfg.STATE_FILE, cfg.LOG_FILE, cfg.DIAG_FILE, cfg.EXPORT_FILE, cfg.GUARD_FILE, cfg.DATA_DIR):
            self.assertIn(name, listed)

    def test_push_is_retried(self):
        self.assertIn("for attempt in 1 2 3 4 5; do", SHADOW)
        self.assertIn('git pull --rebase --autostash origin "${GITHUB_REF_NAME}"', SHADOW)
        self.assertIn("push failed after 5 attempts", SHADOW)

    def test_the_commit_identity_is_not_a_person(self):
        self.assertIn('git config user.name "shadow-bot"', SHADOW)
        self.assertIn('git config user.email "shadow-bot@users.noreply.github.com"', SHADOW)
        self.assertEqual(set(re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", SHADOW)), {"shadow-bot@users.noreply.github.com"})


class IgnoreRules(unittest.TestCase):
    PATTERNS = [l.strip() for l in read(os.path.join(h.ROOT, ".gitignore")).splitlines() if l.strip() and not l.startswith("#")]

    def ignored(self, path):
        for pat in self.PATTERNS:
            if pat.endswith("/"):
                if any(part == pat[:-1] for part in path.split("/")[:-1]):
                    return True
            elif fnmatch.fnmatch(os.path.basename(path), pat) or fnmatch.fnmatch(path, pat):
                return True
        return False

    def test_state_files_are_not_ignored(self):
        """The live bots once lost every state commit to a .gitignore line. These must stay committable."""
        for path in (cfg.STATE_FILE, cfg.LOG_FILE, cfg.DIAG_FILE, cfg.EXPORT_FILE, cfg.GUARD_FILE, cfg.CLOSES_FILE, cfg.INDICES_FILE,
                     cfg.TBILL_FILE):
            self.assertFalse(self.ignored(path), path)

    def test_the_delivery_marker_is_ignored(self):
        self.assertTrue(self.ignored(cfg.DELIVERY_FAILED_FILE))

    def test_noise_is_ignored(self):
        for path in ("__pycache__/x.pyc", "tests/__pycache__/y.cpython-313.pyc", "shadow_state.json.tmp", ".pytest_cache/v", ".venv/bin/python"):
            self.assertTrue(self.ignored(path), path)

    def test_the_temp_files_the_bot_writes_are_ignored(self):
        for name in (cfg.STATE_FILE, cfg.EXPORT_FILE, cfg.GUARD_FILE):
            self.assertTrue(self.ignored(name + ".tmp"), name)


class TestsWorkflow(unittest.TestCase):
    def test_matrix_and_command(self):
        self.assertRegex(TESTS, r'python-version: \["3\.11", "3\.13"\]')
        self.assertIn("python -m unittest discover -s tests -t . -v", TESTS)

    def test_oracle_libraries_are_installed_for_tests_only(self):
        self.assertIn("pip install -r requirements-test.txt", TESTS)
        text = read(os.path.join(h.ROOT, "requirements-test.txt"))
        self.assertRegex(text, r"(?m)^numpy")
        self.assertRegex(text, r"(?m)^scipy")
        self.assertEqual(read(os.path.join(h.ROOT, "requirements.txt")).strip(), "", "the bot itself has no dependencies")

    def test_triggers(self):
        self.assertIn("branches: [main]", TESTS)
        self.assertIn("pull_request:", TESTS)
        self.assertNotIn("secrets.", TESTS, "the test workflow needs no secrets")
        self.assertNotRegex(TESTS, r"(?m)^permissions:\n  contents: write")


class SourceHygiene(unittest.TestCase):
    PY = sorted(p for p in glob.glob(os.path.join(h.ROOT, "*.py")))

    def test_runtime_imports_are_standard_library_or_local(self):
        local = {os.path.splitext(os.path.basename(p))[0] for p in self.PY}
        for path in self.PY:
            tree = ast.parse(read(path), filename=path)
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".")[0]]
                for n in names:
                    self.assertTrue(n in sys.stdlib_module_names or n in local or n == "__future__",
                                    f"{os.path.basename(path)} imports {n}, which is neither standard library nor part of the bot")

    def test_python_311_syntax(self):
        """CI runs 3.11 and 3.13; the job runs 3.13. Nothing may need a newer grammar than 3.11's."""
        for path in self.PY + sorted(glob.glob(os.path.join(h.HERE, "*.py"))):
            ast.parse(read(path), filename=path, feature_version=(3, 11))

    def test_no_fstring_reuses_its_own_quote_inside_braces(self):
        """Python 3.12 allows f"{d["k"]}", 3.11 does not: the tokenizer, not feature_version, decides, so scan for it."""
        import io
        import tokenize
        if sys.version_info >= (3, 12):
            for path in self.PY + sorted(glob.glob(os.path.join(h.HERE, "*.py"))):
                src = read(path)
                toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
                depth_quote = []
                for t in toks:
                    if t.type == tokenize.FSTRING_START:
                        depth_quote.append(t.string[-1])
                    elif t.type == tokenize.FSTRING_END:
                        depth_quote.pop()
                    elif t.type == tokenize.STRING and depth_quote and t.string[-1] == depth_quote[-1] and not t.string.startswith(('"""', "'''")):
                        self.fail(f"{os.path.basename(path)}:{t.start[0]} reuses the f-string's quote inside braces: {t.string}")

    def test_no_module_prints_a_secret_or_the_webhook(self):
        for path in self.PY:
            for i, line in enumerate(read(path).splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if re.search(r"print\([^)]*(webhook_url|url)\b[^)]*\)", line) and "discord" in os.path.basename(path):
                    self.fail(f"{os.path.basename(path)}:{i} may print the webhook URL: {line.strip()}")

    def test_no_personal_email_address_anywhere_in_the_repository(self):
        """The only address allowed is the bot's own no-reply commit identity."""
        pat = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
        files = (self.PY + sorted(glob.glob(os.path.join(h.HERE, "*.py"))) + glob.glob(os.path.join(WF, "*.yml"))
                 + [os.path.join(h.ROOT, n) for n in ("README.md", "requirements.txt", "requirements-test.txt", ".gitignore")]
                 + sorted(glob.glob(os.path.join(h.ROOT, "docs", "*.md"))))
        for path in files:
            if not os.path.exists(path):
                continue
            found = set(pat.findall(read(path))) - {"shadow-bot@users.noreply.github.com"}
            self.assertEqual(found, set(), os.path.relpath(path, h.ROOT))


if __name__ == "__main__":
    unittest.main()
