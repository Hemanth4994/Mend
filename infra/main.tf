data "aws_caller_identity" "current" {}

locals {
  bucket_name = "${var.bucket_name_prefix}-${data.aws_caller_identity.current.account_id}-${var.region}"
}

resource "aws_s3_bucket" "archive" {
  bucket        = local.bucket_name
  force_destroy = var.force_destroy
}

resource "aws_s3_bucket_public_access_block" "archive" {
  bucket                  = aws_s3_bucket.archive.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "archive" {
  bucket = aws_s3_bucket.archive.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "archive" {
  bucket = aws_s3_bucket.archive.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id

  rule {
    bucket_key_enabled = true

    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id

  rule {
    id     = "expire-old-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.archive]
}

data "aws_iam_policy_document" "archive_bucket" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.archive.arn,
      "${aws_s3_bucket.archive.arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "archive" {
  bucket = aws_s3_bucket.archive.id
  policy = data.aws_iam_policy_document.archive_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.archive]
}

resource "aws_iam_user" "writer" {
  name = "mend-archive-writer"
}

data "aws_iam_policy_document" "writer" {
  statement {
    sid       = "WriteAndReadBackUnderThePrefixOnly"
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${aws_s3_bucket.archive.arn}/${var.data_prefix}/*"]
  }
}

resource "aws_iam_user_policy" "writer" {
  name   = "mend-archive-write"
  user   = aws_iam_user.writer.name
  policy = data.aws_iam_policy_document.writer.json
}

resource "aws_budgets_budget" "monthly" {
  count = var.create_budget ? 1 : 0

  name         = "mend-monthly-cost"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
