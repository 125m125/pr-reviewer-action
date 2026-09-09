package example.api;

enum OrderStatus { CREATED, SHIPPED, CANCELLED }

final class OrderController {
    String event(String id) { return "{\"id\":\"" + id + "\",\"status\":\"CANCELLED\"}"; }
}
