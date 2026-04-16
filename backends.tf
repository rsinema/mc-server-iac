terraform {
  backend "s3" {
    # Bucket must be bootstrapped manually first:
    #   aws s3 mb s3://rsinema-mc-server-tf-state --region us-west-2
    #   aws s3api put-bucket-versioning --bucket rsinema-mc-server-tf-state --versioning-configuration Status=Enabled
    bucket       = "rsinema-mc-server-tf-state"
    key          = "mc-server/terraform.tfstate"
    region       = "us-west-2"
    use_lockfile = true
  }
}
