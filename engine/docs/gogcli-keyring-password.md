# gogcli Keyring Password File

This note explains what the MaraClaw `gogcli` keyring password file is, what it looks like on disk, and how to create or rotate it safely.

## What It Is

The keyring password file is a backend-local file that contains the password used by gogcli's file keyring backend.

It is MaraClaw/gogcli runtime configuration. It is not a Google credential.

MaraClaw's OpenClaw Docker runtime is Linux arm64 and continues to use gogcli's file keyring backend:

```text
GOG_KEYRING_BACKEND=file
GOG_KEYRING_PASSWORD_FILE=/run/secrets/gogcli_keyring_password
```

Docker/Linux file-keyring runtime remains unchanged by gogcli v0.34.0.

gogcli itself reads the upstream-required `GOG_KEYRING_PASSWORD` environment variable for the file backend. MaraClaw still exposes only `GOG_KEYRING_PASSWORD_FILE` in Docker container metadata, then `validate-gogcli.sh` and backend Docker exec handoffs read that mounted file and export `GOG_KEYRING_PASSWORD` inside the launched process. The password value is not stored in the Docker env configuration or passed as a command argument.

gogcli v0.34.0 macOS artifacts are signed and notarized locally by OpenClaw Foundation with its Developer ID and retain the `com.steipete.gogcli.gog` identifier. On native macOS, gogcli retains macOS Keychain trust; operators can override the trusted application path with `GOG_KEYCHAIN_TRUST_APPLICATION`.

It is not:

- a Google Account password;
- a Google OAuth access token;
- a Google OAuth refresh token;
- a Google OAuth client secret;
- sent to Google during OAuth.

Google's official OAuth documentation describes consent URLs, scopes, authorization codes, access tokens, and refresh tokens. It does not define `gogcli`, `GOG_KEYRING_PASSWORD_FILE`, or a `gogcli` keyring password file. Those names are local implementation details in this backend and the gogcli runtime.

## What The File Looks Like

The file is plain UTF-8 text. Its entire content is the keyring password string.

Example content:

```text
correct horse battery staple
```

It is not JSON:

```json
{"password":"correct horse battery staple"}
```

It is not an environment file:

```text
GOG_KEYRING_PASSWORD=correct horse battery staple
```

When created by the backend, the file path is:

```text
<STORAGE_LOCAL_ROOT or AGENT_DATA_DIR>/_gogcli_secrets/{agent_id}/keyring_password
```

The directory is set to mode `0700`, and the file is set to mode `0600`.

Inside the OpenClaw container, the same file is mounted read-only at:

```text
/run/secrets/gogcli_keyring_password
```

The container receives only the path:

```text
GOG_KEYRING_PASSWORD_FILE=/run/secrets/gogcli_keyring_password
```

The password value itself is not placed in Docker environment metadata.

## Preferred Creation Method: API

Use the backend API whenever possible. It applies the same permission checks as the rest of the gogcli integration and writes the file with the expected path and permissions.

Request:

```http
POST /api/agents/{agent_id}/gogcli/keyring-secret
```

Body:

```json
{
  "password": "correct horse battery staple"
}
```

Example with `curl`:

```bash
curl -X POST "${MARA_BASE_URL}/api/agents/${AGENT_ID}/gogcli/keyring-secret" \
  -H "Authorization: Bearer ${MARA_TOKEN}" \
  -H "Content-Type: application/json" \
  --data '{"password":"correct horse battery staple"}'
```

Successful response:

```text
204 No Content
```

The caller must be authenticated and must have `manage` access to the agent, or be a `platform_admin` or `org_admin`. The agent must have `gogcli_enabled=true`.

## Local Development Creation Method

For local development or recovery work, you can create the file manually. The API method is still preferred.

1. Pick the local storage root used by the backend.

   The backend uses `STORAGE_LOCAL_ROOT` when set; otherwise it falls back to `AGENT_DATA_DIR`.

2. Create the per-agent secret directory.

   ```bash
   secret_dir="${STORAGE_LOCAL_ROOT:-$AGENT_DATA_DIR}/_gogcli_secrets/${AGENT_ID}"
   mkdir -p "$secret_dir"
   chmod 700 "$secret_dir"
   ```

3. Write the password as the exact file content.

   Use `printf`, not `echo`, so an accidental trailing newline does not become part of the password.

   ```bash
   printf '%s' "$GOGCLI_KEYRING_PASSWORD" > "$secret_dir/keyring_password"
   chmod 600 "$secret_dir/keyring_password"
   ```

4. Restart the agent container.

   Docker mounts are selected when the container starts. If the password file did not exist before the container was started, restart the container so MaraClaw can mount it at `/run/secrets/gogcli_keyring_password` and set `GOG_KEYRING_PASSWORD_FILE`.

## Rotation

To rotate the password, call the same API again with the new value:

```http
POST /api/agents/{agent_id}/gogcli/keyring-secret
```

The backend overwrites the per-agent file atomically.

After rotation, restart the local OpenClaw container before relying on the new password. Existing containers may still be using the old mounted secret file view or may not have the mount if the file was created after startup.

Do not rotate this file casually after gogcli has already stored credentials. The password is what gogcli uses to unlock its local file keyring. Changing it without coordinating gogcli credential storage may make existing local credentials unreadable.

## Relationship To Google OAuth

The keyring password file protects gogcli's local file-based keyring. It does not authenticate the user to Google.

Google OAuth works separately:

1. MaraClaw runs `gog auth add ... --remote --step 1 --plain --no-input` inside the agent container.
2. gogcli prints a Google OAuth consent URL.
3. MaraClaw returns only that Google URL to the website.
4. The human completes consent with Google.
5. gogcli stores resulting OAuth credential material in its configured keyring under `GOG_HOME`.

Google's OAuth documentation states that OAuth lets users authorize access without sharing usernames and passwords with the application. Google also treats access and refresh tokens as user-entrusted credentials that must be stored securely, not logged, and not transmitted in plaintext.

## Security Notes

- Generate the keyring password with a password manager or a cryptographically strong random generator.
- Store it only through the API or in the backend-local keyring password file.
- Do not commit it to git.
- Do not put the password value in Docker environment variables.
- Do not log it.
- Do not show it in the website after creation.
- Do not confuse it with the Google user's password.
- Protect the host filesystem path because local OAuth credential stores can be abused by anyone with filesystem access to the stored credentials.

## References

- Google for Developers, "OAuth 2.0 for iOS & Desktop Apps - Google for Developers," https://developers.google.com/identity/protocols/oauth2/native-app
- Google for Developers, "Using OAuth 2.0 to Access Google APIs | Authorization," https://developers.google.com/identity/protocols/oauth2
- Google for Developers, "OAuth 2.0 for TV and Limited-Input Device Applications," https://developers.google.com/identity/protocols/oauth2/limited-input-device
- Google Workspace, "Configure the OAuth consent screen and choose scopes," https://developers.google.com/workspace/guides/configure-oauth-consent
- Google for Developers, "OAuth 2.0 Policies - Google for Developers," https://developers.google.com/identity/protocols/oauth2/policies
- Google for Developers, "Best Practices | Authorization Resources - Google for Developers," https://developers.google.com/identity/protocols/oauth2/resources/best-practices
- Google for Developers, "Google API Services User Data Policy," https://developers.google.com/terms/api-services-user-data-policy
- Google Cloud SDK, "Authenticate for the gcloud CLI | Google Cloud SDK," https://docs.cloud.google.com/sdk/docs/authenticate
