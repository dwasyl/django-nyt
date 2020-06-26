from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('django_nyt', '0011_add_interval_to_disable_email_notifications'),
    ]

    operations = [
        migrations.AddField(
            model_name='settings',
            name='last_sent',
            field=models.DateTimeField(null=True, verbose_name='E-mail notifications last sent', blank=True),
        ),
    ]
