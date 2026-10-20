# claude-migrate

Move your Claude Code state (session transcripts, memory, history, settings, skills,
plugins, per-project trust and MCP config) to another machine. Paths are rewritten
so that `claude --resume` works even when the home directory or project locations
differ.

One file, Python 3.8+ standard library only.

> **Status:** tested on macOS with Claude Code 2.1.30x, including a restore into a
> Linux-style `/home/<user>` layout. Not yet tested on a real Linux machine, and
> Windows is not supported. The tool depends on Claude Code's undocumented on-disk
> format, so a future release could break it. `import --dry-run` first.

## Usage

On the old machine:

```sh
./claude_migrate.py export -o claude-state.tar.gz
```

Copy the archive over, then on the new machine **quit every Claude Code session** and run:

```sh
./claude_migrate.py inspect claude-state.tar.gz            # what's inside
./claude_migrate.py import claude-state.tar.gz --dry-run   # show the plan
./claude_migrate.py import claude-state.tar.gz --cleanup-days 3650
```

Then run `claude`, log in, `cd` into a project and `claude --resume`.

### Projects in a different place

The old home maps to the new home automatically (`/Users/alice` → `/home/alice`).
For anything else, add `--map OLD=NEW` (repeatable, longest prefix wins; `~` on the
left means the *old* home):

```sh
./claude_migrate.py import claude-state.tar.gz \
  --map ~/projects=~/code \
  --map /Volumes/data/api=~/src/api
```

The dry run marks each project `ok` (the path exists here) or `--` (it doesn't).
Sessions for `--` projects are still restored; they show up in `--resume` once the
repo is at that path.

After importing, the tool lists hooks, the status line and MCP servers whose
commands don't exist on the new machine, such as a hook that calls
`/opt/homebrew/bin/...` after a Mac → Linux move.

### Only some projects

```sh
./claude_migrate.py export --project ~/projects/my-app -o my-app.tar.gz
```

`--project` (repeatable) exports only the sessions, memory, history and per-session
state of projects at or under that path. Settings, skills and plugins are still
included.

### Sharing an archive, or storing it somewhere less private

```sh
./claude_migrate.py export --redact -o claude-state.tar.gz
```

`--redact` replaces credentials with `[REDACTED]`:

- values of env vars and headers with names like `*KEY*`, `*TOKEN*`, `*SECRET*` or
  `Authorization`, in `settings.json` and MCP server configs
- API keys in URL query parameters (`?apiKey=...`)
- well-known token formats anywhere in text: Anthropic, OpenAI, GitHub, AWS, Slack,
  Google, Stripe, JWTs, private keys and `Bearer` tokens

It is pattern-based and best-effort. Credentials in other formats stay in the
transcripts, so still treat the archive as private. You'll need to re-enter the
redacted values after importing.

### Moving a project later, on the same machine

```sh
./claude_migrate.py remap ~/projects/my-app ~/code/my-app
```

## How path rewriting works

Claude Code stores transcripts in `~/.claude/projects/<slug>/`, where `<slug>` is the
project path with **every non-alphanumeric character** replaced by `-` (so
`/Users/a/my-app.v2` → `-Users-a-my-app-v2`). Slugs longer than 200
characters are truncated and given a hash suffix. This matches the encoder in the
Claude Code binary (v2.1.30x).

Because slugs are lossy (`/a/b.c` and `/a/b-c` collide), export reads the real path
of each project from the `cwd` fields inside its transcripts, and records it in the
archive manifest. On import:

1. Each project path is mapped (`--map`, then home), and its directory is renamed to
   the slug of the new path.
2. Every text file is rewritten (transcripts, subagent transcripts, tool results,
   memory files, `history.jsonl`, `settings.json`, plugin manifests, plans, todos).
   This covers `cwd`, `file_path`, paths inside shell commands and messages, and
   `~/.claude/projects/<old-slug>` references, which become the new slug.
3. Paths only match on path boundaries: mapping `/Users/a/app` leaves
   `/Users/a/app-rpc` and `/Users/a/app.rpc` alone, but a bare `/Users/a` (no
   trailing slash) is still rewritten. All mappings are applied in one pass, so
   one mapping's output is never mapped again.
4. `~/.claude.json` is merged into the new machine's copy, not copied over it. The
   `projects` map (trust, allowed tools, project MCP servers) is re-keyed to the new
   paths. User MCP servers and `githubRepoPaths` are merged. Login, `userID`,
   `machineID` and cached server data on the new machine are kept. Existing values
   win unless you pass `--overwrite`.

These are copied byte for byte, never rewritten: `file-history/` (backups of your
files, used by rewind), `paste-cache/`, and plugin code under `plugins/cache` and
`plugins/marketplaces`.

## What is exported

Everything in `~/.claude` except machine-bound or runtime data: `debug`, `statsig`,
`telemetry`, `cache`, `backups`, `ide`, `daemon*`, `sessions`, `session-env`,
`shell-snapshots`, `policy-limits*`, `remote-settings*`, `.credentials.json`, lock
files. `jobs/` can be large, so it is only included with `--include-jobs`. Skip more
with `--exclude GLOB`.

Symlinks are stored as the files they point to. For example, skills installed into
`~/.agents/skills` and linked from `~/.claude/skills` arrive as real files.

From `~/.claude.json`, the account (`oauthAccount`), machine IDs and caches are
removed.

## Safety

- Import refuses to run while Claude Code is running, because Claude Code writes
  `~/.claude.json` on exit and would overwrite the merge. Override with `--force`.
- Existing files are never overwritten by default. Files that differ are listed and
  kept. With `--overwrite`, the replaced files are moved to
  `~/.claude/backups/migrate-<time>/`. An existing `settings.json` is kept and the
  incoming one is written to `settings.imported.json` for you to merge by hand.
- `~/.claude.json` is backed up to `~/.claude.json.before-import-<time>` (or
  `.before-remap-<time>`).
- Re-running an import is safe: unchanged files are skipped and `history.jsonl` is
  deduplicated.
- **Cleanup:** Claude Code deletes transcripts older than `cleanupPeriodDays`
  (default 30) at startup. Imported files keep their original timestamps, so pass
  `--cleanup-days N` (or set it yourself) before you first run `claude`. Also set it
  on the old machine, so nothing is deleted before you export.
- The archive is created with mode `600`. It contains every transcript, and any API
  keys in MCP server configs or `settings.json` `env` (unless you use `--redact`).
  Treat it like a password file. The included `.gitignore` keeps `*.tar.gz` out of
  git.

## Not migrated

- Auth tokens (macOS Keychain or `~/.claude/.credentials.json`). Log in again.
- MCP OAuth tokens. Those servers ask you to authenticate again.
- The `--continue` "last session" pointer. Use `--resume`.
- System binaries that plugins need (LSP servers, etc.). Install them again.

## Tests

```sh
python3 -m unittest -v test_claude_migrate
```

## License

MIT. See [LICENSE](LICENSE).

## Credits

Based on [claude-code-migrate](https://github.com/emreonal11/claude-code-migrate) by
[@emreonal11](https://github.com/emreonal11) (MIT). Its README explains where Claude Code
keeps its state, how project folders are keyed by path, the 30-day transcript cleanup,
and the steps for migrating and remapping paths. This tool automates those steps and
builds on them.
