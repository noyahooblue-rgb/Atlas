package com.example.core;

/**
 * Case 1 - boundary value under-tested.
 *
 * exceedsLimit() uses a strict greater-than against MAX. The test only
 * checks values far away from the boundary (10000 and 50), never the
 * boundary value 100 itself, so PIT's conditionals-boundary mutant
 * (">" changed to ">=") is expected to SURVIVE.
 */
public class AmountLimit {

    public static final int MAX = 100;

    public boolean exceedsLimit(int amount) {
        return amount > MAX;
    }
}
