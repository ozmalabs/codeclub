/**
 * Thin client for clubrouter.com API.
 *
 * Reads API key from (in order):
 *   1. CLUBROUTER_API_KEY env var
 *   2. ~/.config/codeclub/config.toml (written by `codeclub login`)
 *
 * All routing intelligence lives on the server — this module
 * just forwards tool calls and returns the JSON response.
 */

import { readFileSync } from "fs";
import { join } from "path";
import { homedir } from "os";
import { request as httpsRequest } from "https";
import { request as httpRequest, IncomingMessage } from "http";

const DEFAULT_BASE_URL = "https://clubrouter.com";

// ── Config discovery ─────────────────────────────────────────────

function configDir(): string {
  if (process.platform === "win32") {
    return join(process.env.APPDATA || join(homedir(), "AppData", "Roaming"), "codeclub");
  }
  if (process.platform === "darwin") {
    return join(homedir(), "Library", "Application Support", "codeclub");
  }
  return join(process.env.XDG_CONFIG_HOME || join(homedir(), ".config"), "codeclub");
}

function readConfigFile(): { apiKey?: string; baseUrl?: string } {
  try {
    const raw = readFileSync(join(configDir(), "config.toml"), "utf-8");
    // Minimal TOML parsing — just need api_key and base_url
    const apiKey = raw.match(/api_key\s*=\s*"([^"]+)"/)?.[1];
    const baseUrl = raw.match(/base_url\s*=\s*"([^"]+)"/)?.[1];
    return { apiKey, baseUrl };
  } catch {
    return {};
  }
}

let _apiKey: string | undefined;
let _baseUrl: string | undefined;

function resolveConfig(): { apiKey: string | undefined; baseUrl: string } {
  if (_apiKey === undefined) {
    const env = process.env.CLUBROUTER_API_KEY;
    if (env) {
      _apiKey = env;
      _baseUrl = process.env.CLUBROUTER_URL || DEFAULT_BASE_URL;
    } else {
      const file = readConfigFile();
      _apiKey = file.apiKey || "";
      _baseUrl = file.baseUrl || DEFAULT_BASE_URL;
    }
  }
  return { apiKey: _apiKey || undefined, baseUrl: _baseUrl || DEFAULT_BASE_URL };
}

export function isConfigured(): boolean {
  const { apiKey } = resolveConfig();
  return !!apiKey;
}

// ── HTTP client ──────────────────────────────────────────────────

function post(url: string, body: string, headers: Record<string, string>): Promise<{ status: number; body: string }> {
  return new Promise((resolve, reject) => {
    const parsed = new URL(url);
    const fn = parsed.protocol === "https:" ? httpsRequest : httpRequest;

    const req = fn(
      url,
      {
        method: "POST",
        headers: {
          ...headers,
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body).toString(),
        },
      },
      (res: IncomingMessage) => {
        const chunks: Buffer[] = [];
        res.on("data", (c: Buffer) => chunks.push(c));
        res.on("end", () => {
          resolve({
            status: res.statusCode || 0,
            body: Buffer.concat(chunks).toString("utf-8"),
          });
        });
      },
    );

    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

// ── Public API ───────────────────────────────────────────────────

/**
 * Call a clubrouter tool endpoint.
 *
 * Maps tool names to REST endpoints:
 *   pick_model    → POST /api/v1/pick_model
 *   classify_task → POST /api/v1/classify
 *   estimate_cost → POST /api/v1/estimate
 */
export async function clubrouter(
  tool: string,
  args: Record<string, unknown>,
): Promise<unknown> {
  const { apiKey, baseUrl } = resolveConfig();
  if (!apiKey) {
    throw new Error("clubrouter not configured");
  }

  const endpoints: Record<string, string> = {
    pick_model: "/api/v1/pick_model",
    classify_task: "/api/v1/classify",
    estimate_cost: "/api/v1/estimate",
  };

  const path = endpoints[tool];
  if (!path) {
    throw new Error(`Unknown clubrouter tool: ${tool}`);
  }

  const url = `${baseUrl.replace(/\/$/, "")}${path}`;
  const resp = await post(url, JSON.stringify(args), {
    Authorization: `Bearer ${apiKey}`,
    "User-Agent": "codeclub-mcp/0.2.0",
  });

  if (resp.status >= 400) {
    const detail = (() => {
      try { return JSON.parse(resp.body); } catch { return resp.body; }
    })();
    throw new Error(`clubrouter ${resp.status}: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
  }

  return JSON.parse(resp.body);
}
