#!/usr/bin/env python3
"""Export Claude Code state and restore it on another machine.

Claude Code keys most of its state by absolute path: transcripts live in
~/.claude/projects/<slug>/ where <slug> is the encoded project directory, every
transcript line records its `cwd`, tool calls embed file paths, and ~/.claude.json
keys per-project settings by path. When the home directory or project locations
differ on the new machine, all of these are rewritten on import so that
`claude --resume` finds the old sessions from the new locations.

Commands:
  export   write ~/.claude + ~/.claude.json into a portable .tar.gz
  inspect  show what an archive contains
  import   restore an archive, rewriting paths (--map OLD=NEW for moved projects)
  remap    rebind existing sessions on this machine after moving a project

Requires only the Python 3.8+ standard library.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import getpass
import io
import json
import os
import platform
import re
import shlex
import shutil
import socket
import sys
import tarfile
import time
from collections import Counter
from pathlib import Path, PurePosixPath

FORMAT_VERSION = 1

# Top-level entries of ~/.claude that are machine-bound, runtime-only or regenerated.
EXCLUDE_TOP = [
    ".credentials.json",  # auth token on Linux; log in again on the new machine
    ".claude.json*",  # exported separately when CLAUDE_CONFIG_DIR is used
    "debug", "statsig", "telemetry", "cache", "backups", "ide",
    "daemon", "daemon*", "sessions", "session-env", "shell-snapshots",
    "policy-limits*", "remote-settings*", "mcp-needs-auth-cache.json",
    ".last-*", "*.lock",
]
# Large, rarely needed for resuming; opt in with a flag.
OPTIONAL_TOP = {"jobs": "include_jobs"}

# Files whose contents are user data or third-party code, not Claude state.
# Their bytes are copied verbatim, never path-rewritten.
NO_REWRITE_PREFIXES = (
    "file-history/", "paste-cache/", "image-cache/", "jobs/",
    "plugins/cache/", "plugins/marketplaces/",
)
TEXT_SUFFIXES = {".jsonl", ".json", ".md", ".txt", ".sh", ".py", ".js", ".ts",
                 ".toml", ".yaml", ".yml"}

# ~/.claude.json keys tied to the account, the machine, or a server-side cache.
CLAUDE_JSON_DROP = {
    "oauthAccount", "userID", "machineID", "anonymousId", "primaryApiKey",
    "customApiKeyResponses", "numStartups", "firstStartTime", "installMethod",
    "claudeCodeFirstTokenDate", "metricsStatusCacheByPrincipal", "clientDataCacheSlots",
    "changelogLastFetched",
}
# Dict-valued ~/.claude.json keys merged entry-by-entry with the target's.
CLAUDE_JSON_MERGE = {"projects", "mcpServers", "githubRepoPaths", "skillUsage", "pluginUsage"}

MAX_SLUG = 200

# Well-known credential formats, replaced by --redact wherever they appear in text.
# Scanned one by one: each starts with a literal, which re finds far faster than an alternation.
_SECRET_RES = [(name, re.compile(pat, re.DOTALL)) for name, pat in [
    ("anthropic", r"sk-ant-[A-Za-z0-9_-]{20,}"),
    ("openai", r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}"),
    ("github", r"gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}"),
    ("aws", r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    ("slack", r"xox[abposr]-[A-Za-z0-9-]{10,}"),
    ("google", r"AIza[0-9A-Za-z_-]{35}"),
    ("stripe", r"[rs]k_live_[0-9A-Za-z]{20,}"),
    ("jwt", r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("private_key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"),
    ("bearer", r"(?<=Bearer )[A-Za-z0-9._~+/=-]{20,}"),
]]
_TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/_-")
# Credentials passed as URL query parameters, e.g. https://mcp.example.com/?apiKey=...
_SECRET_PARAM_RE = re.compile(r"([?&][A-Za-z_-]*(?:api[_-]?key|token|secret)[A-Za-z_-]*=)([^&\"'\s\\]{8,})",
                              re.IGNORECASE)
# env/header names whose values are redacted whatever they look like.
_SECRET_NAME_RE = re.compile(r"key|token|secret|passw|auth|credential|cookie", re.IGNORECASE)


# --------------------------------------------------------------------------- paths


def default_claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def default_config_file(claude_dir: Path) -> Path:
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return claude_dir / ".claude.json"
    return Path.home() / ".claude.json"


def _java_hash(s: str) -> int:
    """String hash Claude Code uses for long slugs: (h << 5) - h + charCode, int32."""
    h = 0
    data = s.encode("utf-16-le")
    for i in range(0, len(data), 2):
        h = (h * 31 + int.from_bytes(data[i:i + 2], "little")) & 0xFFFFFFFF
    return h - (1 << 32) if h >= (1 << 31) else h


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if n == 0:
            return out


def slugify(path: str) -> str:
    """Encode a project path the way Claude Code names ~/.claude/projects/<slug>.

    Mirrors `path.replace(/[^a-zA-Z0-9]/g, "-")` over UTF-16 code units, with
    names over 200 chars truncated and suffixed with a hash of the full path.
    """
    s = "".join(
        ch if ch.isascii() and ch.isalnum() else ("--" if ord(ch) > 0xFFFF else "-")
        for ch in path
    )
    if len(s) <= MAX_SLUG:
        return s
    return f"{s[:MAX_SLUG]}-{_base36(abs(_java_hash(path)))}"


def _norm(p: str) -> str:
    return p.rstrip("/") if len(p) > 1 else p


class Rewriter:
    """Rewrites absolute path prefixes and project slugs inside text.

    A prefix only matches on path boundaries, so mapping /Users/a/foo leaves
    /Users/a/foo-bar and /Users/a/foo.rpc alone. All prefixes are replaced in a
    single pass (longest first) so one mapping's output is never re-mapped.
    """

    _NAME = r"A-Za-z0-9_~\-"

    def __init__(self, path_map: dict[str, str], slug_map: dict[str, str] | None = None):
        self.path_map = {_norm(k): _norm(v) for k, v in path_map.items() if _norm(k) != _norm(v)}
        self.slug_map = {k: v for k, v in (slug_map or {}).items() if k != v}
        self._path_re = None
        self._slug_re = None
        if self.path_map:
            alts = "|".join(re.escape(k) for k in sorted(self.path_map, key=len, reverse=True))
            self._path_re = re.compile(
                # preceded by a non-path char, a JSON \n \t \r escape, or an ANSI color code
                rf"(?:(?<=\\[nrt])|(?<=\[\dm)|(?<=[\[;]\d\dm)|(?<![{self._NAME}.]))({alts})"
                # followed by "/", end, or a non-path char (a trailing "." is punctuation)
                rf"(?![{self._NAME}]|\.[{self._NAME}])"
            )
        if self.slug_map:
            alts = "|".join(re.escape(k) for k in sorted(self.slug_map, key=len, reverse=True))
            self._slug_re = re.compile(rf"(?<=projects/)({alts})(?![A-Za-z0-9-])")

    def __bool__(self) -> bool:
        return bool(self.path_map or self.slug_map)

    def map_path(self, p: str) -> str:
        return self.text(p, json_escape=False)

    def text(self, s: str, json_escape: bool) -> str:
        esc = (lambda v: json.dumps(v, ensure_ascii=False)[1:-1]) if json_escape else (lambda v: v)
        if self._path_re:
            s = self._path_re.sub(lambda m: esc(self.path_map[m.group(1)]), s)
        if self._slug_re:
            s = self._slug_re.sub(lambda m: self.slug_map[m.group(1)], s)
        return s

    def bytes(self, rel: str, data: bytes) -> bytes:
        if not self or not should_rewrite(rel):
            return data
        try:
            s = data.decode("utf-8")
        except UnicodeDecodeError:
            return data
        out = self.text(s, json_escape=rel.endswith((".json", ".jsonl")))
        return data if out == s else out.encode("utf-8")


def should_rewrite(rel: str) -> bool:
    return not rel.startswith(NO_REWRITE_PREFIXES) and PurePosixPath(rel).suffix in TEXT_SUFFIXES


class Redactor:
    """Best-effort removal of credentials from exported state (--redact).

    Replaces values of secret-looking env vars and headers in settings and MCP
    server configs, plus well-known token formats anywhere in text files.
    """

    def __init__(self):
        self.counts = Counter()

    def text(self, s: str) -> str:
        def fmt(name):
            def sub(m):
                t, i, j = m.string, m.start(), m.end()
                # a token inside a longer base64 blob is not a token (a JSON \n escape may precede one)
                inside = ((i and t[i - 1] in _TOKEN_CHARS and not (i >= 2 and t[i - 2] == "\\" and t[i - 1] in "nrt"))
                          or (j < len(t) and t[j] in _TOKEN_CHARS))
                if inside and name not in ("private_key", "bearer"):
                    return m.group(0)
                self.counts[name] += 1
                return f"[REDACTED:{name}]"
            return sub

        def param(m):
            self.counts["url-param"] += 1
            return m.group(1) + "[REDACTED]"
        for name, pattern in _SECRET_RES:
            s = pattern.sub(fmt(name), s)
        return _SECRET_PARAM_RE.sub(param, s)

    def _values(self, d) -> None:
        for k, v in (d.items() if isinstance(d, dict) else []):
            if isinstance(v, str) and _SECRET_NAME_RE.search(k) and not v.startswith("[REDACTED"):
                d[k] = "[REDACTED]"
                self.counts["env/header"] += 1

    def config(self, config: dict) -> dict:
        """Redact a ~/.claude.json, including per-project MCP servers."""
        scopes = [config] + [p for p in (config.get("projects") or {}).values() if isinstance(p, dict)]
        for scope in scopes:
            for server in (scope.get("mcpServers") or {}).values():
                if isinstance(server, dict):
                    self._values(server.get("env"))
                    self._values(server.get("headers"))
        return json.loads(self.text(json.dumps(config)))

    def bytes(self, rel: str, data: bytes) -> bytes:
        if rel.startswith(("plugins/cache/", "plugins/marketplaces/")):
            return data
        try:
            s = orig = data.decode("utf-8")
        except UnicodeDecodeError:
            return data
        if rel == "settings.json":
            try:
                settings = json.loads(s)
                self._values(settings.get("env"))
                s = json.dumps(settings, indent=2) + "\n"
            except (ValueError, AttributeError):
                pass
        out = self.text(s)
        return data if out == orig else out.encode("utf-8")


def slug_fallback(slug: str, rw: Rewriter) -> str:
    """New slug for a project whose real cwd is unknown, by encoded-prefix match."""
    for old in sorted(rw.path_map, key=len, reverse=True):
        enc = slugify(old)
        if slug == enc or slug.startswith(enc + "-"):
            return slugify(rw.path_map[old]) + slug[len(enc):]
    return slug


# --------------------------------------------------------------------------- helpers


def log(msg: str = "") -> None:
    print(msg, file=sys.stderr)


def excluded(name: str, args) -> bool:
    if name in OPTIONAL_TOP and not getattr(args, OPTIONAL_TOP[name], False):
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE_TOP + (args.exclude or []))


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.migrate-tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def live_sessions(claude_dir: Path) -> list[int]:
    """PIDs of Claude Code processes that registered in ~/.claude/sessions and still run."""
    pids = []
    for f in (claude_dir / "sessions").glob("*.json"):
        try:
            pid = int(f.stem)
            os.kill(pid, 0)
        except (ValueError, ProcessLookupError):
            continue
        except PermissionError:
            pass
        pids.append(pid)
    return pids


_CWD_RE = re.compile(r'"cwd":"((?:[^"\\]|\\.)*)"')


def transcript_cwds(jsonl: Path) -> Counter:
    """Every cwd recorded in a transcript (sessions can move into worktrees midway)."""
    seen = Counter()
    try:
        text = jsonl.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return seen
    for m in _CWD_RE.finditer(text):
        try:
            seen[json.loads(f'"{m.group(1)}"')] += 1
        except ValueError:
            pass
    return seen


def known_paths(claude_dir: Path, config: dict) -> set[str]:
    """Project paths mentioned outside transcripts, used to resolve slugs."""
    paths = set(config.get("projects", {}))
    for repo_paths in config.get("githubRepoPaths", {}).values():
        paths.update(p for p in repo_paths if isinstance(p, str))
    hist = claude_dir / "history.jsonl"
    if hist.exists():
        with hist.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    p = json.loads(line).get("project")
                except (ValueError, AttributeError):
                    continue
                if isinstance(p, str):
                    paths.add(p)
    return paths


def resolve_projects(claude_dir: Path, config: dict) -> list[dict]:
    """Map each projects/<slug> dir to the real path it was created for.

    Slugs are lossy (/a/b-c and /a/b.c both encode to -a-b-c), so the cwd is read
    from the transcripts and only accepted when it encodes back to the slug.
    """
    extra = known_paths(claude_dir, config)
    projects = []
    root = claude_dir / "projects"
    for d in sorted(root.iterdir() if root.is_dir() else []):
        if not d.is_dir():
            continue
        sessions = sorted(d.glob("*.jsonl"))
        seen = sum((transcript_cwds(s) for s in sessions), Counter())
        matches = [c for c, _ in seen.most_common() if slugify(c) == d.name]
        matches += sorted(c for c in extra if slugify(c) == d.name and c not in matches)
        projects.append({"slug": d.name, "cwd": matches[0] if matches else None,
                         "sessions": len(sessions)})
    return projects


def sanitize_config(config: dict) -> dict:
    def drop(k: str) -> bool:
        return (k in CLAUDE_JSON_DROP or k.startswith("cached")
                or k.endswith("Cache") or k.endswith("CacheSlots"))
    return {k: v for k, v in config.items() if not drop(k)}


def merge_config(dst: dict, src: dict, overwrite: bool) -> list[str]:
    """Merge an imported ~/.claude.json into the target's. Returns added project keys."""
    added = []
    for key, val in src.items():
        if key not in dst:
            dst[key] = val
            if key == "projects":
                added.extend(val)
        elif key in CLAUDE_JSON_MERGE and isinstance(val, dict) and isinstance(dst[key], dict):
            cur = dst[key]
            for k, v in val.items():
                if key == "githubRepoPaths" and isinstance(v, list):
                    cur[k] = list(dict.fromkeys((cur.get(k) or []) + v))
                elif k not in cur:
                    cur[k] = v
                    if key == "projects":
                        added.append(k)
                elif isinstance(v, dict) and isinstance(cur[k], dict):
                    cur[k] = {**cur[k], **v} if overwrite else {**v, **cur[k]}
                elif overwrite:
                    cur[k] = v
        elif overwrite:
            dst[key] = val
    return added


