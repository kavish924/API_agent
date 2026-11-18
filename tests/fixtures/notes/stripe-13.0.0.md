This release changes the pinned API version to `2025-09-30.clover` and contains breaking changes (prefixed with ⚠️ below)

* [#1604](https://github.com/stripe/stripe-python/pull/1604) Fixed InvoiceLineItem method definition
  * ⚠️ `InvoiceLineItem.modify` and `InvoiceLineItem.modify_async` now require `invoice` and `line_item_id` as method parameters.
    * Removed `InvoiceLineItem.ModifyParam` class. Use a `typing.dict` to type hint instead.
* [#1538](https://github.com/stripe/stripe-python/pull/1538) ⚠️ Add strongly typed EventNotifications
  We've overhauled how V2 Events are handled in the SDK! This approach should provide a lot more information at authoring and compile time, leading to more robust integrations. As part of this process, there are a number of changes to be aware of.
  - Added matching `EventNotification` classes to every v2 `Event`. For example, there's now a `V1BillingMeterErrorReportTriggeredEventNotification` to match the existing `V1BillingMeterErrorReportTriggeredEvent`. Each notification class defines a `fetch_event()` method to retrieve its corresponding event. For events with related objects, there's a `fetch_related_object()` method that performs the API call and casts the response to the correct type.
  - ⚠️ Rename function `StripeClient.parse_thin_event` to `StripeClient.parse_event_notification` and remove the `Stripe.ThinEvent` class.
      - This function now returns a `stripe.v2.core.EventNotification` (which is the shared base class that all of the more specific `stripe.events.*EventNotification` classes  share) instead of `Stripe.ThinEvent`. When applicable, these event notifications will have the `related_object` property and a `fetch_related_object()` function. They also have a `fetch_()` method to retrieve their corresponding `stripe.events.*Event` instance.
      - If you parse an event the SDK doesn't have types for (e.g. it's newer than the SDK you're using), you'll get an instance of `Stripe.Events.UnknownEventNotification` instead of a more specific type. It has both the `relatedObject` property and the `FetchRelatedObject()` function (but they may be/return `null`)
* [#1602](https://github.com/stripe/stripe-python/pull/1602) Move `V2.Event` API resources to `V2.Core.Events`
  - ⚠️ Move `stripe.v2._event` and `stripe.v2._event_destination` to `stripe.v2.core._event` and `stripe.v2.core._event_destination` respectively. They now correctly match their API path
* [#1589](https://github.com/stripe/stripe-python/pull/1589) Add `StripeContext` object
  - Add the `StripeContext` class. Previously, one could only pass a string for `stripe_context`. You can now use the new class as well.
  - ⚠️ Change `EventNotification` (formerly known as `ThinEvent`)'s `context` property from `string` to `StripeContext`
* [#1565](https://github.com/stripe/stripe-python/pull/1565) ⚠️ Build SDK w/ V2 OpenAPI spec
  - ⚠️ The delete methods for v2 APIs (the ones in the `StripeClient.v2` namespace) now return a `V2DeletedObject` which has the id of the object that has been deleted and a string representing the type of the object that has been deleted.
  - ⚠️ Deeply nested param hashes with no properties no longer have classes generated for them. Instead, they're typed as `Dict[str, Any]`. Because there were no params, it's unlikely you were using these classes.
* [#1569](https://github.com/stripe/stripe-python/pull/1569) Renamed Urllib2Client to UrllibClient
  - ⚠️ Rename `http_client.Urllib2Client` to `http_client.UrllibClient` as Python `urllib2` was renamed to `urllib` in Python 3.
* [#1606](https://github.com/stripe/stripe-python/pull/1606) ⚠️ drop support for Python 3.6 & clarify version policy
  - Read our new [language version support policy](https://docs.stripe.com/sdks/versioning?server=python#stripe-sdk-language-version-support-policy)
      - ⚠️ In this release, we drop support for Python 3.6
      - Support for Python 3.7 is deprecated and will be removed in the next scheduled major release (March 2026)
* [#1596](https://github.com/stripe/stripe-python/pull/1596) ⚠️ Unify resource and service method parameters into one class
  * ⚠️ Resource and service request parameter types have been moved to the top-level and are shared, prepended with their related resource/service
    * For example, `_stripe._account.Account.CreateParams` and `_stripe._account_service.CreateParams` have moved to `_stripe.params._account_create_params.AccountCreateParams`
    * This change only affects users who explicitly refer to params types. No migration is necessary for users otherwise
* [#1572](https://github.com/stripe/stripe-python/pull/1572) migrate from `setup.py` to `pyproject.toml`
  - ⚠️ The package has swapped from `setup.py` to `pyproject.toml`. As a result, we're dropping support for `pip < 10.0.0` (released April 2018).
  - Additionally, we're no longer shipping tests or examples in our sdist now, which should offer a small size reduction for the package if installed without the wheel (approx. 2.5MB unzipped)
* [#1570](https://github.com/stripe/stripe-python/pull/1570) Don't use mutable default arguments
  - Service methods now correctly set `None` as the default function argument instead of `{}`
* ⚠️ Deprecated the V1 service accessors living directly under StripeClient(e.g. customers, products) as they were copied under the new V1 service in our [last release](https://github.com/stripe/stripe-python/releases/tag/v12.5.0). Service accessors living directly under StripeClient(e.g. customers, products) will be removed from StripeClient in a future release. E.g.
  ```diff
  client = StripeClient("sk_test...")

  # Accessing V1 Stripe services on a StripeClient should be through the V1 namespace
  - client.customers.list()
  + client.v1.customers.list()
  ```
  Refer to the [migration guide](https://github.com/stripe/stripe-python/wiki/v1-namespace-in-StripeClient) for help upgrading.
* [#1603](https://github.com/stripe/stripe-python/pull/1603) ⚠️ Remove deprecated compatibility exports
  - ⚠️ Removed deprecated module shims. They've long been available in the `stripe` module directly; now that's the only place to import them. Specifically, we removed:
    - `stripe.stripe_response`
    - `stripe.stripe_object`
    - `stripe.error_object`
    - `stripe.error`
    - `stripe.http_client`
    - `stripe.util`
    - `stripe.oauth`
    - `stripe.webhook`
    - `stripe.multipart_data_generator`
    - `stripe.request_metrics`
    - `stripe.api_resources.abstract`
    - `stripe.api_resources`


  To update your code, follow this pattern:

  ```diff
  -from stripe.<MODULE> import SomeClass
  +from stripe import SomeClass

  -stripe.<MODULE>.SomeClass
  +stripe.SomeClass
  ```

  - ⚠️ Removed the `FileUpload` alias

  To update your code:

  ```diff
  -from stripe import FileUpload
  -from stripe.api_resources import FileUpload
  +from stripe import File
  ```

  - ⚠️ Removed the `io` import from `stripe._util`. If you had code relying on `stripe.util.io`, you'll need to import the `io` package directly yourself.
  - added `UrllibClient` to `stripe` to make creating your own HTTP client easier.

* [#1567](https://github.com/stripe/stripe-python/pull/1567), [#1593](https://github.com/stripe/stripe-python/pull/1593), [#1607](https://github.com/stripe/stripe-python/pull/1607), [#1605](https://github.com/stripe/stripe-python/pull/1605) Update generated code based on incoming API changes in the `2025-09-30.clover` API version.
  * ⚠️ Remove support for `balance_report` and `payout_reconciliation_report` on `AccountSession.Component` and `AccountSession.CreateParamsComponent`
  * ⚠️ Remove support for values `saturday` and `sunday` from enums `Account.CreateParamsSettingPayoutSchedule.weekly_payout_days`, `Account.ModifyParamsSettingPayoutSchedule.weekly_payout_days`, and `Account.Setting.Payout.Schedule.weekly_payout_days`
  * ⚠️ Remove support for `iterations` on `Invoice.CreatePreviewParamsScheduleDetailPhase`, `SubscriptionSchedule.CreateParamsPhase`, and `SubscriptionSchedule.ModifyParamsPhase`
  * ⚠️ Remove support for `link` and `pay_by_bank` on `PaymentMethod.ModifyParams`
  * ⚠️ Remove support for `coupon` on `Discount`, `PromotionCode.CreateParams`, and `PromotionCode`. Use `Discount.source.coupon`, `PromotionCode.CreateParams.promotion.coupon`, and `PromotionCode.promotion.coupon` instead.
  * Add support for new value `prevented` on enum `Dispute.status`
  * Change `Invoice.id` to be required.
  * Add support for new resource `BalanceSettings`
  * Add support for `modify` and `retrieve` methods on resource `BalanceSettings`
  * Add support for new values `external_request` and `unsupported_business_type` on enums `Account.FutureRequirement.Error.code`, `Account.Requirement.Error.code`, `BankAccount.FutureRequirement.Error.code`, `BankAccount.Requirement.Error.code`, `Capability.FutureRequirement.Error.code`, `Capability.Requirement.Error.code`, `Person.FutureRequirement.Error.code`, and `Person.Requirement.Error.code`
  * Add support for `source` on `Discount`
  * Add support for `mb_way_payments` on `Account.Capability`, `Account.CreateParamsCapability`, and `Account.ModifyParamsCapability`
  * Add support for `trial_update_behavior` on `BillingPortal.Configuration.Feature.SubscriptionUpdate`, `billing_portal.Configuration.CreateParamsFeatureSubscriptionUpdate`, and `billing_portal.Configuration.ModifyParamsFeatureSubscriptionUpdate`
  * Add support for `mb_way` on `Charge.PaymentMethodDetail`, `ConfirmationToken.CreateParamsPaymentMethodDatum`, `ConfirmationToken.PaymentMethodPreview`, `PaymentIntent.ConfirmParamsPaymentMethodDatum`, `PaymentIntent.ConfirmParamsPaymentMethodOption`, `PaymentIntent.CreateParamsPaymentMethodDatum`, `PaymentIntent.CreateParamsPaymentMethodOption`, `PaymentIntent.ModifyParamsPaymentMethodDatum`, `PaymentIntent.ModifyParamsPaymentMethodOption`, `PaymentIntent.PaymentMethodOption`, `PaymentMethod.CreateParams`, `PaymentMethod`, `SetupIntent.ConfirmParamsPaymentMethodDatum`, `SetupIntent.CreateParamsPaymentMethodDatum`, and `SetupIntent.ModifyParamsPaymentMethodDatum`
  * Add support for `branding_settings` and `name_collection` on `Checkout.Session` and `checkout.Session.CreateParams`
  * Add support for `excluded_payment_method_types` on `Checkout.Session`, `PaymentIntent.ConfirmParams`, `PaymentIntent.ModifyParams`, and `checkout.Session.CreateParams`
  * Add support for `unit_label` on `Invoice.AddLinesParamsLinePriceDatumProductDatum`, `Invoice.UpdateLinesParamsLinePriceDatumProductDatum`, `InvoiceLineItem.ModifyParamsPriceDatumProductDatum`, `PaymentLink.CreateParamsLineItemPriceDatumProductDatum`, and `checkout.Session.CreateParamsLineItemPriceDatumProductDatum`
  * Add support for `alma`, `billie`, and `satispay` on `Checkout.Session.PaymentMethodOption` and `checkout.Session.CreateParamsPaymentMethodOption`
  * Add support for `demo_pay` on `checkout.Session.CreateParamsPaymentMethodOption`
  * Add support for `capture_method` on `Checkout.Session.PaymentMethodOption.Affirm`, `Checkout.Session.PaymentMethodOption.AfterpayClearpay`, `Checkout.Session.PaymentMethodOption.AmazonPay`, `Checkout.Session.PaymentMethodOption.Card`, `Checkout.Session.PaymentMethodOption.Cashapp`, `Checkout.Session.PaymentMethodOption.Klarna`, `Checkout.Session.PaymentMethodOption.Link`, `Checkout.Session.PaymentMethodOption.Mobilepay`, `Checkout.Session.PaymentMethodOption.RevolutPay`, `checkout.Session.CreateParamsPaymentMethodOptionAffirm`, `checkout.Session.CreateParamsPaymentMethodOptionAfterpayClearpay`, `checkout.Session.CreateParamsPaymentMethodOptionAmazonPay`, `checkout.Session.CreateParamsPaymentMethodOptionCard`, `checkout.Session.CreateParamsPaymentMethodOptionCashapp`, `checkout.Session.CreateParamsPaymentMethodOptionKlarna`, `checkout.Session.CreateParamsPaymentMethodOptionLink`, `checkout.Session.CreateParamsPaymentMethodOptionMobilepay`, and `checkout.Session.CreateParamsPaymentMethodOptionRevolutPay`
  * Add support for `flexible` on `Invoice.CreatePreviewParamsScheduleDetailBillingMode`, `Invoice.CreatePreviewParamsSubscriptionDetailBillingMode`, `Quote.CreateParamsSubscriptionDatumBillingMode`, `Quote.SubscriptionDatum.BillingMode`, `Subscription.BillingMode`, `Subscription.CreateParamsBillingMode`, `Subscription.MigrateParamsBillingMode`, `SubscriptionSchedule.BillingMode`, `SubscriptionSchedule.CreateParamsBillingMode`, and `checkout.Session.CreateParamsSubscriptionDatumBillingMode`
  * Add support for `business_name` and `individual_name` on `Checkout.Session.CollectedInformation`, `Checkout.Session.CustomerDetail`, `Customer.CreateParams`, `Customer.ModifyParams`, and `Customer`
  * Add support for new values `mb_way` on enums `ConfirmationToken.CreateParamsPaymentMethodDatum.type`, `PaymentIntent.ConfirmParamsPaymentMethodDatum.type`, `PaymentIntent.CreateParamsPaymentMethodDatum.type`, `PaymentIntent.ModifyParamsPaymentMethodDatum.type`, `SetupIntent.ConfirmParamsPaymentMethodDatum.type`, `SetupIntent.CreateParamsPaymentMethodDatum.type`, and `SetupIntent.ModifyParamsPaymentMethodDatum.type`
  * Add support for new values `mb_way` on enums `ConfirmationToken.PaymentMethodPreview.type` and `PaymentMethod.type`
  * Add support for new values `mb_way` on enums `Customer.ListPaymentMethodsParams.type`, `PaymentMethod.CreateParams.type`, and `PaymentMethod.ListParams.type`
  * Add support for `chargeback_loss_reason_code` on `Dispute.PaymentMethodDetail.Klarna`
  * Add support for `net_amount` and `proration_details` on `InvoiceItem`
  * Add support for `fraud_disputability_likelihood` and `risk_assessment` on `issuing.Authorization.CreateParams`
  * Add support for `second_line` on `Issuing.Card`
  * Add support for new values `mb_way` on enums `PaymentIntent.CreateParams.excluded_payment_method_types` and `PaymentIntent.excluded_payment_method_types`
  * Add support for `fr_meal_voucher_conecs` on `PaymentMethodConfiguration.CreateParams` and `PaymentMethodConfiguration.ModifyParams`
  * Add support for `promotion` on `PromotionCode.CreateParams` and `PromotionCode`
  * Add support for new values `acknowledged` and `payment_never_settled` on enum `Review.closed_reason`
  * Add support for `provider` on `Tax.Settings.Default`
  * Add support for `bbpos_wisepad3` on `Terminal.Configuration`, `terminal.Configuration.CreateParams`, and `terminal.Configuration.ModifyParams`
  * Add support for `address_kana`, `address_kanji`, `display_name_kana`, `display_name_kanji`, and `phone` on `Terminal.Location`, `terminal.Location.CreateParams`, and `terminal.Location.ModifyParams`
  * Change `terminal.Location.CreateParams.address` to be optional
  * Change `terminal.Location.CreateParams.display_name` to be optional
  * Add support for new value `2025-09-30.clover` on enum `WebhookEndpoint.CreateParams.api_version`
  * Add support for error codes `financial_connections_account_pending_account_numbers` and `financial_connections_account_unavailable_account_numbers` on `Invoice.LastFinalizationError`, `PaymentIntent.LastPaymentError`, `SetupAttempt.SetupError`, `SetupIntent.LastSetupError`, and `StripeError`

See [the changelog for more details](https://github.com/stripe/stripe-python/blob/v13.0.0/CHANGELOG.md).
