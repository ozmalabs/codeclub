#!/usr/bin/env node
/**
 * codeclub MCP server
 *
 * Local: tree-sitter code compression (no dependencies, no API key).
 * Optional: task classification + model routing via clubrouter.com.
 *
 * Usage:
 *   npx @codeclub/mcp-server
 *
 * For routing features, set CLUBROUTER_API_KEY or configure via `codeclub login`.
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

import { compress } from "./compress.js";
import { clubrouter, isConfigured } from "./clubrouter.js";

const server = new Server(
  { name: "codeclub", version: "0.2.0" },
  { capabilities: { tools: {} } },
);

// ── Tool definitions ──────────────────────────────────────────────

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "compress_context",
      description:
        "Compress code/text to reduce tokens. Uses tree-sitter AST stubbing for " +
        "code (replaces function bodies with `...`, keeps signatures + docstrings) " +
        "and structural compression for prose. No API key needed.",
      inputSchema: {
        type: "object" as const,
        properties: {
          text: { type: "string", description: "The text or code to compress" },
          mode: {
            type: "string",
            enum: ["auto", "code", "prose"],
            description: "Compression mode (default: auto)",
          },
          filename: {
            type: "string",
            description: "Filename hint for language detection (e.g. 'app.py', 'index.tsx')",
          },
        },
        required: ["text"],
      },
    },
    {
      name: "pick_model",
      description:
        "Pick the best model for a task. Classifies difficulty/clarity, looks up " +
        "proficiency boundaries, returns cheapest capable model. " +
        "Requires clubrouter.com (set CLUBROUTER_API_KEY or run `codeclub login`).",
      inputSchema: {
        type: "object" as const,
        properties: {
          task: { type: "string", description: "The task description" },
          context_chars: { type: "integer", description: "Approximate context size in characters" },
          language: {
            type: "string",
            description: "Programming language (auto-detected if omitted)",
          },
          budget: {
            type: "string",
            enum: ["cheapest", "strongest"],
            description: "Routing preference (default: cheapest)",
          },
        },
        required: ["task"],
      },
    },
    {
      name: "classify_task",
      description:
        "Classify a coding/sysadmin/cloud task. Returns category, subcategory, " +
        "difficulty, clarity, confidence, and suggested profile. " +
        "Requires clubrouter.com (set CLUBROUTER_API_KEY or run `codeclub login`).",
      inputSchema: {
        type: "object" as const,
        properties: {
          task: { type: "string", description: "The task description to classify" },
        },
        required: ["task"],
      },
    },
    {
      name: "estimate_cost",
      description:
        "Estimate tokens, cost, and time for a task across available models. " +
        "Requires clubrouter.com (set CLUBROUTER_API_KEY or run `codeclub login`).",
      inputSchema: {
        type: "object" as const,
        properties: {
          task: { type: "string", description: "The task description to estimate" },
          difficulty: { type: "integer", description: "Override difficulty (0-100)" },
          clarity: { type: "integer", description: "Override clarity (0-100)" },
        },
        required: ["task"],
      },
    },
  ],
}));

// ── Tool handlers ─────────────────────────────────────────────────

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  try {
    switch (name) {
      case "compress_context":
        return await handleCompress(args as { text: string; mode?: string; filename?: string });
      case "pick_model":
        return await handlePickModel(args as { task: string; context_chars?: number; language?: string; budget?: string });
      case "classify_task":
        return await handleClassify(args as { task: string });
      case "estimate_cost":
        return await handleEstimate(args as { task: string; difficulty?: number; clarity?: number });
      default:
        return text(`Unknown tool: ${name}`);
    }
  } catch (err) {
    return text(`Error: ${err instanceof Error ? err.message : String(err)}`);
  }
});

// ── Helpers ───────────────────────────────────────────────────────

function text(content: string) {
  return { content: [{ type: "text" as const, text: content }] };
}

function json(obj: unknown) {
  return text(JSON.stringify(obj, null, 2));
}

const NOT_CONFIGURED = json({
  error: "clubrouter not configured",
  hint: "Set CLUBROUTER_API_KEY env var, or run `codeclub login` to authenticate.",
  docs: "https://clubrouter.com",
});

// ── Handlers ─────────────────────────────────────────────────────

async function handleCompress(args: { text: string; mode?: string; filename?: string }) {
  const result = await compress(args.text, args.mode || "auto", args.filename);
  return json({
    compressed: result.compressed,
    original_chars: result.originalChars,
    compressed_chars: result.compressedChars,
    approx_original_tokens: result.approxOriginalTokens,
    approx_compressed_tokens: result.approxCompressedTokens,
    savings_pct: result.savingsPct,
  });
}

async function handlePickModel(args: { task: string; context_chars?: number; language?: string; budget?: string }) {
  if (!isConfigured()) return NOT_CONFIGURED;
  return json(await clubrouter("pick_model", args));
}

async function handleClassify(args: { task: string }) {
  if (!isConfigured()) return NOT_CONFIGURED;
  return json(await clubrouter("classify_task", args));
}

async function handleEstimate(args: { task: string; difficulty?: number; clarity?: number }) {
  if (!isConfigured()) return NOT_CONFIGURED;
  return json(await clubrouter("estimate_cost", args));
}

// ── Main ──────────────────────────────────────────────────────────

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err) => {
  console.error("Fatal:", err);
  process.exit(1);
});