_SHELL_BUILTINS = {"cd", "export", "source", ".", "exec", "eval", "test", "[", "echo", "true", "false"}


def missing_commands(settings: dict, config: dict) -> list[tuple[str, str]]:
    """Programs named by hooks, the status line and MCP servers that this machine lacks."""
    found = []

    def check(where: str, cmd) -> None:
        if not isinstance(cmd, str) or not cmd.strip():
            return
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            tokens = cmd.split()
        tokens = [t for t in tokens if "=" not in t.split("/")[0]]  # skip FOO=bar prefixes
        if not tokens or "$" in tokens[0] or tokens[0] in _SHELL_BUILTINS:
            return
        prog = os.path.expanduser(tokens[0])
        if not (Path(prog).exists() if "/" in prog else shutil.which(prog)):
            found.append((where, prog))

    for event, groups in (settings.get("hooks") or {}).items():
        for group in groups if isinstance(groups, list) else []:
            for hook in (group.get("hooks") or []) if isinstance(group, dict) else []:
                if isinstance(hook, dict) and hook.get("type", "command") == "command":
                    check(f"hook {event}", hook.get("command"))
    if isinstance(settings.get("statusLine"), dict):
        check("statusLine", settings["statusLine"].get("command"))
    scopes = [("", config)] + [(f" ({path})", p) for path, p in (config.get("projects") or {}).items()
                               if isinstance(p, dict)]
    for label, scope in scopes:
        for name, server in (scope.get("mcpServers") or {}).items():
            if isinstance(server, dict):
                check(f"MCP server {name}{label}", server.get("command"))
    return list(dict.fromkeys(found))


