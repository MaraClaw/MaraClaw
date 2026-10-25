export class ContractError extends Error {
  constructor(location, detail) {
    super(`${location}: ${detail}`);
    this.name = "ContractError";
    this.location = location;
  }
}

export function record(value, location) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new ContractError(location, "expected an object");
  }
  return value;
}

export function strings(value, location) {
  if (!Array.isArray(value) || value.some(item => typeof item !== "string")) {
    throw new ContractError(location, "expected an array of strings");
  }
  return value;
}

export function referenceName(value, location) {
  const prefix = "#/components/schemas/";
  if (typeof value !== "string" || !value.startsWith(prefix)) {
    throw new ContractError(location, "only local component schema references are supported");
  }
  return value.slice(prefix.length).replaceAll("~1", "/").replaceAll("~0", "~");
}

const reservedSymbols = new Set((
  "abstract accessor any as asserts async await bigint boolean break case catch class const constructor continue " +
  "debugger declare default delete do else enum eval export extends false finally for from function get global " +
  "if implements import in infer instanceof interface intrinsic is keyof let module namespace never new null " +
  "number object of out override package private protected public readonly require return satisfies set static " +
  "string super switch symbol this throw true try type typeof undefined unique unknown using var void while with yield arguments"
).split(" "));

function publicSymbol(name, location) {
  if (typeof name !== "string" || !/^[A-Za-z][A-Za-z0-9]*$/.test(name)) {
    throw new ContractError(location, "invalid public symbol; use an engine-owned semantic name");
  }
  if (reservedSymbols.has(name)) throw new ContractError(location, `reserved public symbol: ${name}`);
  return name;
}

export function enumMemberNames(schema, location) {
  if (!Object.hasOwn(schema, "x-enum-varnames")) return undefined;
  const names = strings(schema["x-enum-varnames"], `${location}.x-enum-varnames`);
  if (!Array.isArray(schema.enum) || schema.enum.length === 0 || schema.enum.some(value => typeof value !== "string")) {
    throw new ContractError(location, "enum member metadata requires a nonempty string enum");
  }
  if (names.length !== schema.enum.length) throw new ContractError(location, "enum member count differs from enum values");
  if (names.some(name => !/^[A-Za-z_$][A-Za-z0-9_$]*$/.test(name))) {
    throw new ContractError(location, "invalid enum member identifier");
  }
  if (new Set(names).size !== names.length) throw new ContractError(location, "duplicate enum member name");
  return names;
}

export function parseDocument(input) {
  const document = record(input, "document");
  const registry = record(document["x-maraclaw-contract"], "registry");
  if (registry.version !== 1) throw new ContractError("registry.version", "expected version 1");
  const schemas = record(record(document.components, "components").schemas, "components.schemas");
  const roots = strings(registry.roots, "registry.roots");
  const declared = strings(registry.schemas, "registry.schemas");
  const publicNames = Object.hasOwn(registry, "names") ? record(registry.names, "registry.names") : {};
  for (const name of Object.keys(publicNames)) {
    if (!declared.includes(name)) throw new ContractError(name, "public name outside registered closure");
  }
  const names = new Map();
  const owners = new Map([["z", "zod import"]]);
  for (const name of declared) {
    if (!Object.hasOwn(schemas, name)) throw new ContractError(name, "unresolved registered schema");
    const exported = publicSymbol(Object.hasOwn(publicNames, name) ? publicNames[name] : name, name);
    for (const symbol of [exported, `${exported}Schema`, `${exported}Values`]) {
      if (owners.has(symbol)) throw new ContractError(name, `export collision: ${symbol}`);
      owners.set(symbol, name);
    }
    names.set(name, exported);
  }
  for (const root of roots) {
    if (!names.has(root)) throw new ContractError(root, "root absent from registered closure");
  }
  return { schemas, roots: [...new Set(roots)].sort(), names };
}

export function isJsonValue(schema, name) {
  if (!schema || typeof schema !== "object" || Array.isArray(schema)) return false;
  if (Object.keys(schema).some(key => !["anyOf", "title"].includes(key))) return false;
  if (!Array.isArray(schema.anyOf)) return false;
  const ref = { $ref: `#/components/schemas/${name}` };
  const branches = [
    { type: "string" }, { type: "integer" }, { type: "number" }, { type: "boolean" },
    { type: "array", items: ref }, { type: "object", additionalProperties: ref }, { type: "null" },
  ];
  const canonical = value => {
    if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
    if (value !== null && typeof value === "object") {
      return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
    }
    return JSON.stringify(value);
  };
  return schema.anyOf.length === branches.length &&
    branches.every(branch => schema.anyOf.some(candidate => canonical(candidate) === canonical(branch)));
}
