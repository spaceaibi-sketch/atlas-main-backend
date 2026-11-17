"""Print Atlas cron intervals from .env for install_cron.sh (safe, no eval)."""
from shared.config import integer

FIELDS = (
    'ATLAS_CRON_SEC_FILINGS_HOURS', 'ATLAS_CRON_SEC_ITEMS_HOURS',
    'ATLAS_CRON_NEWS_HOURS', 'ATLAS_CRON_SEC_AI_HOURS',
    'ATLAS_CRON_NEWS_AI_HOURS', 'ATLAS_CRON_MASSIVE_PRICES_HOURS',
)

def main():
    for field in FIELDS:
        # SEC jobs remain hourly; news ingestion and Massive refresh default to 3h.
        default_hours = 3 if field in ('ATLAS_CRON_NEWS_HOURS',
                                       'ATLAS_CRON_NEWS_AI_HOURS',
                                       'ATLAS_CRON_MASSIVE_PRICES_HOURS') else 1
        print(integer(field, default_hours, minimum=0, maximum=8760))

if __name__ == '__main__':
    main()