def merge_history(existing: bytes, incoming: bytes) -> bytes:
    lines = list(dict.fromkeys(
        ln for ln in (existing + b"\n" + incoming).decode("utf-8", "replace").splitlines() if ln.strip()
    ))

    def ts(line: str) -> float:
        try:
            return float(json.loads(line).get("timestamp") or 0)
        except (ValueError, TypeError, AttributeError):
            return 0.0
    lines.sort(key=ts)
    return ("\n".join(lines) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- export


def cmd_export(args) -> int:
    claude_dir = Path(args.claude_dir).expanduser() if args.claude_dir else default_claude_dir()
    config_file = Path(args.config_file).expanduser() if args.config_file else default_config_file(claude_dir)
    if not claude_dir.is_dir():
        log(f"error: {claude_dir} does not exist")
        return 1
    config = load_json(config_file, {})
    out = Path(args.output or f"claude-state-{socket.gethostname().split('.')[0]}-"
                              f"{dt.datetime.now():%Y%m%d-%H%M%S}.tar.gz").expanduser()

    log(f"Resolving project paths in {claude_dir / 'projects'} ...")
    projects = resolve_projects(claude_dir, config)
    keep = None
    if args.project:
        roots = [_norm(os.path.abspath(os.path.expanduser(p))) for p in args.project]
        projects = [p for p in projects if _selected(p, roots)]
        if not projects:
            log(f"error: no sessions found for {', '.join(roots)}")
            return 1
        keep = ProjectFilter(claude_dir, projects, roots)
        config = _filter_config(config, roots)
    redactor = Redactor() if args.redact else None
    if redactor:
        config = redactor.config(config)
    manifest = {
        "format": FORMAT_VERSION,
        "created": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": {
            "home": str(Path.home()),
            "user": getpass.getuser(),
            "host": socket.gethostname(),
            "platform": platform.system().lower(),
            "claude_dir": str(claude_dir),
            "config_file": str(config_file),
        },
        "projects": projects,
        "redacted": bool(redactor),
    }

    skipped = []
    files = list(collect_files(claude_dir, args, skipped, keep))

    out.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w:gz") as tar:
        # manifest first, so import can read it without scanning the whole archive
        _add_bytes(tar, "manifest.json", json.dumps(manifest, indent=2).encode())
        _add_bytes(tar, "claude.json", json.dumps(sanitize_config(config), indent=2).encode())
        for f, rel in files:
            try:
                info = tar.gettarinfo(str(f), arcname=f"claude/{rel}")
                if info.islnk():  # store hard links as plain copies
                    info.type, info.linkname, info.size = tarfile.REGTYPE, "", f.stat().st_size
                data = None
                if keep and rel == "history.jsonl":
                    data = keep.history(f.read_bytes())
                if redactor:
                    data = redactor.bytes(rel, f.read_bytes() if data is None else data)
                if data is None:
                    with f.open("rb") as src:
                        tar.addfile(info, src)
                else:
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
                total += info.size
            except (OSError, tarfile.TarError) as e:
                log(f"  warning: skipped {rel}: {e}")

    unresolved = [p for p in projects if not p["cwd"]]
    log(f"\nWrote {out} ({out.stat().st_size / 1e6:.1f} MB compressed, {total / 1e6:.1f} MB raw)")
    log(f"  projects: {len(projects)}  sessions: {sum(p['sessions'] for p in projects)}  files: {len(files)}")
    log(f"  excluded: {', '.join(skipped) or '-'}")
    if unresolved:
        log(f"  {len(unresolved)} project dir(s) have no recoverable path; they are remapped by prefix on import:")
        for p in unresolved:
            log(f"    {p['slug']}")
    if redactor:
        found = ", ".join(f"{n} {k}" for k, n in sorted(redactor.counts.items())) or "nothing"
        log(f"  redacted: {found}")
        log("\nRedaction is pattern-based and best-effort. The archive still holds every transcript. "
            "Keep it private.")
    else:
        log("\nThe archive holds every transcript and any API keys in MCP/settings env. Keep it private "
            "(or re-export with --redact).")
    _warn_cleanup(load_json(claude_dir / "settings.json", {}), "this machine",
                  fix="set it in ~/.claude/settings.json now so nothing is pruned before you migrate.")
    return 0


