"""Grounded chat (SPEC.md §9.6, §10): context, prompt, citation validation, abstention.

Every question retrieves fresh evidence. The model sees only numbered excerpts
([S1], [S2], ...) as untrusted data, and an answer is shown only when it cites
at least one of them. Otherwise the assistant abstains.
"""
