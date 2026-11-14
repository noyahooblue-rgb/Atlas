package com.example.core;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

public class GradeClassifierTest {

    private final GradeClassifier classifier = new GradeClassifier();

    @Test
    public void gradeA() {
        assertEquals("A", classifier.classify(100));
        assertEquals("A", classifier.classify(90));
    }

    @Test
    public void gradeB() {
        assertEquals("B", classifier.classify(89));
        assertEquals("B", classifier.classify(80));
    }

    @Test
    public void gradeC() {
        assertEquals("C", classifier.classify(79));
        assertEquals("C", classifier.classify(60));
    }

    @Test
    public void gradeF() {
        assertEquals("F", classifier.classify(59));
        assertEquals("F", classifier.classify(0));
    }
}
