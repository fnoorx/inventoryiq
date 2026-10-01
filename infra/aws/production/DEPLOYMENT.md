# AWS deployment

This folder is a template for running the bot on AWS: one ECS Fargate task,
private RDS PostgreSQL, Secrets Manager, S3, and CloudWatch Logs. It has no NAT
gateway or load balancer, to keep costs low. The service runs at most one bot,
and a rolling deployment stops the old task before starting the new one, so two
bots never write inventory at the same time.

Nothing here is needed for local use: without `DATABASE_URL` and
`CREDENTIALS_BACKEND=aws`, the bot keeps using SQLite and `.env`.

Placeholders below: `<profile>` is your AWS CLI profile, `<region>` your AWS
region, `<account-id>` your account ID.

## 1. Remote state (once per account)

`infra/aws/bootstrap` creates the versioned, encrypted S3 bucket that holds the
Terraform state for both folders.

1. Copy `bootstrap/terraform.tfvars.example` to `terraform.tfvars`, and both
   `backend.hcl.example` files to `backend.hcl`. Fill in your values; all three
   files are ignored by Git.
2. The bucket must exist before it can store state. For the very first run,
   comment out the `backend "s3"` block in `bootstrap/backend.tf`, then run
   `terraform init`, `terraform plan`, and `terraform apply`.
3. Restore the block and move the local state into the bucket:
   `terraform init -migrate-state -backend-config=backend.hcl`.

## 2. Infrastructure

From `infra/aws/production`:

1. Copy `production.tfvars.example` to `production.auto.tfvars` (ignored by Git).
   Keep `bot_enabled = false` for now.
2. Run `terraform init -backend-config=backend.hcl`.
3. Run `terraform plan`, read every change, then `terraform apply`.
4. Fill in the application and StockX secrets as described in [SECRETS.md](SECRETS.md).

## 3. Image

1. Run the test suite.
2. Build for `linux/amd64` with a new, unique tag. Build from a clean checkout so
   uncommitted files can't end up in the image:

   ```sh
   git archive HEAD | docker build --platform linux/amd64 -t inventoryiq:<tag> -
   ```

3. Log in to the ECR repository from `terraform output ecr_repository_url`, then
   tag and push the image. ECR tags are immutable, so every tag must be new.
4. Set `image_tag` in `production.auto.tfvars`.

## 4. Database

The schema is managed by Alembic. Database jobs run as one-off tasks inside the
VPC, using the same image with a different command, and never start Discord:

```sh
terraform output -json > tmp/production-outputs.json
python -m scripts.run_cloud_job --outputs tmp/production-outputs.json python -m scripts.cloud_database migrate
python -m scripts.run_cloud_job --outputs tmp/production-outputs.json python -m scripts.cloud_database verify
```

Run each job separately and continue only after exit code 0.

**Moving existing SQLite data:** stop the local bot first. Upload `inventory.db`
and the processed-orders JSON to `s3://<deployment-bucket>/migration/<prefix>/`,
then run `dry-run`, `transfer`, `verify`, and `export-sqlite` with
`--prefix <prefix>`. The transfer refuses a non-empty destination, copies inside
one transaction, compares every record, and advances PostgreSQL's ID sequence.
Never upload `.env` to S3.

## 5. Start the bot

Set `bot_enabled = true`, then plan and apply. Confirm in the ECS console or CLI
that exactly one task is running, and look for `InventoryIQ connected to Discord.`
in the `/ecs/<project>-production` log group. Then try a read-only Discord command:
startup alone doesn't prove the integrations work.

## Updating

Build and push a new tag, set `image_tag`, then plan and apply. Only the task
definition and service change. The old task stops before the new one starts.

## Rolling back

**Code:** ECR keeps every earlier image. Set `image_tag` back to a previous tag,
then plan and apply. This changes code only; data and secrets are untouched.

**Back to local hosting:** stop the service (`bot_enabled = false`), run
`export-sqlite`, and verify the exported file before starting the local bot. Copy
the current StockX tokens from Secrets Manager into `.env`, because they may have
rotated while the bot ran on AWS. Never run the local and cloud bots at the same time.

## Backups

RDS keeps automated backups for seven days. The deployment bucket expires
migration uploads after seven days and SQLite exports after 90 days. A SQLite
export is an application-data copy, not a full PostgreSQL dump.

## Limits

- The 0.25 vCPU / 1 GiB task size is a starting point. Watch memory while the
  browser-based scrapers run before relying on it.
- Catalogue snapshots live on the container filesystem and are lost when the task
  is replaced. Inventory and processed orders are in RDS.
- Deployment is manual; there is no CI/CD pipeline.
