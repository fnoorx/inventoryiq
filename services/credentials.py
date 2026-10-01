"""Load credentials from the local .env file, or from AWS Secrets Manager when deployed.

``CREDENTIALS_BACKEND=local`` (the default) keeps everything in ``.env``. With
``CREDENTIALS_BACKEND=aws`` the application secret is loaded into the process
environment, StockX tokens are read from and written back to their own secret,
and the RDS password is fetched for each new database connection.
"""

import json
import os
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv, set_key


ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
APPLICATION_KEYS = {
    "DISCORD_TOKEN", "STOCKX_API_KEY", "STOCKX_CLIENT_ID", "STOCKX_CLIENT_SECRET",
    "OPENAI_API_KEY", "SHEET_ID", "GOOGLE_CREDS_PROJECT_ID", "GOOGLE_CREDS_PRIVATE_KEY_ID",
    "GOOGLE_CREDS_PRIVATE_KEY", "GOOGLE_CREDS_CLIENT_EMAIL", "GOOGLE_CREDS_CLIENT_ID",
}


def credentials_backend():
    backend = os.getenv("CREDENTIALS_BACKEND", "local").strip().lower()
    if backend not in {"local", "aws"}:
        raise ValueError("CREDENTIALS_BACKEND must be local or aws.")
    return backend


def _secret_id(setting):
    secret_id = os.getenv(setting, "").strip()
    if not secret_id:
        raise ValueError(f"Set {setting} when using AWS credentials.")
    return secret_id


def _client():
    return boto3.client(
        "secretsmanager",
        region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ca-central-1",
    )


def _read_secret(secret_id):
    # Errors never include the AWS response, which could echo secret metadata.
    try:
        response = _client().get_secret_value(SecretId=secret_id)
    except (BotoCoreError, ClientError):
        raise RuntimeError("Unable to read AWS secret; check credentials and secret permissions.") from None
    try:
        values = json.loads(response["SecretString"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("AWS secret must contain a JSON object of string values.") from None
    if not isinstance(values, dict) or not all(isinstance(value, str) for value in values.values()):
        raise ValueError("AWS secret must contain a JSON object of string values.")
    return values


def load_credentials(env_path=ENV_PATH):
    if credentials_backend() == "local":
        load_dotenv(env_path)
        return

    values = _read_secret(_secret_id("APPLICATION_SECRET_ARN"))
    if not values.keys() <= APPLICATION_KEYS:
        raise ValueError("Application secret contains unsupported keys; keep runtime settings and tokens separate.")
    os.environ.update(values)
    configure_aws_database()


def configure_aws_database():
    """Point DATABASE_URL at RDS over verified TLS; the password is supplied per connection."""

    from sqlalchemy.engine import URL

    host = _secret_id("DATABASE_HOST")
    name = _secret_id("DATABASE_NAME")
    _secret_id("DATABASE_SECRET_ARN")
    os.environ["DATABASE_URL"] = URL.create(
        "postgresql+psycopg", host=host, port=5432, database=name,
        query={"sslmode": "verify-full", "sslrootcert": os.getenv("DATABASE_CA_FILE", "/app/certs/rds.pem")},
    ).render_as_string(hide_password=False)


def read_database_credentials():
    try:
        response = _client().get_secret_value(SecretId=_secret_id("DATABASE_SECRET_ARN"))
        values = json.loads(response["SecretString"])
        if not all(isinstance(values.get(key), str) and values[key] for key in ("username", "password")):
            raise ValueError()
        return {key: values[key] for key in ("username", "password")}
    except (BotoCoreError, ClientError, KeyError, TypeError, ValueError, AttributeError):
        raise RuntimeError("Unable to load RDS credentials; check the database secret and permissions.") from None


def load_stockx_tokens(env_path=ENV_PATH):
    if credentials_backend() == "local":
        load_dotenv(env_path, override=True)
        return {
            "access_token": os.getenv("STOCKX_ACCESS_TOKEN"),
            "refresh_token": os.getenv("STOCKX_REFRESH_TOKEN"),
        }

    tokens = _read_secret(_secret_id("STOCKX_TOKEN_SECRET_ARN"))
    if set(tokens) != {"access_token", "refresh_token"} or not all(tokens.values()):
        raise ValueError("StockX secret requires nonempty access_token and refresh_token strings.")
    os.environ.update(STOCKX_ACCESS_TOKEN=tokens["access_token"], STOCKX_REFRESH_TOKEN=tokens["refresh_token"])
    return tokens


def save_stockx_tokens(access_token, refresh_token, env_path=ENV_PATH):
    """Persist rotated tokens so the next process start can still refresh."""

    if not isinstance(access_token, str) or not access_token or not isinstance(refresh_token, str) or not refresh_token:
        raise ValueError("StockX access and refresh tokens must be nonempty strings.")

    if credentials_backend() == "local":
        set_key(env_path, "STOCKX_ACCESS_TOKEN", access_token)
        set_key(env_path, "STOCKX_REFRESH_TOKEN", refresh_token)
    else:
        secret_id = _secret_id("STOCKX_TOKEN_SECRET_ARN")
        try:
            _client().put_secret_value(
                SecretId=secret_id,
                SecretString=json.dumps({"access_token": access_token, "refresh_token": refresh_token}),
            )
        except (BotoCoreError, ClientError):
            raise RuntimeError("Unable to persist StockX tokens to AWS; check secret write permissions.") from None

    os.environ.update(STOCKX_ACCESS_TOKEN=access_token, STOCKX_REFRESH_TOKEN=refresh_token)
