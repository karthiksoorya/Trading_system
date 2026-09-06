from tests.test_zones import TestDepartureStrength
from engine.zones import detect_zones
from engine.candle import is_exciting, is_boring

t = TestDepartureStrength()
candles = t._make_dbr_candles(n_context=15)

print("count:", len(candles))

for i, c in enumerate(candles):
    print(
        i,
        c,
        "exciting=", is_exciting(c),
        "boring=", is_boring(c),
    )

zones = detect_zones(candles, "5minute")
print("zones:", zones)