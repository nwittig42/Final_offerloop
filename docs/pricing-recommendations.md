# Offerloop Pricing — Canonical Spec

Last updated: 2026-07-28 (.edu 50% discount). This doc is the spec the
code mirrors. If you change pricing, change this file first, then follow
`.claude/skills/offerloop-pricing-update` to land it everywhere.

## Strategy (decided 2026-07-27, .edu discount added 2026-07-28)

Goal: **maximize the number of paying users** with the lowest possible
friction. Simplicity sells. That means:

- One decision per tier: Free or Pro (Elite anchors). No credit slider, no
  annual toggle.
- **.edu = 50% off.** Users who sign up with a .edu email (or verify one
  later) pay half: Pro $4.99, Elite $17.49. Everyone else pays list ($9.99 /
  $34.99). The gate is server-side (`user_is_student_eligible`), restored
  2026-07-28 from the PR #153 implementation.
- 7-day card-on-file free trial (card collected at checkout; billing starts
  day 8 unless canceled). One trial per account lifetime (`trialUsedAt`).
  Same length for both audiences — the .edu benefit is price, not trial.
- Wallet payments on: Link, Apple Pay, Google Pay, Cash App (dashboard
  payment-method configuration governs; code passes no `payment_method_types`).
- Paywall at the moment of pain: UpgradeModal / TrialBanner go straight to
  Stripe Checkout via `src/services/checkout.ts`, not through /pricing.

## Live tiers

| | Free | Pro | Elite |
|---|---|---|---|
| List price | $0 | **$9.99/mo** | $34.99/mo |
| .edu price (50% off) | $0 | **$4.99/mo** | $17.49/mo |
| Credits/month | 300 | 2,000 | 5,000 |
| Contacts/search | 3 | 10 | 15 |
| Trial | — | 7 days, card on file | (checkout also grants 7 days) |

.edu eligibility (any one satisfies): sign-up email ends in `.edu`, Firestore
`isStudent` flag is true, or Firestore `eduEmail` ends in `.edu`.

Also live: Season Pass ($99 .edu / $199 list one-time, 4 months, 3,000 cr/mo —
same eligibility gate) and top-up packs ($4.99/500, $9.99/1,500, $24.99/3,000 —
Pro/Elite only, credits never expire, same price for everyone).

## Stripe SKUs

| SKU | Price ID | Status |
|---|---|---|
| Pro monthly .edu $4.99 | `price_1TyFiKERY2WrVHp1vTi7L5Wj` (lookup_key `pro_monthly_edu_499_2026`) | **Current** — default for `STRIPE_PRO_MONTHLY_STUDENT_2K` |
| Pro monthly list $9.99 | `price_1TxzloERY2WrVHp15NcO6deA` (lookup_key `pro_monthly_999_2026`) | **Current** — default for `STRIPE_PRO_MONTHLY_LIST_2K` |
| Elite monthly .edu $17.49 | `price_1TyFiKERY2WrVHp15fnEAhPu` (lookup_key `elite_monthly_edu_1749_2026`) | **Current** — default for `STRIPE_ELITE_MONTHLY_STUDENT_5K` |
| Elite monthly list $34.99 | `price_1ScLcfERY2WrVHp1c5rcONJ3` | **Current** — default for `STRIPE_ELITE_MONTHLY_LIST_5K` |
| Pro monthly $14.99 (legacy) | `price_1ScLXrERY2WrVHp1bYgdMAu4` | Existing subscribers only; resolved via legacy constant |

Existing subscribers are grandfathered on whatever they pay today; nothing was
migrated. Old multi-stop inline subscriptions ($19.99/3K etc.) renew untouched.
Subscribers who joined at $9.99 before 2026-07-28 keep $9.99 even if they have
a .edu email — the discount applies at checkout, not retroactively.

**Render env check**: verified 2026-07-28 via `/api/tier-config` that none of
the `STRIPE_*_MONTHLY_*` catalog env vars are set in Render, so the code
defaults above govern. If any get set later they override the defaults.

## Retired (2026-07-27) — code kept dormant for possible revert

- Credit slider (SLIDER_STOPS collapsed to one default stop per tier)
- Annual cadence (toggle removed from /pricing; ANNUAL_PRICING dict unused)
- No-card 600-credit trial (`/api/users/start-trial` endpoint still live for
  pre-cutover trial users, but no UI calls it anymore)
- ~~.edu student gate~~ **Un-retired 2026-07-28** — real gate restored for the
  50% .edu discount.

## Mobile (RevenueCat) parity

The iOS/Android app bills through RevenueCat, not Stripe. Keep the structure
identical: Free tier → 7-day intro trial on Pro → $9.99/mo list, Elite
$34.99/mo, no annual. **TODO**: RevenueCat has no .edu-priced packages yet —
mobile users pay list until a student offering is configured there.

## Kill criteria (check ~60 days after launch, late Sept 2026)

- Free→paid conversion falls below the 22% baseline → the card-on-file trial
  is costing more than it converts; test a 7-day no-card variant.
- Trial-to-paid below 25% → trial isn't reaching activation; revisit
  onboarding, not price.
- .edu discount (added 2026-07-28): if ≥80% of new paid signups clear the .edu
  gate, the "50% off" is effectively the price and the list price is fiction —
  either make $4.99 the single price or tighten verification.
