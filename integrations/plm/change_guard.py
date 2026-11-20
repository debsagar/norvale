"""Source-side decision permissions for the pinned InvenTree deployment."""
from datetime import date

from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.fields import empty

from InvenTree.api import GenericMetadataView
from part.models import Part
from part.serializers import BomItemSerializer, PartSerializer
from plugin.serializers import MetadataSerializer


def allowed(user, role):
    required = 'norvale-' + role
    return user.groups.filter(name=required).exists() and not user.groups.filter(
        name__in=('norvale-publisher', 'norvale-engineering', 'norvale-approver')
    ).exclude(name=required).exists()


def protected(part):
    return (part.IPN or '').startswith('NV-PLM-')


def install():
    metadata_serializer = GenericMetadataView.get_serializer
    update_metadata = MetadataSerializer.update
    validate_part = PartSerializer.run_validation
    validate_bom = BomItemSerializer.run_validation

    def serializer(self, *args, **kwargs):
        # InvenTree 1.3.6 constructs this serializer directly without DRF context.
        kwargs.setdefault('context', self.get_serializer_context())
        return metadata_serializer(self, *args, **kwargs)

    def metadata(self, part, data):
        if not isinstance(part, Part) or not protected(part):
            return update_metadata(self, part, data)
        user = self.context['request'].user
        prior = part.metadata or {}
        changes = data['metadata']
        if not self.partial or not isinstance(changes, dict):
            raise PermissionDenied('Norvale definitions require metadata PATCH')
        for key, value in changes.items():
            role = {'norvale_review': 'engineering', 'norvale_approval': 'approver',
                    'norvale_rejection': 'approver'}.get(key, 'publisher')
            if not allowed(user, role):
                raise PermissionDenied('Native group cannot write this definition field')
            if key.startswith('norvale_') and key in prior and prior[key] != value:
                raise ValidationError('Definition transition is already recorded')
            if key in ('norvale_review', 'norvale_approval', 'norvale_rejection'):
                if not isinstance(value, dict) or value.get('actor') != user.username:
                    raise PermissionDenied('Decision actor must match the authenticated user')
                if part.revision != 'A-1' and not prior.get('norvale_proposal'):
                    raise ValidationError('Review and decision require native proposal evidence')
                try:
                    recorded = date.fromisoformat(value['on'])
                    if key == 'norvale_approval' and date.fromisoformat(value['effective']) < recorded:
                        raise ValueError
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValidationError('Invalid decision dates') from exc
                if not value.get('evidence') or not value.get('sha256'):
                    raise ValidationError('Decision requires evidence and a reviewed digest')
                if key != 'norvale_review':
                    review = prior.get('norvale_review', {})
                    if (not review or review.get('actor') == user.username
                            or review.get('sha256') != value.get('sha256')
                            or date.fromisoformat(review['on']) > recorded
                            or ('norvale_rejection' if key == 'norvale_approval'
                                else 'norvale_approval') in prior
                            or (key == 'norvale_approval' and not part.locked)):
                        raise ValidationError('Decision requires independent review and valid native state')
        return update_metadata(self, part, data)

    def part_validation(self, data=empty):
        # Upstream validation mutates the in-memory instance before update().
        part = self.instance
        if isinstance(part, Part) and protected(part) and isinstance(data, dict):
            user = self.context['request'].user
            if 'locked' in data and not part.locked:
                if dict(data) != {'locked': True} or not allowed(user, 'approver') or not (
                        part.metadata or {}).get('norvale_review'):
                    raise PermissionDenied('Only the approver can lock a reviewed definition')
            elif part.locked or not allowed(user, 'publisher'):
                raise PermissionDenied('Definition is locked or requires the publisher')
        return validate_part(self, data)

    def bom_validation(self, data=empty):
        row = self.instance
        if (row is not None and protected(row.part) and not allowed(
                self.context['request'].user, 'publisher')):
            raise PermissionDenied('Only the publisher can edit a definition BOM')
        return validate_bom(self, data)

    GenericMetadataView.get_serializer = serializer
    MetadataSerializer.update = metadata
    PartSerializer.run_validation = part_validation
    BomItemSerializer.run_validation = bom_validation
