import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('django_nyt', '0008_auto_20161023_1641'),
    ]

    operations = [
        migrations.AlterField(
            model_name='settings',
            name='interval',
            field=models.SmallIntegerField(default=0, verbose_name='interval', choices=[(-1, 'never'), (0, 'instantly'), (1380, 'daily'), (9660, 'weekly')]),
        ),
    ]
