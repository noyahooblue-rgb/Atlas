package com.example.app;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

public class OrderServiceTest {

    private final OrderService service = new OrderService();

    @Test
    public void hugeOrderNeedsReview() {
        assertEquals("REVIEW", service.checkOrder(10000));
    }

    @Test
    public void normalOrderIsOk() {
        assertEquals("OK", service.checkOrder(50));
    }
}
