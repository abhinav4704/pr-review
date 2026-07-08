package com.example.app.repository;

import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;

import org.springframework.stereotype.Repository;

import com.example.app.service.OrderService;

@Repository
public class OrderRepository {

    // Deliberate circular constructor dependency (repository -> service) so
    // that OrderService.notify -> OrderRepository.logNotification ->
    // OrderService.recordAudit forms a real service<->repository call cycle
    // for the architecture pass's cycle detector.
    private final OrderService orderService;
    private final Connection connection;

    public OrderRepository(OrderService orderService, Connection connection) {
        this.orderService = orderService;
        this.connection = connection;
    }

    public ResultSet fetchOrder(String orderId) throws SQLException {
        Statement stmt = connection.createStatement();
        String sql = "SELECT * FROM orders WHERE id = '" + orderId + "'";
        return stmt.executeQuery(sql);
    }

    public ResultSet searchOrders(String term) throws SQLException {
        Statement stmt = connection.createStatement();
        String sql = "SELECT * FROM orders WHERE name LIKE '%" + term + "%'";
        return stmt.executeQuery(sql);
    }

    public void saveOrder(String orderId, String payload) throws SQLException {
        Statement stmt = connection.createStatement();
        String sql = "INSERT INTO orders (id, payload) VALUES ('" + orderId + "', '" + payload + "')";
        stmt.executeUpdate(sql);
    }

    public void deleteOrder(String orderId) throws SQLException {
        Statement stmt = connection.createStatement();
        String sql = "DELETE FROM orders WHERE id = '" + orderId + "'";
        stmt.executeUpdate(sql);
    }

    public void logNotification(String orderId, String message) {
        System.out.println("Notification logged for " + orderId + ": " + message);
        orderService.recordAudit(orderId, message);
    }
}
