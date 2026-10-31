from collections import OrderedDict

from django import forms
from django.core.files import File
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _

from eventyay.base.settings import settings_hierarkey
from eventyay.base.signals import register_global_settings, register_ticket_outputs
from eventyay.plugins.passbook.forms import CertificateFileField, validate_rsa_privkey


@receiver(register_ticket_outputs, dispatch_uid='output_passbook')
def register_ticket_output(sender, **kwargs):
    from eventyay.plugins.passbook.ticketoutput import PassbookTicketOutput

    return PassbookTicketOutput


@receiver(register_global_settings, dispatch_uid='passbook_global_settings')
def register_passbook_global_settings(sender, **kwargs):
    return OrderedDict(
        [
            (
                'passbook_team_id',
                forms.CharField(
                    label=_('Passbook team ID'),
                    required=False,
                    help_text=_('Your Apple Developer Team ID (e.g., AGK5BZEN3E).'),
                ),
            ),
            (
                'passbook_pass_type_id',
                forms.CharField(
                    label=_('Passbook Pass Type ID'),
                    required=False,
                    help_text=_('Your Apple Pass Type identifier (e.g., pass.com.yourorg.events).'),
                ),
            ),
            (
                'passbook_certificate_file',
                CertificateFileField(
                    label=_('Passbook certificate file'),
                    required=False,
                    help_text=_('Apple Pass Type ID signing certificate (.pem or .cer).'),
                ),
            ),
            (
                'passbook_wwdr_certificate_file',
                CertificateFileField(
                    label=_('Passbook CA Certificate'),
                    required=False,
                    help_text=_(
                        'Apple Worldwide Developer Relations (WWDR) CA certificate from '
                        'https://www.apple.com/certificateauthority/AppleWWDRCAG4.cer'
                    ),
                ),
            ),
            (
                'passbook_key',
                forms.CharField(
                    label=_('Passbook secret key'),
                    required=False,
                    widget=forms.Textarea,
                    validators=[validate_rsa_privkey],
                    help_text=_('PEM private key associated with the Pass Type ID certificate.'),
                ),
            ),
            (
                'passbook_key_password',
                forms.CharField(
                    label=_('Passbook key password'),
                    widget=forms.PasswordInput(render_value=True),
                    required=False,
                    help_text=_('Optional, only necessary if the private key requires a password to use.'),
                ),
            ),
        ]
    )


settings_hierarkey.add_default('passbook_certificate_file', None, File)
settings_hierarkey.add_default('passbook_wwdr_certificate_file', None, File)
settings_hierarkey.add_default('ticketoutput_passbook_background', None, File)
settings_hierarkey.add_default('ticketoutput_passbook_background2x', None, File)
settings_hierarkey.add_default('ticketoutput_passbook_background3x', None, File)
settings_hierarkey.add_default('ticketoutput_passbook_icon', None, File)
settings_hierarkey.add_default('ticketoutput_passbook_icon2x', None, File)
settings_hierarkey.add_default('ticketoutput_passbook_icon3x', None, File)
settings_hierarkey.add_default('ticketoutput_passbook_logo', None, File)
settings_hierarkey.add_default('ticketoutput_passbook_logo2x', None, File)
settings_hierarkey.add_default('ticketoutput_passbook_logo3x', None, File)

