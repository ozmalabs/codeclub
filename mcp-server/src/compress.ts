/**
 * Tree-sitter code compression — ported from codeclub/compress/tree.py.
 *
 * Replaces function bodies with `...` stubs, keeping signatures and docstrings.
 * Uses web-tree-sitter with WASM grammar binaries for language-agnostic parsing.
 * Supports Python, JavaScript/JSX, TypeScript/TSX, C#.
 */

import { Parser, Language, Node } from "web-tree-sitter";
import { createRequire } from "module";

const require = createRequire(import.meta.url);

// ── Types ────────────────────────────────────────────────────────

export type Lang = "python" | "javascript" | "typescript" | "csharp";

export interface StubEntry {
  name: string;
  origStart: number;
  origEnd: number;
  compStart: number;
  compEnd: number;
}

export interface SourceMap {
  language: Lang;
  originalCode: string;
  stubs: StubEntry[];
}

export interface CompressResult {
  compressed: string;
  originalChars: number;
  compressedChars: number;
  approxOriginalTokens: number;
  approxCompressedTokens: number;
  savingsPct: number;
  sourceMap: SourceMap;
}

// ── Parser initialisation (lazy, one-time) ───────────────────────

let parserReady = false;
const loadedLanguages = new Map<string, Language>();

async function ensureParser(): Promise<void> {
  if (parserReady) return;
  await Parser.init();
  parserReady = true;
}

async function getLanguage(lang: Lang): Promise<Language> {
  // TS uses JS grammar (same approach as Python version)
  const key = lang === "typescript" ? "javascript" : lang;
  if (loadedLanguages.has(key)) return loadedLanguages.get(key)!;

  let wasmPath: string;
  if (key === "python") {
    wasmPath = require.resolve("tree-sitter-python/tree-sitter-python.wasm");
  } else if (key === "javascript") {
    wasmPath = require.resolve("tree-sitter-javascript/tree-sitter-javascript.wasm");
  } else if (key === "csharp") {
    wasmPath = require.resolve("tree-sitter-c-sharp/tree-sitter-c_sharp.wasm");
  } else {
    wasmPath = require.resolve("tree-sitter-javascript/tree-sitter-javascript.wasm");
  }

  const language = await Language.load(wasmPath);
  loadedLanguages.set(key, language);
  return language;
}

// ── Language detection ────────────────────────────────────────────

export function detectLanguage(filename: string): Lang {
  const ext = filename.includes(".") ? filename.split(".").pop()!.toLowerCase() : "";
  if (["js", "jsx", "mjs", "cjs"].includes(ext)) return "javascript";
  if (["ts", "tsx", "mts", "cts"].includes(ext)) return "typescript";
  if (ext === "cs") return "csharp";
  return "python";
}

export function detectLanguageFromContent(code: string): Lang {
  if (/^\s*(?:def |class |import |from .+ import |async def )/m.test(code)) return "python";
  if (/^\s*(?:using |namespace |public class )/m.test(code)) return "csharp";
  return "javascript";
}

// ── Stub collection per language ─────────────────────────────────

type StubInfo = [fnStart: number, fnEnd: number, bodyStart: number, name: string];

function collectPythonStubs(root: Node, lines: string[]): StubInfo[] {
  const results: StubInfo[] = [];
  const seen = new Set<string>();

  function walk(node: Node) {
    if (node.type === "function_definition" || node.type === "decorated_definition") {
      let actual = node;
      if (node.type === "decorated_definition") {
        for (const child of node.children) {
          if (child.type === "function_definition") {
            actual = child;
            break;
          }
        }
      }

      if (actual.type === "function_definition") {
        const fnStart = node.startPosition.row;
        const fnEnd = node.endPosition.row;
        const key = `${fnStart}:${fnEnd}`;

        if (!seen.has(key)) {
          seen.add(key);

          let name = "<anonymous>";
          let bodyStart = fnEnd;
          for (const child of actual.children) {
            if (child.type === "identifier") name = child.text;
            if (child.type === "block") bodyStart = child.startPosition.row;
          }

          // Only stub if body has real content
          const bodyLines = lines.slice(bodyStart, fnEnd + 1);
          const nonTrivial = bodyLines.some((l) => {
            const t = l.trim();
            return t !== "" && t !== "..." && t !== "pass" &&
              !t.startsWith('"""') && !t.startsWith("'''");
          });
          if (nonTrivial) {
            results.push([fnStart, fnEnd, bodyStart, name]);
          }
        }
      }
    }

    for (const child of node.children) {
      walk(child);
    }
  }

  walk(root);
  return results;
}

