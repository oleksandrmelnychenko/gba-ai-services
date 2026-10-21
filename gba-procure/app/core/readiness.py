"""Safe errors shared by required input readers and procurement build boundaries."""


class ProcurementBusinessReadinessError(RuntimeError):
    """A required input is unavailable; a new plan must not be published."""
