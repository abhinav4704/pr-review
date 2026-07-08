package com.example.app.service;

import java.sql.ResultSet;
import java.sql.SQLException;

import org.springframework.stereotype.Service;

import com.example.app.repository.OrderRepository;

@Service
public class OrderService {

    private final OrderRepository orderRepository;

    public OrderService(OrderRepository orderRepository) {
        this.orderRepository = orderRepository;
    }

    public String getOrder(String orderId) throws SQLException {
        ResultSet rs = orderRepository.fetchOrder(orderId);
        // unhandled_empty: indexes into the result set without checking
        // whether fetchOrder actually returned a row first.
        rs.next();
        return rs.getString("payload");
    }

    public void placeOrder(String orderId, String payload) throws SQLException {
        orderRepository.saveOrder(orderId, payload);
    }

    public void notify(String orderId, String message) {
        orderRepository.logNotification(orderId, message);
    }

    public void recordAudit(String orderId, String message) {
        // repository -> service, closing the deliberate cycle with
        // OrderRepository.logNotification above.
        System.out.println("AUDIT " + orderId + ": " + message);
    }
}
