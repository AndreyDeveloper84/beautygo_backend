# S3A canonical catalog rebuild (#1044 / #200) — additive new domain.
# SalonService -> SpecialistService (bookable) + DraftSalonService +
# ExternalSourceMapping. Service / Appointment intentionally NOT touched.
import django.core.validators
import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('services', '0011_servicetemplate_contraindications_and_more'),
        ('tenants', '0003_seed_default_tenants'),
        ('users', '0013_specialistprofile_yclients_staff_id'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='SalonService',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=200)),
                ('duration_minutes', models.PositiveIntegerField(blank=True, null=True, validators=[django.core.validators.MinValueValidator(5), django.core.validators.MaxValueValidator(480)])),
                ('base_price', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True, validators=[django.core.validators.MinValueValidator(1)])),
                ('requires_health_check', models.BooleanField(default=False)),
                ('is_active', models.BooleanField(default=True)),
                ('source', models.CharField(choices=[('manual', 'Manual'), ('yclients', 'YClients'), ('seed', 'Seed')], default='manual', max_length=10)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('category', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='salon_services', to='services.servicecategory')),
                ('template', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='salon_services', to='services.servicetemplate')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='salon_services', to='tenants.tenant')),
            ],
        ),
        migrations.CreateModel(
            name='ExternalSourceMapping',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('source', models.CharField(choices=[('yclients', 'YClients')], default='yclients', max_length=16)),
                ('external_type', models.CharField(choices=[('service', 'Service'), ('staff', 'Staff')], max_length=8)),
                ('external_id', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('specialist', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='external_source_mappings', to='users.specialistprofile')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='external_source_mappings', to='tenants.tenant')),
                ('salon_service', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='external_source_mappings', to='services.salonservice')),
            ],
        ),
        migrations.CreateModel(
            name='DraftSalonService',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('confirmed', 'Confirmed'), ('rejected', 'Rejected'), ('superseded', 'Superseded')], default='pending', max_length=12)),
                ('external_source', models.CharField(choices=[('yclients', 'YClients'), ('csv', 'CSV bootstrap')], default='yclients', max_length=10)),
                ('external_service_id', models.CharField(blank=True, default='', max_length=64)),
                ('external_name', models.CharField(max_length=200)),
                ('suggested_duration', models.PositiveIntegerField(blank=True, null=True)),
                ('suggested_price', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ('raw_payload', models.JSONField(blank=True, default=dict)),
                ('confirmed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('confirmed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('suggested_template', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='services.servicetemplate')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='draft_salon_services', to='tenants.tenant')),
                ('confirmed_salon_service', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='services.salonservice')),
            ],
        ),
        migrations.CreateModel(
            name='SpecialistService',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('duration_minutes', models.PositiveIntegerField(blank=True, null=True, validators=[django.core.validators.MinValueValidator(5), django.core.validators.MaxValueValidator(480)])),
                ('price', models.DecimalField(decimal_places=2, max_digits=10, validators=[django.core.validators.MinValueValidator(1)])),
                ('requires_health_check', models.BooleanField(default=False)),
                ('buffer_after_minutes', models.PositiveSmallIntegerField(default=0)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('salon_service', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='specialist_services', to='services.salonservice')),
                ('specialist', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='specialist_services', to='users.specialistprofile')),
                ('tenant', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='specialist_services', to='tenants.tenant')),
            ],
        ),
        migrations.AddIndex(
            model_name='salonservice',
            index=models.Index(fields=['tenant', 'is_active'], name='salonsvc_tenant_active_idx'),
        ),
        migrations.AddIndex(
            model_name='salonservice',
            index=models.Index(fields=['tenant', 'category', 'is_active'], name='salonsvc_tenant_cat_active_idx'),
        ),
        migrations.AddConstraint(
            model_name='salonservice',
            constraint=models.UniqueConstraint(fields=('tenant', 'template', 'name'), name='salonservice_tenant_template_name_uniq'),
        ),
        migrations.AddIndex(
            model_name='externalsourcemapping',
            index=models.Index(fields=['tenant', 'source', 'external_type'], name='extmap_tenant_src_type_idx'),
        ),
        migrations.AddConstraint(
            model_name='externalsourcemapping',
            constraint=models.UniqueConstraint(fields=('source', 'external_type', 'external_id', 'tenant'), name='externalsourcemapping_key_uniq'),
        ),
        migrations.AddIndex(
            model_name='draftsalonservice',
            index=models.Index(fields=['tenant', 'status'], name='draftsvc_tenant_status_idx'),
        ),
        migrations.AddConstraint(
            model_name='draftsalonservice',
            constraint=models.UniqueConstraint(condition=models.Q(('external_service_id', ''), _negated=True), fields=('tenant', 'external_source', 'external_service_id'), name='draftsalonservice_external_id_uniq'),
        ),
        migrations.AddIndex(
            model_name='specialistservice',
            index=models.Index(fields=['tenant', 'is_active'], name='specsvc_tenant_active_idx'),
        ),
        migrations.AddIndex(
            model_name='specialistservice',
            index=models.Index(fields=['specialist', 'is_active'], name='specsvc_spec_active_idx'),
        ),
        migrations.AddIndex(
            model_name='specialistservice',
            index=models.Index(fields=['salon_service', 'is_active'], name='specsvc_salon_active_idx'),
        ),
        migrations.AddConstraint(
            model_name='specialistservice',
            constraint=models.UniqueConstraint(fields=('specialist', 'salon_service'), name='specialistservice_specialist_salon_uniq'),
        ),
    ]
