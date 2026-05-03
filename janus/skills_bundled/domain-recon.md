---
name: domain-recon
description: Passive domain intel — subdomains, SSL certs, WHOIS, DNS — zero deps, no API keys.
state: quarantined
capabilities:
  code.exec:
    - "python"
  web.fetch:
    - "https://crt.sh/*"
    - "https://dns.google/*"
    - "https://*.iana.org/*"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running domain-recon.

You collect public information about a domain WITHOUT sending traffic to
the target itself. Everything here is observational and uses well-known
public sources. No port scanning, no auth probing, no active enumeration.

Sources you may query:
- **crt.sh** — Certificate Transparency logs:
  `https://crt.sh/?q=%25.<domain>&output=json` returns SAN-disclosed
  subdomains.
- **DNS-over-HTTPS via Google**: `https://dns.google/resolve?name=<host>&type=<rrtype>`
  for A, AAAA, MX, TXT, NS, CAA records.
- **WHOIS** — only via Python's stdlib `socket` to whois.iana.org / the
  TLD's whois server. No third-party WHOIS APIs.
- **System DNS** — `socket.gethostbyname_ex()`, `dns.resolver` if available.

Steps:
1. Confirm the target domain with the user. Only run on domains the user
   owns or has explicit authorization to investigate.
2. Subdomain enumeration via crt.sh JSON. Dedupe by name_value.
3. For each unique subdomain (cap at 25 unless the user raises): resolve A
   records, capture CNAME chains.
4. Pull MX, NS, TXT, CAA for the apex via DoH.
5. Pull WHOIS for the apex (registrar, creation/expiry).
6. Report: a structured table (subdomain → A records / CNAME), DNS records
   for the apex, WHOIS summary. Flag anything unusual (wildcard A,
   unusual TTLs, recently-registered, expired soon).

Never run nmap, masscan, dirb, or any active scanner from this skill —
they belong in a separate, explicitly authorized engagement skill. This
skill is observational only.
