# Errors

Command failures and integration errors.

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
