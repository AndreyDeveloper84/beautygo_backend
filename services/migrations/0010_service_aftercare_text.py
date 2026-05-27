"""Add aftercare_text TextField to Service (task #100, B9 schema scaffolding).

Per founder pilot safety rule (memory project_pilot_scope_discipline):

> NO LLM-generated care advice pilot. ONLY approved canonical text
> per service.

Schema enforces the rule by surfacing aftercare text as a per-Service
field the curator/ops fills via Django admin. The B9 beat task
(notifications.dispatch_post_visit_aftercare) sends ONLY when the field
is non-empty — services without approved content silently skip.

Purely additive: blank default keeps existing rows valid. No backfill
needed; ops adds content per-service as approved.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('services', '0009_service_svc_tenant_active_idx_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='service',
            name='aftercare_text',
            field=models.TextField(
                blank=True,
                default='',
                help_text=(
                    "Approved post-visit aftercare advice. Sent verbatim "
                    "via the B9 T+2h push notification. Empty value (the "
                    "default) suppresses the push — explicit opt-in per "
                    "service. NO LLM-generated content per pilot safety "
                    "rule."
                ),
            ),
        ),
    ]
