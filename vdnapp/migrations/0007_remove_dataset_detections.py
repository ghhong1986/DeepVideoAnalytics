# -*- coding: utf-8 -*-
# Generated by Django 1.10.5 on 2017-04-05 05:08
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('vdnapp', '0006_auto_20170405_0238'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='dataset',
            name='detections',
        ),
    ]
