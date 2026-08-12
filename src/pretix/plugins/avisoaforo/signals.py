"""Aviso por correo cuando el aforo de un evento cruza un tramo de ocupación.

POR QUÉ ESTO NO VIENE DE SERIE. pretix trae notificaciones muy completas, pero **todas son de
PEDIDO** (`order.placed`, `order.paid`, `order.canceled`…): avisan de cada venta. No hay ninguna del
aforo, así que nadie se entera de que un evento está a punto de llenarse hasta que alguien entra a
mirar. Y avisar de cada venta no sirve para eso: con 200 plazas son 200 correos y se acaban
ignorando, que es peor que no avisar.

CÓMO FUNCIONA, Y POR QUÉ ASÍ:

  · **Por tramos, no por venta.** Se avisa al cruzar 50 %, 80 %, 90 % y 100 % (configurable). Cada
    tramo se avisa UNA sola vez, aunque el cron pase cada 15 minutos: lo cruzado se apunta en los
    ajustes del evento. Un aviso que se repite es un aviso que se filtra a la papelera.
  · **Se cuelga de `periodic_task`**, que ya dispara el cron de Dokploy cada 15 min. Cero
    infraestructura nueva, y si el proceso se cae el siguiente pase lo recoge.
  · **Cuenta sobre la cuota**, no sobre los pedidos. `Quota.availability()` es lo que pretix usa
    para decidir si puede vender: incluye carritos en curso y lista de espera, así que un aforo que
    ya no admite compras se detecta aunque los pedidos aún no estén pagados.
  · **Si baja, se rearma.** Una cancelación devuelve plazas; cuando eso hace bajar del tramo, el
    tramo vuelve a estar disponible para avisar. Si no, un evento que se llena, se vacía y se vuelve
    a llenar avisaría una sola vez en su vida.

AJUSTES (en el ORGANIZADOR, y los eventos los heredan):
  · `aviso_aforo_destinatarios` — correos separados por coma. **Sin esto no se envía nada.**
  · `aviso_aforo_umbrales` — porcentajes separados por coma. Por defecto `50,80,90,100`.
"""
import logging

from django.dispatch import receiver
from django_scopes import scopes_disabled
from pretix.base.models import Event
from pretix.base.services.mail import mail
from pretix.base.signals import periodic_task
from pretix.helpers.urls import build_absolute_uri

logger = logging.getLogger(__name__)

UMBRALES_POR_DEFECTO = '50,80,90,100'


def _destinatarios(event: Event):
    crudo = event.settings.get('aviso_aforo_destinatarios') or ''
    return [c.strip() for c in crudo.replace(';', ',').split(',') if c.strip() and '@' in c]


def _umbrales(event: Event):
    crudo = str(event.settings.get('aviso_aforo_umbrales') or UMBRALES_POR_DEFECTO)
    salida = []
    for trozo in crudo.replace(';', ',').split(','):
        trozo = trozo.strip()
        if trozo.isdigit() and 0 < int(trozo) <= 100:
            salida.append(int(trozo))
    return sorted(set(salida)) or [int(x) for x in UMBRALES_POR_DEFECTO.split(',')]


def _revisar_evento(event: Event):
    correos = _destinatarios(event)
    if not correos:
        return

    umbrales = _umbrales(event)
    for quota in event.quotas.all():
        if not quota.size:
            continue  # aforo ilimitado: no hay porcentaje que cruzar
        _, disponibles = quota.availability(allow_cache=True)
        disponibles = max(0, disponibles if disponibles is not None else 0)
        vendidas = max(0, quota.size - disponibles)
        porcentaje = int(vendidas * 100 / quota.size)

        clave = 'aviso_aforo_hecho_%s' % quota.pk
        ya = int(event.settings.get(clave) or 0)

        # Si el aforo BAJÓ por debajo de lo ya avisado (cancelaciones), se rearma ese tramo.
        if porcentaje < ya:
            event.settings.set(clave, max([u for u in umbrales if u <= porcentaje] or [0]))
            ya = int(event.settings.get(clave) or 0)

        cruzados = [u for u in umbrales if ya < u <= porcentaje]
        if not cruzados:
            continue
        alcanzado = max(cruzados)

        contexto = {
            'evento': str(event.name),
            'porcentaje': porcentaje,
            'vendidas': vendidas,
            'aforo': quota.size,
            'quedan': disponibles,
            'agotado': disponibles == 0,
            'umbrales': ', '.join(str(u) for u in umbrales),
            # `build_absolute_uri` de `helpers` toma el NOMBRE de la ruta, no una ruta ya
            # resuelta: pasarle un `reverse()` da una URL doblada.
            'url': build_absolute_uri('control:event.orders', kwargs={
                'organizer': event.organizer.slug, 'event': event.slug,
            }),
        }
        asunto = ('[%s] AFORO AGOTADO' if disponibles == 0 else '[%s] Aforo al %s %%') % (
            (event.name,) if disponibles == 0 else (event.name, porcentaje))
        for correo in correos:
            try:
                mail(correo, asunto, 'pretixplugins/avisoaforo/aviso.txt', contexto,
                     event=event, locale=event.settings.locale)
            except Exception as e:  # noqa: BLE001 — un aviso no puede tumbar el cron
                logger.warning('avisoaforo: no se pudo avisar a %s de %s: %s', correo, event.slug, e)
        # Se marca DESPUÉS de intentar el envío, pero se marca igual aunque falle un destinatario:
        # reintentar cada 15 minutos un correo que rebota es peor que perder un aviso.
        event.settings.set(clave, alcanzado)
        logger.info('avisoaforo: %s cruzó el %s %% (%s/%s)', event.slug, alcanzado, vendidas, quota.size)


@receiver(signal=periodic_task)
def revisar_aforos(sender, **kwargs):
    # `scopes_disabled` NO es opcional: fuera de una petición no hay organizador en el contexto y
    # cualquier consulta a Event lanza `ScopeError`. Es lo que hace el resto de tareas periódicas
    # de pretix (ver `plugins/sendmail/signals.py`).
    with scopes_disabled():
        eventos = list(
            Event.objects.filter(live=True, testmode=False).select_related('organizer')
        )
    for event in eventos:
        if 'pretix.plugins.avisoaforo' not in (event.plugins or ''):
            continue
        try:
            with scopes_disabled():
                _revisar_evento(event)
        except Exception as e:  # noqa: BLE001 — un evento roto no puede parar a los demás
            logger.warning('avisoaforo: fallo revisando %s: %s', event.slug, e)
