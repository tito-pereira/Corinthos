# Foundation decisions

The active product identity is Corinthos. Earlier AgentForge and Nukytera names
record historical decisions, commands and evidence. The Phase 1.1 section
records the current naming contract and the durable identifiers retained for
stored contracts.

Record dated November 8, 2026, for scaffold `0.1.0`. The implementer made the
following practical choices within the Phase 0 order. Their provenance is
technical implementation; no individual human approval is attributed to them.
Product requirements revision v0.2 targets Core v0.1.

## Current Python baseline

Package metadata declares Python `>=3.12`. CI covers CPython 3.12, 3.14.10 and 3.15
on Windows and Ubuntu. Historical post-merge
[CI run 43100767021](https://github.com/tito-pereira/Corinthos/actions/runs/43100767021)
at commit `b35fc74b96c3dc86d898c01f7fbe3cd6e0582169` exercised CPython 3.12.10
on Windows and CPython 3.12.16 on Ubuntu. These are observations for that exact
commit. The [contributor commands](../README.md#contributor-setup) use CPython
3.12. The [Windows source installer](../README.md#install-on-windows) stages an
isolated CPython 3.14.10 embeddable runtime and checksum-verified dependencies.

## Historical Phase 0 practical choices

| Choice | Rationale and boundary |
| --- | --- |
| Python 3.12 baseline | Explicit baseline for installation, tests, and CI, consistent with the approved Python stack. The README documents prerequisites. |
| `argparse` and the standard library | CLI help and version need little logic and require no additional CLI framework. |
| `setuptools`, `pyproject.toml`, and `src/` layout | Conventional packaging with local installation and an entry point, separating sources from tests. |
| PyYAML with `safe_load` | Loads the declarative YAML policy through an appropriate parser. |
| `jsonschema` | Validates the policy structure against JSON Schema, including mandatory and enabled M1–M5. |
| `pytest` | Runs smoke, positive, negative, and packaging checks. It was already part of the approved stack. |
| Ruff | Lightweight static analysis available both locally and in CI. |
| GitHub Actions, Linux and Windows, Python 3.12 | Configures installation, lint, and tests for both relevant systems. A configured workflow requires actual execution before success can be claimed. |

The foundation CLI exposes help and version information. The validator
`agentforge.standard.load_standard(policy_path, schema_path)` takes both paths
explicitly. `policies/standard.yaml` and `schemas/standard.schema.json` remain
repository files whose paths the caller supplies when using this API.

The policy contains `standard_version: "0.1"` and a `commandments` map with M1–M5.
Each entry declares `required: true` and a description. The policy version,
scaffold version, and product requirements revision identify separate artifacts.

## Historical Phase 0 boundaries and assumptions

The minimal schema checks the Standard structure. Tests and technical review
assess code and the adequacy of evidence. The six Engines, eight contract schemas,
SQLite storage, migrations, functional adapters, and operational workflows belong
to subsequent phases.

`.agentforge/` is reserved for future private data and must be ignored by Git,
together with local databases, environments, caches, and secrets. The foundation
uses normal user permissions and operations in the authorized directory.

The original product requirements grouped foundation and Kernel in one
implementation block. The later construction plan and specific Phase 0 order
established the current sequence, reserving contract schemas and operational
persistence for Phase 1. [implementation-plan.md](implementation-plan.md) records
that sequence locally.

## Decisions for the next phase

Phase 1 must define types and schemas for the eight contracts, invariants,
identities/revisions, reference validation, authorization records, the SQLite
model, and migrations. It must define operations and tests for integrity,
interruption, and safe repetition, including the boundary between SQLite and Git.

Material requirement changes and binding technology or structural decisions
require the applicable human authority. Implementation decisions must preserve
their provenance and reference the approved specification.

## Historical Phase 0 verification

The following is the recorded Phase 0 verification from November 8, 2026, on
Windows with Python 3.12.14. It describes that execution. Phase 0.1 must establish
fresh results for the translated repository.

The directory was initially empty, and Git was initialized locally on `main`.
The `.venv/` environment was created without inheriting global packages, using
Python from a locally available application runtime. The original setup also
documented an alternative using a normal Python installation. Public setup
instructions use portable commands; personal runtime paths are omitted.

The direct dependency and tool versions exercised were PyYAML 6.0.3,
jsonschema 4.26.0, pytest 9.2.0, and Ruff 0.17.4. Compatibility ranges are declared
in `pyproject.toml`; later installations may resolve other versions.

| Checkpoint | Recorded result and local evidence |
| --- | --- |
| F0.1 | Done. Empty directory, Git, and Python/pip inspected; baseline and choices documented. |
| F0.2 | Done. Editable installation, CLI, policy, and schema exercised. Git exclusions checked for private state and common secret files. |
| F0.3 | Done. AGENTS/CLAUDE, README, and five technical documents reviewed, with working local links and references to the approved planning sources. |
| F0.4 | Done. 33 tests passed. Ruff lint and format checks passed. CI configured for Windows/Linux. |
| F0.5 | Done. Files and Git state inspected; independent review found no blocking issues, and M1–M5 evidence was recorded below. |

Commands executed from the repository root with `.venv/Scripts/python.exe`
included `-m pip install -e ".[dev]"`, `-m pip check`, `-m agentforge --help`,
`-m pytest`, `-m ruff check .`, and `-m ruff format --check .`. The entry point
`.venv/Scripts/agentforge.exe --version` returned `agentforge 0.1.0`. The README
loading example returned M1, M2, M3, M4, and M5.

`-m pip wheel . --no-deps --wheel-dir .venv/wheelhouse` was also executed. The wheel
and its dependencies were installed into a second clean environment at
`.venv/package-check/`. Help, version, and imports worked from another directory,
with the module loaded from `site-packages`. The policy and schema still require
checkout paths, as described by the API.

| Commandment | Recorded evidence |
| --- | --- |
| M1 | F0.1–F0.5 scope and authority in AGENTS/CLAUDE, the plan, and the original execution order. |
| M2 | Practical choices and provenance in this document; dependencies declared in pyproject. |
| M3 | 33 tests, covering missing/disabled M1–M5, invalid configuration, and unsafe YAML; actual installation and static analysis. |
| M4 | README with executed commands, five technical documents, and references to the approved planning sources. |
| M5 | CLI using argparse, a short validator using appropriate libraries, Ruff, and independent review. |

Remote CI and POSIX commands were unverified in that Phase 0 run. Local validation
used Windows and Python 3.12.14. A virtual environment depends on the interpreter
that created it and may need recreation after its application runtime is updated.
At the end of Phase 0, Git contained 19 new untracked files, with no commits,
remote, or push. These are historical observations. Phase 1 remains subject to
the subsequent authorization required by the plan.

## Historical Phase 0.1 publication decisions

The user authorized an English-only public repository named `AgentForge`,
including a reviewed commit and push after publication prerequisites pass. This
order supersedes the original Portuguese documentation requirement. The Python
package name remained `agentforge`, and the foundation API and M1–M5 identifiers
retained their meaning.

Public documentation uses self-contained local references. Private planning URLs,
personal runtime paths, and private state are excluded from public materials.
The original internal planning retains decision provenance. New test results,
commit identifiers, repository visibility, publication, and remote CI status must
be established by the Phase 0.1 run and its delivery report.

At the time of Phase 0.1, license selection was pending an explicit decision by
the project owner. The later MPL-2.0 decision recorded below supersedes that
historical status.

## Historical Phase 0.1 local verification

The English-language tree was revalidated on November 8, 2026, on Windows with
Python 3.12.14. The existing 33 tests passed again. Ruff lint and formatting checks
passed, and `pip check` found no broken dependencies. Editable installation, the
README Standard example, and both CLI entry points worked with English output.

A new wheel was built and installed with its dependencies in a fresh isolated
environment under `.venv/phase01-check/`. Help, version, and imports worked from
another directory with the module loaded from `site-packages`. The dependency
versions match the historical Phase 0 versions recorded above.

Comparison with the pre-migration tree confirmed unchanged executable logic,
test files, schema constraints, Commandment identifiers and required flags,
dependency declarations, and CI commands. Changes to these artifacts were limited
to human-readable text. Additional ignore patterns protect editor settings and
conventional credential, environment, and private-key files.

The intended 19-file public tree was reviewed for credentials, private state,
private planning URLs, personal paths, and broken local links. The project owner
explicitly authorized a GitHub-provided private commit email for this repository.
Remote publication and observed CI results are reported separately in the delivery
report. POSIX shell setup was not executed locally.

## Historical CI portability correction

The first public workflow passed on Linux and exposed a Windows test assumption.
The installed-entry-point test looked beside the Python interpreter, which works
inside the local virtual environment. A standard Windows installation puts
console scripts in a separate directory. The test now uses
`sysconfig.get_path("scripts")`, preserving its actual executable and metadata
checks for both installation layouts. CLI help uses ASCII punctuation so its
English description remains readable in Windows runner logs.

## Historical Phase 0.2 decisions and verification plan

On November 9, 2026, the project owner explicitly selected the Mozilla Public
License 2.0, with SPDX identifier `MPL-2.0`. This approved choice supersedes the
earlier pending-license status. Phase 0.2 applies the complete official license,
appropriate source notices, packaging metadata, and documentation on a reviewed
feature branch or pull request. Its publication on the default branch requires
separate authorization and must be reported according to the actual branch state.

The license text must remain complete and unmodified. Project-authored files use
appropriate MPL notices or SPDX headers where comments are supported. Formats
without comments retain a nearby accessible license notice. Existing copyright
and third-party attribution remain accurate. No copyright-holder name or ownership
transfer is inferred from this license choice. Official license and notice guidance
are provided by [Mozilla's MPL 2.0 text](https://www.mozilla.org/en-US/MPL/2.0/) and
[FAQ](https://www.mozilla.org/en-US/MPL/2.0/FAQ/).

The verification plan requires fresh Python 3.12 virtual environments on Windows
PowerShell and real Linux, using clean checkouts of the same commit. The README
uses a normal independently installed Python and checks `sys.executable`,
`sys.prefix`, and the checkout's `.venv` location. Tools run through the explicit
venv interpreter. Optional shell activation is checked separately, without changing
machine-wide PowerShell execution policies. The earlier application-runtime setup
remains a historical Phase 0 observation.

The workflow will create and use `.venv` explicitly on Windows and Ubuntu, then
exercise installation, dependency checks, CLI help/version, the full test suite,
Ruff lint, and formatting. Earlier runner-global CI results are separate evidence.
Completion requires observed results for both OS environments and verified
licensing changes on the review branch. New results, tool versions, commit SHA,
branch/PR URLs, and limitations must be recorded after execution. At the time of
preparing this plan, those Phase 0.2 execution results are pending.

English is mandatory for all implementation phases, subphases, checkpoints,
engineering reports, repository artifacts, and related implementation
communication. Phase 1 remains subject to a subsequent owner instruction.

## Historical Phase 0.3 naming decision and migration contract

On November 9, 2026, the project owner renamed the product to **Nukytera** and
authorized the corresponding technical naming migration. This changes identity
while retaining package version `0.1.0`, product requirements revision v0.2, and
the Core v0.1 target. The five Engineering Commandments, six Engines, eight
contract types, human approval rules, and MPL-2.0 remain unchanged.

| Layer | Historical identifier | Required identifier |
| --- | --- | --- |
| Product brand | `AgentForge` | `Nukytera` |
| Repository and workspace name | `AgentForge` | `Nukytera` |
| Python distribution | `agentforge-core` | `nukytera-core` |
| Python import package | `agentforge` | `nukytera` |
| CLI console command | `agentforge` | `nukytera` |
| Source directory | `src/agentforge/` | `src/nukytera/` |
| Private-state convention | `.agentforge/` | `.nukytera/` |

The migration retains only the new active package and CLI, without a legacy
alias or parallel source tree. Existing Standard identifiers, field names,
schema versions, contract identities, and behavior were preserved. Feature work
and Phase 1 were suspended pending a later owner instruction.

The legacy `.agentforge/` ignore rule is retained intentionally to protect any
older private local state. `.nukytera/` was the active Phase 0.3 convention. This safety
exclusion does not introduce runtime state migration or persistence features.
Earlier commands and publication names above remain historical evidence.

Changes use a reviewed migration branch and pull request, with separate
authorization required for merging into the default branch. The historical
`phase-1-baseline` tag and existing commit history remain untouched. Repository
URLs, folder moves, and fresh Windows/Linux results must be recorded only after
they are observed. This decision records the naming contract rather than claiming
completion of those operations.

## Historical Phase 1.0 Contract Kernel decisions

The explicit Phase 1.0 order started from verified main `830943bb`. It preserved
the historical baseline tag, package version `0.1.0`, PRD revision v0.2 and Core
v0.1 target. The [design record](contract-kernel-design.md) separates approved
responsibilities from the wire, identity and storage choices made here.

- Use the existing JSON Schema stack and its offline reference registry. The
  wheel includes eight schemas and a shared envelope, with no added dependency.
- Keep the four exact reference fields in JSON and a frozen `ContractRef` in
  Python. Store sequential content versions and exact predecessors rather than
  implicit latest references. Select active revisions with SQLite pointers.
- Verify approval through the authenticated caller's exact-document receipt
  verifier. Human decisions retain their original authority. Already approved
  exact sources support new plans without repeating their approvals. Changes to
  binding interface definitions or milestones require applicable human proof.
- Use SQLite foreign keys and immediate transactions for publication and CAS.
  SHA-256 detects changes to published documents and local evidence bytes.
  The store retains the prior document even after active resolution changes.
- The first migration initializes an empty local database. Explicit SQLite
  backup and fail-closed version checks preserve existing files. Future schema
  upgrades will need their own migration and recovery evidence.
- The demonstration uses fixtures in an isolated temporary root. A blocked
  interrupted result exercises gate derivation without claiming executed tests.
  Existing CLI help and version remain the supported commands.

The source-to-test matrix records the Engineering Completion Gate. Independent
reviews examine authority, schema coverage, transactions, evidence binding, public
documentation and the final test results. The PR records the exact tested SHA
and observed Windows and Ubuntu evidence. Phase 2 negotiation and the six full
Engines remain future work.

## Phase 1.1 Corinthos naming and compatibility

On November 10, 2026, the project owner authorized the Corinthos naming
migration from Phase 1.0 main
[`e058f4b96454dba44e5b31387c37061b75cec662`](https://github.com/tito-pereira/Nukytera/commit/e058f4b96454dba44e5b31387c37061b75cec662).
The [Phase 1.0 CI run](https://github.com/tito-pereira/Nukytera/actions/runs/42693412409)
and the `phase-1-baseline` tag retain their historical identities.

| Layer | Historical name | Phase 1.1 name |
| --- | --- | --- |
| Brand, repository and workspace | `Nukytera` | `Corinthos` |
| Python distribution | `nukytera-core` | `corinthos-core` |
| Import package and CLI | `nukytera` | `corinthos` |
| Source directory | `src/nukytera/` | `src/corinthos/` |
| Private state directory | `.nukytera/` | `.corinthos/` |

The import prefix change is intentional. Current integrations use
`corinthos` as their sole import prefix and console command. Product target
Core v0.1, PRD revision v0.2 and package version `0.1.0` remain separate.
The five Commandments, six planned Engines, eight contract types, verified
approval rules and MPL-2.0 continue to govern the project.

The schema namespace `urn:nukytera:contracts:1.0:*`, Standard reference
`nukytera-standard` version `0.1`, JSON wire version `1.0`, SQLite application
ID `0x4E554B59` and SQLite `user_version = 1` are durable identities.
Retaining them preserves offline schema resolution and existing immutable
contracts, approvals, evidence and audits. The
[state migration procedure](storage.md#migrating-existing-project-state)
copies a verified closed store without rewriting those records. Both old and
current state directories remain excluded from Git.

The reviewed migration pull request records the exact runtime verification and
publication evidence. The project owner authorizes integration and Phase 2
separately.

## Phase 2 Project Manager

Phase 2 starts from the integrated Phase 1.1 commit
`c96ee0c893b1374d13fff64d4d037fb6bad50134`. Its post-merge Windows and Ubuntu
checks each passed 161 tests before implementation. Product scope remains Core
v0.1, PRD document revision v0.2 and package version `0.1.0`.

- A terminal-independent `ProjectManager` service stores explicit structured
  human choices. A thin CLI asks plain-language questions. An optional callable
  may return advisory text, with no draft mutation or approval authority.
- Deterministic checks detect shape, reference, identity, normalized scope and
  missing-field contradictions. Semantic completeness, acceptance testability
  and technical feasibility require human judgement and remain unverified by
  automation. Supplied alternatives and their decisions remain inspectable.
- SQLite version 2 adds session, turn and receipt tables to the existing state
  database. The application ID and eight wire schemas retain their identities.
  Verified private pre-v2 backups and transactional schema changes support
  recovery. Published contract records remain under the Kernel's existing API.
- A five-minute exact review contains the final approval metadata before its
  SHA-256 is calculated, avoiding digest circularity. The foreground terminal
  requires an unpredictable, explicit confirmation phrase. No public boolean,
  JSON field, batch switch or assistance response authorizes publication.
  The local account and trusted Python process define the authority boundary.
- Session approval, Kernel publication and final receipt consumption have
  separate transactions. Recovery holds the writer reservation while inspecting
  the exact revision. It either reconciles an existing publication or revokes an
  interrupted approval, preventing duplicate publications and stale consent.
- PRR intake pins the exact published base and request. Explicit structural
  triage opens a bounded session. Unaffected obligations and IDs are preserved.
  Keeping, returning or deferring a request publishes no content revision.
  User-initiated changes can create a separate revision session directly.

The service, terminal, approval, migration and regression tests demonstrate
these boundaries. Synthetic consent exists only inside labelled test fixtures.
Workflow planning, actual Codex/Claude adapters, Change Impact Analysis,
independent Validation, Report and product evolution require later phases.
The [Project Manager guide](project-manager.md) provides the API, tutorial,
state transitions and requirement-to-test traceability.

## Historical Phase 3 checkpoints A–D

The initial owner instruction authorized only checkpoints A through D from integrated Phase 2 main
`139d1542f9b15938985909ab1124832cd69de607`. Both prior post-merge platforms and
the new clean installed-wheel baseline passed 245 cases before implementation.
The dedicated local branch is `phase-3.0-workflow-adapters`.

- Adapter Manager uses documented native Codex/Claude interfaces through explicit
  argument lists and stdin, with no shell prompt interpolation or unrestricted
  modes. The same existing validated Standard accompanies managed instructions
  and bounded context. Setup takes explicit policy/schema files, preserving a
  single canonical Standard and current package data behavior.
- The Codex and Claude transports remain separate. Capabilities are inspected
  from installed help. Protocol fixtures and a harmless real read-only Codex
  diagnostic provide distinct evidence. Claude Code is absent locally and its
  live behavior remains unverified. Observed resource counters carry their
  provider scope, and unavailable cost data stays unknown.
- Workflow planning is a terminal-independent service. Draft technology choices
  have stable IDs and retain ranking and user alternatives. Exact contract
  schemas and Kernel semantic checks govern coverage, interfaces, references,
  dependency graphs, milestones and mandatory policy constants.
- One explicit final-candidate set can be approved at the real foreground
  terminal. Each actual publication has its own exact HUMAN receipt. A separate
  explicit choice may grant the tightly scoped 24-hour internal dependency
  mandate. It never authorizes a changed technology, interface or milestone.
- SQLite v3 extended the shared local database only for planning history and
  publication authority. It reuses existing CAS mechanics and verifies a real
  Phase 2 store upgrade with all contract and PM data preserved. Partial
  publication remains inspectable and safe to reconcile.

That initial scope prepared and published plans. Scheduler execution, full
recovery, revision impact/PRR production, the user-project pilot and release
checkpoints E–J awaited a subsequent order. The owner later authorized that
continuation. The [runtime guide](workflow-execution.md) describes its current
boundaries, and the [verification record](phase-3-verification.md) identifies
observed results. The
[design and traceability record](phase-3-design.md) and
[planning guide](workflow-planning.md) define the A–D boundary.

## Technical references

These references were consulted for packaging, validation, lint, and CI during
the foundation work.

- [Python packaging](https://packaging.python.org/en/latest/guides/distributing-packages-using-setuptools/)
- [jsonschema validation](https://python-jsonschema.readthedocs.io/en/stable/validate/)
- [Ruff installation](https://docs.astral.sh/ruff/installation/)
- [checkout releases](https://github.com/actions/checkout/releases)
- [setup-python releases](https://github.com/actions/setup-python/releases)

Related local documents cover [architecture](architecture.md),
[contracts](contracts.md), [workflows](workflows.md), and
[implementation phases](implementation-plan.md).
