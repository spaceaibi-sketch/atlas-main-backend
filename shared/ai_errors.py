"""Common credential-safe exception formatter for both scoring paths."""
from shared.config import text as setting


def brief_ai_error(exc):
    message = str(exc).replace('\r', ' ').replace('\n', ' ')
    for name in ('OPENAI_API_KEY', 'OPENROUTER_API_KEY'):
        secret = setting(name).strip()
        if secret:
            message = message.replace(secret, '[REDACTED]')
    return f'{type(exc).__name__}: {message[:220]}'
