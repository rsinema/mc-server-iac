provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = "mc-server"
      ManagedBy = "opentofu"
      Owner     = var.owner_tag
    }
  }
}

provider "cloudflare" {}
