# Issue #83565 — child-process credential-inheritance boundary

This implementation closes the campaign invariant at process creation: lower-trust child processes do not receive trusted Hermes/provider/profile credentials from ambient parent state.

The policy is destination-aware, case-insensitive, and unwraps `_HERMES_FORCE_`, `APPTAINERENV_`, and `SINGULARITYENV_` before classification. Tier-1 credentials and generic credential-shaped variables are denied even if a caller attempts to reintroduce them through passthrough or wrapper names. Provider credentials require an explicit provider-capability mode; privileged secret helpers use allowlist-only environments plus the exact helper capability they need.

The campaign also carries an executable sink ledger. Any newly introduced mapped subprocess call without an explicit environment, any raw `os.environ` child environment, or any post-scrub `env.update(os.environ)` bypass makes the ledger fail.

Compatibility is explicit rather than ambient. The local operator shell may retain the documented AWS/SSH/GPG operator capabilities through the compatibility class. Other child classes do not receive them automatically.
