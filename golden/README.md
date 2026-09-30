# golden/

Hand-made annotation corrections and curation marks, one JSON document per
sample, mirroring the run's directory layout:

    golden/<run>/<stem>.<view>.json

This directory is committed. `incremental_checks/`, which it describes, is not:
it is regenerated and gitignored, so an edit made in place there would be lost.
Reading a sample means pipeline output with the matching overlay applied on top.

Written and read by the inspector:

    PYTHONPATH=src python scripts/inspect_annotations.py

The document format and element-key scheme are defined in
`src/deskshot/inspector/overlay.py`.
