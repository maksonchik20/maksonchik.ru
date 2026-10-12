# Control: private owner PWA

Production: https://maksonchik.ru/panel/ (existing main-site superuser account).

The panel provides separate Who-Update and GhostNote views: daily revenue and acquisition charts, payment history with referrer/commission details, all-time partner balances, Who-Update withdrawal requests, current Who-Update access and GhostNote token schedules. Dates use Europe/Moscow. GhostNote token creation reuses the admin form and has a UUID request key enforced by a unique database row.

## Data definitions

- Who-Update daily users follow `webhook_tg.user_metrics.collect_daily_user_metrics`, including user-per-day deduplication. Disconnect counts reflect each user's last stored disconnect date, as `/stat` does.
- GhostNote connections are each identified user's first program session; `/start` counts distinct Telegram users per day. The program does not store business-bot disconnect events.
- Paid orders drive revenue. GhostNote real tokens without a linked purchase order are separate manual purchases; an order and its issued token are never counted twice. Test orders/tokens are excluded.
- New `checkout_at` fields record a successful redirect to a payment provider, once per order. Historical orders use creation time when a provider payment ID exists and the order did not fail. This is an approximation, disclosed in the UI.
- Referral balances use the existing ledger. Who-Update new withdrawal requests reserve funds; paid requests reduce balances. GhostNote paid commission flags reduce balances. The panel does not change commission rates or mark payouts automatically.

## GhostNote payouts

GhostNote → Referrals shows each commission with its recipient, buyer, purchase amount, commission and paid checkbox. Filters show unpaid, paid or all entries; search matches partner/buyer. Results are paginated at 20 entries. Partner totals and the admin use the same `GhostReferralCommission.is_paid` field. Checking records an already completed transfer; unchecking restores the amount due.

`POST /panel/api/ghost-payout/` requires an active superuser and CSRF. The server proxies only an integer commission ID, a boolean desired state and the displayed row's revision to the authenticated Ghost bridge (`set-payout`). GhostNote locks the row and rejects stale amount/recipient/state snapshots with HTTP 409. It changes only `is_paid`, without saving a token or issuing a payment. Successful acknowledgements create a `PanelAudit` entry with the authenticated user. A failed or uncertain request refreshes the list; retries never blindly invert a checkbox. No schema or commission-rate changes are required.

## GhostNote token management

GhostNote → Tokens uses phone-friendly cards. Open Manage to view/copy the token, edit exact Moscow start/end times, extend by 30 minutes/one hour/one day, set a note, change local/remote/AI modes, or enable/disable the token. Changes apply on Save. Token creation remains the existing separate form. Active remote-enabled token details also show Open viewer, using the private detail response's `viewer_url` in a separate window with noopener/noreferrer. The server chooses the legacy viewer or the current v2 session URL and viewer key. Only older APIs without `viewer_url` fall back to the encoded token route; an explicit missing/invalid URL is not replaced with a legacy link. Links are restricted to HTTPS GhostNote viewer routes on `yc.maksonchik.ru`. The URL is available only after loading private token details and is not cached.

`GET /panel/api/ghost/token/?id=...` exposes the token secret only to the owner in a no-store detail response. Lists never include the secret. `POST /panel/api/update-token/` requires an active superuser and CSRF; the authenticated bridge locks the row and checks a revision before validating the existing admin form. Only operational columns are updated, bypassing `GhostAccessToken.save()` financial synchronization. User, token value, type, payment, existing paid commissions and usage timestamps stay unchanged. Date precision is preserved when a date is not edited. Test-token edits keep their existing schedule. Successful updates create an owner audit entry. Stale edits return 409 and require reloading the detail.

AI access (`allow_ai`) defaults to off for both real and test tokens; it must be enabled explicitly. The creation form, confirmation and token cards include the AI mode, and Manage can turn it on or off independently of local/remote access. At least one of the three modes must remain selected. A missing AI flag from an older detail API displays as off. The update proxy accepts an optional boolean `allow_ai`; old callers that omit it preserve the server value. Creation defaults an omitted flag to false. Provider credentials stay server-side and never enter the PWA.

## Web Push

`owner-panel-notifications.timer` runs `process_panel_notifications` approximately every 30 seconds. Each source has a cursor and activation cutoff. Unique event keys and delivery records prevent repeated queueing; an overlap covers short delays and outages. Temporary delivery failures retry with backoff. HTTP 404/410 disables a subscription. Subscriptions receive only events occurring after registration/reactivation; old purchases do not trigger an initial flood.

Categories: Who-Update checkout/payment; GhostNote checkout/payment (including recorded manual real-token purchases); expiry of enabled real GhostNote tokens. The notifications carry an account label and amount/token ID, never the token secret. Per-device permission is requested only by a button click. There is an explicit test-push button.

HTTPS and an installed Home Screen app are required for iPhone Web Push (iOS 16.4+). Real-device delivery must be verified after the owner grants permission. No Apple Developer subscription is required.

## Configuration and deployment

Server-only `/srv/maksonchik/owner-panel.json` (root:www-data, 0640) contains `ghost_url`, `bridge_token`, `vapid_private_key` (PEM path), `vapid_public_key`, and `vapid_subject`. The private PEM has the same permissions. Keep keys stable across releases. Do not commit or print either secret.

The GhostNote bridge is `/ghost/owner-api/<action>/`, authenticated with **`X-Owner-Panel-Key`**, not the browser's session. Apache mod_wsgi on this host strips the standard Authorization header. Use the dedicated header on both sides; bridge URLs and secrets never appear in frontend assets.

Who-Update migrations: `webhook_tg.0044_whoupdatepaymentorder_checkout_at`, `owner_panel.0001_initial`.
GhostNote migration: `ghost_note.0018_owner_panel` (checkout timestamp and token request ledger).
Install `pywebpush==2.5.0` in the main site's Python 3.12 virtualenv only. GhostNote remains on its existing Django/Python runtime.

Serve `owner_panel/static/owner_panel/` at `/static/owner_panel/`; service worker lives at `/panel/sw.js`. Increment CSS/JS query versions and the public-cache version in `sw.js` together for future asset updates. Only public static assets and a generic offline page are cached; private HTML, API responses, payments and token secrets are network-only and no-store. Login is superuser-only with CSRF protection and a login attempt throttle. Logout disables this device's subscription.

## Mobile scrolling

The mobile app uses a viewport-height grid. Only `.workspace` scrolls; navigation stays in its own bottom row with safe-area padding. Tab changes reset that scroller. Avoid restoring a document-level fixed footer, which can drift in the installed iPhone PWA. Desktop pages retain normal document scrolling. Current public assets/cache are version 7.

## Verification

Tests in `owner_panel/tests.py` cover roles/CSRF, rate limits, `/stat` agreement, revenue/referrals, duplicate/replayed events, source outages, push retries, subscription expiration and real encryption/VAPID signing with a mocked transport. GhostNote's `test_owner_panel.py` covers bridge auth, creation validation/idempotency, checkout tracking, token filtering, expiry notifications and non-duplicated revenue.

Release report and recovery manifest: `../reports/owner-pwa-2026-09-15.md` relative to the enclosing workspace, plus `output/owner-pwa-2026-09-15/`. Backups remain on the server; rollback should restore only the recorded changed files and stop the new timer. The additive schema can remain; do not restore an old financial database over new purchases.
