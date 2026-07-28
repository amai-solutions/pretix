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
import json
import logging
import re

import time

from django.conf import settings as django_settings
from django.dispatch import receiver
from django.utils.crypto import get_random_string

from pretix.base.middleware import _merge_csp, _parse_csp, _render_csp
from pretix.base.signals import order_paid
from pretix.presale.cookies import CookieProvider, UsageClass
from pretix.presale.signals import (
    global_html_head, html_head, order_meta_from_request, process_response,
    register_cookie_providers,
)

from .capi import capi_purchase, capi_send, new_event_id

logger = logging.getLogger(__name__)

# Tag IDs end up inside a <script> block, so we only accept the characters the vendors actually use.
# Anything else is dropped and logged instead of being embedded.
ID_RE = re.compile(r'^[A-Za-z0-9_\-]{1,64}$')

# Settings that are not tag IDs and must skip the ID_RE check: free-form HTML and the
# Conversions API token (200+ characters of base64-ish text). None of these are ever
# rendered into the page except head_html.
RAW_KEYS = {'head_html', 'meta_capi_token', 'meta_api_version'}

# Marks that this visitor already got an InitiateCheckout for the checkout they are in.
SESSION_KEY_CHECKOUT = '_tracking_checkout_started'

# Written by our own script once the visitor answers the consent dialog, because consent itself
# lives in localStorage and the server has no way to read that.
CONSENT_COOKIE = 'pretix_tracking_consent'

# Campaign parameters ride on the landing URL, never on the checkout page where the order is
# finally created, so they have to be stashed the moment they are first seen or the attribution
# is gone by the time the order exists.
#
# The click identifiers matter as much as the utm_*: they are what each ad platform matches a sale
# back to its own click, and unlike the utm_* they are not guesswork.
CAMPAIGN_KEYS = (
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term', 'utm_id',
    'gclid', 'gbraid', 'wbraid',      # Google Ads (the braid pair covers iOS app-to-web)
    'fbclid',                          # Meta
    'ttclid',                          # TikTok
    'msclkid',                         # Microsoft Ads
    'li_fat_id',                       # LinkedIn
    'twclid',                          # X / Twitter
    'epik',                            # Pinterest
)
SESSION_CAMPAIGN = '_tracking_%s'

# Where the visitor came in and from where. Written once per session: overwriting them on a later
# page would turn "landed on /bogota from an Instagram ad" into "landed on the checkout".
SESSION_LANDING = '_tracking_landing_url'
SESSION_REFERRER = '_tracking_referrer'

# pretix ships a strict Content-Security-Policy: script-src is 'self' with no 'unsafe-inline'.
# Without the entries below the tag script is refused by the browser before it runs a single line,
# and the vendor scripts it wants to fetch are refused too. Each vendor only widens the policy for
# the hosts it actually needs, and only while that vendor is configured.
CSP_BY_VENDOR = {
    'meta_pixel': {
        'script-src': ['https://connect.facebook.net'],
        'img-src': ['https://www.facebook.com', 'https://connect.facebook.net'],
        'connect-src': ['https://www.facebook.com', 'https://connect.facebook.net'],
    },
    'google_analytics': {
        'script-src': ['https://www.googletagmanager.com'],
        'img-src': ['https://www.google-analytics.com', 'https://*.google-analytics.com',
                    'https://www.googletagmanager.com'],
        'connect-src': ['https://www.google-analytics.com', 'https://*.google-analytics.com',
                        'https://*.analytics.google.com', 'https://www.googletagmanager.com'],
    },
    'google_ads': {
        'script-src': ['https://www.googletagmanager.com', 'https://www.googleadservices.com',
                       'https://googleads.g.doubleclick.net'],
        'img-src': ['https://www.google.com', 'https://googleads.g.doubleclick.net',
                    'https://www.googleadservices.com'],
        'connect-src': ['https://www.google.com', 'https://googleads.g.doubleclick.net'],
        'frame-src': ['https://td.doubleclick.net', 'https://www.googletagmanager.com'],
    },
    'google_tag_manager': {
        'script-src': ['https://www.googletagmanager.com', "'unsafe-inline'"],
        'img-src': ['https://www.googletagmanager.com'],
        'connect-src': ['https://www.googletagmanager.com'],
    },
}


