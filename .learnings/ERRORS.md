# Errors

Command failures and integration errors.

---

## [ERR-20260626-011] ruff_test_clip_editor_unused_imports

**Logged**: 2026-06-26T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
Expanded Ruff scope surfaced unused imports in `tests/test_clip_editor_agent.py`.

### Error
```
F401 asyncio / AsyncMock / unused constants imported but unused
```

### Context
- Command: `.venv/bin/ruff check ... tests ...`
- Resolution: remove unused imports only; no behavior change.

### Suggested Fix
Keep test imports minimal when adding new unit tests or expanding lint scope.

### Metadata
- Reproducible: yes
- Related Files: tests/test_clip_editor_agent.py

---

## [ERR-20260626-010] pytest_imported_config_from_lightweight_tests

**Logged**: 2026-06-26T00:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: tests

### Summary
A lightweight repository test imported `DurableJobService`, which imported `config` and failed in the local minimal test environment without `pydantic_settings`.

### Error
```
ModuleNotFoundError: No module named 'pydantic_settings'
```

### Context
- Command: `.venv/bin/pytest -q`
- Trigger: adding a unit test for `DurableJobService.create_project_job`.
- The repository tests had intentionally stayed independent of full app config.

### Suggested Fix
Keep low-level persistence tests focused on repository behavior. Cover service wiring through compile checks or higher-level integration tests where full app dependencies are installed.

### Metadata
- Reproducible: yes
- Related Files: tests/test_job_repository.py, services/durable_job_service.py

---

## [ERR-20260626-009] ruff_main_import_order_existing_debt

**Logged**: 2026-06-26T00:00:00+08:00
**Priority**: medium
**Status**: pending
**Area**: backend

### Summary
Adding `main.py` to the ad hoc Ruff scope exposed existing `E402` import-order debt caused by calling `load_dotenv()` before application imports.

### Error
```
E402 Module level import not at top of file
F401 unused imports in main.py
```

### Context
- Command: `.venv/bin/ruff check ... main.py`
- Safe cleanup removed unused imports.
- Import-order cleanup was deferred because it affects environment-loading semantics and should be handled deliberately.

### Suggested Fix
Refactor startup configuration so `.env` loading happens inside config initialization or an explicit bootstrap module, then add `main.py` to the normal CI Ruff scope.

### Metadata
- Reproducible: yes
- Related Files: main.py, config.py, .github/workflows/ci.yml

---

## [ERR-20260626-008] apply_patch_context_mismatch

**Logged**: 2026-06-26T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: backend

### Summary
Initial API auth patch failed because `harness/__init__.py` had additional exports compared with the expected context.

### Error
```
apply_patch verification failed: Failed to find expected lines in harness/__init__.py
```

### Context
- Attempted to add API auth exports based on stale file context.
- Resolution: inspect the actual file and apply a smaller patch against current content.

### Suggested Fix
Read small target files immediately before broad multi-file patches when prior turns may have changed exports.

### Metadata
- Reproducible: no
- Related Files: harness/__init__.py

---

## [ERR-20260626-007] pytest_readiness_mock_missing_data_backend

**Logged**: 2026-06-26T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
Readiness tests failed after adding `DATA_BACKEND` because test settings used `SimpleNamespace` without the new attribute.

### Error
```
AttributeError: 'types.SimpleNamespace' object has no attribute 'DATA_BACKEND'
```

### Context
- Command: `.venv/bin/pytest -q`
- Trigger: migration from Airtable-required readiness to backend-selectable readiness.

### Suggested Fix
Use `getattr(settings, "DATA_BACKEND", "postgres")` in readiness checks or update all test settings fixtures when adding required config attributes.

### Metadata
- Reproducible: yes
- Related Files: harness/readiness.py, tests/test_readiness.py

---

## [ERR-20260626-008] python310_datetime_compat

**Logged**: 2026-06-26T00:00:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary
New persistence code used `datetime.UTC`, which is not available in the project's target Python 3.10 runtime.

### Error
Would fail under CI Python 3.10 with an import error.

### Context
- Local ad-hoc validation used Python 3.14, which masked the compatibility issue.
- The Dockerfile and README pin Python 3.10.

### Suggested Fix
Use `datetime.timezone.utc` for Python 3.10 compatibility.

