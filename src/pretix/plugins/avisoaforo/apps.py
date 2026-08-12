from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

from pretix import __version__ as version


class AvisoAforoApp(AppConfig):
    name = 'pretix.plugins.avisoaforo'
    verbose_name = _("Aviso de aforo")

    class PretixPluginMeta:
        name = _("Aviso de aforo")
        author = "AMAI"
        version = version
        category = "FEATURE"
        description = _("Avisa por correo cuando el aforo de un evento cruza un porcentaje de "
                        "ocupación (por defecto 50 %, 80 %, 90 % y 100 %). Cada umbral se avisa "
                        "UNA sola vez. Los destinatarios y los umbrales se configuran en los "
                        "ajustes del organizador y se heredan por evento.")

    def ready(self):
        from . import signals  # NOQA
