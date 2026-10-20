"""Tests for claude_migrate. Run: python3 -m unittest -v test_claude_migrate"""
import contextlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

import claude_migrate as cm


def line(**kw):
    return json.dumps(kw) + "\n"


class SlugTest(unittest.TestCase):
    def test_non_alnum_becomes_dash(self):
        self.assertEqual(cm.slugify("/Users/a/rust-booking.rpc"), "-Users-a-rust-booking-rpc")
        self.assertEqual(cm.slugify("/Users/a/.herdr/w_t"), "-Users-a--herdr-w-t")

    def test_non_bmp_char_is_two_code_units(self):
        self.assertEqual(cm.slugify("/a/\U0001F600"), "-a---")

    def test_long_path_gets_hash_suffix(self):
        path = "/x/" + "a" * 250
        slug = cm.slugify(path)
        self.assertEqual(slug[:200], cm.slugify(path[:200]))
        self.assertEqual(slug[200:], "-" + cm._base36(abs(cm._java_hash(path))))

    def test_java_hash_matches_js(self):
        self.assertEqual(cm._java_hash("hello"), 99162322)
        self.assertEqual(cm._java_hash("polygenelubricants"), -2147483648)


class RewriterTest(unittest.TestCase):
    rw = cm.Rewriter({"/Users/a/proj": "/home/b/code/proj", "/Users/a": "/home/b"})

    def t(self, s, json_escape=True):
        return self.rw.text(s, json_escape)

    def test_prefix_boundaries(self):
        self.assertEqual(self.t('"cwd":"/Users/a/proj"'), '"cwd":"/home/b/code/proj"')
        self.assertEqual(self.t("/Users/a/proj/src/x.rs"), "/home/b/code/proj/src/x.rs")
        self.assertEqual(self.t("/Users/a/proj-rpc"), "/home/b/proj-rpc")
        self.assertEqual(self.t("/Users/a/proj.rpc/x"), "/home/b/proj.rpc/x")
        self.assertEqual(self.t("/Users/ab/x"), "/Users/ab/x")
        self.assertEqual(self.t("/mnt/Users/a/x"), "/mnt/Users/a/x")

    def test_bare_home_and_punctuation(self):
        self.assertEqual(self.t('"cwd":"/Users/a"'), '"cwd":"/home/b"')
        self.assertEqual(self.t("saved to /Users/a/proj."), "saved to /home/b/code/proj.")

    def test_json_escapes_and_ansi(self):
        self.assertEqual(self.t(r"ls\n/Users/a/proj\n"), r"ls\n/home/b/code/proj\n")
        self.assertEqual(self.t(r"\u001b[36m/Users/a/x"), r"\u001b[36m/home/b/x")

    def test_single_pass(self):
        rw = cm.Rewriter({"/a": "/b", "/b": "/a"})
        self.assertEqual(rw.text("/a/x /b/y", False), "/b/x /a/y")

    def test_json_escapes_replacement(self):
        rw = cm.Rewriter({"/a": '/we"ird'})
        self.assertEqual(json.loads(rw.text('"/a/x"', True)), '/we"ird/x')

    def test_slugs_after_projects(self):
        rw = cm.Rewriter({}, {"-Users-a-proj": "-home-b-proj"})
        self.assertEqual(rw.text("~/.claude/projects/-Users-a-proj/memory/", False),
                         "~/.claude/projects/-home-b-proj/memory/")
        self.assertEqual(rw.text("projects/-Users-a-proj-rpc/", False), "projects/-Users-a-proj-rpc/")


class MergeConfigTest(unittest.TestCase):
    def test_target_wins_and_sources_fill_in(self):
        dst = {"userID": "t", "projects": {"/p": {"trust": True, "allowedTools": ["x"]}},
               "githubRepoPaths": {"r": ["/p"]}}
        src = {"userID": "s", "theme": "dark",
               "projects": {"/p": {"allowedTools": ["y"], "mcpServers": {"m": {}}}, "/q": {}},
               "githubRepoPaths": {"r": ["/q", "/p"]}}
        added = cm.merge_config(dst, src, overwrite=False)
        self.assertEqual(dst["userID"], "t")
        self.assertEqual(dst["theme"], "dark")
        self.assertEqual(dst["projects"]["/p"], {"trust": True, "allowedTools": ["x"], "mcpServers": {"m": {}}})
        self.assertEqual(dst["githubRepoPaths"]["r"], ["/p", "/q"])
        self.assertEqual(added, ["/q"])

    def test_sanitize_drops_account_and_caches(self):
        cfg = cm.sanitize_config({"oauthAccount": {}, "userID": 1, "cachedFoo": 1, "modelAccessCache": 1,
                                  "mcpServers": {}, "projects": {}})
        self.assertEqual(sorted(cfg), ["mcpServers", "projects"])


