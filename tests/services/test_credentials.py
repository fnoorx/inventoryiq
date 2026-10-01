import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from dotenv import dotenv_values

from services import credentials
from utils import stockx_api


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for key in credentials.APPLICATION_KEYS | {
        "CREDENTIALS_BACKEND", "APPLICATION_SECRET_ARN", "STOCKX_TOKEN_SECRET_ARN",
        "STOCKX_ACCESS_TOKEN", "STOCKX_REFRESH_TOKEN",
    }:
        os.environ.pop(key, None)
    monkeypatch.setattr(credentials.boto3, "client", Mock(side_effect=AssertionError("Unexpected AWS call")))
    monkeypatch.setattr(stockx_api, "token_store", {
        "access_token": None, "refresh_token": None, "expires_at": None,
    })


@pytest.fixture
def dotenv_enabled():
    # conftest disables .env loading suite-wide; these tests exercise it on a temporary file.
    os.environ.pop("PYTHON_DOTENV_DISABLED", None)


def aws_backend(monkeypatch, application=None, tokens=None):
    monkeypatch.setenv("CREDENTIALS_BACKEND", "aws")
    monkeypatch.setenv("APPLICATION_SECRET_ARN", "application-test")
    monkeypatch.setenv("STOCKX_TOKEN_SECRET_ARN", "stockx-test")
    monkeypatch.setenv("DATABASE_HOST", "test.rds.amazonaws.com")
    monkeypatch.setenv("DATABASE_NAME", "inventoryiq")
    monkeypatch.setenv("DATABASE_SECRET_ARN", "database-test")
    values = {
        "application-test": application if application is not None else {"DISCORD_TOKEN": "discord-test"},
        "stockx-test": tokens if tokens is not None else {"access_token": "old-access", "refresh_token": "old-refresh"},
    }

    def read(SecretId):
        return {"SecretString": json.dumps(values[SecretId])}

    def write(SecretId, SecretString):
        values[SecretId] = json.loads(SecretString)
        return {"VersionId": "test-version"}

    client = SimpleNamespace(get_secret_value=Mock(side_effect=read), put_secret_value=Mock(side_effect=write))
    monkeypatch.setattr(credentials.boto3, "client", Mock(return_value=client))
    return client, values


def test_database_url_requires_verified_tls_and_contains_no_password(monkeypatch):
    aws_backend(monkeypatch)
    credentials.configure_aws_database()
    from sqlalchemy.engine import make_url
    url = make_url(os.environ["DATABASE_URL"])
    assert url.password is None
    assert url.host == "test.rds.amazonaws.com"
    assert url.query["sslmode"] == "verify-full"


def test_new_database_connections_read_rotated_credentials(monkeypatch):
    from services.database import create_engine_for_url

    client, values = aws_backend(monkeypatch)
    values["database-test"] = {"username": "user", "password": "first", "port": 5432}
    credentials.configure_aws_database()
    engine = create_engine_for_url(os.environ["DATABASE_URL"])
    try:
        parameters = {}
        engine.dialect.dispatch.do_connect(engine.dialect, None, [], parameters)
        assert parameters["password"] == "first"
        values["database-test"]["password"] = "rotated"
        engine.dialect.dispatch.do_connect(engine.dialect, None, [], parameters)
        assert parameters["password"] == "rotated"
        assert client.get_secret_value.call_count == 2
    finally:
        engine.dispose()


def test_local_loading_preserves_explicit_environment(tmp_path, monkeypatch, dotenv_enabled):
    env_path = tmp_path / ".env"
    env_path.write_text("DISCORD_TOKEN=file-token\nSHEET_ID=test-sheet\n", encoding="utf-8")
    monkeypatch.setenv("DISCORD_TOKEN", "injected-token")

    credentials.load_credentials(env_path)

    assert os.environ["DISCORD_TOKEN"] == "injected-token"
    assert os.environ["SHEET_ID"] == "test-sheet"
    credentials.boto3.client.assert_not_called()


def test_local_tokens_survive_reload_without_changing_other_settings(tmp_path, dotenv_enabled):
    env_path = tmp_path / ".env"
    env_path.write_text("DISCORD_TOKEN=untouched\nSTOCKX_ACCESS_TOKEN=old\nSTOCKX_REFRESH_TOKEN=old\n", encoding="utf-8")

    credentials.save_stockx_tokens("new-access", "new-refresh", env_path)
    os.environ.pop("STOCKX_ACCESS_TOKEN")
    os.environ.pop("STOCKX_REFRESH_TOKEN")

    assert credentials.load_stockx_tokens(env_path) == {
        "access_token": "new-access", "refresh_token": "new-refresh",
    }
    assert dotenv_values(env_path)["DISCORD_TOKEN"] == "untouched"
    credentials.boto3.client.assert_not_called()


def test_aws_application_loads_multiline_key_without_reading_dotenv(monkeypatch, tmp_path):
    values = {"DISCORD_TOKEN": "aws-token", "GOOGLE_CREDS_PRIVATE_KEY": "test\nprivate-key\n"}
    client, _ = aws_backend(monkeypatch, application=values)
    env_path = tmp_path / ".env"
    env_path.write_text("DISCORD_TOKEN=stale-local\n", encoding="utf-8")
    monkeypatch.setattr(credentials, "load_dotenv", Mock(side_effect=AssertionError("AWS read .env")))

    credentials.load_credentials(env_path)

    assert os.environ["DISCORD_TOKEN"] == "aws-token"
    assert os.environ["GOOGLE_CREDS_PRIVATE_KEY"] == "test\nprivate-key\n"
    client.get_secret_value.assert_called_once_with(SecretId="application-test")


