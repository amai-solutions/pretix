# Marketing tracking codes

Embeds Meta Pixel, Google Analytics 4, Google Ads and Google Tag Manager into the ticket shop
without touching any code. IDs come from the server configuration, so on a container deployment
they are plain environment variables.

## Configuration

| Environment variable | `pretix.cfg` | What it is |
| --- | --- | --- |
| `PRETIX_TRACKING_META_PIXEL_ID` | `[tracking] meta_pixel_id` | Meta (Facebook/Instagram) pixel ID |
| `PRETIX_TRACKING_GA4_ID` | `[tracking] ga4_id` | Google Analytics 4 measurement ID (`G-…`) |
| `PRETIX_TRACKING_GTM_ID` | `[tracking] gtm_id` | Google Tag Manager container (`GTM-…`) |
| `PRETIX_TRACKING_GOOGLE_ADS_ID` | `[tracking] google_ads_id` | Google Ads conversion ID (`AW-…`) |
| `PRETIX_TRACKING_GOOGLE_ADS_CONVERSION_LABEL` | `[tracking] google_ads_conversion_label` | Conversion label for the purchase goal |
| `PRETIX_TRACKING_META_CAPI_TOKEN` | `[tracking] meta_capi_token` | Conversions API access token. Enables the server-side twin of every event |
| `PRETIX_TRACKING_META_TEST_EVENT_CODE` | `[tracking] meta_test_event_code` | Events Manager "Test events" code. While set, events do not count as real conversions |
| `PRETIX_TRACKING_META_API_VERSION` | `[tracking] meta_api_version` | Graph API version, defaults to `v21.0` |
| `PRETIX_TRACKING_REQUIRE_CONSENT` | `[tracking] require_consent` | `on` (default) or `off` |

Every value can be overridden per event (or per organizer) through the settings store, using the
same key prefixed with `tracking_`, e.g. `event.settings.tracking_meta_pixel_id`. That is how you
give a single city its own pixel. Empty or missing values simply disable that vendor, and an ID
that is not made of `A-Za-z0-9_-` is dropped with a warning rather than embedded.

## Events sent

| Page | Meta | GA4 |
| --- | --- | --- |
| any shop page | `PageView` | `page_view` |
| event front page | `ViewContent` | `view_item` |
| checkout | `InitiateCheckout` | `begin_checkout` |
| checkout, payment step | `AddPaymentInfo` | `add_payment_info` |
| order page after purchase (`?thanks=…`) | `Purchase` (value, currency) | `purchase` (transaction_id, value) |

The purchase event carries the order code as Meta's `eventID` and as GA4's `transaction_id`, and
every other event carries a random id shared with its server-side twin, so the Conversions API
deduplicates against the browser event instead of double counting.

`Purchase` only fires for orders that are actually **paid**. An unpaid bank-transfer order is not a
conversion, and reporting it at checkout would mean reporting it a second time when the money
arrives.

## Server-side (Meta Conversions API)

With `PRETIX_TRACKING_META_CAPI_TOKEN` set, every event above is also sent straight from the server,
so ad blockers, tracking protection and consent-less browsers stop costing you conversions:

* `ViewContent`, `InitiateCheckout` and `AddPaymentInfo` are queued from the request that renders
  the page, carrying the visitor's IP, user agent and `_fbp`/`_fbc` cookies.
* `Purchase` is sent from the `order_paid` signal instead, so it reports money rather than a page
  view and still arrives when the buyer closed the tab. Its identity block adds SHA-256 hashes of
  email, phone, first/last name, city, ZIP, state and country, which is what raises Meta's event
  match quality score.

Nothing personal is ever hashed outside the worker process and nothing personal is put on the
Celery queue: the task receives only the order's primary key and re-reads it.

Attribution (`_fbp`, `_fbc`, `fbclid`, `utm_*`, `gclid`, IP, user agent and the consent answer) is
frozen onto `order.meta_info['_tracking']` at checkout, because `order_paid` can fire days later
from a webhook with no request and no cookies.

Orders and traffic on a **test-mode event** are skipped entirely unless a test event code is
configured, so a rehearsal never trains the real pixel.

### Consent and the server

The consent dialog stores its answer in `localStorage`, which the server cannot read. The plugin's
script mirrors just the marketing answer into a `pretix_tracking_consent` cookie; the server-side
calls only fire when that cookie says yes (or when `require_consent` is `off`). Storing a consent
choice is itself a strictly necessary cookie.

## Consent

The plugin registers one cookie provider per configured vendor, so each one shows up as its own
checkbox in pretix's cookie consent dialog and nothing loads until the visitor agrees. The dialog
itself only appears once at least one vendor is configured.

Enable it once per organizer:

    organizer.settings.cookie_consent = True

With `require_consent` left at `on` and the dialog disabled, the tags stay off and log a warning to
the browser console — deliberately, since running a marketing pixel without consent is not legal in
the EU. Set `PRETIX_TRACKING_REQUIRE_CONSENT=off` only if consent is being collected elsewhere.
