package com.example.web;

/**
 * Java fixture mirroring the Python one so the Java extractor's event/auth
 * heuristics are exercised too:
 *   - ENFORCES_POLICY  via @PreAuthorize
 *   - CONSUMES_EVENT   via @KafkaListener(topics = "OrderShipped")
 *   - EMITS_EVENT      via publisher.publish("OrderPlaced")
 *   - PASSES           via validate(cart, customer)
 *   - controller role  via @RestController
 */
@RestController
public class SecuredController {

    private final Publisher publisher = new Publisher();

    @PreAuthorize("hasRole('ADMIN')")
    public Order place(Cart cart, Customer customer) {
        validate(cart, customer);            // CALLS + PASSES
        publisher.publish("OrderPlaced");    // EMITS_EVENT
        return new Order();
    }

    @KafkaListener(topics = "OrderShipped")
    public void onShipped(Record record) {
        process(record);
    }

    private void validate(Cart c, Customer cu) {}

    private void process(Record r) {}
}
