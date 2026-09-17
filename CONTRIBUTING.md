# Contributing to BioWorkflowManage

## Current Priority

The platform and its upstream applications are under active development. Public
analysis capabilities are defined here and consumed through a shared contract;
they must not depend on an individual customer's private implementation. Read
[Integration contract governance](docs/22-integration-contract-governance.md)
when changing API or execution-facing behavior. A software Release is not a
prerequisite for this development agreement.

The existing compiler foundation remains governed by:

- `docs/04-phase1-definition-of-done.md`
- `docs/05-tool-spec-schema.md`
- `docs/06-workflow-graph-schema.md`

Keep runtime and customer-specific concerns out of the compiler core.

## Change Workflow

1. Update or reference the relevant specification.
2. Add or update a machine-readable Schema when the external contract changes.
3. Add a positive and negative fixture for semantic changes.
4. Implement the smallest scoped change; preserve existing callers by default.
5. Run contract, golden-file and WDL validation.
6. Classify compatibility impact as internal, additive, or migration in the pull request.
7. For a migration, agree on parallel behavior and acceptance before removing anything.

## Contract Rules

- Do not use Vue Flow objects as the persisted Workflow Graph format.
- Do not use Django ORM models as the only ToolSpec or Graph definition.
- Do not store generated WDL as the source of truth.
- Do not resolve a ToolSpec by an unfixed latest version during compilation.
- Do not silently coerce incompatible bioinformatics semantic types.
- Do not add arbitrary template execution capabilities.

## Pull Request Checklist

- [ ] The change is within the requested scope and has a compatibility classification.
- [ ] JSON Schema remains valid.
- [ ] Existing contract baselines and behavior tests remain valid; old baselines are not rewritten.
- [ ] ToolSpec and Workflow Graph digests are deterministic.
- [ ] Golden WDL changes are intentional and reviewed.
- [ ] `scripts/validate_contracts.py` passes.
- [ ] miniwdl validation passes.
- [ ] UI-only changes do not alter semantic output.

Select checks proportional to the affected behavior. Pure documentation changes
do not require a production workflow run. Platform CI or a reference connector
test does not establish acceptance by every upstream or by a clinical workflow.

## Architecture Decisions

Changes to any of the following require an ADR:

- supported WDL version/profile;
- ToolSpec template language;
- semantic type compatibility rules;
- canonical JSON/digest algorithm;
- Workflow Graph node or edge semantics;
- Compiler IR public contract;
- deterministic ordering rules.
