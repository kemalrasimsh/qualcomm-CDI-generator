# Agent Guide for qualcomm-CDI-generator

This file guides automation agents to develop and validate changes the same
way CI and the maintainers expect:

- the project is a single Python script plus its packaging (setuptools and
  Meson) — no special build toolchain is required to run it, though the script
  imports PyYAML (`python3-yaml`) at startup,
- run the stand-alone unit test suite before opening/updating a PR, and
- follow the project commit-message style with a `Signed-off-by` trailer.

## Project Overview

qualcomm-CDI-generator is a Python script that probes the local hardware and
writes [Container Device Interface (CDI)](https://github.com/cncf-tags/container-device-interface)
JSON files so container runtimes can pass Qualcomm accelerators into
containers. It ships with setuptools and Meson packaging and a `unittest`
suite under `tests/`.

## 1) Pull request / contribution workflow

Follow the repository `CONTRIBUTING.md` contribution flow:

1. Target branch: **main**.
2. Fork `qualcomm-linux/qualcomm-CDI-generator`, create a topic branch,
   implement changes.
3. Rebase on latest upstream `main`.
4. Open a GitHub pull request.
5. Use PR discussion for review iteration.

Important:

- All PRs are statically analysed with [Semgrep](https://github.com/semgrep/semgrep);
  resolve any flagged issues before merge.

Before opening/updating a PR, run the stand-alone test suite. It needs PyYAML,
which the generator itself imports; the validation tests additionally need the
`cdi` tool on `PATH` (or via the `CDI_TOOL` environment variable) and are
skipped when it is absent:

```sh
python3 -m unittest discover -s tests -v
CDI_TOOL=/path/to/cdi python3 -m unittest discover -s tests -v
```

If the change touches packaging, also confirm both packaging paths still build:

```sh
python3 -m build
meson setup build/meson --prefix=/usr
meson compile -C build/meson
```

## 2) Commit message best practices (project style)

Use the style seen in recent history:

- `component: imperative summary` (preferred when scoped), e.g.
  - `qualcomm-cdi-generator.py: reference CDI spec, extract build_cdi_spec()`
  - `ci: run the CDI validation tests during builds`
  - `README.md: document CDI spec conformance, validation and tests`
- Or a concise imperative summary when cross-cutting.

Every commit **must** include a `Signed-off-by` trailer (the project uses the
[DCO](https://developercertificate.org/)) using the identity from the local
git configuration:

```sh
git commit -s   # or pass --signoff; fetches user.name / user.email from git config
```

If committing programmatically, append the trailer explicitly:

```text
Signed-off-by: $(git config user.name) <$(git config user.email)>
```

Never fabricate a name or email; always read from `git config`.

Guidelines:

- Keep subject line short and specific; capture intent, not a file-by-file dump.
- Use imperative mood (`Add`, `Update`, `Drop`, `Enable`, `Revert`).
- Add a body for non-trivial changes explaining **why** and key design decisions.
- Wrap body lines for readability (~72 chars).
- Use consistent version bump wording for version updates, e.g.
  `qualcomm-cdi-generator: Update to vX.Y.Z`.
- Include PR reference in subject when appropriate: `(#NNNN)`.
- Avoid mixing unrelated changes in one commit; split logically.
- Each patch must be logically coherent, self-contained, and independently buildable.
- The tree must remain in a functional state after every commit.
- Fixups within the same patch series are not allowed; changes should be corrected in the patch where they are introduced.
