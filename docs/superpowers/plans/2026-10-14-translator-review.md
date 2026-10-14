# Translator review implementation plan

> **For agentic workers:** Implement each owned area with failing regressions before fixes. Parent integrates and reviews the complete diff.

**Goal:** Review the translator's main flows, migrate retired MiMo models, and repair reproducible defects without unrelated redesign.

**Architecture:** Preserve GUI/core/provider interfaces and existing translation output strategies. Use synthetic fixtures and HTTP doubles; keep project changes local until tests and packaging checks pass.

**Tech Stack:** Python, Tkinter, requests, OpenCC, SQLite, PyInstaller, pytest.

**Spec:** User delegation in this task, including the 2026-10-14 authorization to push an independent branch and open a draft PR.

## Global constraints

- Only Minecraft-Modpack-Translator; never PA_ZH_TW.
- Preserve pre-existing changes. This workspace is a new clean clone of c15531f20d42ba31da841772275ec999d933e5e6.
- No real user content, keys, paid API calls, or blanket dependency upgrades.
- No force push, merge, or Release publication.

## Review focus

- Persisted MiMo flash selection must map to flash, rather than silently selecting pro.
- Truncated/malformed provider responses must not create valid-looking wrong translations.
- Failed or cancelled work must retain complete files and report failure to CLI callers.
- Settings/cache writes must preserve previous data on failure; Windows key storage must not silently downgrade protection.
- Markdown, SNBT, language JSON, book tokens, and ZIP/JAR paths must remain structurally intact.

## Tasks

1. Establish clean Git and corrected test baseline; log all original failures in ../review-evidence/baseline.txt.
2. Parent: test and implement MiMo GUI/config/request migration in gui/main_window.py and translator_providers.py; reproduce provider robustness defects and verify minimal fixes.
3. Batch reviewer: inspect core/batch_translation.py, core/translation_flow.py, core/verification.py; repair reproduced errors, expose completion status, and align stale tests with documented GTX routing.
4. Format reviewer: inspect scans, parsers, masks, archives, and translation_packager.py; preserve code/format and complete source on cancellation, expose analysis status.
5. Settings reviewer: inspect config, cache, CLI, requirements, spec; add atomic settings writer, native Windows encryption, safe persistence, and CLI failure propagation as justified by tests.
6. Parent: review integrated changes, run all tests and syntax checks, build EXE if dependencies allow, smoke-test synthetic GUI/CLI workflows, and document limitations.
7. Parent: commit/push an independent branch, create and attach draft PR, verify remote SHA and available CI; report any blockers exactly.

## Evidence

- Initial default-temp test run was invalid due sandbox temporary-directory write errors.
- Corrected baseline with workspace TEMP/TMP and --basetemp: 319 passed, 13 failed, 3 subtests passed.
- Existing 13 failures are tracked by name in the baseline log; policy changes must not be reverted merely to satisfy outdated assertions.
