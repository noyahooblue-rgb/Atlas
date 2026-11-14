package com.example.core;

import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class DiscountCalculatorTest {

    /**
     * "Fake" test: the assertion is so weak that it cannot fail for
     * these inputs no matter how the arithmetic is mutated
     * (short of producing a negative price).
     */
    @Test
    public void finalPriceIsNeverNegative() {
        DiscountCalculator calc = new DiscountCalculator();
        assertTrue(calc.finalPrice(100, 10) >= 0);
        assertTrue(calc.finalPrice(200, 25) >= 0);
    }
}
