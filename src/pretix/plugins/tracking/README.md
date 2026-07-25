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

The purchase event carries the order code as Meta's `eventID` and as GA4's `transaction_id`, so a
server-side Conversions API call for the same order deduplicates against the browser event instead
of double counting.

## Consent

The plugin registers one cookie provider per configured vendor, so each one shows up as its own
checkbox in pretix's cookie consent dialog and nothing loads until the visitor agrees. The dialog
itself only appears once at least one vendor is configured.

Enable it once per organizer:

    organizer.settings.cookie_consent = True

With `require_consent` left at `on` and the dialog disabled, the tags stay off and log a warning to
the browser console — deliberately, since running a marketing pixel without consent is not legal in
the EU. Set `PRETIX_TRACKING_REQUIRE_CONSENT=off` only if consent is being collected elsewhere.
