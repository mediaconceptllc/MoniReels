"""Job handlers, one module per job kind (app.jobs.kinds.KINDS).

A handler is handed a JobHandle and returns the job's result. How a job is
claimed, bounded by its lane, kept alive, retried and settled is the loop's
business (app.worker), and so is the registry that maps each kind to its
handler here: `app.worker.HANDLERS`, checked against the kinds at worker
start (`validate_registry`).

`export` and `export_all` are one module: they are one render, of the
timeline or of the suggested ideas.
"""
