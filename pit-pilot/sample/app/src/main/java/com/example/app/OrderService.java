package com.example.app;

import com.example.core.AmountLimit;

/**
 * Lives in the app module, which depends on the core module.
 * Used to demonstrate running PIT per-module in a multi-module build.
 */
public class OrderService {

    private final AmountLimit amountLimit = new AmountLimit();

    public String checkOrder(int amount) {
        if (amountLimit.exceedsLimit(amount)) {
            return "REVIEW";
        }
        return "OK";
    }
}
