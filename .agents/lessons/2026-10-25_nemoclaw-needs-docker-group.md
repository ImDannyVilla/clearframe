---
date: 2026-10-25
issue: #1
area: gb10
severity: important
---

# nemoclaw fails with docker_unreachable or gateway preflight errors without the docker group

## What happened
Right after `sudo usermod -aG docker dell`, plain `docker ps` worked only in new login sessions.
`nemoclaw clearframe status` reported "docker_unreachable — Docker daemon is not reachable",
and a re-onboard died at preflight with "gateway.version.compatible, gateway.port.uncontested"
even though the gateway was running and healthy.

## Root cause
The shell (and any tmux session or script started from it) did not carry the new docker group,
so NemoClaw could not talk to the Docker daemon to inspect the gateway. The errors look like
gateway or network failures but are just missing group membership in that process.

## Fix
Either log out/in, or run NemoClaw through `sg docker -c '...'`, which picks up the new group immediately.
For interactive onboarding in tmux, re-exec the script under `sg docker` first
(`id -nG | grep -qw docker || exec sg docker -c "$0"`). The hosted installer does this reactivation itself.

## Prevention
After any group change, verify with `id` and `sg docker -c 'docker ps'` before running nemoclaw.
Onboarding scripts on the GB10 should include the sg re-exec guard above. Noted on issue #1.
