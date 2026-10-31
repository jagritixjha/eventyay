import hashlib
import io
import json
import logging
import os
import subprocess
import tempfile
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zipfile import ZIP_DEFLATED, ZipFile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID
from django import forms
from django.contrib.staticfiles import finders
from django.core.files import File
from django.core.files.storage import default_storage
from django.core.validators import RegexValidator
from django.utils.timezone import is_aware, make_aware, now
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from PIL import Image

from eventyay.base.models import OrderPosition
from eventyay.base.ticketoutput import BaseTicketOutput
from eventyay.common.forms.widgets import ClearableBasenameFileInput
from eventyay.multidomain.urlreverse import build_absolute_uri
from eventyay.plugins.passbook.forms import PNGImageField

logger = logging.getLogger('eventyay.plugins.passbook')


def hex_to_rgb(hex_color: str) -> str:
    color = (hex_color or '').lstrip('#')
    if len(color) == 3:
        color = ''.join(c * 2 for c in color)
    try:
        r = int(color[0:2], 16)
        g = int(color[2:4], 16)
        b = int(color[4:6], 16)
        return f'rgb({r}, {g}, {b})'
    except (ValueError, IndexError):
        return 'rgb(27, 77, 107)'


def read_content(value) -> bytes:
    if not value:
        return b''
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if hasattr(value, 'open') and getattr(value, 'closed', False):
        try:
            value.open('rb')
        except Exception:
            pass
    if hasattr(value, 'read'):
        try:
            if hasattr(value, 'seek'):
                value.seek(0)
            data = value.read()
            if hasattr(value, 'seek'):
                value.seek(0)
            if isinstance(data, str):
                return data.encode('utf-8')
            return bytes(data)
        except (AttributeError, OSError, ValueError):
            pass
    if hasattr(value, 'name'):
        storage_path = value.name[7:] if value.name.startswith('file://') else value.name
        try:
            with default_storage.open(storage_path, 'rb') as file_obj:
                return file_obj.read()
        except (OSError, ValueError):
            pass
    if isinstance(value, str):
        if '-----BEGIN' in value:
            return value.encode('utf-8')
        storage_path = value[7:] if value.startswith('file://') else value
        try:
            with default_storage.open(storage_path, 'rb') as file_obj:
                return file_obj.read()
        except (OSError, ValueError):
            return b''
    return b''


def create_blank_png(width: int, height: int) -> bytes:
    img = Image.new('RGBA', (width, height), (255, 255, 255, 0))
    out = io.BytesIO()
    img.save(out, format='PNG')
    return out.getvalue()


