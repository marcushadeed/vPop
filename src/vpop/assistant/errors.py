"""Errors the assistant raises, and one-line explanations of the ones its backends raise."""

import sys

from vpop.db import DatabaseError


class AuthError(RuntimeError):
    """The model's API has no credentials, or rejected them. The message says what to do."""


class MissingCredentialsError(AuthError):
    """No credentials were found at all, so `vpop auth login` can fix it."""


def api_error_detail(error: object) -> str:
    """The API's own error message ("API key is invalid.") from an Anthropic status error."""
    body = getattr(error, "body", None)
    detail = body.get("error", {}).get("message") if isinstance(body, dict) else None
    return str(detail or getattr(error, "message", error))


def describe_error(exc: BaseException) -> str | None:  # pylint: disable=too-many-return-statements
    """
    A one-line explanation of a failure the user can act on (a backend that's down, a model
    that isn't pulled, a rate limit), or None for anything unexpected, which should surface
    as a traceback. Backends are looked up in `sys.modules`, so one that was never imported
    isn't imported here just to check.
    """
    if isinstance(exc, DatabaseError):
        return str(exc)
    ollama = sys.modules.get("ollama")
    if ollama is not None and isinstance(exc, ollama.ResponseError):
        if exc.status_code == 404:
            return f"Ollama: {exc.error}. Pull the model with: ollama pull <model>"
        return f"Ollama error ({exc.status_code}): {exc.error}"
    anthropic = sys.modules.get("anthropic")
    if anthropic is not None:
        request = f" (request id {getattr(exc, 'request_id', None)})"
        if isinstance(exc, anthropic.RateLimitError):
            return f"Anthropic rate limit reached; wait a moment and retry{request}"
        if isinstance(exc, anthropic.APIConnectionError):
            return f"couldn't reach the Anthropic API: {exc}"
        if isinstance(exc, anthropic.APIStatusError):
            return f"Anthropic API error {exc.status_code}: {api_error_detail(exc)}{request}"
    if isinstance(exc, ConnectionError):
        # What the Ollama client raises when the server isn't running.
        return f"{exc} Start Ollama with: ollama serve"
    return None
