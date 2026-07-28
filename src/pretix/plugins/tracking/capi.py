#
# This file is part of pretix (Community Edition).
#
# Copyright (C) 2014-2020  Raphael Michel and contributors
# Copyright (C) 2020-today pretix GmbH and contributors
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
# Public License as published by the Free Software Foundation in version 3 of the License.
#
# ADDITIONAL TERMS APPLY: Pursuant to Section 7 of the GNU Affero General Public License, additional terms are
# applicable granting you additional permissions and placing additional restrictions on your usage of this software.
# Please refer to the pretix LICENSE file to obtain the full terms applicable to this work. If you did not receive
# this file, see <https://pretix.eu/about/en/license>.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License along with this program.  If not, see
# <https://www.gnu.org/licenses/>.
#
"""Server-side conversion tracking (Meta Conversions API).

The browser pixel loses a large share of events to ad blockers, ITP and consent-less browsers.
This module sends the same events straight from our server, sharing the ``event_id`` with the
pixel so Meta deduplicates the pair instead of counting it twice.

Everything runs in Celery: the checkout must never wait on a Graph API call.
"""
import hashlib
import logging
import re
import time

import requests
from django.utils.crypto import get_random_string
from django_scopes import scopes_disabled

from pretix.celery_app import app

logger = logging.getLogger(__name__)

GRAPH_URL = 'https://graph.facebook.com/%s/%s/events'
TIMEOUT = 15

# Meta rejects events older than 7 days, and a retry storm should not keep hammering the API.
MAX_RETRIES = 4
RETRY_DELAY = 120


def new_event_id():
    """One id per browser event, shared with its server-side twin so Meta can deduplicate."""
    return 'ev-' + get_random_string(24, allowed_chars='abcdefghijklmnopqrstuvwxyz0123456789')


def _hash(value):
    if value is None:
        return None
    value = str(value).strip().lower()
    if not value:
        return None
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _hash_phone(value):
    """Meta wants digits only, country code included, no plus sign."""
    digits = re.sub(r'\D', '', str(value or ''))
    return _hash(digits) if len(digits) >= 6 else None


def _put(target, key, value):
    if value:
        target[key] = value if isinstance(value, list) else [value]


def user_data_from_order(order):
    """Build the identity block Meta uses for attribution and event match quality.

    Everything that identifies a person is SHA-256 hashed here, before it ever leaves the process.
    ``fbp``/``fbc``/IP/user agent are the documented exceptions: Meta requires them in the clear.
    """
    data = {}
    _put(data, 'em', _hash(order.email))
    _put(data, 'ph', _hash_phone(order.phone))

    ia = getattr(order, 'invoice_address', None)
    if ia is not None and ia.pk:
        parts = ia.name_parts or {}
        _put(data, 'fn', _hash(parts.get('given_name') or (ia.name_cached or '').split(' ')[0]))
        _put(data, 'ln', _hash(parts.get('family_name') or (ia.name_cached or '').split(' ')[-1]))
        _put(data, 'ct', _hash(re.sub(r'\s', '', ia.city or '')))
        _put(data, 'zp', _hash(re.sub(r'\s', '', ia.zipcode or '')))
        _put(data, 'st', _hash(ia.state))
        _put(data, 'country', _hash(str(ia.country) if ia.country else None))

    if 'fn' not in data:
        # A shop that only asks for the attendee's name keeps it on the position, not on an invoice
        # address, so without this the name never reaches Meta and the match quality suffers.
        pos = order.positions.first()
        if pos is not None:
            parts = pos.attendee_name_parts or {}
            name = parts.get('_legacy') or pos.attendee_name or ''
            _put(data, 'fn', _hash(parts.get('given_name') or name.split(' ')[0]))
            if len(name.split(' ')) > 1 or parts.get('family_name'):
                _put(data, 'ln', _hash(parts.get('family_name') or name.split(' ')[-1]))

    # Captured from the browser at checkout time by the order_meta_from_request receiver.
    captured = (order.meta_info_data or {}).get('_tracking') or {}
    for key in ('fbp', 'fbc'):
        if captured.get(key):
            data[key] = captured[key]
    if captured.get('ip'):
        data['client_ip_address'] = captured['ip']
    if captured.get('ua'):
        data['client_user_agent'] = captured['ua']
    return data