def csp_nonce(request):
    """One nonce per request, shared between the injected script tag and the CSP header."""
    if not hasattr(request, '_tracking_nonce'):
        request._tracking_nonce = get_random_string(32)
    return request._tracking_nonce

# setting key -> cookie provider identifier the consent dialog uses
VENDOR_OF_SETTING = {
    'meta_pixel_id': 'meta_pixel',
    'ga4_id': 'google_analytics',
    'google_ads_id': 'google_ads',
    'google_ads_conversion_label': 'google_ads',
    'gtm_id': 'google_tag_manager',
}

PROVIDERS = {
    'meta_pixel': dict(
        provider_name='Meta Pixel (Facebook/Instagram)',
        usage_classes=[UsageClass.MARKETING],
        privacy_url='https://www.facebook.com/privacy/policy/',
    ),
    'google_analytics': dict(
        provider_name='Google Analytics',
        usage_classes=[UsageClass.ANALYTICS],
        privacy_url='https://policies.google.com/privacy',
    ),
    'google_ads': dict(
        provider_name='Google Ads',
        usage_classes=[UsageClass.MARKETING],
        privacy_url='https://policies.google.com/privacy',
    ),
    'google_tag_manager': dict(
        provider_name='Google Tag Manager',
        usage_classes=[UsageClass.ANALYTICS, UsageClass.MARKETING],
        privacy_url='https://policies.google.com/privacy',
    ),
}


def get_setting(event, key):
    """Resolve a tracking setting.

    Event settings win over organizer settings (pretix resolves that hierarchy for us), and the
    server configuration is the fallback. The latter is what makes ``PRETIX_TRACKING_<KEY>``
    environment variables work without touching the database.
    """
    value = event.settings.get('tracking_%s' % key) if event else None
    if not value:
        value = django_settings.CONFIG_FILE.get('tracking', key, fallback='')
    value = (value or '').strip()
    if not value or key in RAW_KEYS:
        return value
    if not ID_RE.match(value):
        logger.warning('Ignoring tracking setting %s: %r is not a valid tag ID', key, value)
        return ''
    return value


def configured_vendors(event):
    """Vendors that have at least one ID configured, so we never ask for consent we don't need."""
    vendors = set()
    for key, vendor in VENDOR_OF_SETTING.items():
        if get_setting(event, key):
            vendors.add(vendor)
    return vendors


def require_consent():
    """Whether tags must wait for an explicit yes. Off is a deliberate operator decision."""
    return str(
        django_settings.CONFIG_FILE.get('tracking', 'require_consent', fallback='on')
    ).lower() not in ('off', 'false', '0', 'no')


def marketing_consent_given(request):
    """Read the consent our own script mirrored into a cookie.

    The consent dialog stores its answer in localStorage, which the server cannot see. Without
    this mirror the server-side events would fire for visitors who said no, so a missing cookie
    is treated as "no".
    """
    if not require_consent():
        return True
    return request is not None and request.COOKIES.get(CONSENT_COOKIE) == '1'


def _purchase_data(request, event):
    """Order value for the Purchase/conversion event, read from the order the buyer just landed on.

    Only *paid* orders count. An unpaid bank-transfer order would otherwise be reported as a
    conversion here and reported again by the Conversions API when the money actually arrives.
    """
    from pretix.base.models import Order

    kwargs = request.resolver_match.kwargs
    code, secret = kwargs.get('order'), kwargs.get('secret')
    if not code or not secret:
        return None
    order = event.orders.filter(code=code, secret=secret).first()
    if not order or order.status != Order.STATUS_PAID:
        return None
    return {
        'order': order.code,
        'value': round(float(order.total), 2),
        'currency': event.currency,
        'items': [str(p.item.name) for p in order.positions.all()[:20]],
    }


