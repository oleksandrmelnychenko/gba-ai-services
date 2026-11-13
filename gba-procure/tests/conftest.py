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


@pytest.fixture(autouse=True)
def per_producer_readers_by_default(monkeypatch, request):
    if request.node.get_closest_marker("integration") or request.node.get_closest_marker("shared_inputs"):
        return
    from app.services.replenishment import policy, worker

    monkeypatch.setattr(
        worker,
        "_products_by_producer",
        lambda producer_ids, as_of: {
            pid: policy.repo.products_for_producer(pid, as_of, policy.get_settings().history_days)
            for pid in producer_ids
        },
    )
    monkeypatch.setattr(worker, "_load_inputs", lambda *args: None)


@pytest.fixture(autouse=True)
def no_supply_documents_by_default(monkeypatch, request):
    if request.node.get_closest_marker("integration") or request.node.get_closest_marker("derived_terms"):
        return
    from app.services.replenishment import derived_terms

    monkeypatch.setattr(derived_terms, "derive", lambda *a, **k: [])


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "shared_inputs: exercises the worker's run-scoped set-based readers"
    )
    config.addinivalue_line(
        "markers", "derived_terms: reads the test's own supply documents for derived terms"
    )
    config.addinivalue_line(
        "markers", "order_units: uses the test's own measure units and document costs"
    )


@pytest.fixture(autouse=True)
def no_shared_cart_inputs_by_default(monkeypatch, request):
    if request.node.get_closest_marker("integration") or request.node.get_closest_marker("shared_inputs"):
        return
    from app.services.replenishment import worker

    monkeypatch.setattr(
        worker,
        "cart_inputs",
        lambda as_of, active_days=None, *, products_by_producer=None: (products_by_producer, None),
    )


@pytest.fixture(autouse=True)
def continuous_units_and_no_document_costs_by_default(monkeypatch, request):
    if request.node.get_closest_marker("integration") or request.node.get_closest_marker("order_units"):
        return
    from app.services.replenishment import policy

    monkeypatch.setattr(policy.repo, "product_measure_units", lambda ids: dict.fromkeys(ids, "кг"))
    monkeypatch.setattr(policy.repo, "last_document_costs_eur", lambda *args, **kwargs: {})