def custom_data_from_order(order):
    positions = list(order.positions.select_related('item')[:100])
    # Two tickets of the same type are one content line with quantity 2, not two lines: that is
    # what Meta's catalogue reporting expects.
    grouped, ids = {}, []
    for p in positions:
        item_id = str(p.item_id)
        if item_id not in grouped:
            ids.append(item_id)
            grouped[item_id] = {
                'id': item_id,
                'quantity': 0,
                'item_price': round(float(p.price), 2),
                'title': str(p.item.name),
            }
        grouped[item_id]['quantity'] += 1
    contents = list(grouped.values())
    return {
        'currency': order.event.currency,
        'value': round(float(order.total), 2),
        'content_type': 'product',
        'content_ids': ids or [order.event.slug],
        'content_name': str(order.event.name),
        'contents': contents,
        'num_items': len(positions),
        'order_id': order.code,
    }


def post_events(pixel_id, token, events, test_event_code=None, api_version='v21.0'):
    """Send a batch to the Graph API. Returns the parsed response, raises on transport errors."""
    payload = {'data': events}
    if test_event_code:
        payload['test_event_code'] = test_event_code
    r = requests.post(
        GRAPH_URL % (api_version, pixel_id),
        params={'access_token': token},
        json=payload,
        timeout=TIMEOUT,
    )
    body = {}
    try:
        body = r.json()
    except ValueError:
        pass
    if r.status_code >= 400:
        # Meta answers 400 for both "your token is wrong" and "that one event was malformed".
        # Neither gets better by retrying forever, so log loudly and let the caller decide.
        logger.warning('Meta CAPI rejected %d event(s) for pixel %s: %s %s',
                       len(events), pixel_id, r.status_code, body or r.text[:500])
        r.raise_for_status()
    return body


@app.task(bind=True, max_retries=MAX_RETRIES, default_retry_delay=RETRY_DELAY)
def capi_send(self, *, pixel_id, token, event, test_event_code=None, api_version='v21.0'):
    """Send one already-built event. Used for the funnel events that originate in a request."""
    try:
        return post_events(pixel_id, token, [event], test_event_code, api_version)
    except requests.RequestException as e:
        # Network hiccup or a 5xx on Meta's side: worth another go. A 400 is not.
        status = getattr(getattr(e, 'response', None), 'status_code', None)
        if status is not None and 400 <= status < 500 and status != 429:
            logger.warning('Meta CAPI permanent failure for %s, giving up: %s', event.get('event_name'), e)
            return None
        raise self.retry(exc=e)


@app.task(bind=True, max_retries=MAX_RETRIES, default_retry_delay=RETRY_DELAY)
def capi_purchase(self, *, order_pk):
    """Send the Purchase for a paid order.

    The order is re-read here instead of being passed in, so no personal data ever sits in the
    Redis queue and the payload always reflects the order as it is when the task actually runs.
    """
    from pretix.base.models import Order

    from .signals import get_setting

    with scopes_disabled():
        order = Order.objects.select_related('event', 'event__organizer').filter(pk=order_pk).first()
        if not order:
            return None
        event = order.event
        pixel_id = get_setting(event, 'meta_pixel_id')
        token = get_setting(event, 'meta_capi_token')
        if not pixel_id or not token:
            return None

        test_code = get_setting(event, 'meta_test_event_code')
        if order.testmode and not test_code:
            # Test orders would otherwise poison the real pixel's optimisation data.
            logger.info('Skipping Meta CAPI Purchase for test order %s (no test_event_code set)', order.code)
            return None

        captured = (order.meta_info_data or {}).get('_tracking') or {}
        if not captured.get('consent', False) and not captured.get('consent_not_required', False):
            logger.info('Skipping Meta CAPI Purchase for %s: no marketing consent recorded', order.code)
            return None

        payload = {
            'event_name': 'Purchase',
            'event_time': int(time.time()),
            # Same id the browser pixel uses, which is what makes deduplication work.
            'event_id': order.code,
            'action_source': 'website',
            'user_data': user_data_from_order(order),
            'custom_data': custom_data_from_order(order),
        }
        if captured.get('url'):
            payload['event_source_url'] = captured['url']

        api_version = get_setting(event, 'meta_api_version') or 'v21.0'

    try:
        return post_events(pixel_id, token, [payload], test_code, api_version)
    except requests.RequestException as e:
        status = getattr(getattr(e, 'response', None), 'status_code', None)
        if status is not None and 400 <= status < 500 and status != 429:
            logger.warning('Meta CAPI permanently rejected Purchase for %s: %s', order.code, e)
            return None
        raise self.retry(exc=e)
