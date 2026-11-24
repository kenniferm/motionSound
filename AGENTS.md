# Development rules

- Keep UI, AI, memory, rendering and storage responsibilities separate.
- Run generated code only in response to an explicit generate, render, or retry action in the UI. The user approved running without prior code review. Preserve code and settings fingerprints.
- Direct Python execution is not a sandbox. Do not claim it is safe isolation.
- Preserve generated sources, settings, artifacts, failures and raw reviews; append instead of overwrite.
- Demo and actual API generation must remain clearly distinguishable; never silently fall back.
- Do not persist API keys or pass them to generated subprocesses.
- Run `.venv/bin/python -m pytest -q` after behavioral changes.
- Never create or open a pull request unless explicitly requested in the current conversation.

- Read `CODEX_START_HERE.md`, `docs/ARCHITECTURE.md` and `docs/STATUS.md` before changing product behavior.
- Distinguish approved requirements, provisional choices, hypotheses and actual test results; update `docs/STATUS.md` after changes.
