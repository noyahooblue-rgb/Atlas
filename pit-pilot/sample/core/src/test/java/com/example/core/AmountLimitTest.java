package com.example.core;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class AmountLimitTest {

    private final AmountLimit limit = new AmountLimit();

    @Test
    public void farAboveLimitExceeds() {
        assertTrue(limit.exceedsLimit(10000));
    }

    @Test
    public void smallAmountDoesNotExceed() {
        assertFalse(limit.exceedsLimit(50));
    }
}
