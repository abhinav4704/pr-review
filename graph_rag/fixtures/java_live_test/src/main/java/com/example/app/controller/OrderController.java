package com.example.app.controller;

import java.sql.SQLException;

import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RestController;

import com.example.app.format.PathUtil;
import com.example.app.repository.OrderRepository;
import com.example.app.service.OrderService;

@RestController
public class OrderController {

    private final OrderService orderService;
    // Kept alongside orderService so quickDeleteEndpoint can deliberately
    // skip the service layer (layering_violation).
    private final OrderRepository orderRepository;

    public OrderController(OrderService orderService, OrderRepository orderRepository) {
        this.orderService = orderService;
        this.orderRepository = orderRepository;
    }

    @GetMapping("/orders/{orderId}")
    public String getOrderEndpoint(String orderId) throws SQLException {
        String normalized = PathUtil.normalize(orderId); // exercises the qualified (explicit) import of format.PathUtil
        return orderService.getOrder(normalized);
    }

    @PostMapping("/orders")
    public void placeOrderEndpoint(String orderId, String payload) throws SQLException {
        orderService.placeOrder(orderId, payload);
    }

    @DeleteMapping("/orders/{orderId}/quick")
    public void quickDeleteEndpoint(String orderId) throws SQLException {
        // layering_violation: controller calls OrderRepository directly,
        // skipping OrderService.
        orderRepository.deleteOrder(orderId);
    }

    @DeleteMapping("/admin/orders/{orderId}")
    public void adminDeleteOrderEndpoint(String orderId) throws SQLException {
        // missing_authorization: destructive admin operation with no
        // auth/role check anywhere in this method.
        orderRepository.deleteOrder(orderId);
    }
}
