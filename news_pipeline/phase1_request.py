"""Timeout policy for News Phase 1 AI requests only.

The OpenAI client is configured independently. Phase 1
consists of News event deduplication and company discovery, each of which can
require more time for large batches.
"""

from shared.config import number as number_setting

PHASE1_AI_TIMEOUT_SECONDS = 300.0  # Default; overridden from .env for each request.


def create_phase1_response(client, request):
    """Use a 5-minute single-attempt limit without altering the shared client.

    The official OpenAI SDK supports ``with_options(timeout=..., max_retries=0)``.
    For simple test clients that only implement ``responses.create``, pass the
    timeout at request level (which the SDK also supports).
    """
    timeout_seconds = number_setting('NEWS_PHASE1_TIMEOUT_SECONDS', PHASE1_AI_TIMEOUT_SECONDS, minimum=1)
    with_options = getattr(client, "with_options", None)
    if callable(with_options):
        scoped = with_options(timeout=timeout_seconds, max_retries=0)
        return scoped.responses.create(**request)
    return client.responses.create(**request, timeout=timeout_seconds)
