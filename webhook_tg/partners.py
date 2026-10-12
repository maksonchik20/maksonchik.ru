"""Accrual ledger for direct referrals; payment retries cannot accrue twice."""
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Count, DecimalField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import (UserTg, WhoUpdatePaymentOrder, WhoUpdatePartnerSettings,
                     WhoUpdatePartnerProfile, WhoUpdatePartnerCommission as Commission,
                     WhoUpdatePartnerWithdrawal as Withdrawal)


def program_settings():
    return WhoUpdatePartnerSettings.objects.get(pk=1)


def partner_percent(user, config=None):
    config = config or program_settings()
    if not config.is_active:
        return Decimal("0")
    profile = WhoUpdatePartnerProfile.objects.filter(user=user).first()
    if profile:
        if not profile.is_active:
            return Decimal("0")
        if profile.commission_percent is not None:
            return profile.commission_percent
    return config.commission_percent


@transaction.atomic
def accrue_commission(order):
    order = WhoUpdatePaymentOrder.objects.select_for_update().get(pk=order.pk)
    existing = Commission.objects.filter(order=order).first()
    if existing:
        return existing
    if order.status != order.Status.PAID or order.is_test or not order.paid_at or order.amount <= 0:
        return None
    config = program_settings()
    if not config.is_active or order.paid_at < config.started_at:
        return None
    buyer = UserTg.objects.get(pk=order.user_id)
    if not buyer.referred_by_id:
        return None
    partner = UserTg.objects.get(pk=buyer.referred_by_id)
    if partner.pk == buyer.pk or partner.user_id == buyer.user_id:
        return None
    percent = partner_percent(partner, config)
    amount = (order.amount * percent / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if percent <= 0 or amount <= 0:
        return None
    return Commission.objects.create(
        order=order, partner=partner, buyer=buyer, purchase_amount=order.amount,
        commission_percent=percent, amount=amount, created_at=order.paid_at,
    )


def partner_balance(user):
    earned = Commission.objects.filter(partner=user, status=Commission.Status.EARNED).aggregate(total=zero_sum("amount"))["total"]
    withdrawals = Withdrawal.objects.filter(partner=user).aggregate(
        reserved=zero_sum("amount", filter=Q(status=Withdrawal.Status.NEW)),
        paid=zero_sum("amount", filter=Q(status=Withdrawal.Status.PAID)),
    )
    return {"earned": earned, **withdrawals, "payable": earned - withdrawals["reserved"] - withdrawals["paid"]}


@transaction.atomic
def create_withdrawal(user, amount, *, request_key):
    # All debits and cancellations serialize on the same partner row.
    UserTg.objects.select_for_update().get(pk=user.pk)
    existing = Withdrawal.objects.filter(request_key=request_key).first()
    if existing:
        if existing.partner_id != user.pk or existing.amount != amount:
            raise ValueError("Эта заявка уже использована.")
        return existing
    if not amount.is_finite() or amount < Decimal("0.01") or amount > Decimal("9999999999.99") or amount != amount.quantize(Decimal("0.01")):
        raise ValueError("Укажите положительную сумму с точностью до копеек.")
    available = partner_balance(user)["payable"]
    if amount > available:
        raise ValueError(f"Недостаточно средств. Доступно к выводу: {available:.2f} ₽.")
    return Withdrawal.objects.create(partner=user, amount=amount, request_key=request_key)


@transaction.atomic
def resolve_withdrawals(ids, *, status, actor, note):
    if status not in (Withdrawal.Status.PAID, Withdrawal.Status.REJECTED):
        raise ValueError("Недопустимый статус заявки.")
    note = note.strip()
    if not note or len(note) > 500:
        raise ValueError("Укажите номер перевода или причину отказа (до 500 символов).")
    partner_ids = Withdrawal.objects.filter(pk__in=ids).values_list("partner_id", flat=True)
    list(UserTg.objects.select_for_update().filter(pk__in=partner_ids).order_by("pk"))
    rows = list(Withdrawal.objects.select_for_update().filter(pk__in=ids, status=Withdrawal.Status.NEW).order_by("pk"))
    for row in rows:
        row.status, row.note, row.resolved_at, row.resolved_by = status, note, timezone.now(), actor
        row.save(update_fields=["status", "note", "resolved_at", "resolved_by"])
    return rows


@transaction.atomic
def void_commissions(commission_ids, *, actor, reason):
    reason = reason.strip()
    if not reason or len(reason) > 500:
        raise ValueError("Укажите причину аннулирования (до 500 символов).")
    partner_ids = Commission.objects.filter(pk__in=commission_ids).values_list("partner_id", flat=True)
    partners = list(UserTg.objects.select_for_update().filter(pk__in=partner_ids).order_by("pk"))
    rows = list(Commission.objects.select_for_update().filter(
        pk__in=commission_ids, status=Commission.Status.EARNED,
    ).order_by("pk"))
    for partner in partners:
        amount = sum((r.amount for r in rows if r.partner_id == partner.pk), Decimal("0"))
        if amount > partner_balance(partner)["payable"]:
            raise ValueError(f"Нельзя аннулировать начисления {partner}: средства уже зарезервированы или выплачены.")
    ids = [r.pk for r in rows]
    Commission.objects.filter(pk__in=ids).update(status=Commission.Status.VOID,
        void_reason=reason, voided_by=actor, voided_at=timezone.now())
    return ids


def money_field():
    return DecimalField(max_digits=14, decimal_places=2)


def zero_sum(field, **kwargs):
    return Sum(field, default=Decimal("0"), output_field=money_field(), **kwargs)


def referral_statistics(user):
    commission = Commission.objects.filter(partner=user, buyer_id=OuterRef("pk")).exclude(status=Commission.Status.VOID)
    earned = commission.order_by().values("buyer_id").annotate(total=Sum("amount")).values("total")[:1]
    referrals = UserTg.objects.filter(referred_by=user).annotate(
        purchase_count=Count("payment_orders", filter=Q(payment_orders__status="paid", payment_orders__is_test=False)),
        purchase_total=zero_sum("payment_orders__amount", filter=Q(payment_orders__status="paid", payment_orders__is_test=False)),
        commission_total=Coalesce(Subquery(earned, output_field=money_field()), Value(Decimal("0")), output_field=money_field()),
    ).order_by("-purchase_total", "pk")
    summary = referrals.aggregate(referral_count=Count("pk"), purchases=Sum("purchase_count", default=0),
                                  revenue=zero_sum("purchase_total"))
    summary.update(partner_balance(user))
    return referrals, summary
