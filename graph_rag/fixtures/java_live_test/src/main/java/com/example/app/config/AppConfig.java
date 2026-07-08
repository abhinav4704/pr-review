package com.example.app.config;

import java.io.FileInputStream;
import java.io.IOException;
import java.util.Properties;

import org.springframework.context.annotation.Configuration;

@Configuration
public class AppConfig {

    public Properties loadConfig(String path) {
        Properties props = new Properties();
        try {
            FileInputStream in = new FileInputStream(path);
            props.load(in);
        } catch (IOException e) {
            // bad_error_handling: swallows the real error, caller has no
            // idea config failed to load.
        }
        return props;
    }
}
