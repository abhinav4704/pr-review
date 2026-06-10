#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");

function loadTypeScript() {
  try {
    return require("typescript");
  } catch (_e1) {
    // Best-effort fallback: if the caller has NODE_PATH or cwd-scoped node_modules,
    // createRequire from cwd can resolve it.
    try {
      const { createRequire } = require("module");
      const req = createRequire(path.join(process.cwd(), "package.json"));
      return req("typescript");
    } catch (_e2) {
      process.stderr.write("Cannot resolve 'typescript'. Install it where this command runs.\n");
      process.exit(2);
    }
  }
}

const ts = loadTypeScript();
const relpath = process.argv[2] || "unknown.ts";
const source = fs.readFileSync(0, "utf8");

function scriptKindFor(file) {
  const ext = path.extname(file).toLowerCase();
  if (ext === ".tsx") return ts.ScriptKind.TSX;
  if (ext === ".jsx") return ts.ScriptKind.JSX;
  if (ext === ".ts") return ts.ScriptKind.TS;
  return ts.ScriptKind.JS;
}

const sf = ts.createSourceFile(
  relpath,
  source,
  ts.ScriptTarget.Latest,
  true,
  scriptKindFor(relpath),
);

function lineOfPos(pos) {
  return sf.getLineAndCharacterOfPosition(pos).line + 1;
}

function lineSpan(node) {
  return {
    line_start: lineOfPos(node.getStart(sf)),
    line_end: lineOfPos(node.getEnd()),
  };
}

function isUpperName(name) {
  return /^[A-Z][A-Za-z0-9_]*$/.test(name || "");
}

function stripWrappingQuotes(text) {
  if (!text || text.length < 2) return text || "";
  const a = text[0];
  const b = text[text.length - 1];
  if ((a === "'" && b === "'") || (a === '"' && b === '"') || (a === "`" && b === "`")) {
    return text.slice(1, -1);
  }
  return text;
}

function literalToText(expr) {
  if (!expr) return "";
  if (ts.isStringLiteral(expr) || ts.isNoSubstitutionTemplateLiteral(expr)) {
    return expr.text || "";
  }
  if (ts.isTemplateExpression(expr)) {
    let out = expr.head?.text || "";
    for (const s of expr.templateSpans || []) {
      out += "${expr}";
      out += s.literal?.text || "";
    }
    return out;
  }
  return stripWrappingQuotes(expr.getText(sf));
}

function getPropName(nameNode) {
  if (!nameNode) return "";
  if (ts.isIdentifier(nameNode) || ts.isStringLiteral(nameNode) || ts.isNumericLiteral(nameNode)) {
    return nameNode.text || "";
  }
  return nameNode.getText(sf);
}

function hasJsxWithin(node) {
  let found = false;
  const visit = (n) => {
    if (found) return;
    if (ts.isJsxElement(n) || ts.isJsxSelfClosingElement(n) || ts.isJsxFragment(n)) {
      found = true;
      return;
    }
    ts.forEachChild(n, visit);
  };
  visit(node);
  return found;
}

function collectJsxTagNames(node) {
  const out = new Set();
  const visit = (n) => {
    if (ts.isJsxSelfClosingElement(n)) {
      const t = n.tagName.getText(sf);
      if (isUpperName(t)) out.add(t);
    } else if (ts.isJsxElement(n)) {
      const t = n.openingElement.tagName.getText(sf);
      if (isUpperName(t)) out.add(t);
    }
    ts.forEachChild(n, visit);
  };
  visit(node);
  return out;
}

function methodFromFetchOptions(arg) {
  if (!arg || !ts.isObjectLiteralExpression(arg)) return "GET";
  for (const p of arg.properties) {
    if (!ts.isPropertyAssignment(p)) continue;
    const key = getPropName(p.name).toLowerCase();
    if (key !== "method") continue;
    const v = p.initializer;
    const txt = literalToText(v).trim();
    return (txt || "GET").toUpperCase();
  }
  return "GET";
}

const components = [];
const componentBodies = new Map();
const calls = [];

function recordComponent(name, node, bodyNode) {
  if (!isUpperName(name)) return;
  if (!hasJsxWithin(bodyNode || node)) return;
  const span = lineSpan(node);
  components.push({ name, line_start: span.line_start, line_end: span.line_end });
  componentBodies.set(name, bodyNode || node);
}

function scanCall(node) {
  const expr = node.expression;

  // fetch(url, options)
  if (ts.isIdentifier(expr) && expr.text === "fetch") {
    const args = node.arguments || [];
    const raw = literalToText(args[0]);
    if (raw) {
      const method = methodFromFetchOptions(args[1]);
      const span = lineSpan(node);
      calls.push({
        kind: "fetch",
        receiver: "fetch",
        method,
        raw,
        line_start: span.line_start,
        line_end: span.line_end,
      });
    }
    return;
  }

  // axios-like: client.get(url)
  if (ts.isPropertyAccessExpression(expr)) {
    const method = expr.name?.text || "";
    const allowed = new Set(["get", "post", "put", "patch", "delete"]);
    if (!allowed.has(method)) return;
    const receiver = expr.expression?.getText(sf) || "";
    const raw = literalToText((node.arguments || [])[0]);
    if (!raw) return;
    const span = lineSpan(node);
    calls.push({
      kind: "axios",
      receiver,
      method: method.toUpperCase(),
      raw,
      line_start: span.line_start,
      line_end: span.line_end,
    });
  }
}

function visit(node) {
  if (ts.isFunctionDeclaration(node) && node.name) {
    recordComponent(node.name.text, node, node.body || node);
  } else if (ts.isVariableStatement(node)) {
    for (const d of node.declarationList.declarations) {
      if (!ts.isIdentifier(d.name) || !d.initializer) continue;
      const nm = d.name.text;
      if (ts.isArrowFunction(d.initializer) || ts.isFunctionExpression(d.initializer)) {
        recordComponent(nm, d, d.initializer.body || d.initializer);
      }
    }
  } else if (ts.isClassDeclaration(node) && node.name) {
    const clsName = node.name.text;
    let isReactClass = false;
    for (const hc of node.heritageClauses || []) {
      for (const t of hc.types || []) {
        const txt = t.expression.getText(sf);
        if (txt === "Component" || txt === "PureComponent" || txt.endsWith(".Component") || txt.endsWith(".PureComponent")) {
          isReactClass = true;
        }
      }
    }
    if (isReactClass) recordComponent(clsName, node, node);
  }

  if (ts.isCallExpression(node)) scanCall(node);

  ts.forEachChild(node, visit);
}

visit(sf);

const componentSet = new Set(components.map((c) => c.name));
const renders = [];
for (const c of components) {
  const body = componentBodies.get(c.name);
  if (!body) continue;
  const tags = collectJsxTagNames(body);
  for (const child of tags) {
    if (child === c.name) continue;
    if (!componentSet.has(child)) continue;
    renders.push({ parent: c.name, child });
  }
}

process.stdout.write(JSON.stringify({ calls, components, renders }));
