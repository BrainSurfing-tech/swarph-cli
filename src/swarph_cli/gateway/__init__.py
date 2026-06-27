"""swarph mesh-gateway server (bundled).

The coordination/DM server behind the swarph mesh: peer registry, DM
inbox/outbox, feature aggregation + allowlist/caps, lane + service
control. Exposed via the ``swarph gateway serve`` verb. The FastAPI/
uvicorn stack is an optional extra (``pip install "swarph-cli[gateway]"``)
so the core client paths stay dependency-light.
"""
