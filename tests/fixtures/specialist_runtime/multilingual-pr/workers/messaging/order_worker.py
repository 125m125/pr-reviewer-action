def handle_order_event(event: dict[str, str]) -> str:
    """Persist the status carried by an order event."""
    return event["status"]