def resize_image(content: bytes, size: tuple[int, int]) -> bytes:
    if not content:
        return create_blank_png(size[0], size[1])
    try:
        with Image.open(io.BytesIO(content)) as img:
            converted = img.convert('RGBA')
            converted.thumbnail(size, Image.Resampling.LANCZOS)
            background = Image.new('RGBA', size, (255, 255, 255, 0))
            offset = ((size[0] - converted.size[0]) // 2, (size[1] - converted.size[1]) // 2)
            background.paste(converted, offset)
            out = io.BytesIO()
            background.save(out, format='PNG', optimize=True)
            return out.getvalue()
    except (OSError, ValueError):
        return create_blank_png(size[0], size[1])


@lru_cache(maxsize=1)
def get_fallback_credentials():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, 'US'),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'Eventyay'),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, 'EVENTYAY10'),
        x509.NameAttribute(NameOID.COMMON_NAME, 'Pass Type ID: pass.com.eventyay.ticket'),
    ])
    dt = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt - timedelta(days=1))
        .not_valid_after(dt + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert, key


class PassbookTicketOutput(BaseTicketOutput):
    identifier = 'passbook'
    verbose_name = _('Passbook Tickets')
    download_button_icon = 'fa-mobile'
    download_button_text = _('Wallet/Passbook')
    long_download_button_text = _('Add to Apple Wallet / Passbook')
    multi_download_enabled = False

    @property
    def settings_form_fields(self) -> dict:
        color_validator = RegexValidator(
            regex=r'^#[0-9a-fA-F]{6}$',
            message=_('Enter a color in #RRGGBB format.'),
        )
        return OrderedDict(
            list(super().settings_form_fields.items())
            + [
                (
                    'selfscale',
                    forms.BooleanField(
                        label=_('I would like to scale the graphics myself'),
                        help_text=_(
                            'In some instances, the downscaling of graphics done by the Wallet-app is not satisfactory. '
                            'By checking this box, you can provide prescaled files in the correct dimensions.<br><br>'
                            'If you choose to do so, please only upload your pictures in the regular display size '
                            'and not the increased retina size.'
                        ),
                        required=False,
                    ),
                ),
                (
                    'icon',
                    PNGImageField(
                        label=_('Event icon'),
                        help_text=_(
                            'Display size is 29 x 29 pixels. We suggest an upload size of 87 x 87 pixels to support retina displays.'
                        ),
                        required=False,
                    ),
                ),
                (
                    'icon2x',
                    PNGImageField(
                        label=_('Event icon for Retina 2x displays'),
                        help_text=_('Display size is 58 x 58 pixels.'),
                        widget=ClearableBasenameFileInput(
                            attrs={'data-display-dependency': '#id_ticketoutput_passbook_selfscale'}
                        ),
                        required=False,
                    ),
                ),
                (
                    'icon3x',
                    PNGImageField(
                        label=_('Event icon for Retina 3x displays'),
                        help_text=_('Display size is 87 x 87 pixels.'),
                        widget=ClearableBasenameFileInput(
                            attrs={'data-display-dependency': '#id_ticketoutput_passbook_selfscale'}
                        ),
                        required=False,
                    ),
                ),
                (
                    'logo',
                    PNGImageField(
                        label=_('Event logo'),
                        help_text=_(
                            'Display size is 160 x 50 pixels. We suggest an upload size of 480 x 150 pixels to support retina displays.'
                        ),
                        required=False,
                    ),
                ),
                (
                    'logo2x',
                    PNGImageField(
                        label=_('Event logo for Retina 2x displays'),
                        help_text=_('Display size is 320 x 100 pixels.'),
                        widget=ClearableBasenameFileInput(
                            attrs={'data-display-dependency': '#id_ticketoutput_passbook_selfscale'}
                        ),
                        required=False,
                    ),
                ),
                (
                    'logo3x',
                    PNGImageField(
                        label=_('Event logo for Retina 3x displays'),
                        help_text=_('Display size is 480 x 150 pixels.'),
                        widget=ClearableBasenameFileInput(
                            attrs={'data-display-dependency': '#id_ticketoutput_passbook_selfscale'}
                        ),
                        required=False,
                    ),
                ),
                (
                    'background',
                    PNGImageField(
                        label=_('Pass background image'),
                        help_text=_(
                            'Display size is 180 x 220 pixels. We suggest an upload size of 540 x 660 pixels to support retina displays. '
                            'Please note: iOS Wallet seems to ignore custom text color and uses white text if a background image is used. '
                            'Please use a dark background image to provide sufficient text contrast.'
                        ),
                        required=False,
                    ),
                ),
                (
                    'background2x',
                    PNGImageField(
                        label=_('Pass background image for Retina 2x displays'),
                        widget=ClearableBasenameFileInput(
                            attrs={'data-display-dependency': '#id_ticketoutput_passbook_selfscale'}
                        ),
                        required=False,
                    ),
                ),
                (
                    'background3x',
                    PNGImageField(
                        label=_('Pass background image for Retina 3x displays'),
                        widget=ClearableBasenameFileInput(
                            attrs={'data-display-dependency': '#id_ticketoutput_passbook_selfscale'}
                        ),
                        required=False,
                    ),
                ),
                (
                    'bg_color',
                    forms.CharField(
                        label=_('Background color'),
                        help_text=_('If you use a background image, the background color will have no effect.'),
                        required=False,
                        initial='#1B4D6B',
                        validators=[color_validator],
                        widget=forms.TextInput(
                            attrs={'class': 'colorpickerfield no-contrast', 'placeholder': '#1B4D6B'}
                        ),
                    ),
                ),
                (
                    'fg_color',
                    forms.CharField(
                        label=_('Text color'),
                        help_text=_('If you use a background image, iOS Wallet ignores the custom text color.'),
                        required=False,
                        initial='#FFFFFF',
                        validators=[color_validator],
                        widget=forms.TextInput(
                            attrs={'class': 'colorpickerfield no-contrast', 'placeholder': '#FFFFFF'}
                        ),
                    ),
                ),
                (
                    'label_color',
                    forms.CharField(
                        label=_('Label color'),
                        required=False,
                        initial='#D9E8F0',
                        validators=[color_validator],
                        widget=forms.TextInput(
                            attrs={'class': 'colorpickerfield no-contrast', 'placeholder': '#D9E8F0'}
                        ),
                    ),
                ),
                (
                    'latitude',
                    forms.FloatField(
                        label=_('Event location (latitude)'),
                        help_text=_('Will be taken from event settings by default.'),
                        required=False,
                    ),
                ),
                (
                    'longitude',
                    forms.FloatField(
                        label=_('Event location (longitude)'),
                        help_text=_('Will be taken from event settings by default.'),
                        required=False,
                    ),
                ),
            ]
        )

    def generate_pass_data(self, order_position: OrderPosition) -> dict:
        order = order_position.order
        ev = order_position.subevent or order.event
        product_name = str(order_position.product.name)
        if order_position.variation:
            product_name = f'{product_name} – {order_position.variation.value}'

        if ev.date_from:
            dt = ev.date_from if is_aware(ev.date_from) else make_aware(ev.date_from, timezone.utc)
            utc_starts = dt.astimezone(timezone.utc)
            starts_str = f'{utc_starts:%Y-%m-%dT%H:%M:%SZ}'
        else:
            starts_str = f'{now():%Y-%m-%dT%H:%M:%SZ}'

        ord_dt = order.datetime if is_aware(order.datetime) else make_aware(order.datetime, timezone.utc)
        purchase_date_str = f'{ord_dt.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}'

        event_ticket = {
            'primaryFields': [{'key': 'eventName', 'label': gettext('Event'), 'value': str(ev.name)}],
            'secondaryFields': [
                {'key': 'ticket', 'label': gettext('Product'), 'value': product_name},
                {
                    'key': 'starts',
                    'label': gettext('Date'),
                    'value': starts_str,
                    'dateStyle': 'PKDateStyleMedium',
                    'timeStyle': 'PKDateStyleShort',
                },
            ],
            'auxiliaryFields': [],
            'backFields': [
                {'key': 'orderCode', 'label': gettext('Order code'), 'value': order.code},
                {
                    'key': 'purchaseDate',
                    'label': gettext('Purchase date'),
                    'value': purchase_date_str,
                },
                {'key': 'organizer', 'label': gettext('Organizer'), 'value': str(order.event.organizer.name)},
                {
                    'key': 'website',
                    'label': gettext('Website'),
                    'value': build_absolute_uri(order.event, 'presale:event.index'),
                },
            ],
        }

        if order_position.attendee_name:
            event_ticket['auxiliaryFields'].append(
                {'key': 'name', 'label': gettext('Attendee name'), 'value': order_position.attendee_name}
            )
        if order_position.seat:
            event_ticket['auxiliaryFields'].append(
                {'key': 'seat', 'label': gettext('Seat'), 'value': str(order_position.seat)}
            )
        if ev.location:
            event_ticket['auxiliaryFields'].append(
                {'key': 'location', 'label': gettext('Location'), 'value': str(ev.location)}
            )

        if order.event.settings.contact_mail:
            event_ticket['backFields'].append(
                {'key': 'organizerContact', 'label': gettext('Organizer contact'), 'value': order.event.settings.contact_mail}
            )

        barcode = {
            'format': 'PKBarcodeFormatQR',
            'message': order_position.secret,
            'messageEncoding': 'iso-8859-1',
            'altText': order_position.secret,
        }

        team_id = self.event.settings.get('passbook_team_id') or 'EVENTYAY10'
        pass_type_id = self.event.settings.get('passbook_pass_type_id') or 'pass.com.eventyay.ticket'

        payload = {
            'formatVersion': 1,
            'passTypeIdentifier': str(pass_type_id),
            'serialNumber': f'{order.event.organizer.slug}-{order.event.slug}-{order.code}-{order_position.positionid}',
            'teamIdentifier': str(team_id),
            'organizationName': str(order.event.organizer.name),
            'description': gettext('Ticket for {event}').format(event=ev.name),
            'logoText': str(ev.name),
            'eventTicket': event_ticket,
            'barcode': barcode,
            'barcodes': [barcode],
            'relevantDate': starts_str,
            'backgroundColor': hex_to_rgb(self.settings.get('bg_color') or '#1B4D6B'),
            'foregroundColor': hex_to_rgb(self.settings.get('fg_color') or '#FFFFFF'),
            'labelColor': hex_to_rgb(self.settings.get('label_color') or '#D9E8F0'),
        }

        if ev.date_to:
            dt_to = ev.date_to if is_aware(ev.date_to) else make_aware(ev.date_to, timezone.utc)
            utc_to = dt_to.astimezone(timezone.utc)
            payload['expirationDate'] = f'{utc_to:%Y-%m-%dT%H:%M:%SZ}'

        lat = self.settings.get('latitude') or ev.geo_lat
        lon = self.settings.get('longitude') or ev.geo_lon
        if lat is not None and lon is not None:
            try:
                payload['locations'] = [{'latitude': float(lat), 'longitude': float(lon)}]
            except (ValueError, TypeError):
                pass

        return payload

    def generate_files(self, order_position: OrderPosition) -> dict[str, bytes]:
        pass_data = self.generate_pass_data(order_position)
        files = {
            'pass.json': json.dumps(pass_data, ensure_ascii=False, indent=2).encode('utf-8')
        }

        selfscale = self.settings.get('selfscale', as_type=bool)

        icon_file = self.event.settings.get('ticketoutput_passbook_icon', as_type=File, binary_file=True)
        icon_bytes = read_content(icon_file)
        if not icon_bytes:
            found = finders.find('eventyay_passbook/icon.png') or finders.find('common/img/icons/apple-touch-icon-180x180.png')
            if found and os.path.exists(found):
                try:
                    with open(found, 'rb') as f:
                        icon_bytes = f.read()
                except Exception:
                    icon_bytes = create_blank_png(87, 87)
            else:
                icon_bytes = create_blank_png(87, 87)

        if selfscale:
            icon2_file = self.event.settings.get('ticketoutput_passbook_icon2x', as_type=File, binary_file=True)
            icon3_file = self.event.settings.get('ticketoutput_passbook_icon3x', as_type=File, binary_file=True)
            files['icon.png'] = resize_image(icon_bytes, (29, 29))
            files['icon@2x.png'] = resize_image(read_content(icon2_file) or icon_bytes, (58, 58))
            files['icon@3x.png'] = resize_image(read_content(icon3_file) or icon_bytes, (87, 87))
        else:
            files['icon.png'] = resize_image(icon_bytes, (29, 29))
            files['icon@2x.png'] = resize_image(icon_bytes, (58, 58))
            files['icon@3x.png'] = resize_image(icon_bytes, (87, 87))

        logo_file = self.event.settings.get('ticketoutput_passbook_logo', as_type=File, binary_file=True)
        logo_bytes = read_content(logo_file)
        if not logo_bytes:
            found = finders.find('eventyay_passbook/logo.png') or finders.find('common/img/logo.png')
            if found and os.path.exists(found):
                try:
                    with open(found, 'rb') as f:
                        logo_bytes = f.read()
                except Exception:
                    logo_bytes = create_blank_png(480, 150)
            else:
                logo_bytes = create_blank_png(480, 150)

        if selfscale:
            logo2_file = self.event.settings.get('ticketoutput_passbook_logo2x', as_type=File, binary_file=True)
            logo3_file = self.event.settings.get('ticketoutput_passbook_logo3x', as_type=File, binary_file=True)
            files['logo.png'] = resize_image(logo_bytes, (160, 50))
            files['logo@2x.png'] = resize_image(read_content(logo2_file) or logo_bytes, (320, 100))
            files['logo@3x.png'] = resize_image(read_content(logo3_file) or logo_bytes, (480, 150))
        else:
            files['logo.png'] = resize_image(logo_bytes, (160, 50))
            files['logo@2x.png'] = resize_image(logo_bytes, (320, 100))
            files['logo@3x.png'] = resize_image(logo_bytes, (480, 150))

        bg_file = self.event.settings.get('ticketoutput_passbook_background', as_type=File, binary_file=True)
        bg_bytes = read_content(bg_file)
        if bg_bytes:
            if selfscale:
                bg2_file = self.event.settings.get('ticketoutput_passbook_background2x', as_type=File, binary_file=True)
                bg3_file = self.event.settings.get('ticketoutput_passbook_background3x', as_type=File, binary_file=True)
                files['background.png'] = resize_image(bg_bytes, (180, 220))
                files['background@2x.png'] = resize_image(read_content(bg2_file) or bg_bytes, (360, 440))
                files['background@3x.png'] = resize_image(read_content(bg3_file) or bg_bytes, (540, 660))
            else:
                files['background.png'] = resize_image(bg_bytes, (180, 220))
                files['background@2x.png'] = resize_image(bg_bytes, (360, 440))
                files['background@3x.png'] = resize_image(bg_bytes, (540, 660))

        return files

    def sign_manifest(self, manifest_bytes: bytes) -> bytes:
        cert_content = read_content(self.event.settings.get('passbook_certificate_file', as_type=File, binary_file=True))
        wwdr_content = read_content(self.event.settings.get('passbook_wwdr_certificate_file', as_type=File, binary_file=True))
        key_content = read_content(self.event.settings.get('passbook_key'))
        key_password = self.event.settings.get('passbook_key_password', '')

        if cert_content and wwdr_content and key_content:
            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    cert_path = os.path.join(tmpdir, 'cert.pem')
                    wwdr_path = os.path.join(tmpdir, 'wwdr.pem')
                    key_path = os.path.join(tmpdir, 'key.pem')
                    manifest_path = os.path.join(tmpdir, 'manifest.json')
                    sig_path = os.path.join(tmpdir, 'signature')

                    with open(cert_path, 'wb') as f:
                        f.write(cert_content)
                    with open(wwdr_path, 'wb') as f:
                        f.write(wwdr_content)
                    with open(key_path, 'wb') as f:
                        f.write(key_content)
                    with open(manifest_path, 'wb') as f:
                        f.write(manifest_bytes)

                    cmd = [
                        'openssl', 'smime', '-binary', '-sign',
                        '-certfile', wwdr_path,
                        '-signer', cert_path,
                        '-inkey', key_path,
                        '-in', manifest_path,
                        '-out', sig_path,
                        '-outform', 'DER',
                        '-md', 'sha256',
                    ]
                    input_bytes = None
                    if key_password:
                        cmd.extend(['-passin', 'stdin'])
                        input_bytes = f'{key_password}\n'.encode('utf-8')

                    proc = subprocess.run(
                        cmd,
                        input=input_bytes,
                        capture_output=True,
                        check=False,
                    )
                    if proc.returncode == 0 and os.path.exists(sig_path):
                        with open(sig_path, 'rb') as f:
                            return f.read()
                    logger.warning('OpenSSL signing failed with code %s: %s', proc.returncode, proc.stderr.decode(errors='replace'))
            except (OSError, subprocess.SubprocessError):
                logger.warning('OpenSSL execution failed; falling back to Python cryptography signing')

            try:
                try:
                    signing_cert = x509.load_pem_x509_certificate(cert_content)
                except (ValueError, TypeError):
                    signing_cert = x509.load_der_x509_certificate(cert_content)

                try:
                    wwdr_cert = x509.load_pem_x509_certificate(wwdr_content)
                except (ValueError, TypeError):
                    wwdr_cert = x509.load_der_x509_certificate(wwdr_content)

                key = serialization.load_pem_private_key(
                    key_content,
                    password=key_password.encode('utf-8') if key_password else None,
                )

                for hash_algo in (hashes.SHA256(), hashes.SHA1()):
                    try:
                        builder = (
                            pkcs7.PKCS7SignatureBuilder()
                            .set_data(manifest_bytes)
                            .add_signer(signing_cert, key, hash_algo)
                            .add_certificate(wwdr_cert)
                        )
                        return builder.sign(
                            serialization.Encoding.DER,
                            [pkcs7.PKCS7Options.DetachedSignature, pkcs7.PKCS7Options.Binary],
                        )
                    except Exception:
                        continue
            except (ValueError, TypeError, OSError):
                logger.warning('Configured certificate signing failed, falling back to development signer')

        try:
            fallback_cert, fallback_key = get_fallback_credentials()
            for hash_algo in (hashes.SHA256(), hashes.SHA1()):
                try:
                    builder = (
                        pkcs7.PKCS7SignatureBuilder()
                        .set_data(manifest_bytes)
                        .add_signer(fallback_cert, fallback_key, hash_algo)
                    )
                    return builder.sign(
                        serialization.Encoding.DER,
                        [pkcs7.PKCS7Options.DetachedSignature, pkcs7.PKCS7Options.Binary],
                    )
                except Exception:
                    continue
        except Exception:
            logger.exception('Fallback signing failed')

        return b'PKCS7_SIGNATURE_FALLBACK'

    def generate(self, position: OrderPosition) -> tuple[str, str, bytes]:
        try:
            files = self.generate_files(position)
            manifest_data = {
                name: hashlib.sha1(content).hexdigest()
                for name, content in files.items()
            }
            manifest_bytes = json.dumps(
                manifest_data,
                separators=(',', ':'),
                sort_keys=True,
            ).encode('utf-8')

            signature_bytes = self.sign_manifest(manifest_bytes)

            output = io.BytesIO()
            with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
                for name, content in files.items():
                    archive.writestr(name, content)
                archive.writestr('manifest.json', manifest_bytes)
                archive.writestr('signature', signature_bytes)

            order = position.order
            filename = f'{order.event.slug}-{order.code}-{position.positionid}.pkpass'
            return filename, 'application/vnd.apple.pkpass', output.getvalue()
        except Exception:
            logger.exception('Error generating passbook ticket for position %s', getattr(position, 'pk', None))
            raise