class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src_home = root / "src" / "Users" / "alice"
        self.dst_home = root / "dst" / "home" / "bob"
        self.archive = root / "state.tar.gz"
        self.write_source()

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = cm.main([str(a) for a in argv])
        self.assertEqual(code, 0, err.getvalue())
        return err.getvalue()

    def export(self, *extra):
        sh = str(self.src_home)
        home = os.environ.get("HOME")
        os.environ["HOME"] = sh  # export records $HOME as the source home
        try:
            out = self.run_cli("export", "-o", self.archive, "--claude-dir", f"{sh}/.claude",
                               "--config-file", f"{sh}/.claude.json", *extra)
        finally:
            os.environ["HOME"] = home
        with tarfile.open(self.archive) as tar:
            files = {m.name: tar.extractfile(m).read().decode() for m in tar if m.isfile()}
        return out, files

    def write_source(self):
        h = str(self.src_home)
        cd = self.src_home / ".claude"
        proj = f"{h}/lab/app.rpc"
        slug_dir = cd / "projects" / cm.slugify(proj)
        (slug_dir / "s1" / "subagents").mkdir(parents=True)
        (slug_dir / "memory").mkdir()
        (slug_dir / "s1.jsonl").write_text(
            line(type="user", cwd=proj, message={"content": f"edit {proj}/src/main.rs"})
            + line(type="assistant", cwd=f"{proj}/sub", message={"content": [
                {"type": "tool_use", "input": {"file_path": f"{proj}/src/main.rs", "command": f"cd {h}/lab/app.rpc && ls"}}]})
            + line(type="user", cwd=proj, message={"content": f"memory at {cd}/projects/{slug_dir.name}/memory/"}))
        (slug_dir / "s1" / "subagents" / "agent-1.jsonl").write_text(line(cwd=proj))
        (slug_dir / "memory" / "MEMORY.md").write_text(f"- see {proj}/README.md\n")
        os.utime(slug_dir / "s1.jsonl", (1_000_000_000, 1_000_000_000))
        (cd / "history.jsonl").write_text(line(display="hi", timestamp=2, project=proj))
        (cd / "settings.json").write_text(json.dumps({"statusLine": {"command": f"{h}/.claude/sl.sh"}}))
        (cd / "file-history" / "s1").mkdir(parents=True)
        (cd / "file-history" / "s1" / "abc@v1").write_text(f"literal {h} kept\n")
        (cd / "debug").mkdir()
        (cd / "debug" / "log.txt").write_text("x")
        (cd / "sessions").mkdir()
        external = self.src_home / ".agents" / "skills" / "find"
        external.mkdir(parents=True)
        (external / "SKILL.md").write_text("skill\n")
        (cd / "skills").mkdir()
        os.symlink("../../.agents/skills/find", cd / "skills" / "find")
        (self.src_home / ".claude.json").write_text(json.dumps({
            "userID": "src", "oauthAccount": {"email": "x"},
            "projects": {proj: {"allowedTools": ["Bash(ls)"], "hasTrustDialogAccepted": True}},
            "mcpServers": {"fs": {"command": "npx", "args": [f"{h}/data"]}},
        }))

    def test_export_import_reimport_remap(self):
        sh, dh = str(self.src_home), str(self.dst_home)
        _, files = self.export()
        names = list(files)
        manifest_projects = json.loads(files["manifest.json"])["projects"]
        self.assertEqual(manifest_projects[0]["cwd"], f"{sh}/lab/app.rpc")
        self.assertIn("claude/skills/find/SKILL.md", names)
        self.assertFalse(any(n.startswith(("claude/debug", "claude/sessions")) for n in names))

        # different home, and the project moved from ~/lab to ~/code
        (self.dst_home / ".claude").mkdir(parents=True)
        (self.dst_home / ".claude.json").write_text(json.dumps({"userID": "dst"}))
        args = ["import", self.archive, "--target-home", dh, "--claude-dir", f"{dh}/.claude",
                "--config-file", f"{dh}/.claude.json", "--map", "~/lab=" + f"{dh}/code"]
        self.run_cli(*args, "--dry-run")
        self.assertFalse((self.dst_home / ".claude" / "projects").exists())
        self.run_cli(*args, "--cleanup-days", "3650")

        new_proj = f"{dh}/code/app.rpc"
        cd = self.dst_home / ".claude"
        slug_dir = cd / "projects" / cm.slugify(new_proj)
        recs = [json.loads(x) for x in (slug_dir / "s1.jsonl").read_text().splitlines()]
        self.assertEqual(recs[0]["cwd"], new_proj)
        self.assertEqual(recs[1]["cwd"], f"{new_proj}/sub")
        self.assertEqual(recs[1]["message"]["content"][0]["input"],
                         {"file_path": f"{new_proj}/src/main.rs", "command": f"cd {new_proj} && ls"})
        self.assertEqual(recs[2]["message"]["content"], f"memory at {dh}/.claude/projects/{slug_dir.name}/memory/")
        self.assertEqual((slug_dir / "s1.jsonl").stat().st_mtime, 1_000_000_000)
        self.assertIn(new_proj, (slug_dir / "s1" / "subagents" / "agent-1.jsonl").read_text())
        self.assertIn(f"{new_proj}/README.md", (slug_dir / "memory" / "MEMORY.md").read_text())
        self.assertEqual((cd / "file-history" / "s1" / "abc@v1").read_text(), f"literal {sh} kept\n")
        self.assertEqual(json.loads((cd / "history.jsonl").read_text())["project"], new_proj)
        settings = json.loads((cd / "settings.json").read_text())
        self.assertEqual(settings["statusLine"]["command"], f"{dh}/.claude/sl.sh")
        self.assertEqual(settings["cleanupPeriodDays"], 3650)
        self.assertEqual((cd / "skills" / "find" / "SKILL.md").read_text(), "skill\n")

        cfg = json.loads((self.dst_home / ".claude.json").read_text())
        self.assertEqual(cfg["userID"], "dst")
        self.assertNotIn("oauthAccount", cfg)
        self.assertEqual(list(cfg["projects"]), [new_proj])
        self.assertEqual(cfg["mcpServers"]["fs"]["args"], [f"{dh}/data"])

        # re-import is a no-op apart from the merged history
        out = self.run_cli(*args)
        self.assertIn(" 0 new,", out)
        self.assertEqual(len((cd / "history.jsonl").read_text().splitlines()), 1)

        # then move the project again on the "new" machine
        moved = f"{dh}/src/app"
        self.run_cli("remap", new_proj, moved, "--claude-dir", f"{dh}/.claude", "--config-file", f"{dh}/.claude.json")
        self.assertFalse(slug_dir.exists())
        moved_dir = cd / "projects" / cm.slugify(moved)
        self.assertEqual(json.loads((moved_dir / "s1.jsonl").read_text().splitlines()[0])["cwd"], moved)
        self.assertIn(moved, json.loads((self.dst_home / ".claude.json").read_text())["projects"])

    def test_export_single_project(self):
        sh = str(self.src_home)
        cd = self.src_home / ".claude"
        other = f"{sh}/lab/other"
        (cd / "projects" / cm.slugify(other)).mkdir()
        (cd / "projects" / cm.slugify(other) / "s2.jsonl").write_text(line(cwd=other))
        (cd / "file-history" / "s2").mkdir()
        (cd / "todos").mkdir()
        (cd / "todos" / "s1-agent-s1.json").write_text("[]")
        (cd / "todos" / "s2-agent-s2.json").write_text("[]")
        with (cd / "history.jsonl").open("a") as f:
            f.write(line(display="other", timestamp=3, project=other))
        cfg = json.loads((self.src_home / ".claude.json").read_text())
        cfg["projects"][other] = {}
        (self.src_home / ".claude.json").write_text(json.dumps(cfg))

        _, files = self.export("--project", f"{sh}/lab/app.rpc")
        names = set(files)
        self.assertIn(f"claude/projects/{cm.slugify(sh + '/lab/app.rpc')}/s1.jsonl", names)
        self.assertFalse(any(cm.slugify(other) in n for n in names))
        self.assertIn("claude/todos/s1-agent-s1.json", names)
        self.assertNotIn("claude/todos/s2-agent-s2.json", names)
        self.assertNotIn("claude/file-history/s2", " ".join(names))
        self.assertIn("claude/settings.json", names)
        self.assertNotIn("other", files["claude/history.jsonl"])
        self.assertEqual(list(json.loads(files["claude.json"])["projects"]), [f"{sh}/lab/app.rpc"])
        self.assertEqual(len(json.loads(files["manifest.json"])["projects"]), 1)

    def test_export_redact(self):
        cd = self.src_home / ".claude"
        key = "sk-ant-" + "api03-" + "x" * 40
        with (cd / "projects" / cm.slugify(f"{self.src_home}/lab/app.rpc") / "s1.jsonl").open("a") as f:
            f.write(line(type="user", message={"content": f"export ANTHROPIC_API_KEY={key}"}))
        (cd / "settings.json").write_text(json.dumps({"env": {"MY_TOKEN": "plain-value", "EDITOR": "vim"}}))
        cfg = json.loads((self.src_home / ".claude.json").read_text())
        cfg["mcpServers"]["exa"] = {"type": "http", "url": "https://mcp.example.com/mcp?exaApiKey=abcdef123456",
                                    "headers": {"Authorization": "Bearer abc"}, "env": {"LOG_LEVEL": "debug"}}
        (self.src_home / ".claude.json").write_text(json.dumps(cfg))

        out, files = self.export("--redact")
        self.assertIn("redacted:", out)
        joined = "\n".join(files.values())
        self.assertNotIn(key, joined)
        self.assertIn("[REDACTED:anthropic]", joined)
        settings = json.loads(files["claude/settings.json"])
        self.assertEqual(settings["env"], {"MY_TOKEN": "[REDACTED]", "EDITOR": "vim"})
        exa = json.loads(files["claude.json"])["mcpServers"]["exa"]
        self.assertEqual(exa["url"], "https://mcp.example.com/mcp?exaApiKey=[REDACTED]")
        self.assertEqual(exa["headers"]["Authorization"], "[REDACTED]")
        self.assertEqual(exa["env"]["LOG_LEVEL"], "debug")
        self.assertTrue(json.loads(files["manifest.json"])["redacted"])


