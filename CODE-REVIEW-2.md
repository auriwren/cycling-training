# Code Review 2 — cycling-training

## Summary
Focused on config handling, security, and dashboard generation. Main risks are HTML injection in the generated dashboard and credential exposure during uploads or token storage. There are also config loading failures that crash the CLI before any helpful error output.

## Findings

1) **High — XSS risk in dashboard generation**
The dashboard generator injects values from the database directly into HTML without escaping. Titles, notes, and insights can contain HTML or script tags, which would execute when the dashboard is opened. This includes workout titles, descriptions, insights, and any text pulled from DB tables used in replacements.

**Recommendation:** HTML-escape all DB-derived fields before substitution. Only allow trusted markup in explicit fields, and use a templating engine with auto-escaping.

2) **Medium — Fastmail upload exposes credentials in process list**
`generate_dashboard(upload=True)` shells out via `bash -c` and embeds the Fastmail password in the command string. This can leak via `ps`, shell history, or logs.

**Recommendation:** Avoid shell invocation. Use `subprocess.run` with argument list and set `env` for the password, or use `--netrc`/`.netrc` with restricted permissions.

3) **Medium — Config load fails hard on missing or malformed config**
`config.py` raises `FileNotFoundError` or `JSONDecodeError` during import because `CONFIG = get_config()` is evaluated at module import time. This prevents the CLI from printing a helpful error or fallback guidance.

**Recommendation:** Defer config loading until runtime and catch JSON errors with a clear message pointing to `config.example.json`.

4) **Low — Token cache and env updates rely on default file permissions**
OAuth token cache and Strava env updates are written without explicit `chmod`, so file permissions depend on the current umask. On permissive systems, tokens could be world-readable.

**Recommendation:** Set `0o600` on token cache and updated env files after write.

5) **Low — Hardcoded dashboard constants reduce maintainability**
`dashboard_generator.py` hardcodes FTP target, race dates, default FTP, and athlete/coach names. These values should come from the shared config to prevent divergence with CLI outputs.

**Recommendation:** Move these values into `config.json` and read them from the same config module as the CLI.
