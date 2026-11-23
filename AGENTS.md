# iCS proxy

- Start with [the code navigation guide](docs/code-navigation.md). Keep charging decisions in the shared proxy.
  Home Assistant and the display submit requests and publish observations.
- Inspect and validate with offline tests. Live charger writes, service restarts, deployments, and publication are
  separate actions. Verify the selected installation and its agreed ceiling before any authorized live action.
- Keep the reserved profile identity, journal keys, and management receipt bindings compatible across upgrades.
- Run representative loopback regressions for code changes. Follow [the release process](docs/release-process.md)
  for a complete qualification tied to the exact source.
- Publish only sanitized test material. Keep device settings, TLS keys, journals, logs, and credentials outside Git.
