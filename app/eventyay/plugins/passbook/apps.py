from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class PassbookApp(AppConfig):
    name = 'eventyay.plugins.passbook'
    verbose_name = _('Passbook Tickets')

    def ready(self):
        from eventyay.plugins.passbook import signals  # noqa: F401
