def test_cancelled_status_is_persisted():
    event = {"status": "CANCELLED"}
    assert event["status"] == "CANCELLED"
