# Daily public wheel (formerly owner-only pilot)

Enabled for all registered private Telegram chats as of 8 September 2026. The server checks
signed Telegram `initData` (maximum age one hour); a URL or client-supplied user
ID cannot grant access. Group chats and unknown accounts are rejected. `/spin`
sends the approved announcement and a Mini App button; subscription keyboards
also include the wheel. `is_pilot` is the historical shared eligibility helper
name, retained for compatibility with payment worker imports, and now accepts
any positive private-chat identity. `setup_wheel_pilot` is a legacy owner-menu
command and is not part of the public rollout.

Prizes / independent probabilities: −10% 45%, −15% 25%, −20% 15%, +3 days 4%,
+7 days 1%, double duration 10%. The equal visual sectors are decorative; the
actual probabilities are private server-side configuration, not exposed in
the rules, Mini App API or new invitation messages.

As of 15 October 2026, new monthly orders cost 129 RUB (quarterly 249 RUB,
yearly 800 RUB). The free-day probability is 5% per new spin, with an expected
0.19 free days per spin. Existing rewards, granted access and reserved order
amounts/durations are unchanged. An unreserved checkout link does not fix a
price; the newly created order uses the current tariff, shown before payment.

One spin per Moscow calendar day (resets at 00:00). +3/+7 days extend access
immediately for free, from the current expiry or now if access has expired.
The access extension, reward receipt and notification are atomic and idempotent.
Unlimited access is preserved. No checkout is created for free-day prizes.
Discount/double rewards allow one purchase and 24 hours to reserve an order.
New unreserved rewards replace older purchase bonuses; already granted days remain;
previously reserved orders retain their terms. History is retained and a
database unique constraint enforces `(user, spin_day)` for rewards whose
`spin_reset_at` is null. A superuser-only admin action can mark the latest
current-day reward as reset and allow one additional spin. It does not
rewrite the historical date, prize or payment, and writes an admin audit log.
Repeated/stale actions cannot reset a newer reward. The wheel
does not require a first-ever payment. All three plans
are eligible for discount/double bonuses. Repeat requests return the persisted prize. Order reservations
and provider idempotency prevent duplicate redemption; completion uses the
order's immutable amount and duration, including after reward expiry.

HTML messages and Mini App use `<s>`; Telegram button labels use Unicode
combining strikethrough because button text is plain text. Existing messages
are not retroactively edited; newly generated offers use the saved reward.

Code entry points: `wheel.py`, `wheel_views.py`, `wheel_checkout.py`,
`static/webhook_tg/wheel.js`. Migrations: `0036_wheel_pilot`, `0037_daily_wheel`.
The latter backfills existing rewards using their creation date in Moscow.
`0039_wheel_spin_reset` adds the optional reset timestamp and partial unique
constraint. No test/private-owner wording appears in the Mini App. Source assets must
also be available under the deployed static directory. The wheel route has
its own Telegram-only `frame-ancestors` policy; the Apache exception must stay
limited to `/bot/wheel/`.

`0040_wheel_instant_days` adds `granted_days` (with a database default for safe
rolling upgrades) and `access_expires_at_after`. Free rewards are marked used
immediately, shown as “Начислен бесплатно” in admin, and excluded from all
purchase offers. Previously reserved/used legacy day rewards must not be granted
again; reserved payment snapshots are never rewritten.

Verification: `test_wheel`, `test_wheel_admin` plus existing subscription,
reminder, metric and bot command suites (79 tests, including instant free-day
grants, retries, rollback, unlimited/expired access, midnight and historical checkout handling). Local UI fixture checks covered animation,
discount/double-period displays and the 320px layout. Live checks confirmed
owner state 200, correctly signed nonowner request 403, unchanged default
menu and delivered owner invitation. No real spin or payment was performed
by the deployment.

Instant-days rollout: `/srv/maksonchik/deploy-backups/instant-days-20260908-OxImra`.
The owner's existing unpaid +3-day reward (id 10) was credited once, extending
access from 12 September to 15 September 2026; existing payment orders were
verified unchanged. Both +3/+7 result screens show the new expiry without
tariff/payment buttons. Owner-only gating was subsequently lifted in the public rollout.

Public rollout: 126 tests passed (84 focused tests plus 42 core bot tests), including nonowner signed requests,
free-day rewards, discounted checkout, cross-user isolation, /spin and an
idempotent broadcast. `announce_wheel` is read-only by default; `--send` queues
the approved announcement once per distinct private chat at five due items per
second. Existing outbox workers deliver and retry it. Campaign key:
`wheel-public-launch-20260908:`. Deploy and verify public API/commands before
queuing. Do not rerun `setup_wheel_pilot` to publish the wheel.

Public source backup: `/srv/maksonchik/deploy-backups/public-wheel-20260908-ZvQr3s`.
Live HTTPS state for a registered nonowner returned 200; unsigned spin returned
403. Telegram command lists were read back to verify `/spin` and preserved
owner-only `/stat`. The broadcast was queued for 399 distinct private chats
only after these checks, with its payload compared byte-for-byte to the approved
owner preview (outbox 1435).
Completed broadcast: 399 recipients, 279 sent, 120 permanently undeliverable
(115 blocked the bot, 4 deactivated accounts, 1 chat not found), zero pending.

Deployment backup: `/srv/maksonchik/deploy-backups/wheel-pilot-20260908-HMrEcU`.
Only wheel-related files were deployed; unrelated landing-page changes were
not included. Do not use a broad dirty-worktree deployment to release this.
