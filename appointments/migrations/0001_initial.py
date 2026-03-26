from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('services', '0002_initial'),
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Appointment',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('start_datetime', models.DateTimeField()),
                ('end_datetime', models.DateTimeField()),
                ('status', models.CharField(
                    choices=[
                        ('pending', 'Ожидает подтверждения'),
                        ('confirmed', 'Подтверждена'),
                        ('in_progress', 'В процессе'),
                        ('completed', 'Завершена'),
                        ('cancelled', 'Отменена'),
                        ('no_show', 'Клиент не пришёл'),
                    ],
                    db_index=True,
                    default='pending',
                    max_length=20,
                )),
                ('price', models.DecimalField(decimal_places=2, max_digits=10)),
                ('notes', models.TextField(blank=True)),
                ('cancel_reason', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('client', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='appointments_as_client',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('specialist', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='appointments',
                    to='users.specialistprofile',
                )),
                ('service', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='appointments',
                    to='services.service',
                )),
                ('cancelled_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='cancellations',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'verbose_name': 'Запись',
                'verbose_name_plural': 'Записи',
                'ordering': ['-start_datetime'],
            },
        ),
        migrations.AddIndex(
            model_name='appointment',
            index=models.Index(fields=['client', 'status'], name='appointment_client_status_idx'),
        ),
        migrations.AddIndex(
            model_name='appointment',
            index=models.Index(fields=['specialist', 'start_datetime'], name='appointment_spec_start_idx'),
        ),
        migrations.AddIndex(
            model_name='appointment',
            index=models.Index(fields=['status', 'start_datetime'], name='appointment_status_start_idx'),
        ),
    ]