def page_events(request, event):
    """Map the current page to the standard e-commerce events the ad platforms expect."""
    url = request.resolver_match
    if not url:
        return []
    name, events = url.url_name, []
    content = {'content_type': 'product', 'content_ids': [event.slug], 'content_name': str(event.name)}

    if name == 'event.index':
        events.append({'type': 'ViewContent', 'data': content})
    elif name in ('event.checkout', 'event.checkout.start'):
        step = url.kwargs.get('step')
        if step == 'payment':
            events.append({'type': 'AddPaymentInfo', 'data': content})
        elif step != 'confirm':
            # The checkout has several steps and html_head runs on all of them, so without this the
            # funnel would report one InitiateCheckout per step instead of one per buyer.
            if not request.session.get(SESSION_KEY_CHECKOUT):
                request.session[SESSION_KEY_CHECKOUT] = True
                events.append({'type': 'InitiateCheckout', 'data': content})
    elif name == 'event.order' and request.GET.get('thanks'):
        # pretix redirects here with ?thanks=1 (checkout) or ?thanks=yes (payment retry) once an
        # order is placed, so this is the one page that means "conversion".
        purchase = _purchase_data(request, event)
        if purchase:
            request.session.pop(SESSION_KEY_CHECKOUT, None)
            events.append({'type': 'Purchase', 'data': {**content, **purchase}})

    for ev in events:
        # The order code is a stable id both sides can derive; everything else needs a fresh one.
        ev['id'] = ev['data'].get('order') or new_event_id()
    return events


def client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR') or ''


def remember_campaign(request):
    """Stash campaign parameters and the entry point the first time they are seen.

    Called from ``global_html_head``, which runs on *every* frontend page — including the organizer
    listing, which is not tied to any event — so a visitor who lands with ``?utm_source=…`` still
    carries it however many pages later the order is created.

    The parameters follow last-click: arriving again through a different ad reassigns the sale to
    that ad. The landing URL and referrer are first-touch and never overwritten.
    """
    if not hasattr(request, 'session'):
        return
    for key in CAMPAIGN_KEYS:
        value = request.GET.get(key)
        if value and request.session.get(SESSION_CAMPAIGN % key) != value:
            request.session[SESSION_CAMPAIGN % key] = str(value)[:255]

    if not request.session.get(SESSION_LANDING):
        request.session[SESSION_LANDING] = request.build_absolute_uri()[:1000]
        referrer = request.META.get('HTTP_REFERER', '')
        if referrer:
            request.session[SESSION_REFERRER] = referrer[:1000]


def browser_identifiers(request):
    """The Meta click/browser ids, plus what identifies this request to the Graph API."""
    data = {
        'ip': client_ip(request),
        'ua': request.META.get('HTTP_USER_AGENT', '')[:500],
        'fbp': request.COOKIES.get('_fbp', ''),
        'fbc': request.COOKIES.get('_fbc', ''),
    }
    if not data['fbc'] and request.GET.get('fbclid'):
        # No _fbc cookie yet on the very first page of a click-through, but the click id is right
        # there in the URL and Meta accepts the documented fb.1.<ts>.<fbclid> form.
        data['fbc'] = 'fb.1.%d.%s' % (int(time.time() * 1000), request.GET['fbclid'])
    return {k: v for k, v in data.items() if v}


