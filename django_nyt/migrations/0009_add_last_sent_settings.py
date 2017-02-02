# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('django_nyt', '0008_auto_20170122_1441'),
    ]

    operations = [
        migrations.AddField(
            model_name='settings',
            name='last_sent',
            field=models.DateTimeField(null=True, verbose_name='E-mail notifications last sent', blank=True),
        ),
    ]
