# gogcli OAuth Handoff

This note explains how a website client works with the MaraClaw backend to start gogcli authentication for a locally managed OpenClaw agent and where the credentials are stored.

## One-Sentence Model

The website does not store Google credentials. It asks the backend to run `gog` inside the agent's already-running local OpenClaw container, receives a Google OAuth URL, shows that URL to the human, and later polls whether gogcli has stored credentials in the agent's container-mounted gogcli home. After an authenticated status check, the backend snapshots the gogcli keyring password and `GOG_HOME/data` into encrypted DB state so later local container restarts can restore that state before Docker env and mounts are calculated.

## Actors

- Website client: calls MaraClaw backend APIs and displays the OAuth URL.
- MaraClaw backend: checks permissions, stores the gogcli file-keyring password, snapshots encrypted gogcli credential state, restores that state before local container startup, and runs Docker exec.
- Local OpenClaw container: contains `gogcli` and runs the actual `gog auth` commands.
- Google OAuth: authenticates the human and collects consent.

The backend routes are under:

```text
/api/agents/{agent_id}/gogcli
```

The website-facing endpoints are:

```text
POST /api/agents/{agent_id}/gogcli/keyring-secret
POST /api/agents/{agent_id}/gogcli/auth/start
GET  /api/agents/{agent_id}/gogcli/auth/status
```

## Slow-Motion Flow

### 1. Create or use a gogcli-enabled local agent

The agent must have `gogcli_enabled=true`.

When creating an agent, the client can include:

```json
{
  "gogcli_enabled": true
}
```

That value is stored on `Agent.gogcli_enabled` and appears in `AgentOut` responses.

If the agent is not gogcli-enabled, the gogcli routes reject the request with:

```text
400 gogcli is not enabled for this agent
```

The caller must also have `manage` access to the agent, or be a `platform_admin` or `org_admin`.

### 2. The backend starts the local OpenClaw container

For locally managed agents, background setup eventually starts an OpenClaw container through `agent_manager.start_container(...)`.

Before the Docker env and mount list are computed, the backend restores any encrypted gogcli DB state for that agent. Restore writes the backend-local keyring password file and recreates the agent's `gogcli/data` directory from the encrypted snapshot. If the stored snapshot is corrupt or cannot be restored safely, the state is marked `needs_reauth` and the container still starts without restored gogcli credentials.

If global `GOGCLI_ENABLED` is true and the specific agent has `gogcli_enabled=true`, the container receives:

```text
GOG_HOME=/home/node/.openclaw/gogcli
GOG_KEYRING_BACKEND=file
```

If the backend-local keyring password file exists when the container starts, the backend also mounts it into the container read-only and sets:

```text
GOG_KEYRING_PASSWORD_FILE=/run/secrets/gogcli_keyring_password
```

Important: the password value itself is not placed in Docker environment metadata. Only the path to the mounted file is exposed.

gogcli's file backend reads the upstream-required `GOG_KEYRING_PASSWORD` value. MaraClaw supplies that value only at process launch time: `validate-gogcli.sh` and backend Docker exec handoffs read `GOG_KEYRING_PASSWORD_FILE`, export `GOG_KEYRING_PASSWORD` inside the launched process, and then `exec` the target command. The password is not passed as Docker metadata or as a command argument.

gogcli v0.34.0 macOS artifacts are signed and notarized locally by OpenClaw Foundation with its Developer ID and retain the `com.steipete.gogcli.gog` identifier. On native macOS, gogcli retains macOS Keychain trust; operators can override the trusted application path with `GOG_KEYCHAIN_TRUST_APPLICATION`. MaraClaw's OpenClaw Docker runtime is Linux arm64 and still uses `GOG_KEYRING_BACKEND=file` plus optional `GOG_KEYRING_PASSWORD_FILE`, so the Docker/Linux file-keyring runtime remains unchanged.

### 3. Store or rotate the gogcli file-keyring password

The website calls:

```http
POST /api/agents/{agent_id}/gogcli/keyring-secret
```

Request body:

```json
{
  "password": "some-local-keyring-password"
}
```

The backend stores this password in a backend-local per-agent file:

```text
<STORAGE_LOCAL_ROOT or AGENT_DATA_DIR>/_gogcli_secrets/{agent_id}/keyring_password
```

The response is:

```text
204 No Content
```

This password is not a Google password. It is the password used by gogcli's file keyring backend.

The backend also stores an encrypted copy of this keyring password in `gogcli_credential_states.encrypted_keyring_password`. The plaintext password is not returned in API responses and is not placed in Docker environment metadata.

