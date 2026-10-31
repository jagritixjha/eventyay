import io

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from django import forms
from django.core.files.uploadedfile import InMemoryUploadedFile, UploadedFile
from django.utils.translation import gettext_lazy as _
from PIL import Image


class PNGImageField(forms.FileField):
    def clean(self, data, initial=None):
        file_obj = super().clean(data, initial)
        if not file_obj or file_obj is False or not isinstance(data, UploadedFile):
            return file_obj

        try:
            image = Image.open(file_obj)
            if image.format != 'PNG':
                output = io.BytesIO()
                rgba_image = image.convert('RGBA')
                rgba_image.save(output, format='PNG')
                output.seek(0)
                name = file_obj.name.rsplit('.', 1)[0] + '.png'
                return InMemoryUploadedFile(
                    output,
                    file_obj.field_name,
                    name,
                    'image/png',
                    output.getbuffer().nbytes,
                    file_obj.charset,
                )
            if hasattr(file_obj, 'seek'):
                file_obj.seek(0)
            return file_obj
        except (OSError, ValueError, SyntaxError) as exc:
            raise forms.ValidationError(_('Invalid image file.')) from exc


class CertificateFileField(forms.FileField):
    def clean(self, data, initial=None):
        file_obj = super().clean(data, initial)
        if not file_obj or file_obj is False or not isinstance(data, UploadedFile):
            return file_obj

        try:
            content = file_obj.read()
            if hasattr(file_obj, 'seek'):
                file_obj.seek(0)
            try:
                x509.load_pem_x509_certificate(content)
            except (TypeError, ValueError):
                x509.load_der_x509_certificate(content)
            return file_obj
        except (TypeError, ValueError) as exc:
            raise forms.ValidationError(_('Invalid certificate.')) from exc


def validate_rsa_privkey(value: str) -> None:
    if not value or not value.strip():
        return
    content = value.encode('utf-8') if isinstance(value, str) else value
    try:
        serialization.load_pem_private_key(content, password=None)
    except TypeError:
        # Key might be encrypted with passphrase, which is valid
        pass
    except ValueError as exc:
        raise forms.ValidationError(_('Invalid PEM private key.')) from exc
