# codeclub CLI — Authentication & API Integration

## Overview

The `codeclub` CLI is a Rust binary that talks to clubrouter.com for smart LLM routing. It follows the same pattern as `gh` (GitHub CLI) — open-source client, commercial backend.

## Device Auth Flow

The CLI uses a device authorization flow (similar to `gh auth login`):

```
$ codeclub login
! Opening browser to https://clubrouter.com/auth/device
  Enter code: ABCD-1234
  
✓ Authenticated as matt@example.com
  API key saved to ~/.config/codeclub/config.toml
```

### Sequence

1. CLI calls `POST /api/cli/login` → gets `{device_code, user_code, verification_url, expires_in, interval}`
2. CLI opens browser to `verification_url` (or prints URL if no browser)
3. User authenticates via GitHub/Google OAuth on clubrouter.com and enters `user_code`
4. CLI polls `POST /api/cli/login/poll` with `{device_code}` every `interval` seconds
5. Once authorized → server returns `{api_key: "cr-...", user: {email, name, plan}}`
6. CLI saves to `~/.config/codeclub/config.toml`:
   ```toml
   [auth]
   api_key = "cr-abc123..."
   email = "matt@example.com"
   
   [server]
   base_url = "https://clubrouter.com"
   ```

### Poll Responses

| Status | HTTP | Body |
|--------|------|------|
| Pending | 202 | `{"status": "pending"}` |
| Authorized | 200 | `{"status": "authorized", "api_key": "cr-...", "user": {...}}` |
| Expired | 410 | `{"status": "expired"}` |
| Denied | 403 | `{"status": "denied"}` |

## CLI Commands

### `codeclub login`
Device auth flow as above.

### `codeclub status`
Calls `GET /api/cli/status` with API key → shows connection status, plan, savings.
```
$ codeclub status
✓ Connected to clubrouter.com
  User: matt@example.com
  Plan: Pro
  Savings: $12.34 (42% reduction)
  Requests: 1,234 this month
```

### `codeclub chat`
Interactive chat mode. Sends to `POST /api/chat/completions` with SSE streaming.

### `codeclub savings`
Shows savings summary. Calls `GET /api/savings/summary`.

### `codeclub task <description>`
Submits a dev loop task. Calls `POST /api/tasks`, then streams `GET /api/tasks/{id}/stream`.

### `codeclub proxy`
Starts a local proxy at `localhost:4111` that forwards to clubrouter.com.
Allows using any OpenAI-compatible client: `OPENAI_BASE_URL=http://localhost:4111/v1`

## API Key Format

- Prefix: `cr-` (clubrouter)
- Body: 32 hex characters (128 bits)
- Example: `cr-a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4`
- Storage: SHA-256 hash in `api_keys` table
- Scopes: `proxy` (default), `read` (dashboard), `admin` (settings)

## Required Clubrouter Endpoints

These endpoints must exist on the clubrouter backend:

```
POST   /api/cli/login          # Start device auth flow
POST   /api/cli/login/poll     # Poll for auth completion
GET    /api/cli/status         # Connection test + savings summary
POST   /api/auth/api-keys      # Create new API key
DELETE /api/auth/api-keys/:id  # Revoke API key
```

## Rust Crate Structure

```
cli/
├── Cargo.toml
└── src/
    ├── main.rs          # clap CLI definition
    ├── config.rs        # ~/.config/codeclub/config.toml read/write
    ├── auth.rs          # Device auth flow
    ├── client.rs        # HTTP client (reqwest) for clubrouter API
    ├── proxy.rs         # Local proxy server (localhost:4111)
    ├── chat.rs          # Interactive chat mode
    ├── task.rs          # Task submission + SSE streaming
    ├── savings.rs       # Savings display
    └── display.rs       # Terminal rendering (colors, spinners)
```

### Dependencies

```toml
[dependencies]
clap = { version = "4", features = ["derive"] }
reqwest = { version = "0.12", features = ["json", "stream"] }
tokio = { version = "1", features = ["full"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
toml = "0.8"
dirs = "5"
open = "5"              # open browser
indicatif = "0.17"      # progress bars/spinners
colored = "2"           # terminal colors
eventsource-stream = "0.2"  # SSE parsing
uuid = { version = "1", features = ["v4"] }
```

## Build & Install

```bash
cd cli
cargo build --release
# Binary at target/release/codeclub
# Or: cargo install --path .
```

Cross-compile for distribution:
```bash
cargo build --release --target x86_64-unknown-linux-gnu
cargo build --release --target x86_64-apple-darwin
cargo build --release --target aarch64-apple-darwin
cargo build --release --target x86_64-pc-windows-msvc
```
