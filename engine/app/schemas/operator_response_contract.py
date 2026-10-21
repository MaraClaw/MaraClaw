from pydantic import GetJsonSchemaHandler, GetPydanticSchema, TypeAdapter
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema
from typing_extensions import TypeForm


def response_contract(contract: TypeForm[object]) -> GetPydanticSchema:
    def json_schema(_schema: CoreSchema, handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        return handler.resolve_ref_schema(handler(TypeAdapter[object](contract).core_schema))

    return GetPydanticSchema(get_pydantic_json_schema=json_schema)
