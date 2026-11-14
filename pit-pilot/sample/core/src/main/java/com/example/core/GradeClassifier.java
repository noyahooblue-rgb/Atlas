package com.example.core;

/**
 * Case 3 - thoroughly tested class.
 *
 * Every branch is checked, including the exact boundary values
 * (90/89, 80/79, 60/59), so PIT is expected to report all mutants
 * KILLED for this class.
 */
public class GradeClassifier {

    public String classify(int score) {
        if (score >= 90) {
            return "A";
        }
        if (score >= 80) {
            return "B";
        }
        if (score >= 60) {
            return "C";
        }
        return "F";
    }
}