function collectJsStubs(root: Node, lines: string[]): StubInfo[] {
  const results: StubInfo[] = [];
  const seen = new Set<string>();

  function nodeName(node: Node, nameType: string): string {
    for (const child of node.children) {
      if (child.type === nameType) return child.text;
    }
    return "<anonymous>";
  }

  function bodyNode(node: Node, bodyType: string): Node | null {
    for (const child of node.children) {
      if (child.type === bodyType) return child;
    }
    return null;
  }

  function arrowBody(node: Node): Node | null {
    for (const child of node.children) {
      if (child.type === "statement_block" || child.type === "parenthesized_expression") {
        return child;
      }
    }
    return null;
  }

  function walk(node: Node) {
    let handled = false;

    if (node.type === "function_declaration") {
      const name = nodeName(node, "identifier");
      const body = bodyNode(node, "statement_block");
      if (body && node.endPosition.row > node.startPosition.row) {
        const key = `${node.startPosition.row}:${node.endPosition.row}`;
        if (!seen.has(key)) {
          seen.add(key);
          results.push([node.startPosition.row, node.endPosition.row, body.startPosition.row, name]);
          handled = true;
        }
      }
    } else if (node.type === "method_definition") {
      const name = nodeName(node, "property_identifier");
      const body = bodyNode(node, "statement_block");
      if (body && node.endPosition.row > node.startPosition.row) {
        const key = `${node.startPosition.row}:${node.endPosition.row}`;
        if (!seen.has(key)) {
          seen.add(key);
          results.push([node.startPosition.row, node.endPosition.row, body.startPosition.row, name]);
          handled = true;
        }
      }
    } else if (node.type === "lexical_declaration" || node.type === "variable_declaration") {
      for (const decl of node.children) {
        if (decl.type === "variable_declarator") {
          let nameNode: Node | null = null;
          let arrowFn: Node | null = null;
          for (const child of decl.children) {
            if (child.type === "identifier") nameNode = child;
            if (child.type === "arrow_function" || child.type === "function") arrowFn = child;
          }
          if (nameNode && arrowFn) {
            const body = arrowBody(arrowFn);
            if (body && arrowFn.endPosition.row > arrowFn.startPosition.row) {
              const key = `${node.startPosition.row}:${node.endPosition.row}`;
              if (!seen.has(key)) {
                seen.add(key);
                results.push([node.startPosition.row, node.endPosition.row, body.startPosition.row, nameNode.text]);
              }
            }
          }
        }
      }
    }

    if (!handled) {
      for (const child of node.children) {
        walk(child);
      }
    }
  }

  walk(root);
  return results;
}

function collectCsharpStubs(root: Node, lines: string[]): StubInfo[] {
  const results: StubInfo[] = [];
  const seen = new Set<string>();

  function walk(node: Node) {
    if (["method_declaration", "constructor_declaration",
         "operator_declaration", "conversion_operator_declaration"].includes(node.type)) {
      let name = "<anonymous>";
      let body: Node | null = null;
      for (const child of node.children) {
        if (child.type === "identifier") name = child.text;
        if (child.type === "block") body = child;
      }
      if (body && node.endPosition.row > node.startPosition.row) {
        const key = `${node.startPosition.row}:${node.endPosition.row}`;
        if (!seen.has(key)) {
          seen.add(key);
          results.push([node.startPosition.row, node.endPosition.row, body.startPosition.row, name]);
          return;
        }
      }
    }

    if (node.type === "property_declaration") {
      let name = "<property>";
      for (const child of node.children) {
        if (child.type === "identifier") name = child.text;
        if (child.type === "accessor_list") {
          for (const acc of child.children) {
            if (acc.type === "accessor_declaration") {
              let accBody: Node | null = null;
              for (const c of acc.children) {
                if (c.type === "block") accBody = c;
              }
              if (accBody && acc.endPosition.row > acc.startPosition.row) {
                const key = `${acc.startPosition.row}:${acc.endPosition.row}`;
                if (!seen.has(key)) {
                  seen.add(key);
                  results.push([acc.startPosition.row, acc.endPosition.row, accBody.startPosition.row, name]);
                }
              }
            }
          }
        }
      }
    }

    for (const child of node.children) {
      walk(child);
    }
  }

  walk(root);
  return results;
}

// ── Python docstring extraction ──────────────────────────────────

function extractPythonDocstring(lines: string[], bodyStart: number, maxLen: number): string {
  if (bodyStart >= lines.length) return "";
  const first = lines[bodyStart].trim();
  if (first.startsWith('"""') || first.startsWith("'''")) {
    const quote = first.slice(0, 3);
    const rest = first.slice(3);
    const closeIdx = rest.indexOf(quote);
    if (closeIdx > 0) {
      const doc = rest.slice(0, closeIdx).trim();
      if (doc.length <= maxLen) {
        const indent = lines[bodyStart].length - lines[bodyStart].trimStart().length;
        return `\n${" ".repeat(indent)}${quote}${doc}${quote}`;
      }
    }
  }
  return "";
}

// ── Main stub_functions ──────────────────────────────────────────