def _under(path: str, roots: list[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def _selected(project: dict, roots: list[str]) -> bool:
    if project["cwd"]:
        return _under(project["cwd"], roots)
    return any(project["slug"] == slugify(r) or project["slug"].startswith(slugify(r) + "-") for r in roots)


class ProjectFilter:
    """Limits an export to some projects: their session dirs, the per-session
    state keyed by session id, and their history.jsonl lines."""

    PER_SESSION = ("file-history", "tasks", "todos")

    def __init__(self, claude_dir: Path, projects: list[dict], roots: list[str]):
        self.roots = roots
        self.slugs = {p["slug"] for p in projects}
        self.sessions = set()
        for slug in self.slugs:
            for entry in (claude_dir / "projects" / slug).iterdir():
                self.sessions.add(entry.name[:-6] if entry.name.endswith(".jsonl") else entry.name)

    def __call__(self, rel: str) -> bool:
        parts = rel.split("/")
        if len(parts) < 2:
            return True
        if parts[0] == "projects":
            return parts[1] in self.slugs
        if parts[0] in self.PER_SESSION:
            return parts[1].split("-agent-")[0] in self.sessions  # todos: <session>-agent-<id>.json
        return True

    def history(self, data: bytes) -> bytes:
        out = []
        for ln in data.decode("utf-8", "replace").splitlines():
            try:
                project = json.loads(ln).get("project")
            except (ValueError, AttributeError):
                continue
            if isinstance(project, str) and _under(project, self.roots):
                out.append(ln + "\n")
        return "".join(out).encode("utf-8")


def _filter_config(config: dict, roots: list[str]) -> dict:
    config = dict(config)
    config["projects"] = {k: v for k, v in (config.get("projects") or {}).items() if _under(k, roots)}
    repos = {}
    for repo, paths in (config.get("githubRepoPaths") or {}).items():
        kept = [p for p in paths if isinstance(p, str) and _under(p, roots)]
        if kept:
            repos[repo] = kept
    config["githubRepoPaths"] = repos
    return config


def collect_files(claude_dir: Path, args, skipped: list[str], keep=None):
    """Yield (path on disk, path in archive) for everything to export.

    Symlinks are stored as the content they point to: links into e.g. ~/.agents
    (where some skill installers put skills) would dangle on the new machine.
    """
    def visit(path: Path, rel: str, stack: frozenset):
        if keep and not keep(rel):
            return
        if path.is_symlink():
            if not path.exists():
                log(f"  warning: broken symlink skipped: {rel}")
                return
            path = path.resolve()
        if path.is_dir():
            real = path.resolve()
            if real in stack:  # symlink cycle
                return
            for child in sorted(path.iterdir()):
                yield from visit(child, f"{rel}/{child.name}", stack | {real})
        elif path.is_file():
            yield path, rel

    for entry in sorted(claude_dir.iterdir()):
        if excluded(entry.name, args):
            skipped.append(entry.name)
            continue
        yield from visit(entry, entry.name, frozenset())


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mtime, info.mode = len(data), int(time.time()), 0o600
    tar.addfile(info, io.BytesIO(data))


def _warn_cleanup(settings: dict, where: str, oldest_days: float | None = None,
                  fix: str = "re-run import with --cleanup-days 3650, or set it in ~/.claude/settings.json.") -> None:
    days = settings.get("cleanupPeriodDays")
    if days is not None and (oldest_days is None or days > oldest_days):
        return
    msg = f"cleanupPeriodDays is {'unset (default 30)' if days is None else days} on {where}"
    if oldest_days is not None:
        msg += f"; the oldest imported transcript is {oldest_days:.0f} days old"
    log(f"\nWARNING: {msg}. Claude Code deletes older transcripts at startup.")
    log(f"  Fix: {fix}")


# --------------------------------------------------------------------------- import


def _read_manifest(tar: tarfile.TarFile) -> dict:
    member = tar.next()
    if member is None or member.name != "manifest.json":
        raise SystemExit("error: not a claude-migrate archive (manifest.json missing)")
    manifest = json.load(tar.extractfile(member))
    if manifest.get("format", 0) > FORMAT_VERSION:
        raise SystemExit("error: archive was written by a newer claude-migrate")
    return manifest


def cmd_inspect(args) -> int:
    with tarfile.open(args.archive, "r:*") as tar:
        m = _read_manifest(tar)
    src = m["source"]
    print(f"created  {m['created']}\nsource   {src['user']}@{src['host']} ({src['platform']})"
          f"\nhome     {src['home']}\nclaude   {src['claude_dir']}\n")
    print(f"{'sessions':>8}  project")
    for p in m["projects"]:
        print(f"{p['sessions']:>8}  {p['cwd'] or '(unresolved) ' + p['slug']}")
    return 0


def _parse_maps(items: list[str], src_home: str, target_home: str) -> dict[str, str]:
    out = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"error: --map expects OLD=NEW, got {item!r}")
        old, new = item.split("=", 1)
        if old.startswith("~"):
            old = src_home + old[1:]  # ~ on the left means the *source* home
        elif src_home != target_home and (old == target_home or old.startswith(target_home + "/")):
            # an unquoted ~/x was expanded by the shell to this machine's home
            old = src_home + old[len(target_home):]
        new = os.path.expanduser(new)
        if not (old.startswith("/") and new.startswith("/")) or _norm(old) == "/":
            raise SystemExit(f"error: --map paths must be absolute and not '/': {item!r}")
        out[_norm(old)] = _norm(os.path.abspath(new))
    return out


def cmd_import(args) -> int:
    claude_dir = Path(args.claude_dir).expanduser() if args.claude_dir else default_claude_dir()
    config_file = Path(args.config_file).expanduser() if args.config_file else default_config_file(claude_dir)
    claude_root = claude_dir.absolute()
    dry = args.dry_run

    pids = live_sessions(claude_dir)
    if pids and not (dry or args.force):
        log(f"error: Claude Code is running (pids {', '.join(map(str, pids))}). It rewrites "
            f"{config_file.name} on exit and would undo the import. Quit it, or pass --force.")
        return 1

    tar = tarfile.open(args.archive, "r:*")
    manifest = _read_manifest(tar)
    src = manifest["source"]

    # Path mappings: explicit --map entries win over the automatic home/claude-dir ones.
    target_home = _norm(os.path.expanduser(args.target_home) if args.target_home else str(Path.home()))
    path_map = {src["home"]: target_home}
    auto = Rewriter(path_map)
    if auto.map_path(src["claude_dir"]) != str(claude_root):
        path_map[src["claude_dir"]] = str(claude_root)
    path_map.update(_parse_maps(args.map, src["home"], target_home))
    paths = Rewriter(path_map)

    slug_map, plan = {}, []
    for p in manifest["projects"]:
        new_cwd = paths.map_path(p["cwd"]) if p["cwd"] else None
        new_slug = slugify(new_cwd) if new_cwd else slug_fallback(p["slug"], paths)
        slug_map[p["slug"]] = new_slug
        plan.append((p, new_cwd, new_slug))
    rw = Rewriter(path_map, slug_map)

    log("Path mappings:" if rw.path_map else "Path mappings: none (same paths on both machines)")
    for old, new in sorted(rw.path_map.items(), key=lambda kv: -len(kv[0])):
        log(f"  {old}  ->  {new}")
    log("\nProjects:")
    missing = []
    for p, new_cwd, new_slug in plan:
        label = new_cwd or f"(unresolved) {new_slug}"
        exists = new_cwd is not None and Path(new_cwd).exists()
        if new_cwd and not exists:
            missing.append(new_cwd)
        log(f"  {'ok ' if exists else '-- '} {p['sessions']:>4} sessions  {label}")
        if new_cwd and new_cwd != p["cwd"]:
            log(f"{'':20}(was {p['cwd']})")
    if missing:
        log(f"\n{len(missing)} project path(s) marked -- do not exist here. Their sessions are restored, but "
            "`claude --resume` only lists them when run from that exact path: clone/move the repo there, "
            "or re-run with --map OLD=NEW to point them elsewhere.")

    stats = Counter()
    conflicts, oldest = [], None
    now = time.time()
    backup_dir = claude_dir / "backups" / f"migrate-{dt.datetime.now():%Y%m%d-%H%M%S}"
    imported_config = b"{}"
    incoming_settings = {}

    for m in tar:
        name = m.name
        if name == "claude.json":
            imported_config = tar.extractfile(m).read()
            continue
        if not name.startswith("claude/") or m.isdir():
            continue
        rel = name[len("claude/"):]
        parts = PurePosixPath(rel).parts
        if not parts or rel.startswith("/") or ".." in parts:
            log(f"  warning: unsafe path in archive, skipped: {name}")
            continue
        if parts[0] == "projects" and len(parts) > 1 and parts[1] in slug_map:
            parts = ("projects", slug_map[parts[1]]) + parts[2:]
        dest_rel = "/".join(parts)
        dest = claude_dir / dest_rel

        if not m.isreg():  # export never writes links or devices
            log(f"  warning: skipped non-regular archive entry: {name}")
            continue

        data = rw.bytes(rel, tar.extractfile(m).read())
        if rel == "settings.json":
            incoming_settings = json.loads(data.decode("utf-8") or "{}")
        if rel.endswith(".jsonl") and parts[0] == "projects":
            age = (now - m.mtime) / 86400
            oldest = age if oldest is None else max(oldest, age)

        if dest_rel == "history.jsonl" and dest.exists():
            data = merge_history(dest.read_bytes(), data)
            stats["merged"] += 1
        elif dest.exists() or dest.is_symlink():
            if dest.is_file() and dest.read_bytes() == data:
                stats["identical"] += 1
                continue
            if dest_rel == "settings.json" and not args.overwrite:
                dest = dest.with_name("settings.imported.json")
                log(f"\nNote: {claude_dir / 'settings.json'} already exists; incoming copy written to {dest}"
                    " for you to merge by hand.")
            elif not args.overwrite:
                stats["conflicts"] += 1
                conflicts.append(dest_rel)
                continue
            else:
                stats["overwritten"] += 1
                if not dry:
                    (backup_dir / dest_rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dest), str(backup_dir / dest_rel))
        else:
            stats["new"] += 1
        if dry:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        os.chmod(dest, m.mode & 0o777 or 0o600)
        os.utime(dest, (m.mtime, m.mtime))  # keep mtimes: --resume sorts by them
    tar.close()

    # ~/.claude.json: merge into the target's so its login, machine id and caches survive.
    incoming = json.loads(rw.text(imported_config.decode("utf-8"), json_escape=True))
    current = load_json(config_file, {})
    added = merge_config(current, sanitize_config(incoming), args.overwrite)
    if not dry:
        if config_file.exists():
            shutil.copy2(config_file, f"{config_file}.before-import-{dt.datetime.now():%Y%m%d-%H%M%S}")
        write_atomic(config_file, json.dumps(current, indent=2).encode())

    settings_path = claude_dir / "settings.json"
    settings = load_json(settings_path, {})
    if args.cleanup_days is not None and not dry:
        settings["cleanupPeriodDays"] = args.cleanup_days
        write_atomic(settings_path, json.dumps(settings, indent=2).encode() + b"\n", 0o644)

    log(f"\n{'Would import' if dry else 'Imported'}: {stats['new']} new, {stats['identical']} identical, "
        f"{stats['overwritten']} overwritten, {stats['merged']} merged; "
        f"{len(added)} project entries added to {config_file}")
    if conflicts:
        log(f"Kept {len(conflicts)} existing file(s) that differ from the archive (use --overwrite to replace):")
        for c in conflicts[:10]:
            log(f"  {c}")
        if len(conflicts) > 10:
            log(f"  ... and {len(conflicts) - 10} more")
    if stats["overwritten"] and not dry:
        log(f"Replaced files were moved to {backup_dir}")
    missing = missing_commands(incoming_settings, incoming)
    if missing:
        log("\nThese commands from the imported hooks, status line or MCP servers were not found here:")
        for where, cmd in missing:
            log(f"  {where}: {cmd}")
        log("  Install them, or fix the paths (re-run with --map OLD=NEW if they only moved).")
    if manifest.get("redacted"):
        log("\nThis archive was exported with --redact: re-enter the API keys and tokens marked [REDACTED] "
            "in ~/.claude/settings.json and your MCP server config.")
    if args.cleanup_days is None:
        _warn_cleanup(settings, "this machine", oldest)
    if dry:
        log("\nDry run: nothing was written.")
    else:
        log("\nNext: run `claude` and log in (auth tokens are not migrated; MCP OAuth servers re-prompt), "
            "then `cd <project> && claude --resume`.")
    return 0


