# v0.1 compatibility matrix

The [baseline](baseline.md) records observed behavior. Phase numbers below
refer to the approved v0.1 Architecture and Roadmap sequence.

| Surface | Baseline state | v0.1 promise | Phase allowed to evolve | Required proof |
| --- | --- | --- | --- | --- |
| Contract wire and authority | EXISTING / FROZEN, eight wire 1.0 families | Preserve schema identity, exact references, immutable revisions, scoped HUMAN/delegated/Validation/Report authority and audit lineage. | Deliberate owner-reviewed compatibility event | Schema hash comparison, old-document/exact-ref tests, active-head and publication authorization regressions. |
| SQLite project state | EXISTING / FROZEN at v6 | Upgrade the valid existing DB, retain its markers, records and authority. | 2 for v7, 3 for v8, 4 for v9 | Baseline fixture, verified backup, transactional migration, exact preservation, failure/reopen and integrity/FK checks. |
| CLI and Home | EXISTING / EVOLVABLE BEHIND FACADE | Keep scriptable commands and current-project open. Installed GUI becomes default no-argument experience when delivered, with CLI Home available as fallback. | 1 facade, 8 default GUI | Existing CLI, status equivalence and command/exit behavior tests. |
| Project Manager | EXISTING / EVOLVABLE BEHIND FACADE | Same draft and CAS, three authoring modes, completion, Back/Save/Resume, exact review and HUMAN publication. | 8 GUI integration | Existing tests plus GUI/CLI canonical equivalence, recovery and real authority checks. |
| Provider configuration/discovery | EXISTING / EVOLVABLE BEHIND FACADE | Preserve precedence, explicit reset and bounded native discovery. | 3 registry/adapter evolution | Operation/project/user/discovery precedence, invalid native path and wrapper rejection. |
| Provider execution authority | EXISTING / FROZEN | Setup, reinstall and passive startup execute no provider. Probes follow explicit operations or provider need. | 1/3/8/9 must preserve | Sentinel tests on PATH and in configuration, passive startup and explicit refresh tests. |
| Global state and Recent Projects | EXISTING / EVOLVABLE BEHIND FACADE | Evolve existing v1 settings, provider registry and bounded MRU. Project truth stays in project. | 2 registry, 8 presentation | Preserve compatible files and choices, read-only path validation and bounded/deduplicated MRU. |
| Source installer | EXISTING / EVOLVABLE BEHIND PLATFORM SEAM | Source-first one-command setup, per-user ownership, PATH, rollback and uninstall preservation. | 9 desktop packaging | Fresh Windows installation, pinned integrity, upgrade/rollback/uninstall and no-provider-execution smoke. |
| Private Windows runtime | EXISTING / FROZEN at CPython 3.14.10 x64 | Keep private runtime and isolation unless separately approved. | 1 sidecar reuse, 9 install integration | Python-free PATH install, exact pin and independent runtime, launcher/sidecar lifecycle. |
| Workflow, Validation and Report APIs | EXISTING / EVOLVABLE BEHIND FACADE | Reuse engines and storage, preserve workflow obligations and independent evidence. | 1 facade, 3 supervisor, 6 adaptive workflow, 8 UI | Current suites, exact-source evidence and gates, no parallel runtime/store, GUI/CLI equivalence. |
| Platform boundary | EXISTING / FROZEN | Windows desktop/install, Windows and Ubuntu Core CI, platform-neutral project state. | 1 desktop spike, 9 hardening | Both OS suites, Windows installer smoke and review of OS-specific dependencies. |
| Python source compatibility | EXISTING / FROZEN at metadata `>=3.12` | Verify 3.12, 3.14.10 and 3.15. | Deliberate runtime or support decision | Clean wheel matrix on Windows and Ubuntu. |
| Application Layer, RPC and desktop GUI | ABSENT / PLANNED | One facade, versioned stdio bridge and desktop without independent domain logic. | 1 spike, 8 complete GUI | Handshake, lifecycle and GUI/CLI equivalence, passive startup, installed desktop smoke. |
| Work Items and Project State | ABSENT / PLANNED | Persistent Work Items and a composed read model on the existing project DB. | 2 | v6→v7 preservation, Work Item persistence, read-model provenance. |
| Agent Sessions and switching | ABSENT / PLANNED | Track provider-neutral sessions, preserve continuity through a new session on switch. | 3 and 5 | Real independent providers, lease/recovery and handoff continuity evidence. |
| Context Projection and external import | ABSENT / PLANNED | Deterministic immutable derived context, read-only discovery before changes. | 4 and 7 | Reproducible projection/digests, provenance/redaction, import without source mutation. |

Versioning for the public desktop release remains an owner decision. Package,
desktop, RPC, contract and database versions have separate compatibility roles.
