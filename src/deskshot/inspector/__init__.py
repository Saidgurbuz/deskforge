"""Browsable, editable inspection of pipeline captures.

`overlay` holds the durable edit layer, `samples` finds and reads captures,
`app` is the request/response layer and `server` is the socket glue. The split
exists so the interesting halves - merging edits and answering requests - are
testable without a socket or a browser.
"""
