"""Utilities for controlling Android devices through ADB and scrcpy."""

__all__ = [
    "AdbCommandError",
    "AdbClient",
    "AdbError",
    "AdbResult",
    "Device",
    "DeviceManager",
    "DeviceNotFoundError",
    "DeviceStateError",
    "ScrcpyAlreadyRunningError",
    "ScrcpyError",
    "ScrcpyManager",
    "ScrcpyProcessError",
    "ScrcpySession",
    "ScreenGeometry",
    "GeometryProvider",
    "DeviceHealthMonitor",
    "DeviceHealthError",
    "HealthReport",
    "FpsMeter",
    "LatencyProbe",
    "LatencySample",
    "RecognitionPipeline",
    "RecognitionResult",
    "TemplateMatcher",
    "UiAutomatorAdapter",
    "RuntimeConfig",
    "SafeAdbClient",
    "SafetyController",
    "DryRunResult",
    "EmergencyStop",
    "WorkflowContext",
    "WorkflowError",
    "WorkflowResult",
    "WorkflowRunner",
    "WorkflowStep",
    "WorkflowQueue",
    "QueueItem",
    "QueueControl",
]


def __getattr__(name: str):
    # Lazy loading keeps ``python -m adb_scrcpy.device_manager`` warning-free.
    if name in __all__:
        from . import adb_client, config, device_health, device_manager, geometry, metrics, recognition, recognition_pipeline, safety, scrcpy_manager, workflow, workflow_queue

        if name.startswith("Adb"):
            module = adb_client
        elif name.startswith("Scrcpy"):
            module = scrcpy_manager
        elif name.startswith("Workflow"):
            module = workflow_queue if name == "WorkflowQueue" else workflow
        elif name in {"QueueItem", "QueueControl"}:
            module = workflow_queue
        elif name in {"RuntimeConfig"}:
            module = config
        elif name in {"SafeAdbClient", "SafetyController", "DryRunResult", "EmergencyStop"}:
            module = safety
        elif name in {"ScreenGeometry", "TemplateMatcher", "UiAutomatorAdapter"}:
            module = recognition
        elif name in {"GeometryProvider"}:
            module = geometry
        elif name in {"DeviceHealthMonitor", "DeviceHealthError", "HealthReport"}:
            module = device_health
        elif name in {"FpsMeter", "LatencyProbe", "LatencySample"}:
            module = metrics
        elif name in {"RecognitionPipeline", "RecognitionResult"}:
            module = recognition_pipeline
        else:
            module = device_manager
        return getattr(module, name)
    raise AttributeError(name)
