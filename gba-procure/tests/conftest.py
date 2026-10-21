"""Unit tests must never fall through a missing fixture into business infrastructure."""

import socket

import pymssql
import pytest


@pytest.fixture(autouse=True)
def no_external_unit_io(monkeypatch, request):
    if request.node.get_closest_marker("integration"):
        return

    def forbidden(*args, **kwargs):
        raise AssertionError("external I/O is forbidden in an offline unit test")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(pymssql, "connect", forbidden)

    from app.services.replenishment.warm_requests import REQUEST_WARMS

    # Background work must be explicitly driven by a test, never escape its monkeypatch scope.
    monkeypatch.setattr(REQUEST_WARMS, "submit", lambda key, work: True)
