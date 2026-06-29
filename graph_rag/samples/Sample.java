package com.example.shop;

import com.example.shop.PaymentGateway;

@Service
public class OrderService extends BaseService implements Auditable {

    private final PaymentGateway gateway;

    public OrderService(PaymentGateway gateway) {
        this.gateway = gateway;
    }

    @Transactional
    public Order placeOrder(Cart cart, Customer customer) {
        validate(cart);
        Payment p = gateway.charge(cart.total(), customer);
        return new Order(cart, p);
    }

    private void validate(Cart cart) {
        cart.check();
    }
}

class BaseService {
    void audit(String msg) {}
}