class RedactorTest(unittest.TestCase):
    def test_formats(self):
        r = cm.Redactor()
        gh = "ghp_" + "A" * 36
        pem = "-----BEGIN RSA PRIVATE KEY-----\\nMIIE\\n-----END RSA PRIVATE KEY-----"
        out = r.text(f'"token {gh} and {pem} done"')
        self.assertEqual(out, '"token [REDACTED:github] and [REDACTED:private_key] done"')
        self.assertEqual(r.text("sk-ant-short"), "sk-ant-short")

    def test_tokens_inside_base64_are_left_alone(self):
        r = cm.Redactor()
        google = "AIza" + "B" * 35
        self.assertEqual(r.text(f"QqzQW8fznW{google}Zl2nm8W"), f"QqzQW8fznW{google}Zl2nm8W")
        self.assertEqual(r.text(f'"key":"{google}"'), '"key":"[REDACTED:google]"')
        self.assertEqual(r.text(f"x\\n{google}"), "x\\n[REDACTED:google]")


class MissingCommandsTest(unittest.TestCase):
    def test_reports_only_missing_programs(self):
        settings = {
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "/opt/nope/bin/lint --fix"},
                {"type": "command", "command": "FOO=1 python3 -c pass"},
                {"type": "command", "command": "cd /tmp && ls"},
                {"type": "command", "command": "$CLAUDE_PROJECT_DIR/hook.sh"},
            ]}]},
            "statusLine": {"type": "command", "command": "~/.nope/statusline.sh"},
        }
        config = {"mcpServers": {"a": {"command": "no-such-binary-xyz"}, "b": {"type": "http", "url": "x"}},
                  "projects": {"/p": {"mcpServers": {"c": {"command": "/bin/sh"}}}}}
        found = cm.missing_commands(settings, config)
        self.assertEqual(found, [
            ("hook PreToolUse", "/opt/nope/bin/lint"),
            ("statusLine", os.path.expanduser("~/.nope/statusline.sh")),
            ("MCP server a", "no-such-binary-xyz"),
        ])


if __name__ == "__main__":
    unittest.main()
