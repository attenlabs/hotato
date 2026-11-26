variable "project_id" {
  type        = string
  default     = "hotato-autopilot"
  description = "Dedicated Google Cloud project. Created, with its budgets and base APIs, by the owner's one-time setup script."
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "phase" {
  type        = string
  default     = "sprint"
  description = "sprint = the sprint spend limits apply; monthly = the steady monthly limits apply. The budgets themselves are made by the owner's one-time setup script."
  validation {
    condition     = contains(["sprint", "monthly"], var.phase)
    error_message = "phase must be sprint or monthly."
  }
}

# Spend limits in USD, mirrored from the budgets one-time.sh creates. Jobs enforce them with a ledger.
variable "sprint_budget_usd" {
  type = object({ total = number, agents = number, simulated_calls = number, evaluation = number, infrastructure = number })
  default = {
    total           = 2000
    agents          = 1400
    simulated_calls = 300
    evaluation      = 200
    infrastructure  = 100
  }
}

variable "monthly_budget_usd" {
  type = object({ total = number, agents = number, simulated_calls = number, infrastructure = number, buffer = number })
  default = {
    total           = 500
    agents          = 300
    simulated_calls = 100
    infrastructure  = 50
    buffer          = 50
  }
}

variable "github_repo" {
  type        = string
  description = "owner/name of the hotato repo."
}

variable "alert_email" {
  type        = string
  description = "Email for uptime alerts."
}

variable "monitor_public" {
  type        = bool
  default     = false
  description = "Allow unauthenticated access to the hosted monitoring service."
}


variable "enable_secrets" {
  type        = bool
  default     = false
  description = "Wire Secret Manager values into jobs. Set true only after every secret has a version."
}

variable "site_url" {
  type    = string
  default = "https://hotato.dev"
}
