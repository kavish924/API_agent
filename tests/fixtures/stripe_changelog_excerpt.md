<!--
THIS IS A GENERATED FILE. Any changes you make to it directly will be blown away.
Instead, edit a corresponding `.change.md` file and run `hark build`.
-->

# Changelog

## <a id="17-0-0"></a>17.0.0 - 2026-11-11
* Improve webhook signature verification error messages
  - When diagnostic metadata is present in the `Stripe-Signature` header, error messages help identify an incorrect signing secret or altered request body.
* ⚠️ Omit API keys from pickled StripeObjects

  StripeObject pickles no longer preserve API keys. Before making requests through an unpickled object, call `reattach_api_key(api_key)`. This also reattaches API keys to nested objects.

  See the [v17 migration guide](https://github.com/stripe/stripe-python/wiki/Migration-guide-for-v17#api-keys-are-no-longer-preserved-in-pickled-stripeobjects) for details.
* Fix stale Stripe-Account header when fetching an object related to a directly-retrieved Event
* Reject header values containing CR, LF or NUL in the pycurl HTTP client

## <a id="16-1-0"></a>16.1.0 - 2026-10-29
This release changes the pinned API version to `2026-10-28.endive`.

* [#1940](https://github.com/stripe/stripe-python/pull/1940) Reject ASCII-whitespace-only webhook secrets
  - Reject webhook signing secrets made entirely of ASCII whitespace instead of using them as HMAC keys.
* [#1939](https://github.com/stripe/stripe-python/pull/1939) Add API v2 Search support

  Adds `stripe.v2.SearchResultObject[T]` and support for auto pagination of search results.
* [#1944](https://github.com/stripe/stripe-python/pull/1944) Make Stripe errors serializable

  * (excerpt: the rest of the 16.1.0 section is omitted in this fixture)

## <a id="16-0-0"></a>16.0.0 - 2026-09-30
This release changes the pinned API version to `2026-09-30.endive`.

* [#1904](https://github.com/stripe/stripe-python/pull/1904) Support `EventNotification`s with singleton related objects
* [#1909](https://github.com/stripe/stripe-python/pull/1909) Allow suppressing Stripe notices

  Set the `STRIPE_SUPPRESS_NOTICES` environment variable to `true` to suppress Stripe notices in test and sandbox environments when not running under a detected AI agent. Notices remain enabled by default and continue to be shown to AI agents.
* [#1911](https://github.com/stripe/stripe-python/pull/1911) Fix account scoping for event notification handler callback clients
  - Fix callback clients to use the event's Stripe context and preserve the original client's non-account configuration.
  - Fix API errors when using an event notification handler with a client configured with a Stripe account.
* ⚠️ [#1914](https://github.com/stripe/stripe-python/pull/1914) Remove deprecated `StripeObject.request()` method

  The deprecated `StripeObject.request()` method has been removed. Use `StripeClient.raw_request()` to make custom API requests.
* ⚠️ [#1915](https://github.com/stripe/stripe-python/pull/1915) Rename `stripe.Reversal` to `stripe.TransferReversal`
  - ⚠️ Rename the `stripe.Reversal` resource class to `stripe.TransferReversal`. Update references and type annotations to use `stripe.TransferReversal`.
* ⚠️ [#1920](https://github.com/stripe/stripe-python/pull/1920) Make path parameters positional-only in all service methods

  Path parameters must now be passed positionally to service methods. Passing them by keyword is no longer supported. This prevents parameter names derived from the API specification from becoming part of the public interface.

   Resource methods are unaffected by this change.
* ⚠️ [#1924](https://github.com/stripe/stripe-python/pull/1924) Make path parameters positional-only in all service methods

  Path parameters must now be passed positionally on resource methods that support both classmethod and instance-method call styles (e.g. Coupon.delete). Passing them by keyword is no longer supported.
* ⚠️ [#1899](https://github.com/stripe/stripe-python/pull/1899) Update generated code
  * Add support for new resources `apps.Install`, `product_catalog.TrialOffer`, `tax.Location`, and `three_d_secure.Authentication`
  * Add support for `create`, `list`, `modify`, `retrieve`, and `uninstall` methods on resource `apps.Install`
  * Add support for `create`, `list`, `modify`, and `retrieve` methods on resource `product_catalog.TrialOffer`
  * Add support for `create`, `list`, and `retrieve` methods on resource `tax.Location`
  * Add support for `cancel`, `create`, `list`, `retrieve`, and `submit` methods on resource `three_d_secure.Authentication`
  * Add support for `pause` method on resource `Subscription`
  * ⚠️ Add support for new values `invalid_address_cmra_address` and `invalid_address_registered_agent_address` on enums `Account.FutureRequirement.Error.code`, `Account.Requirement.Error.code`, `BankAccount.FutureRequirement.Error.code`, `BankAccount.Requirement.Error.code`, `Capability.FutureRequirement.Error.code`, `Capability.Requirement.Error.code`, `Person.FutureRequirement.Error.code`, and 
  * Add support for `performance_location` on `Tax.CalculationLineItem` and `tax.CalculationCreateParamsLineItem`
  * Add support for new value `performance` on enums `Tax.Calculation.ShippingCost.TaxBreakdown.sourcing`, `Tax.CalculationLineItem.TaxBreakdown.sourcing`, and `Tax.Transaction.ShippingCost.TaxBreakdown.sourcing`
  * Add support for new values `admissions_tax`, `attendance_tax`, `digital_excise_tax`, `entertainment_tax`, `gross_receipts_tax`, `hospitality_tax`, `luxury_tax`, `recycling_fee`, `resort_tax`, `tourism_tax`, and `utility_users_tax` on enums `Tax.Calculation.ShippingCost.TaxBreakdown.TaxRateDetail.tax_type`, `Tax.Calculation.TaxBreakdown.TaxRateDetail.tax_type`, `Tax.CalculationLineItem.TaxBreak
  * Add support for `destination` on `Reserve.Hold`, `Reserve.Plan`, and `Reserve.Release`
  * Add support for `manual_release` on `Reserve.Plan`
  * ⚠️ Add support for new value `other` on enum `Reserve.Plan.status`
  * ⚠️ Add support for new values `manual_release` and `other` on enum `Reserve.Plan.type`
  * ⚠️ Add support for new value `hold_expired` on enum `Reserve.Release.reason`
  * ⚠️ Remove support for value `bulk_hold_expiry` from enum `Reserve.Release.reason`
  * (excerpt: the rest of the 16.0.0 section is omitted in this fixture)

## <a id="15-6-1"></a>15.6.1 - 2026-09-01
* [#1860](https://github.com/stripe/stripe-python/pull/1860) Dispatch discriminated union fields to their variant class
* [#1896](https://github.com/stripe/stripe-python/pull/1896) Harden API requestor code against malicious URLs
* [#1898](https://github.com/stripe/stripe-python/pull/1898) Use cryptographically secure boundaries for multipart file uploads

