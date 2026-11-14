package com.example.core;

/**
 * Case 2 - target of a "fake" test.
 *
 * The production code is ordinary arithmetic. The problem is in the
 * test: it only asserts that the result is never negative, which stays
 * true for most mutations of the arithmetic, so PIT is expected to
 * report SURVIVED mutants in this class.
 */
public class DiscountCalculator {

    public int finalPrice(int price, int percentOff) {
        int discount = price * percentOff / 100;
        return price - discount;
    }
}
