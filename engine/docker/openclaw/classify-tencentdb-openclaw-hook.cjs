"use strict";

const fs = require("node:fs");

function isNode(value, type) {
    return value !== null && typeof value === "object" && value.type === type;
}

function isIdentifier(node, name) {
    return isNode(node, "Identifier") && node.name === name;
}

function isHookName(node) {
    return isIdentifier(node, "hookEvent") || isIdentifier(node, "hook_event");
}

function isMarker(node) {
    if (node === null || typeof node !== "object") {
        return false;
    }
    if (node.type === "FunctionDeclaration" || node.type === "FunctionExpression" || node.type === "ArrowFunctionExpression" || node.type === "ClassDeclaration" || node.type === "ClassExpression") {
        return false;
    }
    if (isIdentifier(node, "after_tool_call") || (node.type === "Literal" && node.value === "after_tool_call")) {
        return true;
    }
    return Object.values(node).some((value) => {
        if (Array.isArray(value)) {
            return value.some(isMarker);
        }
        return isMarker(value);
    });
}

function directBindings(statement) {
    if (isNode(statement, "VariableDeclaration")) {
        return statement.declarations.flatMap((declaration) =>
            isHookName(declaration.id) && isNode(declaration.init, "ObjectExpression") ? [declaration.init] : []
        );
    }
    const expression = isNode(statement, "ExpressionStatement") ? statement.expression : null;
    return isNode(expression, "AssignmentExpression") && expression.operator === "=" && isHookName(expression.left) && isNode(expression.right, "ObjectExpression")
        ? [expression.right]
        : [];
}

function containsHookRunner(node) {
    if (node === null || typeof node !== "object") {
        return false;
    }
    if (isNode(node, "Identifier") && node.name.includes("hookRunner")) {
        return true;
    }
    return Object.values(node).some((value) => {
        if (Array.isArray(value)) {
            return value.some(containsHookRunner);
        }
        return containsHookRunner(value);
    });
}

function unwrapCatchCall(expression) {
    if (
        isNode(expression, "CallExpression")
        && isNode(expression.callee, "MemberExpression")
        && !expression.callee.computed
        && !expression.callee.optional
        && isIdentifier(expression.callee.property, "catch")
    ) {
        return expression.callee.object;
    }
    return expression;
}

function isRunAfterToolCall(expression) {
    return (
        isNode(expression, "CallExpression")
        && !expression.optional
        && isNode(expression.callee, "MemberExpression")
        && !expression.callee.computed
        && !expression.callee.optional
        && isIdentifier(expression.callee.property, "runAfterToolCall")
        && containsHookRunner(expression.callee.object)
    );
}

function isRunner(statement) {
    if (!isNode(statement, "ExpressionStatement")) {
        return false;
    }
    let expression = isNode(statement.expression, "AwaitExpression") ? statement.expression.argument : statement.expression;
    expression = unwrapCatchCall(expression);
    if (isNode(expression, "CallExpression") && !expression.optional && expression.arguments.length === 0 && isIdentifier(expression.callee, "hookRunnerAfter")) {
        return true;
    }
    return isRunAfterToolCall(expression);
}

function hasPropertyName(property, name) {
    return isNode(property, "Property") && (isIdentifier(property.key, name) || (isNode(property.key, "Literal") && property.key.value === name));
}

function isDurationProperty(property) {
    return hasPropertyName(property, "durationMs") && isIdentifier(property.key, "durationMs") && !property.computed && !property.method && property.kind === "init";
}

function isExactMessagesValue(node) {
    if (!isNode(node, "ChainExpression") || !isNode(node.expression, "MemberExpression")) {
        return false;
    }
    const finalAccess = node.expression;
    if (!finalAccess.optional || finalAccess.computed || !isIdentifier(finalAccess.property, "messages") || !isNode(finalAccess.object, "MemberExpression")) {
        return false;
    }
    const sessionAccess = finalAccess.object;
    if (sessionAccess.optional || sessionAccess.computed || !isIdentifier(sessionAccess.property, "session") || !isNode(sessionAccess.object, "MemberExpression")) {
        return false;
    }
    const paramsAccess = sessionAccess.object;
    return !paramsAccess.optional && !paramsAccess.computed && isIdentifier(paramsAccess.property, "params") && isIdentifier(paramsAccess.object, "ctx");
}

function isMessagesProperty(property) {
    return hasPropertyName(property, "messages") && isIdentifier(property.key, "messages") && !property.computed && !property.method && property.kind === "init" && !property.shorthand && isExactMessagesValue(property.value);
}

function classifyObject(object) {
    let durationCount = 0;
    const messages = [];
    for (const property of object.properties) {
        if (isNode(property, "SpreadElement")) {
            throw new Error("unsupported spread");
        }
        if (hasPropertyName(property, "durationMs")) {
            if (!isDurationProperty(property)) {
                throw new Error("unsupported duration");
            }
            durationCount += 1;
        }
        if (hasPropertyName(property, "messages")) {
            messages.push(property);
        }
    }
    if (durationCount !== 1) {
        throw new Error("unsupported duration");
    }
    if (messages.length === 0) {
        return "unpatched";
    }
    if (messages.length !== 1 || !isMessagesProperty(messages[0])) {
        throw new Error("unsupported messages");
    }
    return "prepatched";
}

function isHookGatedBlock(node, parent) {
    return isNode(node, "BlockStatement") && isNode(parent, "IfStatement") && parent.consequent === node && isMarker(parent.test);
}

function collectStatementLists(node, lists, parent = null) {
    if (node === null || typeof node !== "object") {
        return;
    }
    if (node.type === "Program" || node.type === "BlockStatement" || node.type === "StaticBlock") {
        lists.push({ statements: node.body, seedMarker: isHookGatedBlock(node, parent) });
    } else if (node.type === "SwitchCase") {
        lists.push({ statements: node.consequent, seedMarker: false });
    }
    for (const value of Object.values(node)) {
        if (Array.isArray(value)) {
            value.forEach((item) => {
                collectStatementLists(item, lists, node);
            });
        } else {
            collectStatementLists(value, lists, node);
        }
    }
}

function classify(ast) {
    const lists = [];
    collectStatementLists(ast, lists);
    const associations = [];
    for (const { statements, seedMarker } of lists) {
        let markerSeen = seedMarker;
        for (let index = 0; index < statements.length; index += 1) {
            const bindings = markerSeen && isRunner(statements[index + 1]) ? directBindings(statements[index]) : [];
            for (const binding of bindings) {
                associations.push(classifyObject(binding));
            }
            markerSeen ||= isMarker(statements[index]);
        }
    }
    if (associations.length !== 1) {
        throw new Error("ambiguous association");
    }
    return associations[0];
}

// The CLI boundary intentionally converts all parse and classification errors to silent failure.
try {
    if (process.argv.length !== 3) {
        throw new Error("invalid arguments");
    }
    const acorn = require("internal/deps/acorn/acorn/dist/acorn");
    const source = fs.readFileSync(process.argv[2], "utf8");
    const ast = acorn.parse(source, { ecmaVersion: "latest", sourceType: "module", allowHashBang: true });
    process.stdout.write(`${classify(ast)}\n`);
} catch {
    process.exitCode = 1;
}