Operational note: Docker mounts are established when the container starts. After creating or rotating this keyring secret, restarting the agent container is the safest way to ensure the container sees the correct mounted password file.

### 4. Ask the backend to start OAuth

The website calls:

```http
POST /api/agents/{agent_id}/gogcli/auth/start
```

Request body:

```json
{
  "account_email": "user@example.com"
}
```

The backend verifies:

- the caller is authenticated;
- the caller can manage the agent, or is an allowed admin role;
- `agent.gogcli_enabled` is true;
- `agent.container_id` exists, meaning there is already a running local container.

Then the backend executes this argv list inside the running container:

```text
gog auth add user@example.com --services all-user --remote --step 1 --plain --no-input
```

This is run through Docker exec via python-on-whales. It is not a shell string.

### 5. Return only the Google OAuth URL

gogcli prints output. The backend extracts only a Google OAuth consent URL shaped like:

```text
https://accounts.google.com/o/oauth2/v2/auth?...
```

The successful API response is:

```json
{
  "auth_url": "https://accounts.google.com/o/oauth2/v2/auth?...",
  "detail": "Authentication started"
}
```

Raw gogcli stdout and stderr are not returned. If the backend cannot find a safe Google OAuth URL, it returns a sanitized conflict response instead of exposing gogcli output.

### 6. The website sends the human to Google

The website opens or displays `auth_url`.

The human authenticates directly with Google and grants consent there. The website and MaraClaw backend do not collect:

- Google username;
- Google password;
- OAuth access token;
- OAuth refresh token;
- client secret;
- raw gogcli output.

### 7. gogcli stores credentials in the agent's gogcli home

When gogcli completes authentication successfully, gogcli stores the resulting Google credential material under its configured home/keyring inside the container:

```text
GOG_HOME=/home/node/.openclaw/gogcli
GOG_KEYRING_BACKEND=file
```

The container path `/home/node/.openclaw` is backed by the agent's mounted workspace data. That means the credentials belong to the agent's local OpenClaw runtime state, not to the website and not to an API response.

On the next successful authenticated status poll, the backend archives only the host-side `agent_dir/gogcli/data` directory, base64-encodes it, encrypts it with the backend secret key, and stores it in `gogcli_credential_states.encrypted_gog_data_archive`. The archive is treated as opaque gogcli state. Cache, workspace files, and backend-local `_gogcli_secrets` files are not included.

The file keyring is unlocked using:

```text
GOG_KEYRING_PASSWORD_FILE=/run/secrets/gogcli_keyring_password
```

### 8. Poll authentication status

The website calls:

```http
GET /api/agents/{agent_id}/gogcli/auth/status
```

The backend executes this inside the running container:

```text
gog auth list --check --json --no-input
```

If gogcli reports an authenticated account, the API returns safe status fields:

```json
{
  "authenticated": true,
  "account_hint": "user@example.com",
  "detail": "Authenticated"
}
```

When this authenticated response is observed, the backend captures the encrypted gogcli data snapshot described above.

If not authenticated, the response is:

```json
{
  "authenticated": false,
  "account_hint": null,
  "detail": "Not authenticated"
}
```

If the backend already has a stored gogcli snapshot for the agent and gogcli now reports unauthenticated, the backend marks the DB row `needs_reauth` and returns:

```json
{
  "authenticated": false,
  "account_hint": null,
  "detail": "Needs re-authentication"
}
```

## Current Limitation

The current backend has `auth/start` and `auth/status`. It does not have an `auth/complete` endpoint that accepts a redirected OAuth URL or auth code.

So the current website flow can:

1. create a gogcli-enabled local agent;
2. store the gogcli file-keyring password;
3. start the gogcli OAuth handoff;
4. return a Google consent URL;
5. poll whether gogcli now considers the account authenticated.

If gogcli's remote flow requires a second explicit command such as `--remote --step 2`, that completion step is not implemented as a backend API yet. In that case, the website can start the handoff and poll status, but credentials will not be stored until some other gogcli completion path runs inside the same container.

## Mental Model

```text
Website
  -> MaraClaw backend API
    -> docker exec into the agent's local OpenClaw container
      -> gogcli talks to Google
        -> gogcli stores credentials under the agent's GOG_HOME
        -> backend snapshots encrypted GOG_HOME/data state on authenticated status
        -> backend restores that snapshot before local container restart
```

The website owns the user experience. The backend owns permission checks, Docker exec, encrypted DB snapshotting, and pre-start restore. gogcli owns OAuth and credential storage format. Google owns consent.
