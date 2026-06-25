# Learnings

Corrections, insights, and knowledge gaps captured during development.

**Categories**: correction | insight | knowledge_gap | best_practice

---

## [LRN-20260626-001] best_practice

**Logged**: 2026-06-26T00:00:00+08:00
**Priority**: high
**Status**: applied
**Area**: config

### Summary
The supported Python version must be machine-enforced, not only documented.

### Details
The host defaulted to Python 3.14 while the project and `rembg==2.0.57`
require Python 3.9-3.10. Creating a virtual environment with an unqualified
`python3` produced an environment that could never install the lock set.

### Suggested Action
Pin Python 3.10 in `.python-version`, Docker, CI, and developer setup commands.

### Metadata
- Source: error
- Related Files: README.md, Dockerfile, .github/workflows/ci.yml, .python-version
- Tags: python, reproducibility, dependencies

---
