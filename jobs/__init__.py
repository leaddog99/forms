"""`jobs` — the out-of-process job runner CLI (THE batch entrypoint).

`python -m jobs <subcommand>` is the standalone, server-free way to run the
job system (docs/jobs-as-executables.md). It is the deployment the in-process
runner was reaching for: each run is its own command-line process, so it never
touches uvicorn's event loop (the reason the in-server poll-runner is disabled,
memory/project_job_runner_disabled.md) and a per-run process owns its own
stdout (no interleave → serial-execution constraint lifted).

This package is ONLY the CLI shell. The durable queue, the `_run_one_job`
canonical path, the handler registry, and the entity-lock all live in
`input/pipeline/jobs.py` (imported here as `jobs_lib`) — exactly the same code
the form's Run button drives, so there is one canonical execution path with
several triggers, never a parallel pipeline (memory/feedback_single_path.md).

See `python -m jobs --help` for subcommands.
"""
