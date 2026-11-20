# Which version runs this company?

Norvale owns the company scenario, integrations, checkpoints and dataset provenance. The shared runtime comes from a tested EnterpriseEngine release. This checkout carries the release's runtime files so that a known-good company still starts if upstream changes or disappears.

1. Change shared behavior in EnterpriseEngine, run its tests, then publish a version tag.
2. In a Norvale change, import the tagged runtime files, update the commit and SHA-256 entries in `engine.lock.json`, and make the company-specific adaptation. Verify the pin and run the company regression that exercises the changed behavior.
3. Start a new company on the approved pin. An existing company resumes with its own archived code and private state. Migrate a running company deliberately at a checkpoint with a verified restore point.

Check a source tree or a prepared run before debugging it:

```sh
python3 scripts/check_engine_pin.py
python3 scripts/check_engine_pin.py --release /path/to/company/release
```

Use `backup/native-company-pre-pin` to retrieve the self-contained native company source before pin enforcement. Use `backup/erpnext-email-gitea-q1` to retrieve the published historical quarter bundle on its original dataset branch. The historical quarter and the active full-stack quarter have different software coverage. Git stores code and filtered public evidence; native volumes, keys and private checkpoints stay with the run.
