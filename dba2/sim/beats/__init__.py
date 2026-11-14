"""Beat resolvers -- one module per beat in the chain
(phase-1-simulation-engine.md §3, §4.3). Each module is a pure function
of `(actors, modifiers, tuning, rng)`; the chain driver that sequences
them (`sim/possession.py`) is step 4, not built by this dispatch.
"""
