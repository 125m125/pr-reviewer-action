export type OrderStatus = "CREATED" | "SHIPPED" | "CANCELLED";

export function label(status: OrderStatus): string {
  switch (status) {
    case "CREATED": return "Created";
    case "SHIPPED": return "Shipped";
  }
  return "Unknown";
}
