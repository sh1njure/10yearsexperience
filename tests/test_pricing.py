"""Round-up-to-increment pricing."""
from decimal import Decimal
from app.importer import ceil_to


def test_ceil_to_half():
    assert ceil_to(Decimal("50.2"), Decimal("0.5")) == Decimal("50.5")
    assert ceil_to(Decimal("50.0"), Decimal("0.5")) == Decimal("50.0")
    assert ceil_to(Decimal("50.6"), Decimal("0.5")) == Decimal("51.0")
    assert ceil_to(Decimal("50.5"), Decimal("0.5")) == Decimal("50.5")


def test_ceil_to_disabled():
    assert ceil_to(Decimal("50.23"), Decimal("0")) == Decimal("50.23")


def test_roundup_then_vat_gives_round_gross():
    # gross 50.2 -> round up to 50.5 -> store net -> net*1.23 back == 50.50
    inc, rate = Decimal("0.5"), Decimal("23")
    divisor = Decimal(1) + rate / Decimal(100)
    gross = ceil_to(Decimal("50.2"), inc)          # 50.5
    net = gross / divisor                            # stored
    back = (net * divisor).quantize(Decimal("0.01"))
    assert back == Decimal("50.50")
