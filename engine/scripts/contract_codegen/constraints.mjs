import { ContractError, enumMemberNames } from "./document.mjs";

const annotations = ["title", "description", "default", "examples", "example", "deprecated", "readOnly", "writeOnly", "$comment"];
const shared = [...annotations, "type", "const", "enum", "$ref", "anyOf", "oneOf", "allOf", "discriminator", "x-enum-varnames"];
const typed = {
  string: ["minLength", "maxLength", "pattern", "format"],
  number: ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"],
  integer: ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"],
  array: ["items", "minItems", "maxItems"],
  object: ["properties", "required", "additionalProperties", "minProperties", "maxProperties"],
  boolean: [], null: [],
};

export function validateKeywords(schema, location) {
  enumMemberNames(schema, location);
  const types = Array.isArray(schema.type) ? schema.type : [schema.type];
  const allowed = new Set([...shared, ...types.flatMap(type => typed[type] ?? [])]);
  for (const key of Object.keys(schema)) {
    if (!allowed.has(key)) throw new ContractError(`${location}.${key}`, "unsupported semantic keyword");
  }
  if (schema.discriminator !== undefined && schema.oneOf === undefined && schema.anyOf === undefined) {
    throw new ContractError(location, "discriminator without a union");
  }
}

export function constraints(expression, schema, location) {
  let result = expression;
  const methods = {
    minItems: "min", maxItems: "max",
    minimum: "min", maximum: "max", exclusiveMinimum: "gt", exclusiveMaximum: "lt", multipleOf: "multipleOf",
  };
  for (const [key, method] of Object.entries(methods)) {
    if (schema[key] !== undefined && typed[schema.type]?.includes(key)) {
      if (typeof schema[key] !== "number" || !Number.isFinite(schema[key])) {
        throw new ContractError(`${location}.${key}`, "expected finite numeric constraint");
      }
      result += `.${method}(${schema[key]})`;
    }
  }
  for (const [key, operator] of [["minLength", ">="], ["maxLength", "<="]]) {
    if (schema[key] !== undefined && schema.type === "string") {
      if (!Number.isInteger(schema[key]) || schema[key] < 0) throw new ContractError(location, `invalid ${key}`);
      result += `.refine(value => [...value].length ${operator} ${schema[key]})`;
    }
  }
  if (schema.pattern !== undefined && schema.type === "string") {
    if (typeof schema.pattern !== "string" || /\(\?P|\\[AZz]/.test(schema.pattern)) {
      throw new ContractError(location, "unsupported regular expression dialect");
    }
    try { new RegExp(schema.pattern, "u"); }
    catch (error) {
      if (!(error instanceof SyntaxError)) throw error;
      throw new ContractError(location, `unsupported regular expression: ${error.message}`);
    }
    result += `.regex(new RegExp(${JSON.stringify(schema.pattern)}, "u"))`;
  }
  for (const [key, operator] of [["minProperties", ">="], ["maxProperties", "<="]]) {
    if (schema[key] !== undefined && schema.type === "object") {
      if (!Number.isInteger(schema[key]) || schema[key] < 0) throw new ContractError(location, `invalid ${key}`);
      result += `.refine(value => Object.keys(value).length ${operator} ${schema[key]})`;
    }
  }
  return result;
}

export function stringExpression(format, location) {
  switch (format) {
    case undefined: return "z.string()";
    case "date-time": return "z.iso.datetime({ offset: true, local: true })";
    case "date": return "z.iso.date()";
    case "time": return "z.iso.time()";
    case "uuid": return "z.guid()";
    case "email": return "z.email()";
    case "uri": return "z.url()";
    case "ipv4": return "z.ipv4()";
    case "ipv6": return "z.ipv6()";
    default: throw new ContractError(location, `unsupported string format: ${format}`);
  }
}
