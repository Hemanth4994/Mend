# Mend on AWS: an S3 archive, built with Terraform

Mend can copy what it publishes and what people decided into an S3 bucket:

| In the bucket | What it is |
|---|---|
| `mend/marts/<table>/as_of=<day>/<table>.parquet` | the mart tables, as of the latest day they hold |
| `mend/audit/repairs/<id>/proposal.json` and `decision.json` | what the agent proposed and the gate's result; who approved or rejected it, when, and why |
| `mend/audit/events.jsonl` | every build, proposal and decision, in order |

The bucket is versioned, and the IAM user Mend writes as can only put and get objects under `mend/`. It cannot delete or list, so the
record cannot be rewritten by the program that wrote it. Without a bucket configured, the `archive` steps do nothing and Mend works as before.

Terraform creates: the bucket (private, encrypted, versioned, TLS only, old versions expire after 90 days), the `mend-archive-writer` IAM
user with that one small policy, and a monthly cost budget that emails you. It does not create an access key, so no secret is stored in
Terraform's state; you create the key by hand below.

## What was and was not tested

- Tested here: the archive code against a fake S3 (`orchestration/tests/test_archive.py`: layout, no duplicate uploads, a writer that cannot list, a missing bucket);
  the Terraform files parse; CI runs `terraform fmt -check` and `terraform validate` on every push.
- **Not tested: `terraform apply` against a real account.** The sandbox this was written in cannot reach AWS or HashiCorp. The real run is yours, below.
  If something fails, the error text is the thing to look at; the table at the end covers the likely ones.

## 1. Before you start (once)

- Sign in as the root user only for this: turn on **MFA** for it, create no access keys for it, and use a normal IAM user for everything else.
- A new free-plan account runs on credits and only supports some services. [AWS lists](https://docs.aws.amazon.com/accounts/latest/reference/supported-services-sign-up-new.md)
  S3, IAM and Budgets as included; IAM Identity Center and Organizations are not, which is why this uses a plain IAM user.
- **Your Region is assigned, not chosen.** For the free plan AWS [assigns a Region from your contact country](https://docs.aws.amazon.com/accounts/latest/reference/project-regions.html)
  (for India that page says Sydney, `ap-southeast-2`) and blocks the others. Find yours in the console under *Account*, *AWS Settings*, and use that exact value for `region` below.

## 2. Install the tools (PowerShell)

```powershell
winget install Hashicorp.Terraform
winget install Amazon.AWSCLI
```

Close and reopen PowerShell, then check `terraform version` and `aws --version`.

## 3. A user for Terraform to run as

In the console: IAM, Users, create `mend-terraform`, attach the policy `AdministratorAccess`, then create an access key for it ("Command Line Interface").
That is broad on purpose: this is your own learning account, and you will delete the key when you are done (step 8). Then, in your terminal only:

```powershell
aws configure --profile mend-terraform      # paste the key id, the secret, and your Region; never paste them anywhere else
$env:AWS_PROFILE = "mend-terraform"
aws sts get-caller-identity                 # should show the mend-terraform user
```

## 4. Build it

```powershell
cd infra
copy terraform.tfvars.example terraform.tfvars      # then edit it: your email, and the Region from step 1
terraform init
terraform plan                                      # read it: about 10 things to add, nothing to change or destroy
terraform apply
terraform output
```

`terraform.tfvars` and the state file stay on your machine (git ignores both). The state holds no secret but it is how Terraform remembers what it built: keep it.

## 5. Give Mend a key that can only write

In the console: IAM, Users, `mend-archive-writer`, *Security credentials*, create an access key ("Application running outside AWS"). Then, from the repository root:

```powershell
python orchestration/make_env.py
```

It asks for your Kestra login and Gemini key again, then the bucket (`terraform output -raw bucket`), the Region, and the writer's key id and secret (hidden as you type).
Close the console page that showed the secret.

## 6. Run it

```powershell
docker compose -f orchestration/docker-compose.yml --env-file orchestration/.env up -d --build
```

The first build is a few minutes because the image now includes boto3. Then, in Kestra, run **daily**: it ends with an `archive` step.
Its log says `archive: archived=True ... uploaded=2`. A second run says `uploaded=0 unchanged=2`, because nothing changed. Approving a repair in the **repair** flow archives its proposal and decision too.

## 7. Look at what is in the bucket

```powershell
aws s3 ls s3://<your bucket>/mend/ --recursive         # still with $env:AWS_PROFILE = "mend-terraform"; the writer cannot list
```

## 8. Clean up, and what it costs

- Delete the `mend-terraform` access key (IAM, Users, Security credentials) when you are not changing the infrastructure. Create a new one when you need it.
- The data is a few kilobytes a day and old versions expire after 90 days, so storage is tiny; the budget emails you if something unexpected costs money.
  Look at *Billing and Cost Management* now and then. This README does not quote prices because they change.
- To remove everything: the bucket is versioned and non-empty, so Terraform refuses by default (on purpose). Run
  `terraform apply -var force_destroy=true`, then `terraform destroy`. Also delete the `mend-terraform` user.

## If it fails

| What you see | Likely cause |
|---|---|
| `apply` says an action is not allowed in a Region or by a policy | the `region` is not the one your account was assigned (step 1) |
| `apply` fails on the budget and the rest was created | the free plan did not allow it: set `create_budget = false` in `terraform.tfvars` and apply again |
| `BucketAlreadyExists` | change `bucket_name_prefix` |
| the Kestra `archive` step is orange (warning) with `AccessDenied` | the key is not the `mend-archive-writer`'s, or `MEND_ARCHIVE_PREFIX` is not `mend` |
| it says `no bucket named ...` or `bucket is in another region` | `MEND_ARCHIVE_BUCKET` or `MEND_ARCHIVE_REGION` in `orchestration/.env` is wrong: run `make_env.py` again |
| `archive: archived=False, reason=MEND_ARCHIVE_BUCKET is not set` | `make_env.py` was run without a bucket, or Kestra was not restarted after it |
| CI's `terraform` job is red | open the job: `terraform fmt` shows the lines to fix, `validate` names the argument |

The `archive` step is set to `allowFailure`: if AWS is down, the run turns orange but the pipeline's own work still counts.
