"""Passive fee compatibility. Third-asset balances never silently fund a strategy."""
from decimal import Decimal
from .client import BinanceClientError
from .ledger import number


def inspect(commission):
    if not isinstance(commission, dict):
        raise BinanceClientError('commission evidence incomplete', blocker='FEE_EVIDENCE')
    discount = commission.get('discount')
    # Older account API responses omitted discount: fail closed in live mode.
    if not isinstance(discount, dict) or any(type(discount.get(k)) is not bool for k in ('enabledForAccount', 'enabledForSymbol')):
        raise BinanceClientError('fee discount evidence incomplete', blocker='FEE_EVIDENCE')
    enabled = discount['enabledForAccount'] and discount['enabledForSymbol']
    rate = Decimal(0)
    for key in ('standardCommission', 'taxCommission', 'specialCommission'):
        rates = commission.get(key)
        if not isinstance(rates, dict) or not {'maker', 'taker', 'buyer', 'seller'} <= rates.keys():
            raise BinanceClientError('commission coverage incomplete', blocker='FEE_EVIDENCE')
        rate += max(number(rates['maker'], zero=True), number(rates['taker'], zero=True))
        rate += max(number(rates['buyer'], zero=True), number(rates['seller'], zero=True))
    if rate >= 1:
        raise BinanceClientError('invalid commission rate', blocker='FEE_EVIDENCE')
    return {'compatible': not enabled, 'mode': 'BASE_QUOTE_ONLY', 'feeRateBound': str(rate),
            'discountEnabled': enabled, 'blocker': 'UNSUPPORTED_FEE_ASSET' if enabled else None,
            'remediation': 'OWNER_ACCOUNT_CONFIGURATION_REQUIRED' if enabled else None,
            'discount': discount, 'rates': {k: commission[k] for k in ('standardCommission', 'taxCommission', 'specialCommission')}}
