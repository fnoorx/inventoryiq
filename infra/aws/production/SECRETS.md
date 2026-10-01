# Application credentials

Local runs read `.env`. The ECS task sets `CREDENTIALS_BACKEND=aws`, and the bot
then loads its credentials from AWS Secrets Manager instead
([`services/credentials.py`](../../../services/credentials.py)). Credentials never
appear in Terraform variables, state, outputs, the Docker image, or logs.

## Secret contents

Terraform creates two empty secrets. Fill them in yourself, outside Terraform.

**Application secret** (`<project>/production/application`): a JSON object whose
keys match the `.env` names and whose values are strings. Supply only what your
enabled integrations need:

- `DISCORD_TOKEN`
- `SHEET_ID` and the `GOOGLE_CREDS_*` fields from `.env.example`
- `STOCKX_API_KEY`, `STOCKX_CLIENT_ID`, `STOCKX_CLIENT_SECRET`
- `OPENAI_API_KEY`

JSON-encode the Google private key so its newlines survive. Any other key is
rejected at startup, so runtime settings and tokens can't hide in this secret.
The accepted list is `APPLICATION_KEYS` in `services/credentials.py`.

**StockX token secret** (`<project>/production/stockx-tokens`): StockX rotates
tokens on refresh, so the bot writes each new pair back here.

```json
{
  "access_token": "CURRENT_ACCESS_TOKEN",
  "refresh_token": "CURRENT_REFRESH_TOKEN"
}
```

**Database password:** RDS creates and rotates the master password in its own
managed secret. The bot builds a password-free connection URL and reads the
current password for each new connection, so rotation needs no restart. TLS
verifies the hostname against the AWS RDS certificate bundle baked into the image.

Non-secret settings such as channel IDs and `INVENTORY_DEFAULT_LOCATION` go in the
`app_settings` Terraform variable, which becomes ordinary task environment variables.

## Permissions

- **Execution role** (image pulls, logs, startup): can read the application and
  database secrets.
- **Task role** (the bot's own code): can read all three secrets and can write
  only the StockX token secret.

The container never uses your administrator CLI profile.

## Failure behaviour

- A failed secret read stops startup. There is no fallback to a `.env` file.
- A failed token write is reported as an error, not as a successful refresh.
  Fix the permission and reauthorize StockX if the stored token has gone stale.
- Error messages never include the AWS response, which could contain secret metadata.

Only one bot may use a StockX token pair at a time. Stop any local bot before
copying its tokens into the secret.
