"""Source-specific adapters that emit canonical markdown.

Every adapter signature: returns a string of clean markdown plus optional
metadata dict (source_url, title). When JSON-LD recipe data is present in the
source, the adapter embeds it as a labeled fenced block so the downstream
extract step can treat it as authoritative.
"""