"""Export/check the real application OpenAPI plus the bounded wire registry offline."""

import argparse
from pathlib import Path

from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import JsonValue

from scripts.api_contract_document import DOCUMENT_ADAPTER, assemble_contract, canonical_json, mapping
from scripts.api_contract_registry import AUDITED_HTTP_MODULES, ENUM_TYPES, PUBLIC_SCHEMA_NAMES, explicit_schemas

DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "contracts" / "api-contract.json"


class ExportArguments(argparse.Namespace):
    output: Path = DEFAULT_OUTPUT
    check: bool = False


def export_contract(application: FastAPI) -> dict[str, JsonValue]:
    operations = [
        (context.path_format, method.lower())
        for context in iter_route_contexts(application.routes)
        if isinstance(context.route, APIRoute)
        and context.route.include_in_schema
        and context.path_format is not None
        and context.endpoint.__module__ in AUDITED_HTTP_MODULES
        for method in context.methods or ()
    ]
    document = assemble_contract(
        DOCUMENT_ADAPTER.validate_python(application.openapi()), operations, explicit_schemas()
    )
    schemas = mapping(mapping(document["components"])["schemas"])
    for enum_type in ENUM_TYPES:
        mapping(schemas[enum_type.__name__])["x-enum-varnames"] = [member.name for member in enum_type]
    registry = mapping(document["x-maraclaw-contract"])
    declared = registry["schemas"]
    if not isinstance(declared, list):
        raise TypeError("Expected contract schema registry")
    registry["names"] = {name: public for name, public in PUBLIC_SCHEMA_NAMES.items() if name in declared}
    return document


def write_or_check(output: Path, content: str, *, check: bool) -> bool:
    if check:
        return output.is_file() and output.read_text(encoding="utf-8") == content
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="Fail on missing/stale artifact; never write")
    args = parser.parse_args(namespace=ExportArguments())
    # Import mounts routes only. Do not enter lifespan, TestClient, or run startup hooks.
    from app.main import app

    content = canonical_json(export_contract(app))
    if not write_or_check(args.output, content, check=args.check):
        print(f"Stale API contract: {args.output}; run python -m scripts.export_api_contracts")
        return 1
    print(f"API contract {'checked' if args.check else 'exported'}: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
