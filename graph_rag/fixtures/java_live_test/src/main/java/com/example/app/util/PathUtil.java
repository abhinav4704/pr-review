package com.example.app.util;

public class PathUtil {
    // Same simple name as com.example.app.format.PathUtil, deliberately, in a
    // different package — nothing in this fixture explicitly imports this
    // one, so a correct resolver must NOT let this shadow the explicitly
    // imported com.example.app.format.PathUtil in OrderController.
    public static String normalize(String orderId) {
        return "UTIL:" + orderId;
    }
}
