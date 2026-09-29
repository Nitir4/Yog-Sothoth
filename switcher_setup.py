"""Installation guidance shared by the CLI and desktop setup screen."""

INSTALL_GUIDES = {
    "codex": "https://learn.chatgpt.com/docs/codex/cli",
    "claude": "https://code.claude.com/docs/en/setup",
    "agy": "https://www.antigravity.google/docs/cli/install/",
}


def setup_hint(state) -> str:
    if not state.installed:
        return f"Install {state.label}'s native CLI, then refresh to detect it."
    if not state.available:
        return state.error
    if not state.accounts:
        return "CLI detected. Add an account and complete its normal sign-in."
    return f"Ready. {len(state.accounts)} saved account(s)."
