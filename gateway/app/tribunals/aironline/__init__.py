"""AIROnline (aol1.aironline.in) — Citation Search provider.

    client.py           the site's two endpoints + TLS workaround
    parse.py            HTML fragment -> structured record
    fetch_aironline.py  the provider the router calls
    data/aironline_dropdowns.json   every dropdown combination (read directly)

See README.md for the protocol, the corner cases, and how to build the mapping.

The router (`app/routers/aironline.py`) calls every provider function through
`asyncio.to_thread` so blocking network I/O never stalls the event loop.
"""