def test_aws_rotated_tokens_survive_a_simulated_restart(monkeypatch, tmp_path, capsys):
    client, values = aws_backend(monkeypatch)
    env_path = tmp_path / ".env"
    env_path.write_text("STOCKX_ACCESS_TOKEN=stale\n", encoding="utf-8")
    before = env_path.read_bytes()
    monkeypatch.setattr(stockx_api, "ENV_PATH", env_path)
    stockx_api.token_store.update(credentials.load_stockx_tokens(env_path))
    monkeypatch.setenv("STOCKX_CLIENT_ID", "current-client")
    monkeypatch.setenv("STOCKX_CLIENT_SECRET", "current-client-secret")
    response = SimpleNamespace(status_code=200, json=lambda: {
        "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600,
    })
    post = Mock(return_value=response)
    monkeypatch.setattr(stockx_api.requests, "post", post)

    assert stockx_api.refresh_access_token() is True
    assert values["stockx-test"] == {"access_token": "new-access", "refresh_token": "new-refresh"}
    assert post.call_args.kwargs["json"]["client_id"] == "current-client"
    assert post.call_args.kwargs["json"]["refresh_token"] == "old-refresh"
    client.put_secret_value.assert_called_once()

    stockx_api.token_store.update(access_token=None, refresh_token=None, expires_at=None)
    os.environ.pop("STOCKX_ACCESS_TOKEN")
    os.environ.pop("STOCKX_REFRESH_TOKEN")

    def startup_refresh():
        assert stockx_api.token_store["refresh_token"] == "new-refresh"
        return True

    monkeypatch.setattr(stockx_api, "refresh_access_token", startup_refresh)
    assert stockx_api.ensure_valid_token() == "new-access"
    assert env_path.read_bytes() == before
    assert "new-access" not in capsys.readouterr().out


def test_refresh_response_without_new_refresh_token_preserves_existing(monkeypatch):
    _, values = aws_backend(monkeypatch)
    stockx_api.token_store.update(credentials.load_stockx_tokens())

    stockx_api.store_tokens({"access_token": "new-access", "expires_in": 3600})

    assert values["stockx-test"]["refresh_token"] == "old-refresh"


def test_aws_read_denied_does_not_fall_back_to_local_credentials(monkeypatch, tmp_path):
    client, _ = aws_backend(monkeypatch)
    client.get_secret_value.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "sensitive-response"}}, "GetSecretValue",
    )
    env_path = tmp_path / ".env"
    env_path.write_text("DISCORD_TOKEN=local-fallback\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Unable to read AWS secret") as failure:
        credentials.load_credentials(env_path)

    assert "sensitive-response" not in str(failure.value)
    assert "DISCORD_TOKEN" not in os.environ


def test_aws_write_denied_does_not_report_success_or_write_dotenv(monkeypatch, tmp_path, capsys):
    client, _ = aws_backend(monkeypatch)
    stockx_api.token_store.update(credentials.load_stockx_tokens())
    old_tokens = dict(stockx_api.token_store)
    client.put_secret_value.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "sensitive-response"}}, "PutSecretValue",
    )
    env_path = tmp_path / ".env"
    monkeypatch.setattr(stockx_api, "ENV_PATH", env_path)

    with pytest.raises(RuntimeError, match="Unable to persist StockX tokens"):
        stockx_api.store_tokens({"access_token": "new-access", "refresh_token": "new-refresh"})

    assert stockx_api.token_store == old_tokens
    assert os.environ["STOCKX_REFRESH_TOKEN"] == "old-refresh"
    assert not env_path.exists()
    assert "stored" not in capsys.readouterr().out


@pytest.mark.parametrize("payload", ["[]", '{"DISCORD_TOKEN": 123}', "invalid-secret-json"])
def test_malformed_aws_secret_is_rejected_without_values_in_error(monkeypatch, payload):
    client, _ = aws_backend(monkeypatch)
    client.get_secret_value.side_effect = None
    client.get_secret_value.return_value = {"SecretString": payload}

    with pytest.raises(ValueError, match="JSON object of string values") as failure:
        credentials.load_credentials()

    assert payload not in str(failure.value)


def test_application_secret_cannot_override_runtime_settings(monkeypatch):
    aws_backend(monkeypatch, application={"CREDENTIALS_BACKEND": "local"})

    with pytest.raises(ValueError, match="unsupported keys"):
        credentials.load_credentials()

    assert os.environ["CREDENTIALS_BACKEND"] == "aws"


def test_incomplete_stockx_secret_is_rejected(monkeypatch):
    aws_backend(monkeypatch, tokens={"access_token": "only-access"})

    with pytest.raises(ValueError, match="access_token and refresh_token"):
        credentials.load_stockx_tokens()


def test_missing_secret_reference_stops_before_aws_request(monkeypatch):
    monkeypatch.setenv("CREDENTIALS_BACKEND", "aws")

    with pytest.raises(ValueError, match="APPLICATION_SECRET_ARN"):
        credentials.load_credentials()

    credentials.boto3.client.assert_not_called()


def test_unknown_backend_does_not_silently_load_local_credentials(monkeypatch):
    monkeypatch.setenv("CREDENTIALS_BACKEND", "typo")

    with pytest.raises(ValueError, match="local or aws"):
        credentials.load_credentials()


def test_stockx_error_body_is_not_logged(monkeypatch, capsys):
    stockx_api.token_store["refresh_token"] = "test-refresh"
    monkeypatch.setattr(stockx_api.requests, "post", Mock(return_value=SimpleNamespace(
        status_code=401, text="sensitive-response-body",
    )))

    assert stockx_api.refresh_access_token() is False
    output = capsys.readouterr().out
    assert "401" in output
    assert "sensitive-response-body" not in output
