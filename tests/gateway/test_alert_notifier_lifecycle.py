"""Gateway alert-notifier lifecycle wiring is fail-isolated."""
from types import SimpleNamespace


def test_gateway_monitoring_startup_is_ordered_and_fail_isolated(monkeypatch):
    import gateway.run as gateway_run
    from agent.monitoring import alert_notifier, gateway_health_export
    from hermes_cli import config as config_module

    calls = []
    runtime = SimpleNamespace(enabled=False)
    monkeypatch.setattr(config_module, "load_config", lambda: {})
    monkeypatch.setattr(
        gateway_health_export,
        "start_gateway_health_export",
        lambda config: calls.append("health") or runtime,
    )
    monkeypatch.setattr(
        alert_notifier,
        "start_alert_notifier",
        lambda config: calls.append("notifier"),
    )

    runner = SimpleNamespace(_gateway_health_export_runtime=None)
    gateway_run._start_gateway_monitoring(runner)
    assert calls == ["health", "notifier"]
    assert runner._gateway_health_export_runtime is runtime

    calls.clear()
    runner._gateway_health_export_runtime = None
    monkeypatch.setattr(
        gateway_health_export,
        "start_gateway_health_export",
        lambda config: (_ for _ in ()).throw(RuntimeError("health")),
    )
    gateway_run._start_gateway_monitoring(runner)
    assert calls == ["notifier"]


def test_gateway_notifier_shutdown_precedes_health_and_survives_failures(monkeypatch):
    import gateway.run as gateway_run
    from agent.monitoring import alert_notifier

    calls = []

    def stop_notifier():
        calls.append("notifier")
        raise RuntimeError("notifier")

    runtime = SimpleNamespace(shutdown=lambda: calls.append("health"))
    runner = SimpleNamespace(_gateway_health_export_runtime=runtime)
    monkeypatch.setattr(alert_notifier, "stop_alert_notifier", stop_notifier)
    monkeypatch.setattr(
        gateway_run.logger,
        "debug",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("logger")),
    )

    gateway_run._shutdown_gateway_health_export(runner)

    assert calls == ["notifier", "health"]
    assert runner._gateway_health_export_runtime is None
