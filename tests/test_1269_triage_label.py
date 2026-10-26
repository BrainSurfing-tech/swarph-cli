"""Card #1269. A new triage container is created with the triage-row
label. Fails at dba1ebc: the create payload has no labels.
"""
from swarph_cli.triage.backend import BoardBackend


class _Http:
    def __init__(self):
        self.posts = []

    def post(self, url, payload, token):
        self.posts.append((url, payload))
        return 200, {"id": 77}

    def get(self, url, token):
        raise AssertionError(url)


def test_create_labels_the_container_triage_row():
    http = _Http()
    backend = BoardBackend(
        "http://gw:9", "tok", "lab-ovh", http.get, http.post, project_id=9)
    backend.create_row("droplet triage", "droplet")
    _url, payload = http.posts[-1]
    assert payload.get("labels") == ["triage-row"]
