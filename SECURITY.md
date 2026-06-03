# Security notes

## Leaked OpenAI keys (ACTION REQUIRED: rotate)

The current working tree contains **no** live secrets — `tools/fetch_stock_info.py` and
`stock_analyzer_bot.ipynb` hold only placeholders, and commit `487595c` ("Remove hardcoded API
key") scrubbed the tree. **However, git history still contains three distinct real `sk-proj-`
OpenAI keys** that were committed earlier. Treat all three as compromised and **revoke them at
https://platform.openai.com/ (API keys)**:

| Key prefix (truncated)        | Where it appeared                                   |
|-------------------------------|-----------------------------------------------------|
| `sk-proj-HyLWd4W6xkecfcsd...` | git history                                          |
| `sk-proj-fqMX6eskib0mW2b64...`| git history                                          |
| `sk-proj-rebDNOZD...`         | `stock_analyzer_bot.ipynb` + `tools/fetch_stock_info.py` (removed in `487595c`) |

### Why history is not being rewritten

Per project decision we **document rather than rewrite** history: rewriting SHAs would require a
force-push and break any existing clones, and once the keys are revoked they are dead regardless of
their presence in history. If the repo is later made public and a clean history is required, run
`git filter-repo` to purge these strings and force-push — but rotation is the real fix.

## Secrets handling going forward

All secrets load from a gitignored `.env` via `python-dotenv` / `os.getenv` (see
`trader/config.py` and `.env.example`). No key is ever hardcoded. `users.db` (real user data) is
gitignored and untracked.
