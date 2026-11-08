# PO numbers become text so Access's 999-prefixed POs can be written as ION (99900123 -> ION00123).

from django.db import migrations, models


def to_ion(apps, schema_editor):
    PurchaseOrder = apps.get_model("tracker", "PurchaseOrder")
    for po in PurchaseOrder.objects.filter(number__startswith="999"):
        po.number = "ION" + po.number[3:]
        po.save(update_fields=["number"])


def back_to_999(apps, schema_editor):
    PurchaseOrder = apps.get_model("tracker", "PurchaseOrder")
    for po in PurchaseOrder.objects.filter(number__startswith="ION"):
        po.number = "999" + po.number[3:]
        po.save(update_fields=["number"])


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0008_tender_milestone'),
    ]

    operations = [
        migrations.AlterField(
            model_name='purchaseorder',
            name='number',
            field=models.CharField(max_length=30, unique=True, verbose_name='PO #'),
        ),
        migrations.RunPython(to_ion, back_to_999),
    ]
