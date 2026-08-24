from auction_engine import hard_budget_ceiling, recommend

def player(name="Trevor Lawrence", value=39, position_rank="QB8"):
    return {"player": name, "value": value, "position_rank": position_rank}

def test_bid_is_exactly_one_dollar():
    assert recommend(player(), 20, 200, 16, 0, [], set(), True).action == "BID $21"

def test_target_gets_three_dollar_extension():
    rec = recommend(player(), 41, 200, 16, 0, [], set(), True)
    assert rec.action == "BID $42" and rec.max_bid == 42

def test_forbidden_is_always_pass():
    assert recommend(player("Bijan Robinson", 54, "RB2"), 1, 200, 16, 0, [], {"Bijan Robinson"}, True).action == "PASS"

def test_reserve_caps_bid():
    assert hard_budget_ceiling(20, 10, 5, 1) == 16
    assert recommend(player("Example Player", 30, "WR1"), 16, 20, 10, 5, [], set(), True).action == "STOP AT $16"

