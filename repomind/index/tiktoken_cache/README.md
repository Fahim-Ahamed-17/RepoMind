# Vendored `tiktoken` encoding

`9b5ad71b2ce5302211f9c61530b329a4922fc6a4` is the `cl100k_base` merge-rank
table (~1.6 MB, sha256 `223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7`).

It is shipped here rather than downloaded because `tiktoken` otherwise
fetches it from `openaipublic.blob.core.windows.net` the first time an
encoding is built — and `index/chunker.py` builds one while chunking
**every** file, putting an HTTP call in the indexing path. AGENTS.md
invariant 1 says indexing never touches the network, and
`tests/invariants/test_no_network_during_index.py` enforces it; before
this file existed, that test passed only on machines whose `tiktoken`
cache happened to be warm already, and failed on every cold CI runner.

## Why the opaque name

`tiktoken` looks cache entries up by `sha1` of the blob URL:

```
sha1("https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken")
  == 9b5ad71b2ce5302211f9c61530b329a4922fc6a4
```

So the filename is what makes the file findable at all — renaming it to
something readable would send `tiktoken` back to the network. `chunker.py`
points `TIKTOKEN_CACHE_DIR` at this directory for the one call that builds
the encoding.

## If `tiktoken` ever changes that URL

The lookup would miss, and the invariant test would fail on a cold cache —
loudly, which is the point. Recompute the name from the new URL and
re-vendor. The dependency is pinned (`tiktoken>=0.7,<0.9`) partly to bound
that risk.