export async function stubFunctions(
  code: string,
  language: Lang,
  options: { keepDocstrings?: boolean; maxDocLen?: number } = {},
): Promise<{ compressed: string; sourceMap: SourceMap }> {
  const { keepDocstrings = true, maxDocLen = 120 } = options;
  const sourceMap: SourceMap = { language, originalCode: code, stubs: [] };

  await ensureParser();
  const lang = await getLanguage(language);
  const parser = new Parser();
  parser.setLanguage(lang);

  const tree = parser.parse(code);
  if (!tree) {
    return { compressed: code, sourceMap };
  }

  // Split into lines preserving newlines
  const lines = code.split("\n").map((l, i, arr) =>
    i < arr.length - 1 ? l + "\n" : l
  );
  if (lines.length > 0 && !lines[lines.length - 1].endsWith("\n") && code.endsWith("\n")) {
    lines[lines.length - 1] += "\n";
  }

  // Collect stubs
  let stubInfos: StubInfo[];
  if (language === "python") {
    stubInfos = collectPythonStubs(tree.rootNode, lines);
  } else if (language === "csharp") {
    stubInfos = collectCsharpStubs(tree.rootNode, lines);
  } else {
    stubInfos = collectJsStubs(tree.rootNode, lines);
  }

  if (stubInfos.length === 0) {
    return { compressed: code, sourceMap };
  }

  // Build replacements
  const replacements: Array<{
    fnStart: number; fnEnd: number; stubText: string; name: string;
  }> = [];

  for (const [fnStart, fnEnd, bodyStart, name] of stubInfos) {
    let stubText: string;

    if (language === "python") {
      const sigLines = lines.slice(fnStart, bodyStart);
      const sigText = sigLines.join("").trimEnd();
      let docText = "";
      if (keepDocstrings) {
        docText = extractPythonDocstring(lines, bodyStart, maxDocLen);
      }
      let stubIndent = "    ";
      if (bodyStart < lines.length) {
        const raw = lines[bodyStart];
        stubIndent = " ".repeat(raw.length - raw.trimStart().length);
      }
      stubText = sigText + docText + `\n${stubIndent}...`;
    } else {
      const sigEndRow = bodyStart;
      const sigLines = lines.slice(fnStart, sigEndRow + 1);
      const sigText = sigLines.join("").trimEnd();
      let stubIndent = "  ";
      const firstBodyContent = sigEndRow + 1;
      if (firstBodyContent < lines.length) {
        const raw = lines[firstBodyContent];
        stubIndent = raw.trim() ? " ".repeat(raw.length - raw.trimStart().length) : "  ";
      }
      const closing = fnEnd < lines.length ? lines[fnEnd].trimEnd() : "}";
      stubText = sigText + `\n${stubIndent}...\n${closing}`;
    }

    replacements.push({ fnStart, fnEnd, stubText, name });
  }

  // Apply replacements bottom-up
  replacements.sort((a, b) => b.fnStart - a.fnStart);
  const resultLines = [...lines];
  for (const { fnStart, fnEnd, stubText } of replacements) {
    const stubLines = stubText.split("\n").map((l, i, arr) =>
      i < arr.length - 1 ? l + "\n" : l
    );
    if (stubLines.length > 0 && !stubLines[stubLines.length - 1].endsWith("\n")) {
      stubLines[stubLines.length - 1] += "\n";
    }
    resultLines.splice(fnStart, fnEnd - fnStart + 1, ...stubLines);
  }

  const compressed = resultLines.join("");

  // Build source map
  const forwardSorted = [...replacements].sort((a, b) => a.fnStart - b.fnStart);
  let runningDelta = 0;
  for (const { fnStart, fnEnd, stubText, name } of forwardSorted) {
    const origSpan = fnEnd - fnStart + 1;
    const stubLineCount = stubText.split("\n").length;
    const compStart = fnStart + runningDelta;
    const compEnd = compStart + stubLineCount - 1;

    sourceMap.stubs.push({
      name,
      origStart: fnStart,
      origEnd: fnEnd,
      compStart: Math.max(0, compStart),
      compEnd: Math.max(0, compEnd),
    });

    runningDelta += stubLineCount - origSpan;
  }

  return { compressed, sourceMap };
}

// ── Public compress API ──────────────────────────────────────────

export async function compress(
  text: string,
  mode: string = "auto",
  filename?: string,
): Promise<CompressResult> {
  let compressed: string;
  let sourceMap: SourceMap;

  if (mode === "prose") {
    compressed = text.replace(/\n{3,}/g, "\n\n").replace(/[ \t]+/g, " ");
    sourceMap = { language: "python", originalCode: text, stubs: [] };
  } else {
    const lang = filename ? detectLanguage(filename) : detectLanguageFromContent(text);
    const result = await stubFunctions(text, lang);
    compressed = result.compressed;
    sourceMap = result.sourceMap;
  }

  const origTokens = Math.ceil(text.length / 4);
  const compTokens = Math.ceil(compressed.length / 4);
  const savings = origTokens > 0 ? (1 - compTokens / origTokens) * 100 : 0;

  return {
    compressed,
    originalChars: text.length,
    compressedChars: compressed.length,
    approxOriginalTokens: origTokens,
    approxCompressedTokens: compTokens,
    savingsPct: Math.round(savings * 10) / 10,
    sourceMap,
  };
}
