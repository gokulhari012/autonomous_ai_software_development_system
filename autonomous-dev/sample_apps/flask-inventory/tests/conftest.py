import os
import tempfile

import pytest

from inventory import create_app


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: browser tests run with Playwright against a running server (--base-url)")
    config.addinivalue_line("markers", "acceptance: acceptance tests run against staging")


@pytest.fixture
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    application = create_app({"TESTING": True, "DATABASE": path, "SECRET_KEY": "test-only"})
    yield application
    os.unlink(path)


@pytest.fixture
def client(app):
    return app.test_client()
