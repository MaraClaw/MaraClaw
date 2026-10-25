# Offline API contracts

The checked `api-contract.json` contains the complete real `app.main.app.openapi()` document and an `x-maraclaw-contract` registry. The registry selects HTTP request/response/parameter schemas by concrete route-owner metadata in `scripts/api_contract_registry.py`, adds canonical engine enums and the explicit `ChatServerEvent` adapter, then records their reference closure. Unrelated HTTP operations remain in the artifact, not in generated validators. This is not an HTTP SDK.

From `engine/`:

```sh
uv run python -m scripts.export_api_contracts
uv run python -m scripts.export_api_contracts --check
```

Neither command enters lifespan or starts DB, Redis, connectors, workers, or a server. Operation IDs are replaced only in the exported copy with `<method>_<UTF-8 path hex>` to make multi-method proxy operations unique. JSON keys and registry names are sorted; there are no timestamps or machine paths. Refresh after concurrent engine schema work has finished. `--check` exits nonzero for missing/stale bytes and never writes. `--output <path>` overrides the checked-artifact destination.

From the repository root, with an existing output directory:

```sh
node engine/scripts/contract_codegen/generate.mjs \
  --input engine/contracts/api-contract.json --output web-e/src/lib/engine-contracts.generated.ts
node engine/scripts/contract_codegen/generate.mjs \
  --input engine/contracts/api-contract.json --output web-e/src/lib/engine-contracts.generated.ts --check
```

The Node generator has no dependencies and never imports application code. Its output imports the consumer's Zod 4 and exports `NameSchema`, `type Name = z.infer<typeof NameSchema>`, and named enum objects plus `NameValues` readonly tuples for registered enums. Engine models own domain names such as `AuthUser`, `AdminUserSummary`, `ControlActionResult`, and `ApprovalDecisionRequest`. The registry's `names` map explicitly assigns public names to third-party or input/output alias collisions: `JsonValue`, `JsonInputValue`, `JsonOutputValue`, `JsonInputObject`, and `JsonOutputObject`. The explicit chat adapter's separate JSON definition exports as `ToolCallValue`, retaining its tool-call arguments/result references without colliding with the HTTP `JsonValue`. Schema keys, references, and validation stay unchanged by this metadata. Public symbols must be alphanumeric identifiers starting with a letter, not reserved words; private names, compiler artifacts, duplicate names, and collisions with schema/value exports fail explicitly. No prefix stripping or schema merging occurs.

Requiredness comes only from `required`, independently of nullability and default annotations. Omitted `additionalProperties` allows and preserves extras; `false` rejects them; a schema validates extras. References, primitive literals/enums, arrays, anyOf, exclusive oneOf, allOf, primitive type arrays, common Pydantic formats, and encountered bounds/patterns are compiled faithfully. Exact recursive JSON aliases become `z.json()`; other recursion, unknown formats/keywords, external references, and nonportable regular expressions fail with a schema location. No semantic keyword silently becomes `unknown`, `any`, or a permissive fallback. Object/array-valued enum literals and conditional/tuple/unique-item schemas are deliberately unsupported.

Focused verification:

```sh
uv run pytest tests/test_api_contract_export.py
node --test tests/contract_codegen.test.mjs tests/contract_public_names.test.mjs
```

Runtime tests use the already-installed `web-e` Zod; set `CONTRACT_ZOD_MODULE` to an absolute Zod module filename to use another existing Zod 4 installation. Nothing is installed. Tests compile actual generated validators, not string snapshots. Frontend owners separately run their TypeScript/build gates after adoption.
