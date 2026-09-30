"""Turning the corpus into a publishable dataset.

`keys` fixes the identifier scheme, `schema` the release record. Both are
imported by every release script so a key means the same thing in the tar, in
the Parquet index and in the checksum file.
"""
