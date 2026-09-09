"""Signed-in page capture (walker + folder contract).

See docs/capture-folder-contract.md. Self-contained on purpose: nothing
outside this package imports it, so backing out is deleting the folder
(restore point: git tag `pre-capture-walker`).
"""
