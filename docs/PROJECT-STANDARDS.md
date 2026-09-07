# Project Standards

## Product Authority

`docs/PRODUCT-REQUIREMENTS.md` is the maintained product contract. Current explicit user instructions override it; update the contract and `docs/DEVLOG.md` in the same change when scope or behavior changes.

The active delivery scope is all five chapters and 38 canonical sections. Chapter 1 section 1 remains a continuous regression sample; it does not block the other sections.

## Architecture And Ownership

- `ybt_learning/`: current Python domain logic for packets, state, coverage, OCR/vision, contexts, and simulations.
- `scripts/`: orchestration, build, migration, rendering, and acceptance commands.
- `codex-skill/ybt-all-chapters-learning-path/`: reusable agent workflow and fail-closed validators.
- `data/`: source-derived data and durable learner progress. Never store credentials.
- `reports/`: generated evidence and learner-facing artifacts.
- `frontend/`: ownership boundary for future learner-facing rendering code.
- `backend/`: ownership boundary for a future service/API; current domain code remains in `ybt_learning/` until an intentional migration.

Do not duplicate existing implementations merely to populate `frontend/` or `backend/`.

## Data And Status Rules

- Preserve canonical item identity and source order.
- Keep course coverage, course consumption, item mastery, human acceptance, and delayed retention as separate fields.
- Use explicit states such as `not_started`, `in_progress`, `completed`, `blocked`, `failed`, `not_run`, and `stale`; do not promote one evidence layer into another.
- The growing learner profile starts with only the user-approved zero-base assumption. Add a strength, weakness, or dependency only when an attempt record supports it.
- Generated evidence must bind current source hashes when the relevant schema requires them.

## Paths And Portability

- Resolve project files from the repository root or command-line parameters.
- Do not introduce hard-coded user profiles such as `C:\Users\<name>` or fixed historical project roots.
- Historical absolute paths may remain inside immutable evidence, but executable code must not depend on them when a repository-relative artifact exists.

## Learner-Facing Output

- Optimize for repeated study, not audit verbosity.
- Show a course once in the chapter ledger and reference it compactly later.
- Put shared recognition, method, first-line, continuation, and self-check guidance at the smallest shared textbook unit.
- For individual items, show the label and only item-specific deviations or blockers.
- Keep hashes, internal IDs, persona traces, source paths, and validator details in machine evidence.
- Markdown and HTML must remain answer-free, UTF-8, responsive, and readable on narrow screens.

## Verification

Run tests proportional to the change. Contract changes require:

1. Existing section validator regression tests.
2. Growing-learner chapter progress validator tests.
3. Validation of committed chapter 1 and 2 progress files.
4. A clean Git diff review for accidental generated or path-specific changes.

The bundled Codex Python runtime may be used when the system `python` command is unavailable.
