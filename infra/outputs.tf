output "bucket" {
  description = "The bucket Mend archives to. This is MEND_ARCHIVE_BUCKET."
  value       = aws_s3_bucket.archive.bucket
}

output "region" {
  description = "The region of the bucket. This is MEND_ARCHIVE_REGION."
  value       = var.region
}

output "writer_user" {
  description = "The IAM user Mend writes as. Create its access key by hand (see infra/README.md); terraform never creates a key, so no secret is stored in its state."
  value       = aws_iam_user.writer.name
}
