"""Provider-neutral Responses transport policy for News and SEC Item AI."""
from shared.config import number as number_setting, text as setting


def ai_timeout_seconds():
    """AI_TIMEOUT_SECONDS overrides the legacy OPENAI_TIMEOUT_SECONDS value."""
    if setting('AI_TIMEOUT_SECONDS').strip():
        return number_setting('AI_TIMEOUT_SECONDS', 300.0, minimum=1)
    return number_setting('OPENAI_TIMEOUT_SECONDS', 300.0, minimum=1)


def create_scoring_response(client, request, *, timeout_seconds=None):
    """One bounded AI attempt with SDK retries disabled."""
    if timeout_seconds is None:
        timeout_seconds = ai_timeout_seconds()
    with_options = getattr(client, 'with_options', None)
    if callable(with_options):
        return with_options(timeout=timeout_seconds, max_retries=0).responses.create(**request)
    return client.responses.create(**request, timeout=timeout_seconds)
