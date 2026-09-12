"""Buy or Wait? -- deterministic financial decision engine.

Module map (see docs/architecture.md once M4 lands):

    money.py   Decimal parsing, rounding policy, dataset rendering convention
    schema.py  output contract, closed input vocabularies, typed records
    data.py    strict loaders, indexes, request scoping, input hashing
    audit.py   cross-row structural and referential dataset audit

Nothing in this package imports a model provider or touches the network.
"""
