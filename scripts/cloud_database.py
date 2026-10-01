"""Run migration and transfer jobs inside the VPC, without starting the bot."""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile

import boto3
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from scripts.database_transfer import read_processed_orders, summarize_database, transfer_database
from services.credentials import configure_aws_database, credentials_backend
from services.database import create_engine_for_url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["migrate", "dry-run", "transfer", "verify", "export-sqlite"])
    parser.add_argument("--prefix")
    args = parser.parse_args()
    if credentials_backend() != "aws":
        parser.error("cloud database jobs require CREDENTIALS_BACKEND=aws")
    configure_aws_database()
    if args.action == "migrate":
        command.upgrade(Config("alembic.ini"), "head")
        print("Schema migration completed.")
        return

    engine = create_engine_for_url(os.environ["DATABASE_URL"])
    try:
        if args.action == "verify":
            with engine.connect() as connection:
                summary = summarize_database(connection)
                revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                tls = connection.execute(text("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")).scalar_one()
                print(json.dumps({"summary": asdict(summary), "revision": revision, "tls": tls}))
            return

        if not args.prefix:
            parser.error("--prefix is required for transfer and export jobs")
        bucket = os.environ["DEPLOYMENT_BUCKET"]
        s3 = boto3.client("s3", region_name=os.environ["AWS_REGION"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.db"
            orders_path = Path(directory) / "orders.json"
            if args.action == "export-sqlite":
                from services.database_schema import ensure_database_schema

                ensure_database_schema(path)
                sqlite_engine = create_engine_for_url(f"sqlite:///{path.as_posix()}")
                try:
                    with engine.connect() as source:
                        summary = transfer_database(source, sqlite_engine, dry_run=False)
                finally:
                    sqlite_engine.dispose()
                s3.upload_file(str(path), bucket, f"database-backups/{args.prefix}/inventory.db")
                print(json.dumps({"export": asdict(summary)}))
                return

            s3.download_file(bucket, f"migration/{args.prefix}/inventory.db", str(path))
            s3.download_file(bucket, f"migration/{args.prefix}/orders.json", str(orders_path))
            source_engine = create_engine_for_url(f"sqlite:///{path.as_posix()}")
            try:
                with source_engine.connect() as source:
                    orders = read_processed_orders(source, str(orders_path))
                    summary = transfer_database(source, engine, dry_run=args.action == "dry-run", processed_orders=orders)
                    print(json.dumps({"action": args.action, "summary": asdict(summary)}))
            finally:
                source_engine.dispose()
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Database job failed ({type(error).__name__}); bot remains disabled.", flush=True)
        raise SystemExit(1) from None
