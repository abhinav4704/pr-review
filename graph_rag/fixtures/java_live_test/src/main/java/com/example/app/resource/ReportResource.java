package com.example.app.resource;

import java.io.IOException;
import java.io.InputStream;

import org.springframework.stereotype.Component;

@Component
public class ReportResource {

    public String processReport(InputStream in) throws IOException {
        byte[] data = in.readAllBytes();
        in.close(); // closed once here...
        return new String(data);
    }

    public void generateReport(InputStream in) throws IOException {
        processReport(in);
        in.close(); // ...and again here: resource_double_release across the two functions.
    }
}