def send_server_side(event, request, page_evs, pixel_id, token):
    """Queue the server-side twin of every browser event except Purchase.

    Purchase is deliberately left out: it is sent from the ``order_paid`` receiver instead, so it
    reports the money rather than the page view, and it still carries the order code as event id.
    """
    test_code = get_setting(event, 'meta_test_event_code')
    if getattr(event, 'testmode', False) and not test_code:
        # Traffic on a test event must not train the real pixel.
        return
    api_version = get_setting(event, 'meta_api_version') or 'v21.0'
    user_data = browser_identifiers(request)
    source_url = request.build_absolute_uri()[:1000]
    for ev in page_evs:
        if ev['type'] == 'Purchase':
            continue
        payload = {
            'event_name': ev['type'],
            'event_time': int(time.time()),
            'event_id': ev['id'],
            'action_source': 'website',
            'event_source_url': source_url,
            'user_data': {
                'client_ip_address': user_data.get('ip', ''),
                'client_user_agent': user_data.get('ua', ''),
                **{k: user_data[k] for k in ('fbp', 'fbc') if k in user_data},
            },
            'custom_data': {
                'content_type': ev['data']['content_type'],
                'content_ids': ev['data']['content_ids'],
                'content_name': ev['data']['content_name'],
            },
        }
        try:
            capi_send.apply_async(kwargs={
                'pixel_id': pixel_id, 'token': token, 'event': payload,
                'test_event_code': test_code or None, 'api_version': api_version,
            })
        except Exception:
            # A broker hiccup must never break the shop page the visitor is looking at.
            logger.exception('Could not queue Meta CAPI event %s', ev['type'])


@receiver(register_cookie_providers, dispatch_uid="tracking_cookie_providers")
def cookie_providers(sender, request=None, **kwargs):
    return [
        CookieProvider(identifier=identifier, **PROVIDERS[identifier])
        for identifier in sorted(configured_vendors(sender))
    ]


@receiver(html_head, dispatch_uid="tracking_html_head")
def add_tracking_codes(sender, request=None, **kwargs):
    event = sender
    if request is None:
        return ""

    config = {
        'meta_pixel_id': get_setting(event, 'meta_pixel_id'),
        'ga4_id': get_setting(event, 'ga4_id'),
        'google_ads_id': get_setting(event, 'google_ads_id'),
        'google_ads_conversion_label': get_setting(event, 'google_ads_conversion_label'),
        'gtm_id': get_setting(event, 'gtm_id'),
        'require_consent': require_consent(),
        'consent_cookie': CONSENT_COOKIE,
        'events': page_events(request, event),
    }
    if not any(config[k] for k in ('meta_pixel_id', 'ga4_id', 'google_ads_id', 'gtm_id')):
        head_html = get_setting(event, 'head_html')
        return head_html or ""

    capi_token = get_setting(event, 'meta_capi_token')
    if config['meta_pixel_id'] and capi_token and config['events'] and marketing_consent_given(request):
        send_server_side(event, request, config['events'], config['meta_pixel_id'], capi_token)

    payload = json.dumps(config).replace('</', '<\\/')
    return (
        TEMPLATE.replace('__CONFIG__', payload).replace('__NONCE__', csp_nonce(request))
        + get_setting(event, 'head_html')
    )


@receiver(process_response, dispatch_uid="tracking_csp")
def extend_csp(sender, request=None, response=None, **kwargs):
    """Open the Content-Security-Policy just enough for the vendors that are actually configured.

    pretix's default policy is ``script-src 'self'``: without this the injected tag script never
    executes and every vendor request is blocked, which looks exactly like a pixel that "does not
    fire" with nothing in the logs to explain it.
    """
    vendors = configured_vendors(sender)
    if not vendors or request is None or response is None:
        return response

    csps = {'script-src': ["'nonce-%s'" % csp_nonce(request)]}
    for vendor in sorted(vendors):
        for directive, sources in CSP_BY_VENDOR.get(vendor, {}).items():
            csps.setdefault(directive, []).extend(sources)

    h = _parse_csp(response['Content-Security-Policy']) if 'Content-Security-Policy' in response else {}
    _merge_csp(h, csps)
    if h:
        response['Content-Security-Policy'] = _render_csp(h)
    return response


