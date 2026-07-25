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

from django.conf import settings as django_settings
from django.dispatch import receiver

from pretix.presale.cookies import CookieProvider, UsageClass
from pretix.presale.signals import html_head, register_cookie_providers

logger = logging.getLogger(__name__)

# Tag IDs end up inside a <script> block, so we only accept the characters the vendors actually use.
# Anything else is dropped and logged instead of being embedded.
ID_RE = re.compile(r'^[A-Za-z0-9_\-]{1,64}$')

# Marks that this visitor already got an InitiateCheckout for the checkout they are in.
SESSION_KEY_CHECKOUT = '_tracking_checkout_started'

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
    if not value or key == 'head_html':
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


def _purchase_data(request, event):
    """Order value for the Purchase/conversion event, read from the order the buyer just landed on."""
    kwargs = request.resolver_match.kwargs
    code, secret = kwargs.get('order'), kwargs.get('secret')
    if not code or not secret:
        return None
    order = event.orders.filter(code=code, secret=secret).first()
    if not order:
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
    return events


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
        'require_consent': str(
            django_settings.CONFIG_FILE.get('tracking', 'require_consent', fallback='on')
        ).lower() not in ('off', 'false', '0', 'no'),
        'events': page_events(request, event),
    }
    if not any(config[k] for k in ('meta_pixel_id', 'ga4_id', 'google_ads_id', 'gtm_id')):
        head_html = get_setting(event, 'head_html')
        return head_html or ""

    payload = json.dumps(config).replace('</', '<\\/')
    return TEMPLATE.replace('__CONFIG__', payload) + get_setting(event, 'head_html')


TEMPLATE = """
<script type="text/javascript">
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
                // Same event_id server-side, so the Conversions API can deduplicate.
                fbq("track", "Purchase", d, {eventID: ev.data.order});
                return;
            }
            fbq("track", ev.type, d);
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

    function apply(consent) {
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
