variable "region" {
  description = "AWS region for the bucket. On a free-plan account this is the Region your account was assigned (AWS Settings); other Regions are blocked there."
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]$", var.region))
    error_message = "region must look like ap-southeast-2."
  }
}

variable "alert_email" {
  description = "Where the budget alerts are sent."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be an email address."
  }
}

variable "create_budget" {
  description = "Create the monthly cost budget with email alerts. Set false if your account does not allow it."
  type        = bool
  default     = true
}

variable "monthly_budget_usd" {
  description = "Monthly cost, in US dollars, at which the budget alerts start."
  type        = number
  default     = 5
}

variable "bucket_name_prefix" {
  description = "Start of the bucket name. The account id and region are added, because bucket names are global."
  type        = string
  default     = "mend-archive"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,28}[a-z0-9]$", var.bucket_name_prefix))
    error_message = "bucket_name_prefix must be 3 to 30 lowercase letters, digits or hyphens, and start and end with a letter or digit."
  }
}

variable "data_prefix" {
  description = "Folder inside the bucket that Mend may write to. The IAM user cannot touch anything outside it."
  type        = string
  default     = "mend"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]*$", var.data_prefix))
    error_message = "data_prefix must be lowercase letters, digits or hyphens."
  }
}

variable "force_destroy" {
  description = "Let terraform destroy delete the bucket even when it holds objects. Leave false so the audit trail cannot be removed by accident."
  type        = bool
  default     = false
}