# --------------------------------------------------------------------------- remap


def cmd_remap(args) -> int:
    claude_dir = Path(args.claude_dir).expanduser() if args.claude_dir else default_claude_dir()
    config_file = Path(args.config_file).expanduser() if args.config_file else default_config_file(claude_dir)
    old, new = _norm(os.path.abspath(os.path.expanduser(args.old))), _norm(os.path.abspath(os.path.expanduser(args.new)))
    pids = live_sessions(claude_dir)
    if pids and not (args.dry_run or args.force):
        log(f"error: Claude Code is running (pids {', '.join(map(str, pids))}); quit it or pass --force.")
        return 1

    config = load_json(config_file, {})
    paths = Rewriter({old: new})
    moves = {}
    for p in resolve_projects(claude_dir, config):
        cwd = p["cwd"]
        new_slug = slugify(paths.map_path(cwd)) if cwd else slug_fallback(p["slug"], paths)
        if new_slug != p["slug"]:
            moves[p["slug"]] = new_slug
    if not moves:
        log(f"No session directories belong to {old}.")
    rw = Rewriter({old: new}, moves)

    root = claude_dir / "projects"
    for old_slug, new_slug in moves.items():
        log(f"  {old_slug}  ->  {new_slug}")
        if (root / new_slug).exists():
            log(f"error: {root / new_slug} already exists; merge it by hand first.")
            return 1
    if args.dry_run:
        log("\nDry run: nothing was written.")
        return 0

    for old_slug, new_slug in moves.items():
        for f in (root / old_slug).rglob("*"):
            if f.is_file() and not f.is_symlink():
                rel = f.relative_to(claude_dir).as_posix()
                data = f.read_bytes()
                out = rw.bytes(rel, data)
                if out is not data:
                    st = f.stat()
                    f.write_bytes(out)
                    os.utime(f, (st.st_atime, st.st_mtime))
        (root / old_slug).rename(root / new_slug)

    hist = claude_dir / "history.jsonl"
    if hist.exists():
        st = hist.stat()
        hist.write_bytes(rw.bytes("history.jsonl", hist.read_bytes()))
        os.utime(hist, (st.st_atime, st.st_mtime))
    if config_file.exists():
        shutil.copy2(config_file, f"{config_file}.before-remap-{dt.datetime.now():%Y%m%d-%H%M%S}")
        text = config_file.read_text(encoding="utf-8")
        write_atomic(config_file, rw.text(text, json_escape=True).encode())
    log(f"\nRemapped {len(moves)} project dir(s), history and {config_file.name}.")
    return 0


