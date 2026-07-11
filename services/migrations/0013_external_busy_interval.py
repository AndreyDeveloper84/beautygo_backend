# S3-CAL (#1044 / EPIC #317) — external busy intervals (additive).
# Source-abstracted busy-guard input; YClients coupled only in the webhook
# ingress (S3-CAL.3). No changes to Service / Appointment.
import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('services', '0012_catalog_domain_s3a'),
        ('tenants', '0003_seed_default_tenants'),
        ('users', '0013_specialistprofile_yclients_staff_id'),
    ]

    operations = [
        migrations.CreateModel(
            name='ExternalBusyInterval',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('start_at', models.DateTimeField()),
                ('end_at', models.DateTimeField()),
                ('source', models.CharField(choices=[('yclients', 'YClients')], default='yclients', max_length=16)),
                ('external_id', models.CharField(blank=True, default='', max_length=64)),
                ('raw_payload', models.JSONField(blank=True, default=dict)),
                ('received_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('specialist', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='external_busy_intervals', to='users.specialistprofile')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='external_busy_intervals', to='tenants.tenant')),
            ],
            options={
                'indexes': [models.Index(fields=['specialist', 'start_at', 'end_at'], name='extbusy_spec_window_idx')],
                'constraints': [models.UniqueConstraint(condition=models.Q(('external_id', ''), _negated=True), fields=('source', 'external_id', 'tenant'), name='externalbusyinterval_ext_id_uniq')],
            },
        ),
    ]
