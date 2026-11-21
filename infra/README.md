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

- Tested in the sandbox this was written in: the archive code against a fake S3 (`orchestration/tests/test_archive.py`: layout, no duplicate uploads,
  a writer that cannot list, a missing bucket); the Terraform files parse; CI runs `terraform fmt -check` and `terraform validate` on every push.
- **Applied for real:** on 20 November 2026, `terraform apply` built all 10 resources (the bucket and its settings, the writer user and its policy, and the
  budget) in a new project on a free-plan AWS account, in `ap-southeast-2`, with Terraform 1.17 and AWS provider 6.73. The plan before it said
  `10 to add, 0 to change, 0 to destroy`.
- **Not yet confirmed:** the Kestra `archive` step uploading to that bucket as the writer user (step 6 and 7). This line changes when someone has seen it work.

## 1. Before you start (once)

- **Make a project for it.** New AWS accounts use [projects](https://docs.aws.amazon.com/accounts/latest/reference/create-project.html): each project
  contains one AWS account of its own. In AWS Settings choose *Projects*, *Create project*, and name it `mend` (names allow letters, digits and dots only).
  That keeps this apart from anything else you run on AWS, and it can be removed by closing the project.
- **There is no root user** in this kind of account: your sign-in (your AWS Builder ID) owns the projects. Turn on two-step verification on that sign-in.
  Do not activate "advanced features" if AWS offers them: it cannot be undone, it adds a root user and an AWS Organization, and it makes you responsible for all costs.
- A free-plan project runs on credits and only supports some services. [AWS lists](https://docs.aws.amazon.com/accounts/latest/reference/supported-services-sign-up-new.md)
  S3, IAM and Budgets as included; IAM Identity Center and Organizations are not. You can create IAM users, but only for programmatic access (no console password),
  which is all this needs.
- **Your Region is assigned, not chosen.** For the free plan AWS [assigns a Region from your contact country](https://docs.aws.amazon.com/accounts/latest/reference/create-project.html)
  (for India that is Sydney, `ap-southeast-2`) and blocks the others. Open the project in AWS Settings, expand *Additional info*, and use that exact value for `region` below.

## 2. Install the tools (PowerShell)

```powershell
winget install Hashicorp.Terraform
winget install Amazon.AWSCLI
```

Close and reopen PowerShell (Windows only sees new programs in a new window), then check `terraform -version` and `aws --version`.
A notice that Terraform is out of date is harmless.

## 3. A user for Terraform to run as

In AWS Settings open the `mend` project and choose *AWS Console*. Check that the account menu at the top right shows the project name `mend`, because you are about to
create an administrator user and it must go in the right account. Then:

1. IAM, Users, *Create user*. Name it `mend-terraform`. Leave console access off.
2. *Attach policies directly*, tick `AdministratorAccess`, create the user.
3. Open the user, *Security credentials*, *Create access key*, use case *Command Line Interface (CLI)*, tick the confirmation box.

That is broad on purpose: it is your own learning account, and you will delete the key when you are done (step 8). Then, in your terminal only:

```powershell
aws configure --profile mend-terraform      # key id, secret, your Region, json. A newer CLI also asks about AI coding tools: answer n
$env:AWS_PROFILE = "mend-terraform"         # lasts for this PowerShell window only; set it again in a new window
aws sts get-caller-identity                 # the Arn should end in user/mend-terraform
```

Never paste the key or the secret anywhere else.

## 4. Build it

```powershell
cd infra
copy terraform.tfvars.example terraform.tfvars      # then edit it: your email, and the Region from step 1
terraform init                                      # creates .terraform.lock.hcl: commit that file, it pins the provider version
terraform plan                                      # read it: Plan: 10 to add, 0 to change, 0 to destroy
terraform apply                                     # type yes
terraform output
```

`terraform.tfvars` and the state file stay on your machine (git ignores both). The state holds no secret but it is how Terraform remembers what it built: keep it.

## 5. Give Mend a key that can only write

In the AWS Console: IAM, Users, `mend-archive-writer`, *Security credentials*, *Create access key*, use case *Application running outside AWS*. Then, from the repository root:

```powershell
python orchestration/make_env.py
```

It asks for your Kestra login and Gemini key again (use the same login as before), then the bucket (`terraform output -raw bucket` prints it without quotes), the Region, and the
writer's key id and secret (hidden as you type). Close the console page that showed the secret.

## 6. Run it

```powershell
docker compose -f orchestration/docker-compose.yml --env-file orchestration/.env up -d --build
```

The first build takes a few minutes (about six on a laptop) because the image now includes boto3. Then, in Kestra, run **daily**: it ends with an `archive` step.
Its log says `archive: archived=True ... uploaded=N`. N is 2 on a fresh pipeline (the marts and the event log) and larger once repairs exist, because each repair adds a
proposal and a decision. A later run uploads only what changed (the new day's marts, the longer event log) and reports the rest as `unchanged`.
Approving a repair in the **repair** flow archives its proposal and decision too.

## 7. Look at what is in the bucket

```powershell
aws s3 ls s3://<your bucket>/mend/ --recursive --profile mend-terraform      # the writer cannot list, so use the admin profile
```

## 8. Clean up, and what it costs

- Delete the `mend-terraform` access key (IAM, Users, Security credentials) when you are not changing the infrastructure. Create a new one when you need it.
- The data is a few kilobytes a day and old versions expire after 90 days, so storage is tiny; the budget emails you if something unexpected costs money.
  Look at *Billing and Cost Management* now and then. This README does not quote prices because they change.
- To remove everything: the bucket is versioned and non-empty, so Terraform refuses by default (on purpose). Run
  `terraform apply -var force_destroy=true`, then `terraform destroy`. Also delete the `mend-terraform` user. Closing the whole `mend` project in AWS Settings
  also works; AWS keeps it for 90 days in case you reopen it.

## If it fails

| What you see | Likely cause |
|---|---|
| `apply` says an action is not allowed in a Region or by a policy | the `region` is not the one your project was assigned (step 1) |
| `apply` fails on the budget and the rest was created | the free plan did not allow it: set `create_budget = false` in `terraform.tfvars` and apply again |
| `BucketAlreadyExists` | change `bucket_name_prefix` |
| `terraform` or `aws` is "not recognized" | the PowerShell window is older than the install: open a new one |
| Terraform says no valid credentials | `$env:AWS_PROFILE` is not set in this window (step 3) |
| the Kestra `archive` step is orange (warning) with `AccessDenied` | the key is not the `mend-archive-writer`'s, or `MEND_ARCHIVE_PREFIX` is not `mend` |
| it says `no bucket named ...` or `bucket is in another region` | `MEND_ARCHIVE_BUCKET` or `MEND_ARCHIVE_REGION` in `orchestration/.env` is wrong: run `make_env.py` again |
| `archive: archived=False, reason=MEND_ARCHIVE_BUCKET is not set` | `make_env.py` was run without a bucket, or Kestra was not restarted after it |
| CI's `terraform` job is red | open the job: `terraform fmt` shows the lines to fix, `validate` names the argument |

The `archive` step is set to `allowFailure`: if AWS is down, the run turns orange but the pipeline's own work still counts.