@receiver(global_html_head, dispatch_uid="tracking_campaign_capture")
def capture_campaign_everywhere(sender, request=None, **kwargs):
    """Catch the campaign parameters on any frontend page, not just event pages.

    ``html_head`` is an event signal, so a visitor whose first stop is the organizer listing —or any
    page of an event without the plugin— would arrive at the checkout with the attribution already
    lost. This one fires everywhere and emits nothing.
    """
    if request is not None:
        remember_campaign(request)
    return ""


@receiver(order_meta_from_request, dispatch_uid="tracking_order_meta")
def store_attribution(sender, request=None, **kwargs):
    """Freeze the campaign identifiers onto the order while we still have the browser.

    ``order_paid`` can fire days later from a cron or a webhook, with no request and no cookies,
    so whatever the Conversions API will need has to be captured here.
    """
    if request is None:
        return {}
    data = browser_identifiers(request)
    data['consent'] = marketing_consent_given(request)
    data['consent_not_required'] = not require_consent()
    data['url'] = request.build_absolute_uri()[:1000]
    if request.session.get(SESSION_LANDING):
        data['landing_url'] = request.session[SESSION_LANDING]
    if request.session.get(SESSION_REFERRER):
        data['referrer'] = request.session[SESSION_REFERRER]
    for key in CAMPAIGN_KEYS:
        value = request.GET.get(key) or request.session.get(SESSION_CAMPAIGN % key)
        if value:
            data[key] = str(value)[:255]
    return {'_tracking': data}


@receiver(order_paid, dispatch_uid="tracking_order_paid_capi")
def order_paid_capi(sender, order=None, **kwargs):
    """The conversion that matters. Sent server-side so ad blockers cannot drop it."""
    if order is None:
        return
    if not get_setting(sender, 'meta_pixel_id') or not get_setting(sender, 'meta_capi_token'):
        return
    try:
        capi_purchase.apply_async(kwargs={'order_pk': order.pk})
    except Exception:
        logger.exception('Could not queue Meta CAPI Purchase for order %s', order.code)


