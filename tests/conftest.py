import os

os.environ["DATABASE_URL"] = "sqlite:///./test.db"
os.environ["COOKIE_SECURE"] = "0"
import tempfile
os.environ["UPLOAD_DIR"] = tempfile.mkdtemp(prefix="ctf-test-uploads-")

import pytest
from fastapi.testclient import TestClient

import re

from app import boardcache, mailer
from app.db import Base, engine
from app.main import app
from app.security import limiter

PW = "correct-horse-battery"


def last_code(email):
    """The newest 6-digit code mailed to this address (development outbox)."""
    for m in reversed(mailer.OUTBOX):
        if m["to"] == email.lower():
            found = re.search(r"\b(\d{6})\b", m["text"])
            if found:
                return found.group(1)
    raise AssertionError(f"no code was mailed to {email}")


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    limiter._events.clear()
    boardcache.clear()  # ids restart with each fresh database
    mailer.OUTBOX.clear()
    yield


class Client:
    """TestClient that remembers the CSRF token like the browser app will."""

    def __init__(self):
        self.c = TestClient(app)
        self.csrf = ""

    def register(self, name, email=None):
        """Sign up the way the browser does: email, username and password in one step."""
        email = email or f"{name}@x.io"
        r = self.c.post("/api/auth/register", json={"email": email, "username": name, "password": PW})
        assert r.status_code == 201, r.text
        self.csrf = r.json()["csrf"]
        return r

    def get(self, url):
        return self.c.get(url)

    def post(self, url, json=None, csrf=True):
        return self.c.post(url, json=json, headers={"X-CSRF-Token": self.csrf} if csrf else {})

    def patch(self, url, json=None):
        return self.c.patch(url, json=json, headers={"X-CSRF-Token": self.csrf})

    def put(self, url, json=None):
        return self.c.put(url, json=json, headers={"X-CSRF-Token": self.csrf})

    def delete(self, url):
        return self.c.delete(url, headers={"X-CSRF-Token": self.csrf})

    def upload(self, url, name, data: bytes):
        return self.c.post(url, files={"file": (name, data)}, headers={"X-CSRF-Token": self.csrf})


@pytest.fixture
def client():
    return Client


@pytest.fixture
def org_with_event():
    """An organiser with org 'cysec' and a published event 'fall-ctf'."""
    o = Client()
    o.register("organiser")
    assert o.post("/api/orgs", {"name": "CySec Club", "slug": "cysec"}).status_code == 201
    assert o.post("/api/orgs/cysec/events", {"slug": "fall-ctf", "title": "Fall CTF",
                                             "max_team_size": 2}).status_code == 201
    assert o.patch("/api/events/fall-ctf", {"status": "published"}).status_code == 200
    return o
