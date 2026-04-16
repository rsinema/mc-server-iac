data "cloudflare_zone" "root" {
  filter = {
    name = var.domain_name
  }
}

resource "cloudflare_dns_record" "mc_server" {
  zone_id = data.cloudflare_zone.root.zone_id
  name    = var.subdomain
  content = var.eip_address
  type    = "A"
  proxied = false
  ttl     = 60
}
