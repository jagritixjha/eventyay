import json
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from zipfile import ZipFile

import pytest
from django.apps import apps
from django.utils.timezone import now
from django_scopes import scope
from rest_framework.test import APIRequestFactory

from eventyay.api.serializers.order import OrderDownloadsField
from eventyay.base.email import get_combined_ticket_output_identifier
from eventyay.base.models import Event, Order, OrderPosition, Organizer, Product, ProductVariation
from eventyay.base.services import tickets
from eventyay.base.signals import register_ticket_outputs
from eventyay.control.forms.global_settings import GlobalTicketingSettingsForm
from eventyay.plugins.passbook.ticketoutput import PassbookTicketOutput, hex_to_rgb


@pytest.fixture
def env():
    organizer = Organizer.objects.create(name='Passbook Org', slug='passbook-org')
    event = Event.objects.create(
        organizer=organizer,
        name='Passbook Conference',
        slug='passbook-conf',
        date_from=now(),
        date_to=now() + timedelta(days=2),
        live=True,
    )
    product = Product.objects.create(
        event=event,
        name='Standard Admission',
        default_price=Decimal('50.00'),
    )
    variation = ProductVariation.objects.create(
        product=product,
        value='General Access',
        default_price=Decimal('50.00'),
    )
    order = Order.objects.create(
        code='PASSB',
        event=event,
        email='attendee@example.com',
        status=Order.STATUS_PAID,
        datetime=now(),
        expires=now() + timedelta(days=5),
        total=Decimal('50.00'),
    )
    position = OrderPosition.objects.create(
        order=order,
        product=product,
        variation=variation,
        price=Decimal('50.00'),
        attendee_name_parts={'full_name': 'Alice Wonderland'},
        secret='SECRET1234567890',
        positionid=1,
    )
    return organizer, event, order, position


def test_hex_to_rgb():
    assert hex_to_rgb('#1B4D6B') == 'rgb(27, 77, 107)'
    assert hex_to_rgb('#FFF') == 'rgb(255, 255, 255)'
    assert hex_to_rgb('invalid') == 'rgb(27, 77, 107)'


def test_plugin_registered():
    app_config = apps.get_app_config('passbook')
    assert app_config.name == 'eventyay.plugins.passbook'


@pytest.mark.django_db
def test_output_properties_and_defaults(env):
    organizer, event, order, position = env
    with scope(organizer=organizer, event=event):
        output = PassbookTicketOutput(event)
        assert output.identifier == 'passbook'
        assert output.multi_download_enabled is False
        assert output.download_button_icon == 'fa-mobile'

        fields = output.settings_form_fields
        assert '_enabled' in fields
        assert 'selfscale' in fields
        assert 'icon' in fields
        assert 'icon2x' in fields
        assert 'logo' in fields
        assert 'bg_color' in fields
        assert 'fg_color' in fields
        assert 'label_color' in fields
        assert 'latitude' in fields
        assert 'longitude' in fields

        assert fields['icon2x'].widget.attrs.get('data-display-dependency') == '#id_ticketoutput_passbook_selfscale'
        assert fields['logo2x'].widget.attrs.get('data-display-dependency') == '#id_ticketoutput_passbook_selfscale'

        # Signal registration
        responses = [resp(event) for _rcv, resp in register_ticket_outputs.send(event)]
        output_classes = [p.__class__ for p in responses]
        assert PassbookTicketOutput in output_classes


@pytest.mark.django_db
def test_generate_pkpass_archive(env):
    organizer, event, order, position = env
    with scope(organizer=organizer, event=event):
        output = PassbookTicketOutput(event)
        filename, mimetype, data = output.generate(position)

        assert filename == 'passbook-conf-PASSB-1.pkpass'
        assert mimetype == 'application/vnd.apple.pkpass'
        assert isinstance(data, bytes)
        assert len(data) > 0

        with ZipFile(BytesIO(data), 'r') as zf:
            namelist = zf.namelist()
            assert 'pass.json' in namelist
            assert 'manifest.json' in namelist
            assert 'signature' in namelist
            assert 'icon.png' in namelist
            assert 'logo.png' in namelist

            pass_json = json.loads(zf.read('pass.json').decode('utf-8'))
            assert pass_json['formatVersion'] == 1
            assert pass_json['serialNumber'] == 'passbook-org-passbook-conf-PASSB-1'
            assert 'eventTicket' in pass_json
            assert 'barcode' in pass_json
            assert 'barcodes' in pass_json
            assert pass_json['barcode']['message'] == 'SECRET1234567890'
            assert pass_json['barcode']['format'] == 'PKBarcodeFormatQR'
            assert pass_json['barcodes'][0]['message'] == 'SECRET1234567890'

            manifest_json = json.loads(zf.read('manifest.json').decode('utf-8'))
            assert 'pass.json' in manifest_json
            assert 'icon.png' in manifest_json
            assert 'logo.png' in manifest_json


@pytest.mark.django_db
def test_preview_generation(env):
    organizer, event, order, position = env
    with scope(organizer=organizer, event=event):
        fname, mimet, data = tickets.preview(event.pk, 'passbook')
        assert mimet == 'application/vnd.apple.pkpass'
        assert isinstance(data, bytes)
        assert len(data) > 0

        with ZipFile(BytesIO(data), 'r') as zf:
            assert 'pass.json' in zf.namelist()
            assert 'manifest.json' in zf.namelist()
            assert 'signature' in zf.namelist()


@pytest.mark.django_db
def test_combined_download_exclusion(env):
    organizer, event, order, position = env
    with scope(organizer=organizer, event=event):
        event.settings.ticketoutput_passbook__enabled = True
        output = PassbookTicketOutput(event)
        assert output.is_enabled is True
        assert output.multi_download_enabled is False

        factory = APIRequestFactory()
        request = factory.get('/')
        downloads_field = OrderDownloadsField()
        downloads_field._context = {'request': request}
        downloads = downloads_field.to_representation(order)

        passbook_downloads = [d for d in downloads if d['output'] == 'passbook']
        assert len(passbook_downloads) == 0

        combined_id = get_combined_ticket_output_identifier(event)
        assert combined_id != 'passbook'


@pytest.mark.django_db
def test_global_ticketing_settings_form():
    form = GlobalTicketingSettingsForm()
    group_keys = [g[0] for g in form.field_groups]
    assert 'passbook' in group_keys

    expected_fields = [
        'passbook_team_id',
        'passbook_pass_type_id',
        'passbook_certificate_file',
        'passbook_wwdr_certificate_file',
        'passbook_key',
        'passbook_key_password',
    ]
    for field_name in expected_fields:
        assert field_name in form.fields

    passbook_group = next(g for g in form.field_groups if g[0] == 'passbook')
    for field_name in expected_fields:
        assert field_name in passbook_group[2]


def test_png_imAn unexpected error has occurred, please try again later.age_field_clean():
    from eventyay.plugins.passbook.forms import PNGImageField

    field = PNGImageField(required=False)
    # Clearing file returns False
    assert field.clean(False) is False
    # Retaining initial file when no new upload is provided
    sentinel = object()
    assert field.clean(None, initial=sentinel) is sentinel


def test_certificate_file_field_clean():
    from eventyay.plugins.passbook.forms import CertificateFileField

    field = CertificateFileField(required=False)
    # Clearing file returns False
    assert field.clean(False) is False
    # Retaining initial file when no new upload is provided
    sentinel = object()
    assert field.clean(None, initial=sentinel) is sentinel


