package com.acme.reports;

/**
 * Fixture: a class in com.acme.reports must NOT receive the "repository" role
 * just because "repo" is a prefix of "reports". The pkg-segment rule must use
 * exact dotted-segment matching (["com","acme","reports"]), not substring.
 */
public class ReportGenerator {
    public String generate(String reportId) {
        return "report:" + reportId;
    }
}
