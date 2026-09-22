"""Formats the agent's execution-budget constraints into a system-prompt string,
and the pure decision logic for stamping tool outputs with a cap-out warning."""


def format_runtime_context(max_iterations: int, max_execution_time: float) -> str:
    return (
        f"Execution budget: you have at most {max_iterations} reasoning/tool-call "
        f"steps and {int(max_execution_time)} seconds of wall-clock time to complete "
        "this task before the agent executor stops automatically. If the task is "
        "large or multi-phase, follow the Multi-steps Completion Format guidance "
        "rather than risk running out of steps mid-task."
    )


def apply_iteration_marker(result, remaining: int, margin: int):
    """Given a tool's raw return value and the iterations remaining before the
    agent executor stops, return the value unchanged if there's still margin
    left, or a value carrying a wrap-up notice once the budget is tight.

    str/dict returns get the notice merged in-shape. Any other return type
    (e.g. a bool, a tuple) is passed through completely untouched — appending
    a string would crash on those types, and coercing them to str would
    silently change the tool's return type for no reason."""
    if remaining > margin:
        return result
    marker = (
        f"\n\n[SYSTEM NOTICE: {remaining} agent step(s) remaining before "
        "the executor stops. Wrap up and provide a Final Answer soon.]"
    )
    if isinstance(result, str):
        return result + marker
    if isinstance(result, dict):
        merged = dict(result)
        merged["_iteration_notice"] = marker.strip()
        return merged
    return result
