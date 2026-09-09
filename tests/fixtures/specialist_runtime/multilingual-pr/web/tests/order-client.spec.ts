import { label } from "../src/order-client";

test("labels cancelled orders", () => expect(label("CANCELLED")).toBe("Cancelled"));
