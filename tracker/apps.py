from django.apps import AppConfig


class TrackerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'tracker'

    def ready(self):
        from . import changelog
        changelog.connect()      # record changes to POs, invoices and change orders