TEMPLATE = """
<script type="text/javascript" nonce="__NONCE__">
(function () {
    var cfg = __CONFIG__;
    var loaded = false;

    function vendorAllowed(consent, vendor) {
        if (consent === null || typeof consent === "undefined") {
            // The organizer has not enabled the cookie consent dialog. Loading anyway is their
            // decision to make; refusing would silently break every tag they configured.
            if (cfg.require_consent) {
                if (window.console) {
                    console.warn("[tracking] cookie consent dialog is disabled, tags stay off. "
                                 + "Enable it under organizer settings, or set "
                                 + "PRETIX_TRACKING_REQUIRE_CONSENT=off.");
                }
                return false;
            }
            return true;
        }
        return !!consent[vendor];
    }

    function loadMetaPixel() {
        !function (f, b, e, v, n, t, s) {
            if (f.fbq) return; n = f.fbq = function () {
                n.callMethod ? n.callMethod.apply(n, arguments) : n.queue.push(arguments)
            };
            if (!f._fbq) f._fbq = n; n.push = n; n.loaded = !0; n.version = "2.0"; n.queue = [];
            t = b.createElement(e); t.async = !0; t.src = v;
            s = b.getElementsByTagName(e)[0]; s.parentNode.insertBefore(t, s)
        }(window, document, "script", "https://connect.facebook.net/en_US/fbevents.js");
        fbq("init", cfg.meta_pixel_id);
        fbq("track", "PageView");
        cfg.events.forEach(function (ev) {
            var d = {
                content_type: ev.data.content_type,
                content_ids: ev.data.content_ids,
                content_name: ev.data.content_name
            };
            if (ev.type === "Purchase") {
                d.value = ev.data.value;
                d.currency = ev.data.currency;
            }
            // Same event_id server-side, so the Conversions API deduplicates the pair
            // instead of counting the same action twice.
            fbq("track", ev.type, d, {eventID: ev.id});
        });
    }

    function loadGtag(id, extra) {
        if (!window.gtag) {
            window.dataLayer = window.dataLayer || [];
            window.gtag = function () { window.dataLayer.push(arguments); };
            var s = document.createElement("script");
            s.async = true;
            s.src = "https://www.googletagmanager.com/gtag/js?id=" + encodeURIComponent(id);
            document.head.appendChild(s);
            gtag("js", new Date());
        }
        gtag("config", id, extra || {});
    }

    function loadGoogleAnalytics() {
        loadGtag(cfg.ga4_id);
        cfg.events.forEach(function (ev) {
            if (ev.type === "Purchase") {
                gtag("event", "purchase", {
                    transaction_id: ev.data.order,
                    value: ev.data.value,
                    currency: ev.data.currency,
                    items: [{item_id: ev.data.content_ids[0], item_name: ev.data.content_name}]
                });
            } else if (ev.type === "InitiateCheckout") {
                gtag("event", "begin_checkout", {items: [{item_id: ev.data.content_ids[0]}]});
            } else if (ev.type === "AddPaymentInfo") {
                gtag("event", "add_payment_info", {items: [{item_id: ev.data.content_ids[0]}]});
            } else if (ev.type === "ViewContent") {
                gtag("event", "view_item", {items: [{item_id: ev.data.content_ids[0]}]});
            }
        });
    }

    function loadGoogleAds() {
        loadGtag(cfg.google_ads_id);
        if (!cfg.google_ads_conversion_label) return;
        cfg.events.forEach(function (ev) {
            if (ev.type !== "Purchase") return;
            gtag("event", "conversion", {
                send_to: cfg.google_ads_id + "/" + cfg.google_ads_conversion_label,
                value: ev.data.value,
                currency: ev.data.currency,
                transaction_id: ev.data.order
            });
        });
    }

    function loadGtm() {
        window.dataLayer = window.dataLayer || [];
        window.dataLayer.push({"gtm.start": new Date().getTime(), event: "gtm.js"});
        cfg.events.forEach(function (ev) {
            window.dataLayer.push({event: "pretix_" + ev.type, ecommerce: ev.data});
        });
        var s = document.createElement("script");
        s.async = true;
        s.src = "https://www.googletagmanager.com/gtm.js?id=" + encodeURIComponent(cfg.gtm_id);
        document.head.appendChild(s);
    }

    function rememberConsent(consent) {
        // The dialog keeps its answer in localStorage, which the server cannot read. Mirroring
        // just the marketing bit into a cookie is what lets the Conversions API know whether it
        // is allowed to fire. Storing a consent choice is itself a strictly necessary cookie.
        var allowed = vendorAllowed(consent, "meta_pixel") ? "1" : "0";
        document.cookie = cfg.consent_cookie + "=" + allowed + ";path=/;max-age=31536000;SameSite=Lax"
            + (location.protocol === "https:" ? ";Secure" : "");
    }

    function apply(consent) {
        rememberConsent(consent);
        if (loaded) return;
        var any = false;
        if (cfg.meta_pixel_id && vendorAllowed(consent, "meta_pixel")) { loadMetaPixel(); any = true; }
        if (cfg.ga4_id && vendorAllowed(consent, "google_analytics")) { loadGoogleAnalytics(); any = true; }
        if (cfg.google_ads_id && vendorAllowed(consent, "google_ads")) { loadGoogleAds(); any = true; }
        if (cfg.gtm_id && vendorAllowed(consent, "google_tag_manager")) { loadGtm(); any = true; }
        loaded = any;
    }

    document.addEventListener("pretix:cookie-consent:change", function (e) {
        apply(e.detail);
    });
    if (window.pretix && typeof window.pretix.cookie_consent !== "undefined") {
        // Consent was already resolved before this script ran (cached page, widget).
        apply(window.pretix.cookie_consent);
    }
})();
</script>
"""