### Metadata
- Reproducible: yes
- Related Files: persistence/models.py, persistence/job_repository.py, services/job_manager.py

---

## [ERR-20260626-007] alembic_sqlite_probe

**Logged**: 2026-06-26T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
Local Alembic validation against SQLite failed before reaching migration execution.

### Error
`sqlite3.OperationalError: unable to open database file`

### Context
- The validation command targeted `./tmp/alembic_check.sqlite`.
- The `tmp/` runtime directory was not present on the host.
- The initial migration also needed a generic JSON type variant for SQLite probes.

### Suggested Fix
Create the runtime directory before local SQLite migration validation and use `sa.JSON().with_variant(JSONB, "postgresql")`.

### Metadata
- Reproducible: yes
- Related Files: migrations/versions/20260626_0001_initial_production_schema.py

---

## [ERR-20260626-006] persistence_import_coupling

**Logged**: 2026-06-26T00:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: backend

### Summary
Persistence tests and Alembic checks imported application settings before core dependencies were installed.

### Error
`ModuleNotFoundError: No module named 'pydantic_settings'`

### Context
- The database module and Alembic env imported `config.settings`.
- Pure persistence tests should not require full application configuration packages.

### Suggested Fix
Read `DATABASE_URL` directly from the environment in the persistence bootstrap and Alembic env.

### Metadata
- Reproducible: yes
- Related Files: persistence/database.py, migrations/env.py

---

## [ERR-20260626-005] git_init

**Logged**: 2026-06-26T00:00:00+08:00
**Priority**: medium
**Status**: pending_permission
**Area**: infra

### Summary
The workspace sandbox blocked creation of the repository's `.git` directory.

### Error
`.git: Operation not permitted`

### Context
- The project was inherited from a parent Git repository rooted at the user's home directory.
- Creating an independent repository requires writing Git metadata in the project root.

### Suggested Fix
Initialize the repository with explicit user approval outside the restricted sandbox.

### Metadata
- Reproducible: yes
- Related Files: .gitignore

---

## [ERR-20260626-004] pytest_import_path

**Logged**: 2026-06-26T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
The project root was not available on the pytest import path.

### Error
Unit tests could not import the local `harness` and `services` packages.

### Context
- Tests were executed through the virtual environment's pytest entry point.
- The repository is not installed as a Python package.

### Suggested Fix
Add the repository root to pytest's configured `pythonpath`.

### Metadata
- Reproducible: yes
- Related Files: pytest.ini

---

## [ERR-20260626-003] pytest_collection

**Logged**: 2026-06-26T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
Pytest collected the manual end-to-end script as a unit test module.

### Error
`test_workflow.py` imported optional production dependencies during collection.

### Context
- The repository had no pytest configuration.
- The manual API smoke script uses the `test_*.py` naming convention.

### Suggested Fix
Restrict unit-test discovery to the `tests/` directory.

### Metadata
- Reproducible: yes
- Related Files: pytest.ini, test_workflow.py

---

## [ERR-20260626-002] docker_build

**Logged**: 2026-06-26T00:00:00+08:00
**Priority**: medium
**Status**: pending_environment
**Area**: infra

### Summary
The Python 3.10 container validation could not start because Docker Desktop was not running.

### Error
`Cannot connect to the Docker daemon`

### Context
- Attempted to build the application image for dependency and test validation.
- The Docker socket existed but the daemon was unavailable.

### Suggested Fix
Start Docker Desktop and rerun `docker compose build video-replication`.

### Metadata
- Reproducible: yes
- Related Files: Dockerfile, docker-compose.yml

---

## [ERR-20260626-001] pip_install

**Logged**: 2026-06-26T00:00:00+08:00
**Priority**: high
**Status**: resolved_by_version_pin
**Area**: config

### Summary
Development dependencies could not be installed with the host's Python 3.14.

### Error
`rembg==2.0.57` has no compatible distribution for Python 3.14.

### Context
- Attempted to install `requirements-dev.txt` in a virtual environment.
- The unqualified `python3` executable resolved to Python 3.14.2.
- The application declares Python 3.9-3.10 support.

### Suggested Fix
Use Python 3.10 locally or run validation through the Python 3.10 Docker image.

### Metadata
- Reproducible: yes
- Related Files: requirements.txt, requirements-dev.txt, Dockerfile

---
