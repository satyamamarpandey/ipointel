from app.services.backtest import brier

def test_brier():
    assert round(brier([(80,1),(20,0)]),3)==.04
