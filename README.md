# Yog-Sothoth

Manage saved accounts and local conversation history across Codex CLI,
Claude Code, and Google Antigravity CLI. Yog-Sothoth provides a unified CLI
and an optional Linux desktop interface. Each account uses an isolated native
data directory; history can optionally be shared between accounts of the same
tool.

Account changes affect future launches. Running sessions keep their original
account. Each coding tool has its own saved default.

## Requirements and platform support

Install Python **3.10+** and the coding CLI you want to use. The command-line
interface uses only Python's standard library. The coding tools remain separate
installations and handle their own sign-in and subscriptions.

| Platform | CLI | Desktop interface |
| --- | --- | --- |
| Linux | Primary development and test platform | GTK 4.10+ |
| macOS | Codex and Claude adapters are designed for macOS; native validation is pending | Not supported |
| Windows | Native compatibility and PowerShell integration are pending | Not supported |

The Antigravity adapter supports only inspected Linux executable builds. Check
compatibility with `./agy-switch doctor` before adding an account.

There are currently **no AppImage, Windows executable, or macOS application
bundles**. Run the source checkout using the instructions below. Cross-platform
CLI support and Linux AppImage packaging are planned work, not released features.

Native Codex behavior was checked against CLI **0.160.0**. Claude workflows are
tested with a synthetic executable; real Claude sign-in and resume still need
validation. See [Verification](#verification) for the test scope.

## Get started

Clone the repository and enter it:

```sh
git clone https://github.com/Nitir4/Yog-Sothoth.git
cd Yog-Sothoth
```

Add and select your own Codex accounts:

```sh
./switcher codex add personal
./switcher codex add work
./switcher codex list
./switcher codex use work
./switcher codex run
```

`add` opens the coding tool's native sign-in flow. Choose the account to save in
your browser. The first successfully saved account becomes the default; adding
another account preserves the existing selection. To complete an interrupted
login, run `./switcher codex login work`.

The individual `codex-switch`, `claude-switch`, and `agy-switch` commands expose
the same adapter commands. For example, `./codex-switch use work` is equivalent
to `./switcher codex use work`. You can also invoke scripts with `python3`:

```sh
python3 switcher status
python3 codex-switch --help
```

### Commands

| Command | Purpose |
| --- | --- |
| `./switcher status` | Show stores, saved accounts, and local login-cache status |
| `./switcher TOOL add NAME` | Save an account through native sign-in |
| `./switcher TOOL login NAME` | Complete or renew a saved account's login |
| `./switcher TOOL use NAME` | Save the default for future wrapper launches |
| `./switcher TOOL list` | List accounts and locally cached login status |
| `./switcher TOOL current` | Print the saved default |
| `./switcher TOOL run -- [ARGS...]` | Launch the tool with its default account |
| `./switcher TOOL run --account NAME -- [ARGS...]` | Use an account once without changing the default |
| `./switcher TOOL shell-init zsh` | Print shell integration; `bash` is also supported |
| `./switcher TOOL history` | Inspect per-account or shared history storage |
| `./switcher TOOL history share --source-home PATH` | Import and share that tool's local history |
| `./switcher history` | Browse conversations across tools |
| `./switcher resume TOOL ID --account NAME` | Resume a saved conversation in the current terminal |
| `./switcher gui` | Open the Linux desktop manager |

Replace `TOOL` with `codex`, `claude`, or `agy`. Pass native CLI arguments after
`--`; arguments containing spaces must be quoted normally:

```sh
./switcher codex run --account work -- exec --json "Review this code"
./switcher claude run --account work -- -p "Explain this repository"
./switcher agy run --account work -- -p "Explain this repository"
```

Cached login status does not verify token revocation, remaining quota, or server
access. The native tools validate and refresh their credentials during use.
Yog-Sothoth does not pool subscriptions or automatically move running tasks
between accounts.

## Linux desktop interface

Install PyGObject and **GTK 4.10+**, using your distribution's system Python:

```sh
# Arch Linux
sudo pacman -S python-gobject gtk4

# Ubuntu 24.04+ or Debian with GTK 4.10+
sudo apt install python3-gi gir1.2-gtk-4.0

# Fedora
sudo dnf install python3-gobject gtk4
```

See [PyGObject's installation guide](https://pygobject.gnome.org/getting_started.html)
for other distributions. Then launch from a graphical desktop terminal:

```sh
python3 switcher gui
```

The desktop manager provides account creation and sign-in, automatic default
selection, a project-directory chooser, conversation filtering, native terminal
launches, and shared-history setup.

Choosing an account in the dropdown checks its login and saves the default for
future wrapper launches, including terminals using shell integration. Wait for
the switch confirmation. The account card displays the saved default; use
**Switch account** to retry a failed switch. Opening or refreshing the window
only loads the current selection.

**Launch in terminal** uses the chosen account and project directory. **Resume**
uses the conversation's saved project directory; select **Resume in the chosen
project directory** if the original project was moved. Sign-in and history
migration run in a terminal. Click **Refresh** after they finish.

### Application-menu launcher

Install a launcher configured for your checkout and Python interpreter:

```sh
python3 scripts/install_desktop_launcher.py
```

It installs `coding-switcher.desktop` under `$XDG_DATA_HOME/applications`, or
`~/.local/share/applications` by default. Open **Yog-Sothoth** from the app menu,
or run:

```sh
gtk-launch coding-switcher
```

The checked-in `.desktop` file is a template; use the installer instead of
copying it directly. Rerun the installer if you move the checkout or change the
Python interpreter. Keep the checkout available: the launcher runs its source
files, so reopening the window loads subsequent code updates.

For a desktop shortcut on desktops that support them:

```sh
python3 scripts/install_desktop_launcher.py \
  --output "$(xdg-user-dir DESKTOP)/coding-switcher.desktop"
chmod +x "$(xdg-user-dir DESKTOP)/coding-switcher.desktop"
```

Your desktop may require you to allow launching that shortcut.

### Terminal and executable detection

The desktop manager tries Kitty, GNOME Terminal, Konsole, Xfce Terminal,
WezTerm, and xterm, in that order. Override detection with a command prefix that
accepts a program and its arguments:

```sh
SWITCHER_TERMINAL='kitty --title "Coding session" --' python3 switcher gui
```

Terminal launches use argument lists without shell evaluation. If a tool is
shown as **Unavailable**, check its installation and `PATH`. The adapters also
look in `~/.local/bin` if the executable is missing from `PATH`. An explicit
executable override takes precedence. Restart the manager after changing its
environment.

## Keep using the native command names

Shell integration defines functions that read the saved default on each launch.
From the checkout, enable the tools you have installed:

```sh
eval "$(./codex-switch shell-init zsh)"
eval "$(./claude-switch shell-init zsh)"
eval "$(./agy-switch shell-init zsh)"
```

Use `bash` instead of `zsh` for Bash. Antigravity integration requires a
compatible executable. Afterwards:

```sh
./switcher codex use work
codex
codex resume --last
```

To make integration permanent, put the relevant `eval` lines in your shell's
startup file, replacing `./codex-switch` and the other wrapper paths with their
absolute checkout paths. The wrappers do not edit shell configuration.

Use `unfunction codex` in zsh or `unset -f codex` in Bash to remove a function;
the same applies to `claude` and `agy`. Without these functions, bare native
commands use their own original login rather than the wrapper's saved default.
PowerShell integration is not implemented.

## Conversation browsing and shared history

Browse and filter local conversations:

```sh
./switcher history
./switcher history --tool codex --project my-project --query authentication
./switcher history --json --limit 1000
./switcher resume codex THREAD_ID --account personal
./switcher resume claude SESSION_ID --account work --project /path/to/project
./switcher resume agy CONVERSATION_ID --account work
```

Filters are case-insensitive substring matches. Results are sorted by recency;
the default limit is 500 and the maximum is 10,000. `status --json` reports
account names, storage locations, and local cache presence without printing
credential contents. SQLite browsing reads temporary snapshots, including
committed WAL data, without opening the original databases writable.

Resume uses the saved project directory unless `--project` overrides it. It
launches the requested account without changing the default. Per-account
conversations can be resumed only by an account containing them. Shared history
allows another saved account of the same coding tool to discover the local
conversation; server-side account access still applies. Conversations cannot
be resumed in a different coding tool.

### Enable sharing

Close all running sessions for the tool before migrating its history:

```sh
./switcher codex history share --source-home ~/.codex
./switcher claude history share --source-home ~/.claude
./switcher agy history share --source-home ~/.gemini
```

Run only the commands for tools you have configured. Each imports the source
home and every saved account in that tool's store into its `history/` directory.
Original native homes stay unchanged. Existing account history is backed up
under `.history-backup/`; credentials and settings stay account-specific.
Subsequent wrapper launches use shared history, and new accounts join on launch.

Migration checks conflicting transcripts and incompatible database schemas
before moving account history. For a schema mismatch, use the same native CLI
version in each home before retrying. Repeating a completed migration reports
that sharing is already enabled; it does not import later changes from an
independently used original home. Native retention settings still apply.

- **Codex:** shares sessions, archived sessions, prompt history, session indexes,
  and supported SQLite conversation databases, including committed WAL data.
  Launches use the shared SQLite directory. Use `codex resume --all` to include
  other project directories, or resume a known thread ID.
- **Claude Code:** shares project transcripts and indexes, prompt history, file
  checkpoints, plans, todos, and project auto memory stored in `projects/`.
  Transcript paths in merged indexes are updated. Use `claude --resume` or pass
  a session ID directly. See the [session documentation](https://code.claude.com/docs/en/sessions).
- **Antigravity:** shares conversation databases, summaries, artifacts,
  annotations, and prompt history. The wrapper synchronizes the atomically
  replaced resume-cache files before launches, retaining account-local default
  project IDs and onboarding state. Use `/resume`, `agy -c`, or
  `agy --conversation ID`. See the [resume documentation](https://www.antigravity.google/docs/cli/commands/resume/).

Sharing covers local storage only, including Claude project memory. It does not
merge ChatGPT web history, transfer cloud conversations, or grant access to
another account's server-side data.

## Account-specific behavior

### Codex CLI

Use device-code sign-in where supported:

```sh
./codex-switch add personal --device-auth
./codex-switch login personal --device-auth
```

Import an existing file-based ChatGPT login:

```sh
./codex-switch add personal --import-current --source-home ~/.codex
```

Import copies `auth.json` without logging out the original home. Keyring-only
logins require a fresh native login. Imported refresh tokens can diverge from
the source; avoid concurrently using two copies of the same imported login.

By default, `add` copies `config.toml` and `*.config.toml` from the source home.
Use `--no-copy-config` for fresh settings. Skills, plugins, hooks, MCP logins,
and conversation history are not copied. Relative paths in copied settings may
need adjustment. Administrator login restrictions still apply.

The adapter forces file-based credentials and controls SQLite storage. It
removes inherited `OPENAI_API_KEY`, `CODEX_ACCESS_TOKEN`, and `CODEX_SQLITE_HOME`
from child launches. It uses `--no-daemon` when supported to keep authentication
local to the chosen home. Remote-server authentication and overrides of managed
credential/SQLite settings are refused.

Logging out through the wrapper affects only that account's login:

```sh
./codex-switch run --account work -- logout
```

See OpenAI's [authentication guide](https://learn.chatgpt.com/docs/auth) and
[configuration guide](https://learn.chatgpt.com/docs/config-file/config-advanced).
Yog-Sothoth is an independent wrapper, not an official account-switching feature.

### Claude Code

```sh
./claude-switch add personal
./claude-switch add work
./claude-switch use work
./claude-switch run
```

Each account uses a separate `CLAUDE_CONFIG_DIR` and starts with fresh settings.
The adapter does not import credentials, settings, history, or plugins.
Sign-in uses `claude auth login`; status must report a Claude subscription login
and, when exposed, the selected configuration directory.

Credentials remain in Claude's native storage: a directory-specific macOS
Keychain entry, or a plaintext `.credentials.json` file on Linux and macOS
fallback. Inherited API credentials, OAuth overrides, provider selectors, and
gateway overrides are removed. Explicit settings or arguments may still change
authentication; check Claude's `/status` after configuring a profile.

Local `auth` commands, `--help`, and `--version` are available for recovery even
when a profile has no active subscription login:

```sh
./claude-switch run --account work -- auth login
./claude-switch run --account work -- auth logout
```

See Claude's [environment variables](https://code.claude.com/docs/en/env-vars),
[authentication](https://code.claude.com/docs/en/authentication), and
[CLI reference](https://code.claude.com/docs/en/cli-reference).

### Google Antigravity CLI

```sh
./agy-switch doctor
./agy-switch add personal
./agy-switch add work
./agy-switch use work
./agy-switch run
```

`add` opens the native AGY session. Complete its URL/code sign-in and type
`/exit` to return to the wrapper. Existing file-backed OAuth logins can be
imported with `--import-current --source-home /path/to/.gemini`; keyring-only
logins require a fresh sign-in.

The adapter uses native `--gemini_dir`, `--app_data_dir`, and
`--use_host_auth=false` flags. A child-only `SSH_CONNECTION` value selects AGY's
manual sign-in and file-backed OAuth storage. The caller's `HOME` and SSH
variables stay unchanged. Profiles have fresh settings; credentials, plugins,
skills, and project state are not copied from the original home.

These directory flags are internal. Launches require Linux and an executable
fingerprint listed in `agy_switcher.py`. `doctor` reports the executable path,
fingerprint, and compatibility result. Unsupported builds are refused before
account creation or launch; `list` and `current` remain available.

Inherited API keys, host authentication, and known remote routing overrides are
removed. Wrapper launches disable native automatic updates. Run the native AGY
command directly for updates, installation, and daemon management. A new build
requires inspection of account storage and token behavior before its fingerprint
can be accepted. Explicit profile settings can still change providers.

See Google's [authentication documentation](https://www.antigravity.google/docs/cli/install/).
This adapter provides local Google OAuth account profiles; it is not an
API-key selector or an official Google account-switching interface.

## Storage, configuration, and credential handling

Default stores:

| Tool | Store | Native account data |
| --- | --- | --- |
| Codex | `~/.local/share/codex-switcher/` | `accounts/NAME/` |
| Claude Code | `~/.local/share/claude-switcher/` | `accounts/NAME/` |
| Antigravity | `~/.local/share/agy-switcher/` | `accounts/NAME/antigravity-cli/` |

Each store contains `selected.json`, account directories, and an optional shared
`history/` directory. Storage names and wrapper commands remain unchanged by
the Yog-Sothoth project name.

| Environment variable | Purpose |
| --- | --- |
| `CODEX_SWITCHER_HOME` | Codex account-store location |
| `CODEX_SWITCHER_CODEX` | Real Codex executable path or command name |
| `CLAUDE_SWITCHER_HOME` | Claude account-store location |
| `CLAUDE_SWITCHER_CLAUDE` | Real Claude executable path or command name |
| `AGY_SWITCHER_HOME` | Antigravity account-store location |
| `AGY_SWITCHER_AGY` | Real Antigravity executable path or command name |
| `SWITCHER_TERMINAL` | Desktop terminal command prefix |

The unified command also recognizes an existing checkout-local `.agy-switcher/`
when `AGY_SWITCHER_HOME` is unset and the default AGY store has no accounts.
An explicit store override always wins. Use `./switcher status` to inspect the
chosen locations. Keep the same overrides for migration and regular launches.

**File-backed login tokens are plaintext, not encrypted.** On Linux/macOS,
storage directories must be owned by the current user with mode `700`.
Wrapper-written files use mode `600`, and native child processes inherit a
restrictive umask. The wrappers suppress native login-status output and never
print cached credentials.

Keep account stores, native homes, tokens, and conversation data outside the
repository. Do not commit them or include them in release bundles. The ignore
rules exclude common credential caches, account stores, logs, databases,
assistant workspace files, and local build output. These rules do not replace
reviewing the exact files being published.

Use your own accounts and follow each provider's subscription and account-access
rules. See OpenAI's [account-sharing policy](https://help.openai.com/en/articles/10471989-openai-account-sharing-policy).

## Verification

Run the test suite:

```sh
python3 -m unittest discover -s tests -v
```

Tests use synthetic executables, fake credentials, and temporary homes. They do
not use real account tokens or make inference requests. Coverage includes:

- Account isolation, login failures, default selection, token refresh persistence,
  native argument forwarding, and shell integration.
- Shared-history migration, conflict handling, backups, SQLite WAL snapshots,
  session indexes, and Antigravity resume-cache synchronization.
- Unified routing, conversation filters, account-scoped resume, terminal argument
  preservation, executable discovery, and missing-display errors.
- Dropdown switching for every tool, default persistence, failed validation,
  and protection against accidental switches during startup or refresh.

GTK checks require PyGObject and GTK 4.10+. The real-window test runs only when
an accessible desktop display is available. Native Windows/macOS runs, live
Claude sign-in, and live switching between Antigravity accounts have not been
verified. Native Antigravity data paths, selected executable fingerprints,
MCP-profile isolation, and history migration were inspected on Linux.
