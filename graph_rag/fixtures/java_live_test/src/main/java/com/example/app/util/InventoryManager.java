package com.example.app.util;

import java.util.HashMap;
import java.util.Map;

import org.springframework.stereotype.Component;

@Component
public class InventoryManager {

    // Shared mutable state, no lock — reserveStock does a classic
    // read-modify-write race_condition.
    private static final Map<String, Integer> stock = new HashMap<>();

    public boolean reserveStock(String sku, int qty) {
        Integer current = stock.getOrDefault(sku, 0);
        if (current < qty) {
            return false;
        }
        stock.put(sku, current - qty); // read-modify-write, no synchronization
        return true;
    }
}
