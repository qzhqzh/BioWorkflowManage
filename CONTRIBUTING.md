# Contributing to BioWorkflowManage

## Current Priority

All work must serve the Phase 1 Graph-to-WDL compiler prototype defined in:

- `docs/04-phase1-definition-of-done.md`
- `docs/05-tool-spec-schema.md`
- `docs/06-workflow-graph-schema.md`

Phase 2 capabilities must not be introduced into the compiler core during Phase 1.

## Change Workflow

1. Update or reference the relevant specification.
2. Add or update a machine-readable Schema when the external contract changes.
3. Add a positive and negative fixture for semantic changes.
4. Implement the smallest compiler or UI change.
5. Run contract, golden-file and WDL validation.
6. Explain compatibility impact in the pull request.

## Contract Rules

- Do not use Vue Flow objects as the persisted Workflow Graph format.
- Do not use Django ORM models as the only ToolSpec or Graph definition.
- Do not store generated WDL as the source of truth.
- Do not resolve a ToolSpec by an unfixed latest version during compilation.
- Do not silently coerce incompatible bioinformatics semantic types.
- Do not add arbitrary template execution capabilities.

## Pull Request Checklist

- [ ] The change is within Phase 1 scope.
- [ ] JSON Schema remains valid.
- [ ] Existing fixtures remain valid or have an explained migration.
- [ ] ToolSpec and Workflow Graph digests are deterministic.
- [ ] Golden WDL changes are intentional and reviewed.
- [ ] `scripts/validate_contracts.py` passes.
- [ ] miniwdl validation passes.
- [ ] UI-only changes do not alter semantic output.

## Architecture Decisions

Changes to any of the following require an ADR:

- supported WDL version/profile;
- ToolSpec template language;
- semantic type compatibility rules;
- canonical JSON/digest algorithm;
- Workflow Graph node or edge semantics;
- Compiler IR public contract;
- deterministic ordering rules.
