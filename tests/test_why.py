import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from whyblame import cli, gitlog
from whyblame.explain import render_prompt
from whyblame.story import gather


def git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=Ada", "-c", "user.email=ada@example.com", *args],
        cwd=root, check=True, capture_output=True,
    )


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-q")
        f = self.root / "retry.py"
        f.write_text("def fetch():\n    return get()\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "Add fetch")
        f.write_text("def fetch():\n    for _ in range(3):  # flaky upstream\n        return get()\n")
        git(self.root, "commit", "-qam", "Retry fetch 3 times (#42)\n\nUpstream drops ~1% of requests. Fixes #17.")
        self.file = f

    def tearDown(self):
        self.tmp.cleanup()

    def test_history_is_scoped_newest_first_with_refs(self):
        commits = gitlog.history(self.root, "retry.py", 2, 2, 10)
        self.assertEqual(commits[0].subject, "Retry fetch 3 times (#42)")
        self.assertEqual(commits[0].refs, [42, 17])
        self.assertIn("flaky upstream", commits[0].patch)
        self.assertIn("Upstream drops", commits[0].body)

    def test_gather_offline_and_prompt_numbers_sources(self):
        story = gather(self.file, 2, 3, use_forge=False)
        self.assertEqual(story.path, "retry.py")
        self.assertEqual(story.code[0], "    for _ in range(3):  # flaky upstream")
        self.assertFalse(story.has_discussion)
        prompt = render_prompt(story)
        self.assertIn('<source id="1" kind="commit"', prompt)
        self.assertIn("    2      for _ in range(3)", prompt)

    def test_no_github_remote_is_a_note_not_an_error(self):
        story = gather(self.file, 1, 1)
        self.assertIn("no GitHub remote found; using git history only", story.warnings)

    def test_cli_json(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main([f"{self.file}:2", "--offline", "--backend", "none", "--json"])
        self.assertEqual(code, 0)
        out = json.loads(buf.getvalue())
        self.assertIsNone(out["summary"])
        self.assertEqual(out["sources"][0]["title"], "Retry fetch 3 times (#42)")

    def test_cli_without_model_shows_commit_body(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main([f"{self.file}:2", "--offline", "--backend", "none"])
        self.assertIn("│ Upstream drops ~1% of requests. Fixes #17.", buf.getvalue())


class OpenRouter(unittest.TestCase):
    def test_request_shape_and_reply(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from unittest import mock

        from whyblame import explain as ex
        from whyblame.story import Source, Story

        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen["auth"] = self.headers["Authorization"]
                seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                reply = json.dumps({"choices": [{"message": {"content": " It retries [1]. "}}]})
                self.send_response(200)
                self.end_headers()
                self.wfile.write(reply.encode())

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        story = Story("a.py", 1, 1, ["x = 1"])
        story.sources.append(Source(1, "commit", "add x", "sha", "Ada", "2026-01-01", "add x"))
        url = f"http://127.0.0.1:{server.server_port}/chat"
        env = {"OPENROUTER_API_KEY": "test-key"}
        try:
            with mock.patch.object(ex, "OPENROUTER_URL", url), mock.patch.dict("os.environ", env):
                self.assertEqual(ex.pick_backend("auto") in ("claude", "openrouter"), True)
                self.assertEqual(ex.explain(story, "openrouter"), "It retries [1].")
        finally:
            server.shutdown()
        self.assertEqual(seen["auth"], "Bearer test-key")
        self.assertEqual(seen["body"]["model"], "anthropic/claude-opus-5.5")
        self.assertEqual(seen["body"]["messages"][0]["role"], "system")


class EnvDefaults(Repo):
    def run_cli(self, env, *extra):
        from unittest import mock

        calls = []
        fake = lambda story, backend, model=None: calls.append((backend, model)) or "ok"
        with mock.patch.dict("os.environ", env), mock.patch.object(cli, "explain", fake), \
                redirect_stdout(io.StringIO()):
            cli.main([f"{self.file}:2", "--offline", *extra])
        return calls[0]

    def test_why_backend_and_model_from_env(self):
        env = {"WHY_BACKEND": "openrouter", "WHY_MODEL": "anthropic/claude-haiku-5.5"}
        self.assertEqual(self.run_cli(env), ("openrouter", "anthropic/claude-haiku-5.5"))

    def test_flags_beat_env(self):
        env = {"WHY_BACKEND": "openrouter", "WHY_MODEL": "a"}
        self.assertEqual(self.run_cli(env, "--backend", "ollama", "--model", "b"), ("ollama", "b"))

    def test_bad_why_backend_is_a_clear_error(self):
        from unittest import mock

        with mock.patch.dict("os.environ", {"WHY_BACKEND": "openruter"}), \
                redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            cli.main([f"{self.file}:2"])


class Context(unittest.TestCase):
    def test_small_file_is_sent_whole(self):
        from whyblame.context import surrounding

        lines = ["// Bump CACHE when FILES changes.", "var CACHE = 'v1';", "use(CACHE);"]
        self.assertEqual(surrounding(lines, 3, 3), [(1, lines[0]), (2, lines[1]), (3, lines[2])])

    def test_large_file_keeps_header_window_and_far_definitions(self):
        from whyblame.context import surrounding

        lines = ["# Rule: keep RETRIES small.", "import x", "RETRIES = 3", ""]
        lines += [f"filler_{i} = {i}" for i in range(300)]
        lines += ["def fetch():", "    for _ in range(RETRIES):", "        get()"]
        target = len(lines) - 1  # the "for" line
        kept = dict(surrounding(lines, target, target))
        self.assertIn(1, kept)  # header comment
        self.assertEqual(kept[3], "RETRIES = 3")  # definition far above the window
        self.assertIn(target, kept)
        self.assertNotIn(100, kept)  # unrelated middle of the file is dropped

    def test_prompt_marks_asked_lines(self):
        from whyblame.story import Story

        story = Story("a.js", 2, 2, ["use(X);"], [(1, "var X = 1;"), (2, "use(X);")])
        prompt = render_prompt(story)
        self.assertIn("\n     1  var X = 1;", prompt)
        self.assertIn(">    2  use(X);", prompt)


class Web(Repo):
    def test_blame_url_uses_upstream_or_head(self):
        git(self.root, "remote", "add", "origin", "git@github.com:octo/hello.git")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root,
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(
            gitlog.blame_url(self.root, "retry.py", 2, 3),
            f"https://github.com/octo/hello/blame/{head}/retry.py#L2-L3",
        )

    def test_web_without_github_remote_errors(self):
        from contextlib import redirect_stderr

        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main([f"{self.file}:2", "--web"]), 1)
        self.assertIn("needs a GitHub remote", err.getvalue())


class Parsing(unittest.TestCase):
    def test_targets(self):
        self.assertEqual(cli.parse_target("a.py:4", None)[1:], (4, 4))
        self.assertEqual(cli.parse_target("a.py:4-9", None)[1:], (4, 9))
        self.assertEqual(cli.parse_target("a.py", "4,9")[1:], (4, 9))
        with self.assertRaises(ValueError):
            cli.parse_target("a.py", None)
        with self.assertRaises(ValueError):
            cli.parse_target("a.py:9-4", None)

    def test_refs(self):
        text = "Merge pull request #12 from x/y\nsee GH-3, https://github.com/o/r/issues/7, #12 again, &#39;"
        self.assertEqual(gitlog.extract_refs(text), [12, 3, 7])

    def test_remote_slug_formats(self):
        with tempfile.TemporaryDirectory() as d:
            git(d, "init", "-q")
            for url in ("git@github.com:octo/hello.git", "https://github.com/octo/hello"):
                git(d, "remote", "remove", "origin") if url.startswith("https") else None
                git(d, "remote", "add", "origin", url)
                self.assertEqual(gitlog.remote_slug(Path(d)), ("octo", "hello"))


if __name__ == "__main__":
    unittest.main()
