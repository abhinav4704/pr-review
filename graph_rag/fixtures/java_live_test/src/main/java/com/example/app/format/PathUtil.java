package com.example.app.format;

public class PathUtil {
    // Deliberately same simple class name as com.example.app.util.PathUtil —
    // OrderController explicitly imports THIS one
    // (com.example.app.format.PathUtil), so the resolver must pick this
    // class, not the other same-named one, for the CALLS/qualified-import
    // precedence test.
    public static String normalize(String orderId) {
        return orderId == null ? "" : orderId.trim().toLowerCase();
    }
}