# --------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="claude-migrate", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--claude-dir", help="Claude config dir (default: $CLAUDE_CONFIG_DIR or ~/.claude)")
        p.add_argument("--config-file", help="global config file (default: ~/.claude.json)")

    p = sub.add_parser("export", help="write Claude Code state to an archive")
    p.add_argument("-o", "--output", help="archive path (default: ./claude-state-<host>-<time>.tar.gz)")
    p.add_argument("--project", action="append", metavar="PATH",
                   help="only export sessions for projects at or under PATH (repeatable); "
                        "settings, skills and plugins are still included")
    p.add_argument("--redact", action="store_true",
                   help="replace API keys, tokens and secret env values with [REDACTED] (best-effort)")
    p.add_argument("--include-jobs", action="store_true", help="also export ~/.claude/jobs (can be large)")
    p.add_argument("--exclude", action="append", metavar="GLOB",
                   help="extra top-level entry of ~/.claude to skip (repeatable)")
    common(p)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("inspect", help="list the projects and sessions in an archive")
    p.add_argument("archive")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("import", help="restore an archive on this machine")
    p.add_argument("archive")
    p.add_argument("--map", action="append", metavar="OLD=NEW",
                   help="rewrite a source path prefix to a new location (repeatable; OLD may start with ~ "
                        "for the source home). The source home maps to this home automatically.")
    p.add_argument("--target-home", help="home dir to map the source home to (default: this user's home)")
    p.add_argument("--dry-run", action="store_true", help="show the plan without writing anything")
    p.add_argument("--overwrite", action="store_true",
                   help="replace differing existing files (originals moved to ~/.claude/backups)")
    p.add_argument("--cleanup-days", type=int, metavar="N",
                   help="set cleanupPeriodDays so old transcripts are not pruned at startup")
    p.add_argument("--force", action="store_true", help="import even while Claude Code is running")
    common(p)
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("remap", help="rebind sessions after moving a project on this machine")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="remap even while Claude Code is running")
    common(p)
    p.set_defaults(func=cmd_remap)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
