"""`dba2/media/write/` -- docs/design/media-v2-newsroom.md MV-6: the writer
seam's one shippable-today implementation. `budget.py` is MV-4's tier
selection and per-tier rendering budgets, pure; `template.py` is
`TemplateWriter`, the `WriterBackend` (`dba2.domain.media_writer`)
implementation built from the existing template-selection machinery
(`dba2.media.select`), generalized to the package's own `Fact.key` set
instead of a fixed nine-slot resolver list.

Zero I/O, the same purity rules every other module under `dba2/media/`
carries (`tests/unit/test_layering.py`'s automatic sweep covers this
package the moment it exists, per MV-9's own note -- no new machinery
needed): no `dba2.data`, no `datetime.now()`, no bare `random.Random(`, no
`open(`, no `os.environ`. `dba2/services/media_writers.py` (MV-6) is where
`WRITER_BACKENDS` lives, one layer up, specifically because a REGISTRY
that could name a future `AnthropicWriter` (which lives under `dba2.
services`) cannot live here -- `test_media_module_source_imports_only_
domain_core_managers_and_agency` restricts this package to importing only
`domain`/`core`/`managers`/`agency`.
"""
