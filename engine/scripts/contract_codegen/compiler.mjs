import { ContractError, enumMemberNames, record, referenceName, strings, isJsonValue } from "./document.mjs";
import { constraints, stringExpression, validateKeywords } from "./constraints.mjs";

export function compileDocument(document) {
  const emitted = new Map();
  const visiting = new Set();
  const json = JSON.stringify;

  function component(name) {
    if (emitted.has(name)) return;
    const exported = document.names.get(name);
    if (exported === undefined) throw new ContractError(name, "reference outside registered closure");
    if (visiting.has(name)) throw new ContractError(name, "unsupported recursive schema (only exact JSON recursion is supported)");
    visiting.add(name);
    const schema = document.schemas[name];
    const expression = isJsonValue(schema, name) ? "z.json()" : compile(schema, name);
    const values = schema.enum?.every(value => typeof value === "string")
      ? `export const ${exported}Values = ${json(schema.enum)} as const;\n` : "";
    const members = enumMemberNames(schema, name);
    const named = members === undefined ? "" : `export const ${exported} = { ${members.map((member, index) =>
      `${member === "__proto__" ? `[${json(member)}]` : member}: ${json(schema.enum[index])}`).join(", ")} } as const;\n`;
    emitted.set(name, `${values}${named}export const ${exported}Schema = ${expression};\nexport type ${exported} = z.infer<typeof ${exported}Schema>;\n`);
    visiting.delete(name);
  }

  function compile(input, location) {
    if (typeof input === "boolean") return input ? "z.json()" : "z.never()";
    const schema = record(input, location);
    validateKeywords(schema, location);
    const pieces = [];
    if (schema.$ref !== undefined) {
      const name = referenceName(schema.$ref, location);
      component(name);
      pieces.push(`${document.names.get(name)}Schema`);
    }
    for (const keyword of ["anyOf", "oneOf", "allOf"]) {
      if (schema[keyword] === undefined) continue;
      if (!Array.isArray(schema[keyword]) || schema[keyword].length === 0) {
        throw new ContractError(location, `${keyword} requires nonempty schemas`);
      }
      const alternatives = schema[keyword].map((child, index) => compile(child, `${location}.${keyword}[${index}]`));
      switch (keyword) {
        case "allOf": pieces.push(alternatives.reduce((left, right) => `z.intersection(${left}, ${right})`)); break;
        case "anyOf": pieces.push(alternatives.length === 1 ? alternatives[0] : `z.union([${alternatives.join(", ")}])`); break;
        case "oneOf": {
          const union = alternatives.length === 1 ? alternatives[0] : `z.union([${alternatives.join(", ")}])`;
          pieces.push(`${union}.refine(value => [${alternatives.join(", ")}].filter(schema => schema.safeParse(value).success).length === 1)`);
          break;
        }
        default: throw new ContractError(location, "unhandled union keyword");
      }
    }
    if (schema.const !== undefined || Object.hasOwn(schema, "const")) pieces.push(literal(schema.const, location));
    if (schema.enum !== undefined) {
      if (!Array.isArray(schema.enum)) throw new ContractError(location, "enum requires an array");
      const literals = schema.enum.map(value => literal(value, location));
      pieces.push(literals.length === 0 ? "z.never()" : literals.length === 1 ? literals[0] : `z.union([${literals.join(", ")}])`);
    }
    if (schema.type !== undefined) {
      const types = Array.isArray(schema.type) ? schema.type : [schema.type];
      if (types.length === 0) throw new ContractError(location, "type requires a nonempty union");
      const expressions = types.map(type => typed({ ...schema, type }, location));
      pieces.push(expressions.length === 1 ? expressions[0] : `z.union([${expressions.join(", ")}])`);
    }
    if (pieces.length === 0) return "z.json()";
    return pieces.reduce((left, right) => `z.intersection(${left}, ${right})`);
  }

  function literal(value, location) {
    if (value === null) return "z.null()";
    if (!["string", "number", "boolean"].includes(typeof value)) {
      throw new ContractError(location, "only primitive literal values are supported");
    }
    return `z.literal(${json(value)})`;
  }

  function typed(schema, location) {
    let expression;
    switch (schema.type) {
      case "string": expression = stringExpression(schema.format, location); break;
      case "integer": expression = "z.number()"; break;
      case "number": expression = "z.number()"; break;
      case "boolean": expression = "z.boolean()"; break;
      case "null": expression = "z.null()"; break;
      case "array": expression = `z.array(${compile(schema.items === undefined ? true : schema.items, `${location}.items`)})`; break;
      case "object": expression = object(schema, location); break;
      default: throw new ContractError(location, `unsupported type: ${schema.type}`);
    }
    expression = constraints(expression, schema, location);
    return schema.type === "integer" ? `${expression}.refine(Number.isInteger)` : expression;
  }

  function object(schema, location) {
    const properties = record(schema.properties === undefined ? {} : schema.properties, `${location}.properties`);
    const required = new Set(strings(schema.required === undefined ? [] : schema.required, `${location}.required`));
    const keys = [...new Set([...Object.keys(properties), ...required])].sort();
    const fields = keys.map(key => {
      const additional = schema.additionalProperties === undefined ? true : schema.additionalProperties;
      const expression = compile(Object.hasOwn(properties, key) ? properties[key] : additional, `${location}.${key}`);
      return `[${json(key)}]: ${expression}${required.has(key) ? "" : ".optional()"}`;
    });
    const shape = `{ ${fields.join(", ")} }`;
    switch (schema.additionalProperties) {
      case false: return `z.strictObject(${shape})`;
      case true: case undefined: return `z.looseObject(${shape})`;
      default: return `z.object(${shape}).catchall(${compile(schema.additionalProperties, `${location}.additionalProperties`)})`;
    }
  }

  for (const root of document.roots) component(root);
  if (emitted.size !== document.names.size) throw new ContractError("registry.schemas", "declared closure contains unreachable schemas");
  return 'import { z } from "zod";\n\n' + [...emitted.values()].join("\n");
}
