#!/usr/bin/env node
/** Fail when frontend failure handling has no diagnostic classification. */

import fs from "node:fs";
import path from "node:path";
import { API } from "../ui/node_modules/typescript/dist/api/sync/api.js";
import {
  isArrowFunction,
  isCallExpression,
  isCatchClause,
  isFunctionExpression,
  isPropertyAccessExpression,
} from "../ui/node_modules/typescript/dist/ast/is.js";

const root = path.resolve(import.meta.dirname, "..");
const sourceRoot = path.join(root, "ui", "src");
const classified = /log(?:Caught)?Diagnostic|diagnostic-expected|\bthrow\b/;
const failures = [];

function sourceFiles(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const candidate = path.join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(candidate);
    if (!/\.(?:ts|tsx)$/.test(entry.name) || /\.test\./.test(entry.name)) return [];
    return [candidate];
  });
}

function location(source, file, position) {
  const point = source.getLineAndCharacterOfPosition(position);
  return `${path.relative(root, file)}:${point.line + 1}:${point.character + 1}`;
}

const api = new API({ cwd: path.join(root, "ui") });
const snapshot = api.updateSnapshot({
  openProjects: [path.join(root, "ui", "tsconfig.app.json")],
});
const project = snapshot.getProjects()[0];

for (const file of sourceFiles(sourceRoot)) {
  const text = fs.readFileSync(file, "utf8");
  const source = project?.program.getSourceFile(file);
  if (!source) {
    failures.push(`${path.relative(root, file)}: source was absent from the TypeScript program`);
    continue;
  }

  function inspect(node) {
    if (isCatchClause(node)) {
      const body = text.slice(node.block.getStart(source), node.block.end);
      if (!classified.test(body)) {
        failures.push(`${location(source, file, node.getStart(source))}: unclassified catch clause`);
      }
    }

    if (
      isCallExpression(node)
      && isPropertyAccessExpression(node.expression)
      && node.expression.name.text === "catch"
    ) {
      const callback = node.arguments[0];
      if (!callback || (!isArrowFunction(callback) && !isFunctionExpression(callback))) {
        failures.push(`${location(source, file, node.getStart(source))}: unsupported Promise.catch handler`);
      } else {
        const body = text.slice(callback.body.getStart(source), callback.body.end);
        if (!classified.test(body)) {
          failures.push(`${location(source, file, node.getStart(source))}: unclassified Promise.catch handler`);
        }
      }
    }
    node.forEachChild(inspect);
  }

  inspect(source);
}

snapshot.dispose();
api.close();

if (failures.length) {
  console.error("Frontend diagnostic blind spots:\n" + failures.join("\n"));
  process.exitCode = 1;
} else {
  console.log("Frontend diagnostic audit: zero unclassified catch handlers.");
}
